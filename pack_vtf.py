import struct
import sys
import os
import subprocess
import tempfile

IMAGE_FORMAT_ASTC4x4 = 41
IMAGE_FORMAT_RGBA8888 = 0

TEXTUREFLAGS_SRGB = 0x00040000


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


def build_vtf_header(width, height, flags, num_frames, mip_count, img_format=IMAGE_FORMAT_ASTC4x4):
    num_resources = 1

    buf = bytearray()
    buf += b'VTF\0'
    buf += struct.pack('<II', 7, 3)
    buf += struct.pack('<I', 80)
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
    buf += b'\0' * 8

    image_offset = len(buf) + num_resources * 8
    buf += struct.pack('<II', 0x30, image_offset)

    buf[12:16] = struct.pack('<I', image_offset)
    return bytes(buf)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Wrap ASTC data in a VTF container with mip chain generation')
    parser.add_argument('--vtf', required=True, help='Original VTF file (for header reference)')
    parser.add_argument('--astc', help='ASTC or KTX file from astcenc (single level or multi-mip)')
    parser.add_argument('--tga', help='Source TGA image for mip chain generation')
    parser.add_argument('--astcenc', help='Path to astcenc executable (required when using --tga)')
    parser.add_argument('--quality', default='-fast', help='astcenc quality preset (default: -fast)')
    parser.add_argument('--output', required=True, help='Output VTF file path')
    parser.add_argument('--rgba-fallback', action='store_true',
                        help='If ASTC conversion fails, fall back to RGBA8888 instead of failing')
    args = parser.parse_args()

    if not args.astc and not args.tga:
        parser.error("Either --astc or --tga must be provided")

    orig = read_vtf_header(args.vtf)
    is_srgb = bool(orig['flags'] & TEXTUREFLAGS_SRGB)

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
                flags=orig['flags'], num_frames=orig['num_frames'],
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

    full_chain = compute_num_mip_levels(orig['width'], orig['height'])

    if num_mip_levels < full_chain:
        print(f"  WARNING: Only {num_mip_levels} mip levels, engine expects {full_chain}.", file=sys.stderr)

    vtf_mip_data = list(reversed(all_mip_data))
    total_data_size = sum(len(d) for d in vtf_mip_data)

    header = build_vtf_header(
                width=orig['width'], height=orig['height'],
                flags=orig['flags'], num_frames=orig['num_frames'],
                mip_count=num_mip_levels,
    )

    with open(args.output, 'wb') as f:
        f.write(header)
        for level_data in vtf_mip_data:
            f.write(level_data)

    print(f"  Wrote {args.output} (ASTC 4x4, {len(header) + total_data_size} bytes, {num_mip_levels} mips)", file=sys.stderr)


if __name__ == '__main__':
    main()