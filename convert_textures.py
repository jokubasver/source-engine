#!/usr/bin/env python3
"""
Unified texture converter for Source engine games.
Converts VTF textures from DXT to ASTC 4x4 format.

Usage:
    python convert_textures.py --game-dir "C:/Program Files/.../Half-Life 2" --game hl2 --output-dir ./hl2_astc
    python convert_textures.py --game-dir "C:/Program Files/.../Portal" --game portal --output-dir ./portal_astc
    python convert_textures.py --game-dir "C:/Program Files/.../Half-Life 2" --game hl2 --output-dir ./hl2_astc --process-bsp
"""

import argparse
import concurrent.futures
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile

try:
    from sourcepp import vpkpp, vtfpp, bsppp
except ImportError:
    print("ERROR: sourcepp not installed. Run: pip install sourcepp", file=sys.stderr)
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    print("ERROR: tqdm not installed. Run: pip install tqdm", file=sys.stderr)
    sys.exit(1)


IMAGE_FORMAT_ASTC4x4 = 41
IMAGE_FORMAT_ASTC4x4_HDR = 42

# 2D ASTC LDR
IMAGE_FORMAT_ASTC5x4 = 43
IMAGE_FORMAT_ASTC5x5 = 44
IMAGE_FORMAT_ASTC6x5 = 45
IMAGE_FORMAT_ASTC6x6 = 46
IMAGE_FORMAT_ASTC8x5 = 47
IMAGE_FORMAT_ASTC8x6 = 48
IMAGE_FORMAT_ASTC8x8 = 49
IMAGE_FORMAT_ASTC10x5 = 50
IMAGE_FORMAT_ASTC10x6 = 51
IMAGE_FORMAT_ASTC10x8 = 52
IMAGE_FORMAT_ASTC10x10 = 53
IMAGE_FORMAT_ASTC12x10 = 54
IMAGE_FORMAT_ASTC12x12 = 55

# 3D ASTC LDR
IMAGE_FORMAT_ASTC3x3x3 = 56
IMAGE_FORMAT_ASTC4x3x3 = 57
IMAGE_FORMAT_ASTC4x4x3 = 58
IMAGE_FORMAT_ASTC4x4x4 = 59
IMAGE_FORMAT_ASTC5x4x4 = 60
IMAGE_FORMAT_ASTC5x5x4 = 61
IMAGE_FORMAT_ASTC5x5x5 = 62
IMAGE_FORMAT_ASTC6x5x5 = 63
IMAGE_FORMAT_ASTC6x6x5 = 64
IMAGE_FORMAT_ASTC6x6x6 = 65

# 2D ASTC HDR
IMAGE_FORMAT_ASTC5x4_HDR = 66
IMAGE_FORMAT_ASTC5x5_HDR = 67
IMAGE_FORMAT_ASTC6x5_HDR = 68
IMAGE_FORMAT_ASTC6x6_HDR = 69
IMAGE_FORMAT_ASTC8x5_HDR = 70
IMAGE_FORMAT_ASTC8x6_HDR = 71
IMAGE_FORMAT_ASTC8x8_HDR = 72
IMAGE_FORMAT_ASTC10x5_HDR = 73
IMAGE_FORMAT_ASTC10x6_HDR = 74
IMAGE_FORMAT_ASTC10x8_HDR = 75
IMAGE_FORMAT_ASTC10x10_HDR = 76
IMAGE_FORMAT_ASTC12x10_HDR = 77
IMAGE_FORMAT_ASTC12x12_HDR = 78

# 3D ASTC HDR
IMAGE_FORMAT_ASTC3x3x3_HDR = 79
IMAGE_FORMAT_ASTC4x3x3_HDR = 80
IMAGE_FORMAT_ASTC4x4x3_HDR = 81
IMAGE_FORMAT_ASTC4x4x4_HDR = 82
IMAGE_FORMAT_ASTC5x4x4_HDR = 83
IMAGE_FORMAT_ASTC5x5x4_HDR = 84
IMAGE_FORMAT_ASTC5x5x5_HDR = 85
IMAGE_FORMAT_ASTC6x5x5_HDR = 86
IMAGE_FORMAT_ASTC6x6x5_HDR = 87
IMAGE_FORMAT_ASTC6x6x6_HDR = 88

