import struct
import sys
import os
import re
import shutil
import subprocess
import tempfile

IMAGE_FORMAT_ASTC4x4 = 41
IMAGE_FORMAT_RGBA8888 = 0

TEXTUREFLAGS_SRGB = 0x00000040
TEXTUREFLAGS_NORMAL = 0x00000080
TEXTUREFLAGS_SSBUMP = 0x08000000


def read_vtf_header(vtf_path):
    with open(vtf_path, 'rb') as f:
        data = f.read(80)
    sig = data[0:4]
    if sig != b'VTF\0':
        raise ValueError(f"Not a VTF file: {vtf_path}")
    ver_major = struct.unpack_from('<I', data, 4)[0]
    ver_minor = struct.unpack_from('<I', data, 8)[0]
    header_size = struct.unpack_from('<I', data, 12)[0]
    width = struct.unpack_from('<H', data, 16)[0]
    height = struct.unpack_from('<H', data, 18)[0]
    flags = struct.unpack_from('<I', data, 20)[0]
    num_frames = struct.unpack_from('<H', data, 24)[0]
    mip_count = data[56]
    img_format = struct.unpack_from('<I', data, 52)[0]
    return {
        'version': (ver_major, ver_minor),
        'header_size': header_size,
        'width': width,
        'height': height,
        'flags': flags,
        'num_frames': num_frames,
        'mip_count': mip_count,
        'img_format': img_format,
    }