# All known ASTC format values (for detecting already-compressed textures)
ALL_ASTC_FORMATS = {
    IMAGE_FORMAT_ASTC4x4, IMAGE_FORMAT_ASTC4x4_HDR,
    IMAGE_FORMAT_ASTC5x4, IMAGE_FORMAT_ASTC5x4_HDR,
    IMAGE_FORMAT_ASTC5x5, IMAGE_FORMAT_ASTC5x5_HDR,
    IMAGE_FORMAT_ASTC6x5, IMAGE_FORMAT_ASTC6x5_HDR,
    IMAGE_FORMAT_ASTC6x6, IMAGE_FORMAT_ASTC6x6_HDR,
    IMAGE_FORMAT_ASTC8x5, IMAGE_FORMAT_ASTC8x5_HDR,
    IMAGE_FORMAT_ASTC8x6, IMAGE_FORMAT_ASTC8x6_HDR,
    IMAGE_FORMAT_ASTC8x8, IMAGE_FORMAT_ASTC8x8_HDR,
    IMAGE_FORMAT_ASTC10x5, IMAGE_FORMAT_ASTC10x5_HDR,
    IMAGE_FORMAT_ASTC10x6, IMAGE_FORMAT_ASTC10x6_HDR,
    IMAGE_FORMAT_ASTC10x8, IMAGE_FORMAT_ASTC10x8_HDR,
    IMAGE_FORMAT_ASTC10x10, IMAGE_FORMAT_ASTC10x10_HDR,
    IMAGE_FORMAT_ASTC12x10, IMAGE_FORMAT_ASTC12x10_HDR,
    IMAGE_FORMAT_ASTC12x12, IMAGE_FORMAT_ASTC12x12_HDR,
    IMAGE_FORMAT_ASTC3x3x3, IMAGE_FORMAT_ASTC3x3x3_HDR,
    IMAGE_FORMAT_ASTC4x3x3, IMAGE_FORMAT_ASTC4x3x3_HDR,
    IMAGE_FORMAT_ASTC4x4x3, IMAGE_FORMAT_ASTC4x4x3_HDR,
    IMAGE_FORMAT_ASTC4x4x4, IMAGE_FORMAT_ASTC4x4x4_HDR,
    IMAGE_FORMAT_ASTC5x4x4, IMAGE_FORMAT_ASTC5x4x4_HDR,
    IMAGE_FORMAT_ASTC5x5x4, IMAGE_FORMAT_ASTC5x5x4_HDR,
    IMAGE_FORMAT_ASTC5x5x5, IMAGE_FORMAT_ASTC5x5x5_HDR,
    IMAGE_FORMAT_ASTC6x5x5, IMAGE_FORMAT_ASTC6x5x5_HDR,
    IMAGE_FORMAT_ASTC6x6x5, IMAGE_FORMAT_ASTC6x6x5_HDR,
    IMAGE_FORMAT_ASTC6x6x6, IMAGE_FORMAT_ASTC6x6x6_HDR,
}

# Source format -> bitrate tier mapping (per Khronos spec Table C.2.2 and
# ARM ASTC encoder format equivalence guide).
#
# Each tier targets the ASTC block size whose bitrate most closely matches
# the source format's bits-per-pixel:
#   ~4 bpp  -> 6x5  (4.27 bpp)
#   ~8 bpp  -> 5x4  (6.40 bpp)
#   ~32 bpp -> 5x5  (5.12 bpp)
#   HDR     -> 4x4  (8.00 bpp, only HDR-capable ASTC)

# ~4 bpp: DXT1, 1-bit alpha, grayscale, 16-bit RGB
ASTC_FORMATS_4BPP = {
    vtfpp.ImageFormat.DXT1,
    vtfpp.ImageFormat.DXT1_ONE_BIT_ALPHA,
    vtfpp.ImageFormat.RGB565,
    vtfpp.ImageFormat.I8,
    vtfpp.ImageFormat.P8,
}

# ~8 bpp: DXT3/DXT5, BC4/BC5 analogs, luminance+alpha, alpha-only
ASTC_FORMATS_8BPP = {
    vtfpp.ImageFormat.DXT5,
    vtfpp.ImageFormat.DXT3,
    vtfpp.ImageFormat.ATI1N,
    vtfpp.ImageFormat.ATI2N,
    vtfpp.ImageFormat.IA88,
    vtfpp.ImageFormat.A8,
}

# ~32 bpp: Uncompressed color
ASTC_FORMATS_32BPP = {
    vtfpp.ImageFormat.RGBA8888,
    vtfpp.ImageFormat.ABGR8888,
    vtfpp.ImageFormat.BGRA8888,
    vtfpp.ImageFormat.ARGB8888,
    vtfpp.ImageFormat.BGRX8888,
    vtfpp.ImageFormat.RGB888,
    vtfpp.ImageFormat.BGR888,
    vtfpp.ImageFormat.RGB888_BLUESCREEN,
    vtfpp.ImageFormat.BGR888_BLUESCREEN,
    vtfpp.ImageFormat.BGRA4444,
    vtfpp.ImageFormat.BGRA5551,
    vtfpp.ImageFormat.BGRX5551,
    vtfpp.ImageFormat.BGR565,
    vtfpp.ImageFormat.RGBX8888,
    vtfpp.ImageFormat.UVWQ8888,
    vtfpp.ImageFormat.UVLX8888,
}

# HDR formats (floating-point, need ASTC HDR profile)
ASTC_FORMATS_HDR = {
    vtfpp.ImageFormat.RGBA16161616F,
    vtfpp.ImageFormat.RGB323232F,
    vtfpp.ImageFormat.RGBA32323232F,
    vtfpp.ImageFormat.R32F,
    vtfpp.ImageFormat.R16F,
    vtfpp.ImageFormat.RG1616F,
    vtfpp.ImageFormat.RG3232F,
}

TEXTUREFLAGS_SRGB = 0x00000040
TEXTUREFLAGS_NORMAL = 0x00000080
TEXTUREFLAGS_SSBUMP = 0x08000000

NON_SRGB_SUFFIXES = (
    '_normal', '_exp', '_exponent', '_lightwarp',
    '_bump', '_height', '_phongexp', '_envmapmask', '_lw',
)

GAME_CONFIGS = {
    'hl2': {
        'mod_dir': 'hl2',
        'bsp_dir': 'hl2/maps',
    },
    'portal': {
        'mod_dir': 'portal',
        'bsp_dir': 'portal/maps',
    },
    'episodic': {
        'mod_dir': 'episodic',
        'bsp_dir': 'episodic/maps',
    },
    'ep2': {
        'mod_dir': 'ep2',
        'bsp_dir': 'ep2/maps',
    },
}

ASTCENC_URL = (
    'https://github.com/ARM-software/astc-encoder/releases/download/5.6.0/'
    'astcenc-5.6.0-windows-x64.zip'
)


def compute_num_mip_levels(width, height):
    n = 1
    w, h = width, height
    while w > 1 or h > 1:
        w = max(1, w >> 1)
        h = max(1, h >> 1)
        n += 1
    return n


def build_vtf_header(width, height, flags, num_frames, mip_count, img_format=IMAGE_FORMAT_ASTC4x4, start_frame=0):
    HEADER_SIZE = 80
    num_resources = 1

    buf = bytearray()
    buf += b'VTF\0'
    buf += struct.pack('<II', 7, 5)
    buf += struct.pack('<I', HEADER_SIZE + num_resources * 8)
    buf += struct.pack('<HH', width, height)
    buf += struct.pack('<I', flags)
    buf += struct.pack('<HH', num_frames, start_frame)
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
    buf += b'\0' * (HEADER_SIZE - len(buf))

    image_offset = HEADER_SIZE + num_resources * 8
    buf += struct.pack('<II', 0x30, image_offset)
    buf[12:16] = struct.pack('<I', image_offset)

    return bytes(buf)


def is_srgb_texture(vtf, file_name):
    flags_val = int(vtf.flags)
    if flags_val & TEXTUREFLAGS_NORMAL or flags_val & TEXTUREFLAGS_SSBUMP:
        return False
    if flags_val & TEXTUREFLAGS_SRGB:
        return True
    name = os.path.splitext(os.path.basename(file_name))[0].lower()
    if any(name.endswith(s) for s in NON_SRGB_SUFFIXES):
        return False
    return True


# Block size -> bits-per-texel mapping
ASTC_BLOCK_BPP = {
    '4x4': 8.00, '5x4': 6.40, '5x5': 5.12, '6x5': 4.27,
    '6x6': 3.56, '8x5': 3.20, '8x6': 2.67, '8x8': 2.00,
    '10x5': 2.56, '10x6': 2.13, '10x8': 1.60, '10x10': 1.28,
    '12x10': 1.07, '12x12': 0.89,
}

# Map block dimensions to IMAGE_FORMAT values
ASTC_LDR_FORMATS = {
    (4, 4): IMAGE_FORMAT_ASTC4x4, (5, 4): IMAGE_FORMAT_ASTC5x4,
    (5, 5): IMAGE_FORMAT_ASTC5x5, (6, 5): IMAGE_FORMAT_ASTC6x5,
    (6, 6): IMAGE_FORMAT_ASTC6x6, (8, 5): IMAGE_FORMAT_ASTC8x5,
    (8, 6): IMAGE_FORMAT_ASTC8x6, (8, 8): IMAGE_FORMAT_ASTC8x8,
    (10, 5): IMAGE_FORMAT_ASTC10x5, (10, 6): IMAGE_FORMAT_ASTC10x6,
    (10, 8): IMAGE_FORMAT_ASTC10x8, (10, 10): IMAGE_FORMAT_ASTC10x10,
    (12, 10): IMAGE_FORMAT_ASTC12x10, (12, 12): IMAGE_FORMAT_ASTC12x12,
}
ASTC_HDR_FORMATS = {
    (4, 4): IMAGE_FORMAT_ASTC4x4_HDR, (5, 4): IMAGE_FORMAT_ASTC5x4_HDR,
    (5, 5): IMAGE_FORMAT_ASTC5x5_HDR, (6, 5): IMAGE_FORMAT_ASTC6x5_HDR,
    (6, 6): IMAGE_FORMAT_ASTC6x6_HDR, (8, 5): IMAGE_FORMAT_ASTC8x5_HDR,
    (8, 6): IMAGE_FORMAT_ASTC8x6_HDR, (8, 8): IMAGE_FORMAT_ASTC8x8_HDR,
    (10, 5): IMAGE_FORMAT_ASTC10x5_HDR, (10, 6): IMAGE_FORMAT_ASTC10x6_HDR,
    (10, 8): IMAGE_FORMAT_ASTC10x8_HDR, (10, 10): IMAGE_FORMAT_ASTC10x10_HDR,
    (12, 10): IMAGE_FORMAT_ASTC12x10_HDR, (12, 12): IMAGE_FORMAT_ASTC12x12_HDR,
}

def select_astc_block_size(vtf, file_name):
    """Select best ASTC block size based on source format and texture properties.

    Maps source format to equivalent ASTC quality tier per Khronos spec
    Table C.2.2 and ARM ASTC encoder format equivalence guide:
      - HDR formats         -> 4x4 HDR (8 bpp, only HDR-capable ASTC)
      - Normal/SSBump maps  -> 4x4 (8 bpp, dual-plane for XY channels)
      - ~4 bpp sources      -> 6x5 (4.27 bpp, closest bitrate match)
      - ~8 bpp sources      -> 5x4 (6.40 bpp, preserves alpha quality)
      - ~32 bpp sources     -> 5x5 (5.12 bpp, best balance)
      - Small textures      -> capped at 6x6 to avoid padding waste
    """
    is_normal = bool(int(vtf.flags) & TEXTUREFLAGS_NORMAL)
    is_ssbumper = bool(int(vtf.flags) & TEXTUREFLAGS_SSBUMP)
    fmt = vtf.format
    name_lower = os.path.splitext(os.path.basename(file_name))[0].lower()

    # Filename-based overrides (secondary hints, checked first)
    if is_normal or is_ssbumper or name_lower.endswith('_normal') or name_lower.endswith('_nrm'):
        return '4x4'

    # HDR always -> 4x4 (only viable HDR ASTC block size)
    if fmt in ASTC_FORMATS_HDR:
        return '4x4'

    # Source format -> bitrate tier -> ASTC block size
    if fmt in ASTC_FORMATS_4BPP:
        block = '6x5'    # 4.27 bpp (closest to ~4 bpp source)
    elif fmt in ASTC_FORMATS_8BPP:
        block = '5x4'    # 6.40 bpp (close to ~8 bpp, good alpha quality)
    elif fmt in ASTC_FORMATS_32BPP:
        block = '5x5'    # 5.12 bpp (best balance for uncompressed sources)
    else:
        block = '6x6'    # 3.56 bpp (safe default for unknown formats)

    # Small texture override: cap at 6x6 to avoid wasting blocks on padding
    if (vtf.width <= 64 or vtf.height <= 64) and block in ('5x5', '5x4', '6x5'):
        block = '6x6'

    return block