def astc_blocks(w, h):
    return ((w + 3) // 4) * ((h + 3) // 4)


def astc_mip_size(w, h):
    return astc_blocks(w, h) * 16


def generate_mip_chain_from_tga(tga_path, astcenc_path, is_srgb, quality='-fast', block_size='4x4'):
    from PIL import Image

    img = Image.open(tga_path)
    img = img.convert('RGBA')
    w, h = img.size

    tmpdir = tempfile.mkdtemp(prefix='mip_')
    mip_astc_data = []
    level = 0

    # Choose color profile flag based on original texture flags
    color_profile = '-cs' if is_srgb else '-cl'

    while True:
        cur_w = max(1, w >> level)
        cur_h = max(1, h >> level)

        if level == 0:
            mip_img = img
        else:
            mip_img = img.resize((cur_w, cur_h), Image.LANCZOS)

        # Pad canvas to a multiple of 4 to guarantee predictable astcenc block behavior
        pad_w = ((cur_w + 3) // 4) * 4
        pad_h = ((cur_h + 3) // 4) * 4
        padded_img = Image.new('RGBA', (pad_w, pad_h), (0, 0, 0, 0))
        padded_img.paste(mip_img, (0, 0))

        tga_file = os.path.join(tmpdir, f'mip_{level}.tga')
        astc_file = os.path.join(tmpdir, f'mip_{level}.astc')

        padded_img.save(tga_file)

        result = subprocess.run(
            [astcenc_path, color_profile, tga_file, astc_file, block_size, quality, '-j', '1', '-silent'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  WARNING: astcenc failed for mip {level} ({cur_w}x{cur_h}): {result.stderr.strip()}", file=sys.stderr)
            break

        with open(astc_file, 'rb') as f:
            data = f.read()
        if len(data) < 16:
            print(f"  WARNING: ASTC output too small for mip {level}", file=sys.stderr)
            break

        mip_astc_data.append(data[16:])

        level += 1
        if cur_w <= 1 and cur_h <= 1:
            break

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    return mip_astc_data


def generate_rgba8888_mip_chain_from_tga(tga_path):
    from PIL import Image

    img = Image.open(tga_path).convert('RGBA')
    w, h = img.size

    mip_data = []
    level = 0

    while True:
        cur_w = max(1, w >> level)
        cur_h = max(1, h >> level)

        if level == 0:
            mip_img = img
        else:
            mip_img = img.resize((cur_w, cur_h), Image.LANCZOS)

        raw = mip_img.tobytes()
        mip_data.append(raw)

        level += 1
        if cur_w <= 1 and cur_h <= 1:
            break

    return mip_data


def generate_rgba8888_from_tga(tga_path):
    """Return the raw RGBA8888 bytes of a single (already-mipped) TGA."""
    from PIL import Image
    return Image.open(tga_path).convert('RGBA').tobytes()


def compress_single_tga(tga_path, astcenc_path, is_srgb, quality):
    """Compress one already-sized TGA mip to ASTC, returning raw block bytes
    (the 16-byte .astc container header stripped). Returns None on failure."""
    color_profile = '-cs' if is_srgb else '-cl'
    tmp = tempfile.mktemp(suffix='.astc')
    result = subprocess.run(
        [astcenc_path, color_profile, tga_path, tmp, '4x4', quality, '-j', '1', '-silent'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  WARNING: astcenc failed for {tga_path}: {result.stderr.strip()}", file=sys.stderr)
        return None
    with open(tmp, 'rb') as f:
        data = f.read()
    if len(data) < 16:
        return None
    return data[16:]


def extract_vtf_mip_tgas(vtf_path, vtf2tga_path):
    """Run 'vtf2tga -mip' on a source VTF and return mip TGAs grouped by frame.

    vtf2tga -mip names outputs '<base>[NNN]_mip<M>.tga' where NNN is a 3-digit
    frame index (omitted for single-frame textures). Returns a list indexed by
    frame number; each element is a list of TGA paths ordered largest mip first
    (mip0, mip1, ...). Returns [] on failure."""
    workdir = tempfile.mkdtemp(prefix='vtfmip_')
    shutil.copy(vtf_path, workdir)
    base = os.path.splitext(os.path.basename(vtf_path))[0]
    result = subprocess.run(
        [vtf2tga_path, '-i', os.path.join(workdir, os.path.basename(vtf_path)), '-mip'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  WARNING: vtf2tga -mip failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    frames = {}
    for fn in os.listdir(workdir):
        m = re.match(re.escape(base) + r'(?:(\d{3}))?_mip(\d+)\.tga$', fn)
        if not m:
            continue
        frame = int(m.group(1)) if m.group(1) is not None else 0
        mip = int(m.group(2))
        frames.setdefault(frame, {})[mip] = os.path.join(workdir, fn)
    if not frames:
        return []
    max_frame = max(frames.keys())
    out = []
    for f in range(max_frame + 1):
        if f not in frames or not frames[f]:
            print(f"  WARNING: vtf2tga -mip missing frame {f} for {vtf_path}", file=sys.stderr)
            return []
        out.append([frames[f][i] for i in sorted(frames[f].keys())])
    return out


def parse_ktx(ktx_path):
    with open(ktx_path, 'rb') as f:
        data = f.read()

    if len(data) < 64:
        raise ValueError(f"KTX file too small: {ktx_path}")

    identifier = data[0:12]
    expected_id = b'\xABKTX 11\xBB\r\n\x1A\n'
    if identifier != expected_id:
        raise ValueError(f"Not a KTX 1.0 file: {ktx_path}")

    endianness = struct.unpack_from('<I', data, 12)[0]
    if endianness != 0x04030201:
        raise ValueError(f"Unexpected endianness: 0x{endianness:08X}")

    gl_internal_format = struct.unpack_from('<I', data, 28)[0]
    pixel_width = struct.unpack_from('<I', data, 36)[0]
    pixel_height = struct.unpack_from('<I', data, 40)[0]
    num_mip_levels = struct.unpack_from('<I', data, 56)[0]
    bytes_of_kv_data = struct.unpack_from('<I', data, 60)[0]

    print(f"  KTX: {pixel_width}x{pixel_height} glInternalFormat=0x{gl_internal_format:04X} "
          f"numMipLevels={num_mip_levels}", file=sys.stderr)

    if num_mip_levels == 0:
        w, h = pixel_width, pixel_height
        num_mip_levels = 1
        while w > 1 or h > 1:
            w = max(1, w >> 1)
            h = max(1, h >> 1)
            num_mip_levels += 1

    offset = 64 + bytes_of_kv_data
    mip_data_list = []

    for i in range(num_mip_levels):
        if offset + 4 > len(data):
            print(f"  WARNING: KTX truncated at mip {i}", file=sys.stderr)
            break
        image_size = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        if offset + image_size > len(data):
            print(f"  WARNING: KTX data truncated at mip {i}", file=sys.stderr)
            break
        mip_data_list.append(data[offset:offset + image_size])
        offset += image_size
        offset = (offset + 3) & ~3

    return {
        'width': pixel_width,
        'height': pixel_height,
        'num_mip_levels': len(mip_data_list),
        'mip_data': mip_data_list,
    }


def parse_single_astc(astc_path):
    with open(astc_path, 'rb') as f:
        data = f.read()

    if len(data) < 16:
        raise ValueError(f"ASTC file too small: {astc_path}")

    magic = data[0:4]
    if magic != b'\x13\xAB\xA1\x5C':
        raise ValueError(f"Not an ASTC file: {astc_path}")

    dim_x = data[7] | (data[8] << 8) | (data[9] << 16)
    dim_y = data[10] | (data[11] << 8) | (data[12] << 16)

    return {
        'width': dim_x,
        'height': dim_y,
        'num_mip_levels': 1,
        'mip_data': [data[16:]],
    }


def compute_num_mip_levels(width, height):
    n = 1
    w, h = width, height
    while w > 1 or h > 1:
        w = max(1, w >> 1)
        h = max(1, h >> 1)
        n += 1
    return n


def truncate_mip_chain(mip_data, target_count):
    """Truncate a mip chain (largest-first) to target_count levels."""
    if len(mip_data) > target_count:
        return mip_data[:target_count]
    return mip_data


def build_vtf_header(width, height, flags, num_frames, mip_count, img_format=IMAGE_FORMAT_ASTC4x4):
    num_resources = 1

    # sizeof(VTFFileHeaderV7_3_t) = 80 bytes (72 bytes fields + 8 bytes alignas(16) padding)
    HEADER_SIZE = 80

    buf = bytearray()
    buf += b'VTF\0'
    buf += struct.pack('<II', 7, 5)
    buf += struct.pack('<I', HEADER_SIZE + num_resources * 8)
    buf += struct.pack('<HH', width, height)
    buf += struct.pack('<I', flags)
    buf += struct.pack('<HH', num_frames, 0)
    buf += b'\0' * 4
    buf += struct.pack('<fff', 0.0, 0.0, 0.0)
    buf += b'\0' * 4
    buf += struct.pack('<f', 1.0)
    buf += struct.pack('<I', img_format)
    buf += struct.pack('<B', mip_count)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<BB', 0, 0)
    buf += struct.pack('<H', 1)
    buf += b'\0' * 3
    buf += struct.pack('<I', num_resources)

    # alignas(16) padding (bytes 72-79)
    buf += b'\0' * (HEADER_SIZE - len(buf))

    # Resource dictionary entries follow at offset HEADER_SIZE
    # VTF_LEGACY_RSRC_IMAGE = 0x30
    image_offset = HEADER_SIZE + num_resources * 8
    buf += struct.pack('<II', 0x30, image_offset)

    buf[12:16] = struct.pack('<I', image_offset)
    return bytes(buf)


def build_cubemap_vtf(face_tga_paths, astcenc_path, orig, is_srgb, quality='-fast', block_size='4x4'):
    """Build a cubemap VTF from 6 face TGA files.

    VTF cubemap data layout: for each mip level, all 6 faces are stored sequentially.
    Mip 0 face 0, mip 0 face 1, ..., mip 0 face 5, mip 1 face 0, ...
    """
    face_names = ['rt', 'lf', 'bk', 'ft', 'up', 'dn']
    if len(face_tga_paths) != 6:
        raise ValueError(f"Cubemap requires exactly 6 face TGA files, got {len(face_tga_paths)}")

    # Generate mip chains for each face
    face_mip_chains = []
    for i, tga_path in enumerate(face_tga_paths):
        mip_chain = generate_mip_chain_from_tga(tga_path, astcenc_path, is_srgb, quality, block_size)
        if not mip_chain:
            raise RuntimeError(f"ASTC conversion failed for face {face_names[i]}: {tga_path}")
        face_mip_chains.append(mip_chain)

    # All faces should have the same mip count; use the minimum.
    num_mip_levels = min(len(chain) for chain in face_mip_chains)

    full_chain = compute_num_mip_levels(orig['width'], orig['height'])

    if num_mip_levels > full_chain:
        print(f"  Truncating cubemap mip chain: {num_mip_levels} -> {full_chain} levels (full chain for {orig['width']}x{orig['height']})", file=sys.stderr)
        for i in range(len(face_mip_chains)):
            face_mip_chains[i] = face_mip_chains[i][:full_chain]
        num_mip_levels = full_chain

    if num_mip_levels < full_chain:
        print(f"  WARNING: Cubemap has {num_mip_levels} mip levels, expected {full_chain}. Missing mips will be padded with the smallest level.", file=sys.stderr)
        for i in range(len(face_mip_chains)):
            while len(face_mip_chains[i]) < full_chain:
                face_mip_chains[i].append(face_mip_chains[i][-1])
            num_mip_levels = full_chain

    # VTF stores mips in reverse order (smallest mip first), with all 6 faces per level.
    # Layout: mipN_face0..face5, mip(N-1)_face0..face5, ..., mip0_face0..face5
    vtf_mip_data = []
    for mip_level in range(num_mip_levels - 1, -1, -1):
        for face_idx in range(6):
            vtf_mip_data.append(face_mip_chains[face_idx][mip_level])

    total_data_size = sum(len(d) for d in vtf_mip_data)

    output_flags = orig['flags'] | (TEXTUREFLAGS_SRGB if is_srgb else 0)

    header = build_vtf_header(
        width=orig['width'], height=orig['height'],
        flags=output_flags, num_frames=orig['num_frames'],
        mip_count=num_mip_levels, img_format=IMAGE_FORMAT_ASTC4x4,
    )

    return header, vtf_mip_data, num_mip_levels, total_data_size


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Wrap ASTC data in a VTF container with mip chain generation')
    parser.add_argument('--vtf', required=True, help='Original VTF file (for header reference)')
    parser.add_argument('--astc', help='ASTC or KTX file from astcenc (single level or multi-mip)')
    parser.add_argument('--tga', help='Source TGA image for mip chain generation')
    parser.add_argument('--mip-tgas', nargs='+', metavar='MIP_TGA',
                        help='Pre-generated per-mip TGAs (mip0, mip1, ... largest to smallest) '
                             'to compress individually. Preserves authored mip colors (use '
                             'vtf2tga -mip). Avoids regenerating mips from a single full-res TGA, '
                             'which darkens sparse-atlas textures at distance.')
    parser.add_argument('--cubemap', nargs=6, metavar='FACE_TGA',
                        help='6 cubemap face TGA files in order: rt lt up dn ft bk')
    parser.add_argument('--astcenc', help='Path to astcenc executable (required when using --tga or --cubemap)')
    parser.add_argument('--vtf2tga', help='Path to vtf2tga.exe. When set with --vtf (non-ASTC source), '
                        'per-mip TGAs are extracted via "vtf2tga -mip" and each is compressed individually, '
                        'preserving authored mip colors (fixes fade-to-black on sparse-atlas textures).')
    parser.add_argument('--quality', default='-fast', help='astcenc quality preset (default: -fast)')
    parser.add_argument('--output', required=True, help='Output VTF file path')
    parser.add_argument('--rgba-fallback', action='store_true',
                        help='If ASTC conversion fails, fall back to RGBA8888 instead of failing')
    args = parser.parse_args()

    if not args.astc and not args.tga and not args.cubemap and not (args.vtf and args.vtf2tga):
        parser.error("Either --astc, --tga, --cubemap, or --vtf with --vtf2tga must be provided")

    orig = read_vtf_header(args.vtf)

    # Determine whether this texture contains sRGB color data.
    # Explicit VTF flags take priority.
    if orig['flags'] & TEXTUREFLAGS_NORMAL or orig['flags'] & TEXTUREFLAGS_SSBUMP:
        is_srgb = False
    elif orig['flags'] & TEXTUREFLAGS_SRGB:
        is_srgb = True
    else:
        # No explicit flags — check filename for known non-color suffixes.
        # Portal's shaders apply sRGB at load time for color textures
        # via LoadTexture(..., TEXTUREFLAGS_SRGB), so we need to match
        # that: non-color textures use LoadBumpMap or LoadTexture with 0 flags.
        name = os.path.splitext(os.path.basename(args.vtf))[0].lower()
        non_srgb_suffixes = ('_normal', '_exp', '_exponent', '_lightwarp',
                             '_bump', '_height', '_phongexp', '_envmapmask',
                             '_lw')
        if any(name.endswith(s) for s in non_srgb_suffixes):
            is_srgb = False
        else:
            # Default to sRGB — most Portal color textures fall here.
            is_srgb = True

    output_flags = orig['flags'] | (TEXTUREFLAGS_SRGB if is_srgb else 0)

    if args.cubemap:
        if not args.astcenc:
            parser.error("--astcenc is required when using --cubemap")
        if not os.path.isfile(args.astcenc):
            print(f"  ERROR: astcenc not found at {args.astcenc}", file=sys.stderr)
            sys.exit(1)
        for p in args.cubemap:
            if not os.path.isfile(p):
                print(f"  ERROR: Face TGA not found: {p}", file=sys.stderr)
                sys.exit(1)

        try:
            header, vtf_mip_data, num_mip_levels, total_data_size = build_cubemap_vtf(
                args.cubemap, args.astcenc, orig, is_srgb, args.quality
            )
        except RuntimeError as e:
            if args.rgba_fallback:
                print(f"  ASTC cubemap failed ({e}), falling back to RGBA8888", file=sys.stderr)
                face_mip_chains_rgba = [generate_rgba8888_mip_chain_from_tga(p) for p in args.cubemap]
                num_mip_levels = min(len(c) for c in face_mip_chains_rgba)
                full_chain = compute_num_mip_levels(orig['width'], orig['height'])
                if num_mip_levels > full_chain:
                    print(f"  Truncating cubemap mip chain (RGBA fallback): {num_mip_levels} -> {full_chain}", file=sys.stderr)
                    for i in range(len(face_mip_chains_rgba)):
                        face_mip_chains_rgba[i] = face_mip_chains_rgba[i][:full_chain]
                    num_mip_levels = full_chain
                if num_mip_levels < full_chain:
                    print(f"  WARNING: RGBA fallback cubemap has {num_mip_levels} mip levels, expected {full_chain}. Padding.", file=sys.stderr)
                    for i in range(len(face_mip_chains_rgba)):
                        while len(face_mip_chains_rgba[i]) < full_chain:
                            face_mip_chains_rgba[i].append(face_mip_chains_rgba[i][-1])
                        num_mip_levels = full_chain
                vtf_mip_data = []
                for mip_level in range(num_mip_levels - 1, -1, -1):
                    for face_idx in range(6):
                        vtf_mip_data.append(face_mip_chains_rgba[face_idx][mip_level])
                total_data_size = sum(len(d) for d in vtf_mip_data)
                header = build_vtf_header(
                    width=orig['width'], height=orig['height'],
                    flags=output_flags, num_frames=orig['num_frames'],
                    mip_count=num_mip_levels, img_format=IMAGE_FORMAT_RGBA8888,
                )
            else:
                print(f"  ERROR: {e}", file=sys.stderr)
                sys.exit(1)

        with open(args.output, 'wb') as f:
            f.write(header)
            for level_data in vtf_mip_data:
                f.write(level_data)
        print(f"  Wrote {args.output} (ASTC 4x4 cubemap, {len(header) + total_data_size} bytes, "
              f"{num_mip_levels} mips x 6 faces)", file=sys.stderr)
        return

    # Per-mip path (multi-frame capable), driven by --mip-tgas or --vtf2tga.
    # 'frames' is a list indexed by frame; each element is a list of mip TGA paths
    # ordered largest-first (mip0, mip1, ...). A single-frame texture is simply
    # frames=[ mip_list ]. VTF disk order is mip-outer (smallest->largest),
    # frame-inner, so the data is interleaved accordingly.
    frames = None
    if args.mip_tgas:
        frames = [list(args.mip_tgas)]
    elif args.vtf2tga and orig['img_format'] != IMAGE_FORMAT_ASTC4x4 and \
         not args.astc and not args.tga and not args.cubemap:
        if not args.astcenc:
            parser.error("--astcenc is required when using --vtf2tga")
        frames = extract_vtf_mip_tgas(args.vtf, args.vtf2tga)
        if not frames:
            print("  ERROR: vtf2tga -mip failed to produce mips for " + args.vtf, file=sys.stderr)
            sys.exit(1)

    if frames is not None:
        if not args.astcenc:
            parser.error("--astcenc is required when using --mip-tgas/--vtf2tga")
        if not os.path.isfile(args.astcenc):
            print(f"  ERROR: astcenc not found at {args.astcenc}", file=sys.stderr)
            sys.exit(1)

        num_frames = len(frames)
        if orig['num_frames'] and num_frames != orig['num_frames']:
            print(f"  WARNING: extracted {num_frames} frames but source VTF declares "
                  f"{orig['num_frames']}", file=sys.stderr)
        full_chain = compute_num_mip_levels(orig['width'], orig['height'])

        # Compress each frame's mip chain individually.
        frames_data = []
        failed = False
        for frame_mips in frames:
            for t in frame_mips:
                if not os.path.isfile(t):
                    print(f"  ERROR: mip TGA not found: {t}", file=sys.stderr)
                    sys.exit(1)
            per_frame = []
            for t in frame_mips:
                body = compress_single_tga(t, args.astcenc, is_srgb, args.quality)
                if body is None:
                    failed = True
                    break
                per_frame.append(body)
            if failed:
                break
            frames_data.append(per_frame)

        if failed or not frames_data or not frames_data[0]:
            if args.rgba_fallback:
                print(f"  ASTC mip-tga failed, falling back to RGBA8888", file=sys.stderr)
                frames_data = [[generate_rgba8888_from_tga(t) for t in fm] for fm in frames]
                img_format = IMAGE_FORMAT_RGBA8888
            else:
                print(f"  ERROR: ASTC mip-tga conversion failed and no --rgba-fallback", file=sys.stderr)
                sys.exit(1)
        else:
            img_format = IMAGE_FORMAT_ASTC4x4

        # Pad/truncate each frame's mip chain to the full expected chain.
        for fi in range(len(frames_data)):
            chain = frames_data[fi]
            if len(chain) < full_chain:
                print(f"  WARNING: frame {fi} has {len(chain)} mip levels, expected "
                      f"{full_chain}. Padding with smallest level.", file=sys.stderr)
                while len(chain) < full_chain:
                    chain.append(chain[-1])
            elif len(chain) > full_chain:
                print(f"  Truncating frame {fi} mip chain: {len(chain)} -> {full_chain}", file=sys.stderr)
                chain = chain[:full_chain]
            frames_data[fi] = chain

        num_mip_levels = full_chain

        # VTF disk order: for each mip (smallest->largest), for each frame.
        vtf_mip_data = []
        for mip in range(num_mip_levels - 1, -1, -1):
            for fi in range(num_frames):
                vtf_mip_data.append(frames_data[fi][mip])

        total_data_size = sum(len(d) for d in vtf_mip_data)
        header = build_vtf_header(
            width=orig['width'], height=orig['height'],
            flags=output_flags, num_frames=num_frames,
            mip_count=num_mip_levels, img_format=img_format,
        )
        with open(args.output, 'wb') as f:
            f.write(header)
            for level_data in vtf_mip_data:
                f.write(level_data)
        print(f"  Wrote {args.output} ({'ASTC 4x4' if img_format == IMAGE_FORMAT_ASTC4x4 else 'RGBA8888'} "
              f"multi-frame, {len(header) + total_data_size} bytes, {num_mip_levels} mips x "
              f"{num_frames} frames)", file=sys.stderr)
        return

    if args.tga:
        if not args.astcenc:
            parser.error("--astcenc is required when using --tga")

        if not os.path.isfile(args.astcenc):
            print(f"  ERROR: astcenc not found at {args.astcenc}", file=sys.stderr)
            sys.exit(1)

        all_mip_data = generate_mip_chain_from_tga(args.tga, args.astcenc, is_srgb, args.quality)
        num_mip_levels = len(all_mip_data)

        if num_mip_levels == 0 and args.rgba_fallback:
            print(f"  ASTC failed, falling back to RGBA8888", file=sys.stderr)
            all_mip_data = generate_rgba8888_mip_chain_from_tga(args.tga)
            num_mip_levels = len(all_mip_data)

            vtf_mip_data = list(reversed(all_mip_data))
            total_data_size = sum(len(d) for d in vtf_mip_data)
            header = build_vtf_header(
                width=orig['width'], height=orig['height'],
                flags=output_flags, num_frames=orig['num_frames'],
                mip_count=num_mip_levels, img_format=IMAGE_FORMAT_RGBA8888,
            )
            with open(args.output, 'wb') as f:
                f.write(header)
                for level_data in vtf_mip_data:
                    f.write(level_data)
            print(f"  Wrote {args.output} (RGBA8888 fallback, {len(header) + total_data_size} bytes, {num_mip_levels} mips)", file=sys.stderr)
            return

        if num_mip_levels == 0:
            print(f"  ERROR: ASTC conversion failed and no --rgba-fallback", file=sys.stderr)
            sys.exit(1)
    else:
        ext = os.path.splitext(args.astc)[1].lower()
        if ext == '.ktx':
            parsed = parse_ktx(args.astc)
        else:
            parsed = parse_single_astc(args.astc)
        all_mip_data = parsed['mip_data']
        num_mip_levels = parsed['num_mip_levels']

    target_mips = orig['mip_count'] if orig['mip_count'] > 0 else num_mip_levels

    full_chain = compute_num_mip_levels(orig['width'], orig['height'])

    # The engine's ComputeMipCount() always computes the full mip chain from dimensions.
    # If header.numMipLevels is less than that, LoadImageData skips reading data for
    # the higher mips, leaving them uninitialized (zeros). WriteTexels then uploads
    # those zeros to GL, causing "black at distance" rendering artifacts.
    if num_mip_levels < full_chain:
        print(f"  WARNING: Only {num_mip_levels} mip levels, expected {full_chain}. Padding with smallest level.", file=sys.stderr)
        while len(all_mip_data) < full_chain:
            all_mip_data.append(all_mip_data[-1])
        num_mip_levels = full_chain

    if num_mip_levels > full_chain:
        print(f"  Truncating mip chain: {num_mip_levels} -> {full_chain} levels (full chain for {orig['width']}x{orig['height']})", file=sys.stderr)
        all_mip_data = all_mip_data[:full_chain]
        num_mip_levels = full_chain

    vtf_mip_data = list(reversed(all_mip_data))
    total_data_size = sum(len(d) for d in vtf_mip_data)

    header = build_vtf_header(
                width=orig['width'], height=orig['height'],
                flags=output_flags, num_frames=orig['num_frames'],
                mip_count=num_mip_levels,
    )

    with open(args.output, 'wb') as f:
        f.write(header)
        for level_data in vtf_mip_data:
            f.write(level_data)

    print(f"  Wrote {args.output} (ASTC 4x4, {len(header) + total_data_size} bytes, {num_mip_levels} mips)", file=sys.stderr)


if __name__ == '__main__':
    main()