def get_astc_format(block_size, is_hdr):
    """Get the IMAGE_FORMAT value for a given block size and HDR flag."""
    w, h = map(int, block_size.split('x'))
    if is_hdr:
        return ASTC_HDR_FORMATS.get((w, h), IMAGE_FORMAT_ASTC4x4_HDR)
    else:
        return ASTC_LDR_FORMATS.get((w, h), IMAGE_FORMAT_ASTC4x4)


def _rgba_to_tga(rgba_data, width, height):
    header = bytearray(18)
    header[2] = 2
    header[12] = width & 0xFF
    header[13] = (width >> 8) & 0xFF
    header[14] = height & 0xFF
    header[15] = (height >> 8) & 0xFF
    header[16] = 32
    header[17] = 40

    pixels = bytearray(len(rgba_data))
    src_mv = memoryview(rgba_data)
    dst_mv = memoryview(pixels)
    dst_mv[0::4] = src_mv[2::4]
    dst_mv[1::4] = src_mv[1::4]
    dst_mv[2::4] = src_mv[0::4]
    dst_mv[3::4] = src_mv[3::4]

    return bytes(header) + bytes(pixels)


def _pad_rgba_ldr(rgba_data, width, height, pad_w, pad_h):
    padded = bytearray(pad_w * pad_h * 4)
    src_mv = memoryview(rgba_data)
    dst_mv = memoryview(padded)
    for y in range(pad_h):
        src_y = min(y, height - 1)
        src_row_start = src_y * width * 4
        dst_row_start = y * pad_w * 4
        copy_len = min(width, pad_w) * 4
        dst_mv[dst_row_start:dst_row_start + copy_len] = src_mv[src_row_start:src_row_start + copy_len]
        if pad_w > width:
            last_pixel = bytes(src_mv[src_row_start + (width - 1) * 4:src_row_start + width * 4])
            dst_mv[dst_row_start + width * 4:dst_row_start + pad_w * 4] = last_pixel * (pad_w - width)
    return bytes(padded)


def _rgba16f_to_dds(rgba16f_data, width, height):
    buf = bytearray()
    buf += b'DDS '
    buf += struct.pack('<I', 124)
    flags = 0x00000001 | 0x00000002 | 0x00000004 | 0x00000008 | 0x00080000
    buf += struct.pack('<I', flags)
    buf += struct.pack('<I', height)
    buf += struct.pack('<I', width)
    buf += struct.pack('<I', width * 8)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0)
    buf += b'\x00' * 44
    buf += struct.pack('<I', 32)
    buf += struct.pack('<I', 0x00000004)
    buf += b'DX10'
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0x00001000)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 10)
    buf += struct.pack('<I', 3)
    buf += struct.pack('<I', 0)
    buf += struct.pack('<I', 1)
    buf += struct.pack('<I', 0)
    buf += bytes(rgba16f_data)
    return bytes(buf)


def _pad_rgba16f(rgba16f_data, width, height, pad_w, pad_h):
    padded = bytearray(pad_w * pad_h * 8)
    src_mv = memoryview(rgba16f_data)
    dst_mv = memoryview(padded)
    for y in range(pad_h):
        src_y = min(y, height - 1)
        src_row_start = src_y * width * 8
        dst_row_start = y * pad_w * 8
        copy_len = min(width, pad_w) * 8
        dst_mv[dst_row_start:dst_row_start + copy_len] = src_mv[src_row_start:src_row_start + copy_len]
        if pad_w > width:
            last_pixel = bytes(src_mv[src_row_start + (width - 1) * 8:src_row_start + width * 8])
            dst_mv[dst_row_start + width * 8:dst_row_start + pad_w * 8] = last_pixel * (pad_w - width)
    return bytes(padded)


def _downscale_rgba16f(src_data, src_w, src_h, dst_w, dst_h):
    result = bytearray(dst_w * dst_h * 8)
    tmp_pixels = [0.0] * (dst_w * dst_h * 4)
    counts = [0] * (dst_w * dst_h)
    for sy in range(src_h):
        dy = sy * dst_h // src_h
        row_src = sy * src_w * 8
        for sx in range(src_w):
            dx = sx * dst_w // src_w
            idx = row_src + sx * 8
            pixel_idx = (dy * dst_w + dx) * 4
            r, g, b, a = struct.unpack_from('<4e', src_data, idx)
            tmp_pixels[pixel_idx] += r
            tmp_pixels[pixel_idx + 1] += g
            tmp_pixels[pixel_idx + 2] += b
            tmp_pixels[pixel_idx + 3] += a
            counts[dy * dst_w + dx] += 1
    for i in range(dst_w * dst_h):
        c = counts[i]
        base = i * 4
        if c > 0:
            tmp_pixels[base] /= c
            tmp_pixels[base + 1] /= c
            tmp_pixels[base + 2] /= c
            tmp_pixels[base + 3] /= c
            struct.pack_into('<4e', result, i * 8,
                             tmp_pixels[base],
                             tmp_pixels[base + 1],
                             tmp_pixels[base + 2],
                             tmp_pixels[base + 3])
        else:
            struct.pack_into('<4e', result, i * 8, 0.0, 0.0, 0.0, 0.0)
    return bytes(result)


def compress_to_astc(pixel_data, width, height, astcenc_path, color_profile, quality, is_hdr=False, jobs=1, block_size='4x4'):
    bw, bh = map(int, block_size.split('x'))
    pad_w = ((width + bw - 1) // bw) * bw
    pad_h = ((height + bh - 1) // bh) * bh
    needs_padding = pad_w != width or pad_h != height

    tmpdir = tempfile.mkdtemp(prefix='vtf_astc_')
    try:
        if is_hdr:
            in_data = _pad_rgba16f(pixel_data, width, height, pad_w, pad_h) if needs_padding else bytes(pixel_data)
            in_path = os.path.join(tmpdir, 'input.dds')
            out_path = os.path.join(tmpdir, 'output.astc')
            with open(in_path, 'wb') as f:
                f.write(_rgba16f_to_dds(in_data, pad_w, pad_h))
            cp = '-ch'
        else:
            in_data = _pad_rgba_ldr(pixel_data, width, height, pad_w, pad_h) if needs_padding else bytes(pixel_data)
            tga_data = _rgba_to_tga(in_data, pad_w, pad_h)
            in_path = os.path.join(tmpdir, 'input.tga')
            out_path = os.path.join(tmpdir, 'output.astc')
            with open(in_path, 'wb') as f:
                f.write(tga_data)
            cp = color_profile

        startupinfo = None
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        result = subprocess.run(
            [astcenc_path, cp, in_path, out_path, block_size, '-' + quality, '-j', str(jobs), '-silent'],
            capture_output=True, timeout=120,
            startupinfo=startupinfo,
        )
        if result.returncode != 0:
            return None

        with open(out_path, 'rb') as f:
            data = f.read()

        if len(data) < 16:
            return None

        return data[16:]
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def get_vtf_format_raw(vtf_data):
    return struct.unpack_from('<I', vtf_data, 52)[0]

def downscale_rgba(rgba_data, src_w, src_h, dst_w, dst_h):
    result = bytearray(dst_w * dst_h * 4)
    for dy in range(dst_h):
        sy0 = dy * src_h // dst_h
        sy1 = (dy + 1) * src_h // dst_h
        for dx in range(dst_w):
            sx0 = dx * src_w // dst_w
            sx1 = (dx + 1) * src_w // dst_w
            r = g = b = a = n = 0
            for sy in range(sy0, sy1):
                row_start = sy * src_w
                for sx in range(sx0, sx1):
                    idx = (row_start + sx) * 4
                    r += rgba_data[idx]
                    g += rgba_data[idx + 1]
                    b += rgba_data[idx + 2]
                    a += rgba_data[idx + 3]
                    n += 1
            idx = (dy * dst_w + dx) * 4
            if n:
                result[idx] = (r + n // 2) // n
                result[idx + 1] = (g + n // 2) // n
                result[idx + 2] = (b + n // 2) // n
                result[idx + 3] = (a + n // 2) // n
    return bytes(result)

def convert_vtf_entry(entry_data, entry_name, output_path, astcenc_path, quality, skip_existing, jobs=1, block_size='auto'):
    if skip_existing and os.path.isfile(output_path):
        return {'status': 'skip', 'entry_name': entry_name}

    fmt = get_vtf_format_raw(entry_data)
    if fmt in ALL_ASTC_FORMATS:
        return {'status': 'already_astc', 'entry_name': entry_name}

    try:
        vtf = vtfpp.VTF(entry_data)
        if not vtf:
            return {'status': 'fail', 'entry_name': entry_name, 'error': 'Failed to parse VTF'}
    except Exception as e:
        return {'status': 'fail', 'entry_name': entry_name, 'error': f'Parse error: {e}'}

    width = vtf.width
    height = vtf.height
    flags_val = int(vtf.flags)
    num_frames = vtf.frame_count
    num_faces = vtf.face_count
    mip_count = vtf.mip_count
    start_frame = vtf.start_frame
    is_hdr = (vtf.format == vtfpp.ImageFormat.RGBA16161616F)

    if block_size == 'auto':
        block_size = select_astc_block_size(vtf, entry_name)

    max_mips = compute_num_mip_levels(width, height)
    if mip_count <= 0:
        mip_count = max_mips

    actual_mip_count = min(mip_count, max_mips)

    color_profile = '-cs' if is_srgb_texture(vtf, entry_name) else '-cl'

    blocks = {}
    failed = False
    pixel_cache = {}

    for mip in range(actual_mip_count):
        mip_w = vtf.width_for_mip(mip)
        mip_h = vtf.height_for_mip(mip)

        for frame in range(num_frames):
            for face in range(num_faces):
                pixels = None
                try:
                    if is_hdr:
                        pixels = vtf.get_image_data_as(vtfpp.ImageFormat.RGBA16161616F, mip, frame, face)
                    else:
                        pixels = vtf.get_image_data_as_rgba8888(mip, frame, face)
                    if pixels is None or len(pixels) == 0:
                        pixels = None
                except Exception:
                    pixels = None

                if pixels is None:
                    prev = pixel_cache.get((mip - 1, frame, face))
                    if prev is not None:
                        prev_w = vtf.width_for_mip(mip - 1)
                        prev_h = vtf.height_for_mip(mip - 1)
                        if is_hdr:
                            pixels = _downscale_rgba16f(prev, prev_w, prev_h, mip_w, mip_h)
                        else:
                            pixels = downscale_rgba(prev, prev_w, prev_h, mip_w, mip_h)
                    else:
                        bpp = 8 if is_hdr else 4
                        pixels = bytes(mip_w * mip_h * bpp)

                pixel_cache[(mip, frame, face)] = bytes(pixels)

                astc_data = compress_to_astc(
                    bytes(pixels), mip_w, mip_h,
                    astcenc_path, color_profile, quality, is_hdr=is_hdr,
                    jobs=jobs, block_size=block_size,
                )
                if astc_data is None:
                    failed = True
                    break
                blocks[(mip, frame, face)] = astc_data
            if failed:
                break
        if failed:
            break

    if failed:
        return {'status': 'fail', 'entry_name': entry_name, 'error': 'ASTC compression failed'}

    vtf_data = bytearray()
    for mip in range(actual_mip_count - 1, -1, -1):
        for frame in range(num_frames):
            for face in range(num_faces):
                vtf_data.extend(blocks[(mip, frame, face)])

    header = build_vtf_header(
        width=width, height=height,
        flags=flags_val,
        num_frames=num_frames,
        mip_count=actual_mip_count,
        img_format=get_astc_format(block_size, is_hdr),
        start_frame=start_frame,
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'wb') as f:
        f.write(header)
        f.write(vtf_data)

    return {'status': 'ok', 'entry_name': entry_name}


def download_astcenc(dest_path):
    if os.path.isfile(dest_path):
        print(f'  astcenc already present at {dest_path}')
        return
    print(f'  Downloading astcenc from {ASTCENC_URL} ...')
    import urllib.request
    import zipfile
    try:
        tmp_zip = dest_path + '.tmp.zip'
        urllib.request.urlretrieve(ASTCENC_URL, tmp_zip)
        with zipfile.ZipFile(tmp_zip, 'r') as zf:
            exe_name = next(n for n in zf.namelist() if 'avx2' in n and n.endswith('.exe'))
            if not exe_name:
                exe_name = next(n for n in zf.namelist() if n.endswith('.exe'))
            with open(dest_path, 'wb') as f:
                f.write(zf.read(exe_name))
        os.remove(tmp_zip)
        print('  Downloaded')
    except Exception as e:
        print(f'  ERROR: Failed to download astcenc: {e}', file=sys.stderr)
        sys.exit(1)


def check_astcenc(astcenc_path, allow_download):
    if not os.path.isfile(astcenc_path):
        if allow_download:
            download_astcenc(astcenc_path)
        else:
            print(f'  ERROR: astcenc not found at {astcenc_path}', file=sys.stderr)
            print(f'  Use --download-astcenc to download automatically, or place astcenc.exe at that path', file=sys.stderr)
            sys.exit(1)
    result = subprocess.run([astcenc_path, '-help'], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f'  ERROR: astcenc failed to run: {result.stderr.strip()}', file=sys.stderr)
        sys.exit(1)
    version_line = (result.stdout or result.stderr).split('\n')[0]
    print(f'  {version_line.strip()}')


def process_vpks(game_dir, mod_dir, output_dir, astcenc_path, quality, skip_existing, threads, block_size='auto'):
    vpk_dir = os.path.join(game_dir, mod_dir)
    if not os.path.isdir(vpk_dir):
        print(f'  WARNING: Mod directory not found: {vpk_dir}', file=sys.stderr)
        return {'converted': 0, 'skipped': 0, 'already_astc': 0, 'failed': [], 'total': 0}

    vpk_files = sorted(f for f in os.listdir(vpk_dir) if f.endswith('_dir.vpk'))
    if not vpk_files:
        print(f'  No *_dir.vpk files found in {vpk_dir}', file=sys.stderr)
        return {'converted': 0, 'skipped': 0, 'already_astc': 0, 'failed': [], 'total': 0}

    results = {'converted': 0, 'skipped': 0, 'already_astc': 0, 'failed': [], 'total': 0}

    for vpk_name in vpk_files:
        vpk_path = os.path.join(vpk_dir, vpk_name)
        print(f'\n  Opening VPK: {vpk_name}')

        try:
            pak = vpkpp.PackFile.open(vpk_path)
        except Exception as e:
            print(f'    ERROR: Failed to open VPK: {e}', file=sys.stderr)
            continue

        entries = []
        try:
            def collect_entries(entry_name, entry):
                if entry_name.lower().endswith('.vtf'):
                    entries.append(entry_name)
                return True
            pak.run_for_all_entries(collect_entries)
        except Exception as e:
            print(f'    ERROR: Failed to list entries: {e}', file=sys.stderr)
            del pak
            continue

        results['total'] += len(entries)
        print(f'    Found {len(entries)} VTF files')

        if not entries:
            del pak
            continue

        vpk_jobs = max(1, (os.cpu_count() or 1) // max(threads, 1))

        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {}
            for entry_name in entries:
                out_path = os.path.join(output_dir, entry_name)
                try:
                    data = pak[entry_name]
                    if data is None:
                        results['failed'].append(entry_name)
                        continue
                except Exception:
                    results['failed'].append(entry_name)
                    continue

                future = executor.submit(
                    convert_vtf_entry, data, entry_name, out_path,
                    astcenc_path, quality, skip_existing, vpk_jobs, block_size,
                )
                futures[future] = entry_name

            for future in tqdm(
                concurrent.futures.as_completed(futures),
                total=len(futures),
                desc=f'  {vpk_name}',
                unit='vtf',
                ncols=80,
            ):
                try:
                    result = future.result()
                except Exception:
                    entry_name = futures[future]
                    results['failed'].append(entry_name)
                    continue

                if result['status'] == 'ok':
                    results['converted'] += 1
                elif result['status'] == 'skip':
                    results['skipped'] += 1
                elif result['status'] == 'already_astc':
                    results['already_astc'] += 1
                else:
                    results['failed'].append(result['entry_name'])

        del pak

    return results


def process_bsp_file(bsp_src_path, bsp_dst_path, astcenc_path, quality, skip_existing, threads, jobs=1, progress_callback=None, block_size='auto'):
    results = {'converted': 0, 'skipped': 0, 'already_astc': 0, 'failed': [], 'error': None}

    os.makedirs(os.path.dirname(bsp_dst_path), exist_ok=True)
    try:
        shutil.copy2(bsp_src_path, bsp_dst_path)
    except Exception as e:
        results['error'] = f'Failed to copy BSP: {e}'
        return results

    try:
        bsp = bsppp.BSP(bsp_dst_path)
    except Exception as e:
        results['error'] = f'Failed to open copied BSP: {e}'
        return results

    if not bsp.has_lump(40):
        return results

    try:
        pak_data = bsp.get_lump_data(40)
        lump_version = bsp.get_lump_version(40)
    except Exception as e:
        results['error'] = f'Failed to read PAK lump: {e}'
        return results

    try:
        zf = zipfile.ZipFile(io.BytesIO(pak_data), 'r')
        all_names = zf.namelist()
        all_entries = {n: zf.read(n) for n in all_names}
        zf.close()
    except Exception as e:
        results['error'] = f'Failed to read PAK ZIP: {e}'
        return results

    vtf_names = [n for n in all_names if n.lower().endswith('.vtf')]
    results['total'] = len(vtf_names)
    if not vtf_names:
        return results
    converted = {}
    conv_lock = threading.Lock()

    for entry_name in vtf_names:
        data = all_entries[entry_name]
        try:
            conv_result = convert_vtf_entry_inline(data, entry_name, astcenc_path, quality, skip_existing, jobs=jobs, block_size=block_size)
        except Exception as e:
            results['failed'].append(entry_name)
            if progress_callback:
                progress_callback()
            continue

        if conv_result['status'] == 'ok':
            converted[entry_name] = conv_result['data']
            results['converted'] += 1
        elif conv_result['status'] == 'already_astc':
            results['already_astc'] += 1
        elif conv_result['status'] == 'skip':
            results['skipped'] += 1
        else:
            results['failed'].append(entry_name)

        if progress_callback:
            progress_callback()

    if results['converted'] == 0:
        return results

    new_zip_buf = io.BytesIO()
    with zipfile.ZipFile(new_zip_buf, 'w', zipfile.ZIP_STORED) as zf_new:
        for name in all_names:
            if name in converted:
                zf_new.writestr(name, converted[name])
            else:
                zf_new.writestr(name, all_entries[name])

    try:
        bsp.set_lump(40, lump_version, new_zip_buf.getvalue(), 0)
        bsp.bake(bsp_dst_path)
    except Exception as e:
        results['error'] = f'Failed to write back PAK lump: {e}'
        return results

    return results


def convert_vtf_entry_inline(entry_data, entry_name, astcenc_path, quality, skip_existing, jobs=1, block_size='auto'):
    fmt = get_vtf_format_raw(entry_data)
    if fmt in ALL_ASTC_FORMATS:
        return {'status': 'already_astc', 'entry_name': entry_name}

    tmp_dir = tempfile.mkdtemp(prefix='bsp_vtf_')
    tmp_out = os.path.join(tmp_dir, os.path.basename(entry_name))
    try:
        result = convert_vtf_entry(entry_data, entry_name, tmp_out, astcenc_path, quality, skip_existing=skip_existing, jobs=jobs, block_size=block_size)
        if result['status'] == 'ok':
            with open(tmp_out, 'rb') as f:
                result['data'] = f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return result


def _count_bsp_vtfs(bsp_src_dir, bsp_name):
    path = os.path.join(bsp_src_dir, bsp_name)
    try:
        bsp = bsppp.BSP(path)
        if not bsp.has_lump(40):
            return 0
        sf = io.BytesIO(bsp.get_lump_data(40))
        zf = zipfile.ZipFile(sf)
        count = sum(1 for n in zf.namelist() if n.lower().endswith('.vtf'))
        zf.close()
        return count
    except Exception:
        return 0


def process_bsps(game_dir, mod_bsp_dir, output_dir, astcenc_path, quality, skip_existing, threads, block_size='auto'):
    bsp_src_dir = os.path.join(game_dir, mod_bsp_dir)
    if not os.path.isdir(bsp_src_dir):
        print(f'  WARNING: BSP directory not found: {bsp_src_dir}', file=sys.stderr)
        return {'converted': 0, 'skipped': 0, 'already_astc': 0, 'failed': [], 'total': 0}

    bsp_files = sorted(f for f in os.listdir(bsp_src_dir) if f.lower().endswith('.bsp'))
    if not bsp_files:
        print(f'  No BSP files found in {bsp_src_dir}', file=sys.stderr)
        return {'converted': 0, 'skipped': 0, 'already_astc': 0, 'failed': [], 'total': 0}

    print(f'\n  Found {len(bsp_files)} BSP files, scanning VTFs...', end=' ', flush=True)
    vtf_counts = {}
    for bn in bsp_files:
        vtf_counts[bn] = _count_bsp_vtfs(bsp_src_dir, bn)

    total_vtfs = sum(vtf_counts.values())
    print(f'{total_vtfs} embedded VTFs found')

    results = {'converted': 0, 'skipped': 0, 'already_astc': 0, 'failed': [], 'total': 0}

    bsp_workers = min(threads, len(bsp_files))
    bsp_astc_jobs = max(1, (os.cpu_count() or 1) // max(bsp_workers, 1))

    vtf_progress_lock = threading.Lock()
    vtf_progress_done = 0
    vtf_progress_bar = tqdm(total=total_vtfs, desc='  BSP VTFs', unit='vtf', ncols=80)

    def _progress():
        nonlocal vtf_progress_done
        with vtf_progress_lock:
            vtf_progress_done += 1
            vtf_progress_bar.update(1)

    def _process_one(bsp_name):
        src = os.path.join(bsp_src_dir, bsp_name)
        dst = os.path.join(output_dir, mod_bsp_dir, bsp_name)
        return bsp_name, process_bsp_file(src, dst, astcenc_path, quality, skip_existing, threads, jobs=bsp_astc_jobs, progress_callback=_progress, block_size=block_size)

    bsp_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=bsp_workers) as executor:
        futures = {executor.submit(_process_one, bn): bn for bn in bsp_files}
        for future in concurrent.futures.as_completed(futures):
            try:
                bsp_results.append(future.result())
            except Exception as e:
                bn = futures[future]
                bsp_results.append((bn, {'error': str(e), 'converted': 0, 'skipped': 0, 'already_astc': 0, 'failed': []}))

    vtf_progress_bar.close()

    for bsp_name, r in bsp_results:
        if r.get('error'):
            tqdm.write(f'    ERROR processing {bsp_name}: {r["error"]}')
        results['converted'] += r['converted']
        results['skipped'] += r['skipped']
        results['already_astc'] += r['already_astc']
        if r['converted'] > 0:
            results['total'] += r['converted']
        for f in r['failed']:
            results['failed'].append(f'{bsp_name}:{f}')

    return results


def print_summary(name, results):
    c = results['converted']
    s = results['skipped']
    a = results['already_astc']
    t = results.get('total', c + s + a + len(results['failed']))
    f = results['failed']
    print(f'\n  {name}: Converted={c} Skipped={s} AlreadyASTC={a} Failed={len(f)} Total={t}')
    if f:
        print(f'  WARNING: {len(f)} textures failed and were copied as original format.')
        print(f'  They will appear purple/black on Mali GPUs. Listing failed items below:')
        for fe in f:
            print(f'    -> {fe}')


def main():
    parser = argparse.ArgumentParser(
        description='Convert Source engine VTF textures from DXT to ASTC 4x4 for GLES Mali GPUs',
    )
    parser.add_argument('--game-dir', required=True, help='Game install directory')
    parser.add_argument('--game', required=True, choices=list(GAME_CONFIGS.keys()),
                        help='Game configuration: ' + ', '.join(GAME_CONFIGS.keys()))
    parser.add_argument('--output-dir', required=True, help='Output directory')
    parser.add_argument('--astcenc', default='astcenc.exe', help='Path to astcenc executable')
    parser.add_argument('--threads', type=int, default=os.cpu_count() or 4,
                        help='Number of parallel workers (default: CPU count)')
    parser.add_argument('--quality', default='fast',
                        choices=['fast', 'medium', 'thorough', 'exhaustive'],
                        help='astcenc quality preset (default: fast)')
    parser.add_argument('--block-size', default='auto',
                        choices=['auto'] + sorted(ASTC_BLOCK_BPP.keys()),
                        help='ASTC block size (default: auto-select per texture)')
    parser.add_argument('--skip-existing', action='store_true',
                        help='Skip VTF files that already exist in output')
    parser.add_argument('--process-bsp', action='store_true',
                        help='Also convert embedded VTFs inside BSP files')
    parser.add_argument('--download-astcenc', action='store_true',
                        help='Download astcenc automatically if not found')
    args = parser.parse_args()

    if not os.path.isdir(args.game_dir):
        print(f'ERROR: Game directory not found: {args.game_dir}', file=sys.stderr)
        sys.exit(1)

    print('=== Checking prerequisites ===')
    check_astcenc(args.astcenc, args.download_astcenc)
    print('  sourcepp: OK')
    print('  tqdm: OK')

    cfg = GAME_CONFIGS[args.game]
    game_label = args.game
    output_dir = os.path.abspath(args.output_dir)

    # Phase 1: VPK textures
    print(f'\n=== Converting VPK textures for {game_label} ===')
    vpk_results = process_vpks(
        args.game_dir, cfg['mod_dir'], output_dir,
        args.astcenc, args.quality, args.skip_existing, args.threads,
        block_size=args.block_size,
    )
    print_summary(f'VPK textures ({game_label})', vpk_results)

    # Phase 2: BSP embedded textures
    if args.process_bsp:
        print(f'\n=== Converting BSP embedded textures for {game_label} ===')
        bsp_results = process_bsps(
            args.game_dir, cfg['bsp_dir'], output_dir,
            args.astcenc, args.quality, args.skip_existing, args.threads,
            block_size=args.block_size,
        )
        print_summary(f'BSP textures ({game_label})', bsp_results)

    total_failed = len(vpk_results['failed'])
    if args.process_bsp:
        total_failed += len(bsp_results.get('failed', []))

    print(f'\n=== Done! ===')
    print(f'  Output: {output_dir}')
    if total_failed > 0:
        print(f'  WARNING: {total_failed} total failures occurred.', file=sys.stderr)


if __name__ == '__main__':
    main()
