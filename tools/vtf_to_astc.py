#!/usr/bin/env python3
"""
Source engine VTF -> ASTC VTF converter using MareTF edit mode.

Uses `maretf edit --set-format` for direct in-place format conversion.
This is simpler than the two-step extract+create approach and preserves
the original VTF structure (mips, flags, resources) better.

Requirements:
  - maretf binary (built from MareTF/ with astc-encoder linked)
  - sourcepp Python package (pip install sourcepp) for VPK/BSP access
  - tqdm (pip install tqdm) for progress bars
"""

import argparse
import concurrent.futures
import io
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile

try:
    from sourcepp import vpkpp, bsppp
except ImportError:
    print("ERROR: sourcepp not installed. Run: pip install sourcepp", file=sys.stderr)
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    print("ERROR: tqdm not installed. Run: pip install tqdm", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

ALL_ASTC_FORMATS = set(range(41, 89))

# HDR source formats (raw VTF format enum values)
RAW_HDR_FORMATS = {
    24,  # IMAGE_FORMAT_RGBA16161616F
    27,  # IMAGE_FORMAT_R32F
    28,  # IMAGE_FORMAT_RGB323232F
    29,  # IMAGE_FORMAT_RGBA32323232F
}


# ---------------------------------------------------------------------------
# VTF flags
# ---------------------------------------------------------------------------

TEXTUREFLAGS_SRGB = 0x00000040
TEXTUREFLAGS_NORMAL = 0x00000080
TEXTUREFLAGS_PRE_SRGB = 0x00080000
TEXTUREFLAGS_SSBUMP = 0x08000000


# ---------------------------------------------------------------------------
# Filename heuristics for sRGB detection
# ---------------------------------------------------------------------------

NON_SRGB_TOKENS = {
    "normal", "normalmap", "nrm", "bump", "bumpmap",
    "height", "exp", "exponent", "lightwarp", "lw",
    "phongexp", "envmapmask", "mask", "spec", "specular",
    "gloss", "rough", "roughness", "ao", "ambientocclusion",
    "selfillum", "selfillummask", "blend", "modulate", "detailmask",
}


def _name_tokens(file_name):
    base = os.path.basename(file_name.replace("\\", "/"))
    base = os.path.splitext(base)[0].lower()
    return [t for t in re.split(r"[^a-z0-9]+", base) if t]


def _has_token(tokens, candidates):
    for token in tokens:
        for candidate in candidates:
            if token == candidate:
                return True
            if token.startswith(candidate):
                suffix = token[len(candidate):]
                if suffix.isdigit():
                    return True
    return False


# ---------------------------------------------------------------------------
# Game configuration
# ---------------------------------------------------------------------------

GAME_CONFIGS = {
    "hl2": {
        "mod_dir": "hl2",
        "bsp_dir": "hl2/maps",
    },
    "portal": {
        "mod_dir": "portal",
        "bsp_dir": "portal/maps",
    },
    "episodic": {
        "mod_dir": "episodic",
        "bsp_dir": "episodic/maps",
    },
    "ep2": {
        "mod_dir": "ep2",
        "bsp_dir": "ep2/maps",
    },
}


# ---------------------------------------------------------------------------
# ASTC block size selection
# ---------------------------------------------------------------------------

ASTC_BLOCK_ORDER = [
    "4x4", "5x4", "5x5", "6x5", "6x6",
    "8x5", "8x6", "8x8",
    "10x5", "10x6", "10x8", "10x10",
    "12x10", "12x12",
]

# Map block size to maretf format string
ASTC_FORMAT_STR_LDR = {
    "4x4": "ASTC4X4",
    "5x4": "ASTC5X4",
    "5x5": "ASTC5X5",
    "6x5": "ASTC6X5",
    "6x6": "ASTC6X6",
    "8x5": "ASTC8X5",
    "8x6": "ASTC8X6",
    "8x8": "ASTC8X8",
    "10x5": "ASTC10X5",
    "10x6": "ASTC10X6",
    "10x8": "ASTC10X8",
    "10x10": "ASTC10X10",
    "12x10": "ASTC12X10",
    "12x12": "ASTC12X12",
}

ASTC_FORMAT_STR_HDR = {
    "4x4": "ASTC4X4_HDR",
    "5x4": "ASTC5X4_HDR",
    "5x5": "ASTC5X5_HDR",
    "6x5": "ASTC6X5_HDR",
    "6x6": "ASTC6X6_HDR",
    "8x5": "ASTC8X5_HDR",
    "8x6": "ASTC8X6_HDR",
    "8x8": "ASTC8X8_HDR",
    "10x5": "ASTC10X5_HDR",
    "10x6": "ASTC10X6_HDR",
    "10x8": "ASTC10X8_HDR",
    "10x10": "ASTC10X10_HDR",
    "12x10": "ASTC12X10_HDR",
    "12x12": "ASTC12X12_HDR",
}


# ---------------------------------------------------------------------------
# VTF header reading (minimal, from raw bytes)
# ---------------------------------------------------------------------------

def read_vtf_header(vtf_data):
    """
    Read key fields from a VTF 7.x header.
    Returns dict with: width, height, flags, frames, format, mip_count, depth
    or None if the data is too small / invalid.
    """
    if len(vtf_data) < 64:
        return None

    # Check signature
    if vtf_data[0:4] != b"VTF\x00":
        return None

    try:
        # Version minor at offset 8 (0=7.0, 1=7.1, 2=7.2, etc.)
        version_minor = struct.unpack_from("<I", vtf_data, 8)[0]
        width = struct.unpack_from("<H", vtf_data, 16)[0]
        height = struct.unpack_from("<H", vtf_data, 18)[0]
        flags = struct.unpack_from("<I", vtf_data, 20)[0]
        frames = struct.unpack_from("<H", vtf_data, 24)[0]
        img_format = struct.unpack_from("<I", vtf_data, 52)[0]
        mip_count = struct.unpack_from("<B", vtf_data, 56)[0]
        # Depth field only exists in VTF 7.2+ (version_minor >= 2)
        if version_minor >= 2 and len(vtf_data) >= 65:
            depth = struct.unpack_from("<H", vtf_data, 63)[0]
        else:
            depth = 1
    except struct.error:
        return None

    if depth == 0:
        depth = 1

    return {
        "width": width,
        "height": height,
        "flags": flags,
        "frames": max(1, frames),
        "format": img_format,
        "mip_count": mip_count,
        "depth": depth,
    }


# ---------------------------------------------------------------------------
# Texture classification
# ---------------------------------------------------------------------------

def is_srgb_texture(flags, file_name):
    # Normal maps and SSBUMP maps should be linear.
    if flags & TEXTUREFLAGS_NORMAL or flags & TEXTUREFLAGS_SSBUMP:
        return False

    # If the VTF explicitly says sRGB, trust it.
    if flags & (TEXTUREFLAGS_SRGB | TEXTUREFLAGS_PRE_SRGB):
        return True

    # Filename heuristics.
    if _has_token(_name_tokens(file_name), NON_SRGB_TOKENS):
        return False

    # Default to sRGB for albedo/base textures.
    return True


def compute_output_flags(flags_raw, file_name, is_hdr):
    """
    Compute output VTF flags with intentional sRGB management.
    Preserves all original flags except sRGB bits which are managed.
    """
    out_flags = flags_raw

    if is_hdr:
        # HDR ASTC has no sRGB internal format.
        out_flags &= ~TEXTUREFLAGS_SRGB
        out_flags &= ~TEXTUREFLAGS_PRE_SRGB
    else:
        srgb = is_srgb_texture(flags_raw, file_name)
        if srgb:
            out_flags |= TEXTUREFLAGS_SRGB
            out_flags &= ~TEXTUREFLAGS_PRE_SRGB
        else:
            out_flags &= ~TEXTUREFLAGS_SRGB
            out_flags &= ~TEXTUREFLAGS_PRE_SRGB

    return out_flags


# ---------------------------------------------------------------------------
# Safe path helpers
# ---------------------------------------------------------------------------

def safe_output_path(base_dir, entry_name):
    """Join an archive entry name to an output directory safely."""
    name = entry_name.replace("\\", "/").strip()
    name = posixpath.normpath(name)

    if not name or name == ".":
        raise ValueError("Empty output path")

    parts = name.split("/")
    if parts and parts[0] == "":
        parts = parts[1:]

    if not parts:
        raise ValueError("Empty output path")

    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Unsafe path: {entry_name}")

    if re.match(r"^[A-Za-z]:$", parts[0]):
        raise ValueError(f"Unsafe drive path: {entry_name}")

    return os.path.join(base_dir, *parts)


# ---------------------------------------------------------------------------
# MareTF conversion (edit mode)
# ---------------------------------------------------------------------------

def check_maretf(maretf_path):
    """Verify maretf binary exists and is runnable."""
    if not os.path.isfile(maretf_path):
        print(f"ERROR: maretf not found at: {maretf_path}", file=sys.stderr)
        print("Build MareTF: tools/build_maretf.bat (Windows) or tools/build_maretf.sh (Linux)", file=sys.stderr)
        sys.exit(1)

    try:
        result = subprocess.run(
            [maretf_path, "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:
        print(f"ERROR: maretf failed to run: {e}", file=sys.stderr)
        sys.exit(1)

    # maretf --help may return 0 or nonzero; just check it produced output
    output = (result.stdout or "") + (result.stderr or "")
    if "maretf" not in output.lower() and "MareTF" not in output and len(output) < 10:
        print(f"WARNING: maretf produced unexpected output", file=sys.stderr)

    print(f"  maretf: OK ({maretf_path})")


def convert_vtf_with_maretf(
    vtf_data,
    entry_name,
    output_path,
    maretf_path,
    block_size="4x4",
    skip_existing=False,
    timeout=300,
    quality=1.0,
):
    """
    Convert a single VTF to ASTC format using maretf edit --set-format.

    This is a single-step conversion that preserves the original VTF
    structure (mips, frames, flags) while changing the pixel format.

    Returns a dict with 'status' key: ok, skip, already_astc, fail
    """
    if skip_existing and os.path.isfile(output_path):
        return {"status": "skip", "entry_name": entry_name}

    if not isinstance(vtf_data, (bytes, bytearray)):
        vtf_data = bytes(vtf_data)

    # Parse VTF header
    header = read_vtf_header(vtf_data)
    if header is None:
        return {
            "status": "fail",
            "entry_name": entry_name,
            "error": "File too small or invalid VTF header",
        }

    fmt_raw = header["format"]

    # Skip already-ASTC textures
    if fmt_raw in ALL_ASTC_FORMATS:
        return {"status": "already_astc", "entry_name": entry_name}

    # Skip 3D/volume textures
    if header["depth"] > 1:
        return {
            "status": "fail",
            "entry_name": entry_name,
            "error": "3D/volume VTF textures are not supported",
        }

    # Determine HDR vs LDR
    is_hdr = fmt_raw in RAW_HDR_FORMATS

    # Select format string
    if is_hdr:
        format_str = ASTC_FORMAT_STR_HDR.get(block_size, "ASTC4X4_HDR")
    else:
        format_str = ASTC_FORMAT_STR_LDR.get(block_size, "ASTC4X4")

    # Compute output flags for sRGB management
    out_flags = compute_output_flags(header["flags"], entry_name, is_hdr)

    # Single-step conversion using maretf edit --set-format
    tmp_dir = tempfile.mkdtemp(prefix="vtf_astc_edit_")

    try:
        input_path = os.path.join(tmp_dir, "input.vtf")
        maretf_output = os.path.join(tmp_dir, "output.vtf")

        with open(input_path, "wb") as f:
            f.write(vtf_data)

        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        # Build edit command
        edit_cmd = [
            maretf_path,
            "edit",
            input_path,
            "--set-format", format_str,
            "--set-quality", str(quality),
            "--set-flags-uint", str(out_flags),
            "-o", maretf_output,
            "--yes",
        ]

        try:
            result = subprocess.run(
                edit_cmd,
                capture_output=True,
                timeout=timeout,
                startupinfo=startupinfo,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "fail",
                "entry_name": entry_name,
                "error": f"maretf edit timed out after {timeout}s",
            }

        if result.returncode != 0:
            err = (
                result.stderr.decode("utf-8", "replace")
                or result.stdout.decode("utf-8", "replace")
                or f"maretf edit exited with code {result.returncode}"
            )
            return {
                "status": "fail",
                "entry_name": entry_name,
                "error": err.strip()[:500],
            }

        if not os.path.isfile(maretf_output):
            return {
                "status": "fail",
                "entry_name": entry_name,
                "error": "maretf edit did not produce output file",
            }

        with open(maretf_output, "rb") as f:
            output_data = f.read()

        if len(output_data) < 64:
            return {
                "status": "fail",
                "entry_name": entry_name,
                "error": "maretf output is too small to be a valid VTF",
            }

        # Write to final destination
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        tmp_out = output_path + ".tmp"
        try:
            with open(tmp_out, "wb") as f:
                f.write(output_data)
            os.replace(tmp_out, output_path)
        except Exception as e:
            try:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
            except OSError:
                pass
            return {
                "status": "fail",
                "entry_name": entry_name,
                "error": f"Failed to write output: {e}",
            }

        return {"status": "ok", "entry_name": entry_name}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def convert_vtf_inline(
    vtf_data,
    entry_name,
    maretf_path,
    block_size="4x4",
    timeout=300,
    quality=1.0,
):
    """
    Convert a VTF to ASTC in-memory (for BSP embedded textures).
    Returns dict with 'status' and optionally 'data' keys.
    """
    fmt_raw = None
    header = read_vtf_header(vtf_data)
    if header:
        fmt_raw = header["format"]

    if fmt_raw is not None and fmt_raw in ALL_ASTC_FORMATS:
        return {"status": "already_astc", "entry_name": entry_name}

    tmp_dir = tempfile.mkdtemp(prefix="bsp_vtf_")

    try:
        base_name = os.path.basename(entry_name.replace("\\", "/"))
        if not base_name:
            base_name = "embedded.vtf"

        tmp_out = os.path.join(tmp_dir, base_name)

        result = convert_vtf_with_maretf(
            vtf_data,
            entry_name,
            tmp_out,
            maretf_path,
            block_size=block_size,
            skip_existing=False,
            timeout=timeout,
            quality=quality,
        )

        if result["status"] == "ok":
            with open(tmp_out, "rb") as f:
                result["data"] = f.read()

        return result

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# VPK processing
# ---------------------------------------------------------------------------

def convert_vtf_to_memory(
    vtf_data,
    entry_name,
    maretf_path,
    block_size="4x4",
    timeout=300,
    quality=1.0,
):
    """
    Convert a VTF to ASTC in-memory (for VPK repacking).
    Returns dict with 'status' and optionally 'data' keys.
    """
    header = read_vtf_header(vtf_data)
    if header is None:
        return {
            "status": "fail",
            "entry_name": entry_name,
            "error": "File too small or invalid VTF header",
        }

    fmt_raw = header["format"]

    if fmt_raw in ALL_ASTC_FORMATS:
        return {"status": "already_astc", "entry_name": entry_name}

    if header["depth"] > 1:
        return {
            "status": "fail",
            "entry_name": entry_name,
            "error": "3D/volume VTF textures are not supported",
        }

    tmp_dir = tempfile.mkdtemp(prefix="vtf_astc_mem_")

    try:
        tmp_out = os.path.join(tmp_dir, "output.vtf")

        result = convert_vtf_with_maretf(
            vtf_data,
            entry_name,
            tmp_out,
            maretf_path,
            block_size=block_size,
            skip_existing=False,
            timeout=timeout,
            quality=quality,
        )

        if result["status"] == "ok":
            with open(tmp_out, "rb") as f:
                result["data"] = f.read()

        return result

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def process_vpks_repack(
    game_dir,
    mod_dir,
    output_dir,
    maretf_path,
    threads,
    block_size="4x4",
    timeout=300,
    quality=1.0,
):
    """
    Process VPKs and repack converted VTFs back into VPK files.
    Output VPKs are written to output_dir, preserving original names.
    """
    vpk_dir = os.path.join(game_dir, mod_dir)

    empty_results = {
        "converted": 0,
        "skipped": 0,
        "already_astc": 0,
        "failed": [],
        "total": 0,
    }

    if not os.path.isdir(vpk_dir):
        print(f"  WARNING: Mod directory not found: {vpk_dir}", file=sys.stderr)
        return empty_results

    vpk_files = sorted(f for f in os.listdir(vpk_dir) if f.endswith("_dir.vpk"))

    if not vpk_files:
        print(f"  No *_dir.vpk files found in {vpk_dir}", file=sys.stderr)
        return empty_results

    results = {
        "converted": 0,
        "skipped": 0,
        "already_astc": 0,
        "failed": [],
        "total": 0,
    }

    threads = max(1, int(threads))
    batch_size = max(threads * 4, 16)

    os.makedirs(output_dir, exist_ok=True)

    for vpk_name in vpk_files:
        vpk_path = os.path.join(vpk_dir, vpk_name)

        print(f"\nOpening VPK: {vpk_name}")

        try:
            pak = vpkpp.PackFile.open(vpk_path)
        except Exception as e:
            print(f"    ERROR: Failed to open VPK: {e}", file=sys.stderr)
            continue

        entries = []

        try:
            def collect_entries(entry_name, entry):
                if entry_name.lower().endswith(".vtf"):
                    entries.append(entry_name)
                return True

            pak.run_for_all_entries(collect_entries)
        except Exception as e:
            print(f"    ERROR: Failed to list entries: {e}", file=sys.stderr)
            del pak
            continue

        results["total"] += len(entries)
        print(f"    Found {len(entries)} VTF files")

        if not entries:
            del pak
            continue

        pbar = tqdm(
            total=len(entries),
            desc=f"  {vpk_name}",
            unit="vtf",
            ncols=80,
        )

        converted_data = {}

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                for start in range(0, len(entries), batch_size):
                    batch = entries[start:start + batch_size]
                    futures = {}

                    for entry_name in batch:
                        try:
                            data = pak[entry_name]
                            if data is None:
                                raise RuntimeError("VPK entry returned no data")
                            data = bytes(data)
                        except Exception as e:
                            results["failed"].append(f"{entry_name}: {e}")
                            pbar.update(1)
                            continue

                        future = executor.submit(
                            convert_vtf_to_memory,
                            data,
                            entry_name,
                            maretf_path,
                            block_size,
                            timeout,
                            quality,
                        )
                        futures[future] = entry_name

                    for future in concurrent.futures.as_completed(futures):
                        entry_name = futures[future]

                        try:
                            result = future.result()
                        except Exception as e:
                            result = {
                                "status": "fail",
                                "entry_name": entry_name,
                                "error": str(e),
                            }

                        if result["status"] == "ok":
                            results["converted"] += 1
                            converted_data[entry_name] = result["data"]
                        elif result["status"] == "already_astc":
                            results["already_astc"] += 1
                        else:
                            results["failed"].append(
                                f"{result.get('entry_name', entry_name)}: "
                                f"{result.get('error', 'unknown error')}"
                            )

                        pbar.update(1)

        finally:
            pbar.close()

        # Add converted VTFs back to the VPK
        if converted_data:
            print(f"    Repacking {len(converted_data)} converted VTFs...")
            for entry_name, data in converted_data.items():
                try:
                    pak.add_entry(entry_name, data)
                except Exception as e:
                    results["failed"].append(f"{entry_name}: Failed to add to VPK: {e}")

            # Bake the modified VPK to output directory
            try:
                pak.bake(output_dir)
                print(f"    Wrote: {output_dir}/{vpk_name}")
            except Exception as e:
                print(f"    ERROR: Failed to bake VPK: {e}", file=sys.stderr)
                results["failed"].append(f"{vpk_name}: Failed to bake VPK: {e}")

        del pak

    return results


def process_vpks(
    game_dir,
    mod_dir,
    output_dir,
    maretf_path,
    skip_existing,
    threads,
    block_size="4x4",
    timeout=300,
    quality=1.0,
):
    vpk_dir = os.path.join(game_dir, mod_dir)

    empty_results = {
        "converted": 0,
        "skipped": 0,
        "already_astc": 0,
        "failed": [],
        "total": 0,
    }

    if not os.path.isdir(vpk_dir):
        print(f"  WARNING: Mod directory not found: {vpk_dir}", file=sys.stderr)
        return empty_results

    vpk_files = sorted(f for f in os.listdir(vpk_dir) if f.endswith("_dir.vpk"))

    if not vpk_files:
        print(f"  No *_dir.vpk files found in {vpk_dir}", file=sys.stderr)
        return empty_results

    results = {
        "converted": 0,
        "skipped": 0,
        "already_astc": 0,
        "failed": [],
        "total": 0,
    }

    threads = max(1, int(threads))
    batch_size = max(threads * 4, 16)

    for vpk_name in vpk_files:
        vpk_path = os.path.join(vpk_dir, vpk_name)

        print(f"\nOpening VPK: {vpk_name}")

        try:
            pak = vpkpp.PackFile.open(vpk_path)
        except Exception as e:
            print(f"    ERROR: Failed to open VPK: {e}", file=sys.stderr)
            continue

        entries = []

        try:
            def collect_entries(entry_name, entry):
                if entry_name.lower().endswith(".vtf"):
                    entries.append(entry_name)
                return True

            pak.run_for_all_entries(collect_entries)
        except Exception as e:
            print(f"    ERROR: Failed to list entries: {e}", file=sys.stderr)
            del pak
            continue

        results["total"] += len(entries)
        print(f"    Found {len(entries)} VTF files")

        if not entries:
            del pak
            continue

        pbar = tqdm(
            total=len(entries),
            desc=f"  {vpk_name}",
            unit="vtf",
            ncols=80,
        )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                for start in range(0, len(entries), batch_size):
                    batch = entries[start:start + batch_size]
                    futures = {}

                    for entry_name in batch:
                        try:
                            out_path = safe_output_path(output_dir, entry_name)
                        except Exception as e:
                            results["failed"].append(f"{entry_name}: {e}")
                            pbar.update(1)
                            continue

                        if skip_existing and os.path.isfile(out_path):
                            results["skipped"] += 1
                            pbar.update(1)
                            continue

                        try:
                            data = pak[entry_name]
                            if data is None:
                                raise RuntimeError("VPK entry returned no data")
                            data = bytes(data)
                        except Exception as e:
                            results["failed"].append(f"{entry_name}: {e}")
                            pbar.update(1)
                            continue

                        future = executor.submit(
                            convert_vtf_with_maretf,
                            data,
                            entry_name,
                            out_path,
                            maretf_path,
                            block_size,
                            False,  # skip_existing already handled
                            timeout,
                            quality,
                        )
                        futures[future] = entry_name

                    for future in concurrent.futures.as_completed(futures):
                        entry_name = futures[future]

                        try:
                            result = future.result()
                        except Exception as e:
                            result = {
                                "status": "fail",
                                "entry_name": entry_name,
                                "error": str(e),
                            }

                        if result["status"] == "ok":
                            results["converted"] += 1
                        elif result["status"] == "skip":
                            results["skipped"] += 1
                        elif result["status"] == "already_astc":
                            results["already_astc"] += 1
                        else:
                            results["failed"].append(
                                f"{result.get('entry_name', entry_name)}: "
                                f"{result.get('error', 'unknown error')}"
                            )

                        pbar.update(1)

        finally:
            pbar.close()
            del pak

    return results


# ---------------------------------------------------------------------------
# BSP processing
# ---------------------------------------------------------------------------

def _count_bsp_vtfs(bsp_src_dir, bsp_name):
    path = os.path.join(bsp_src_dir, bsp_name)

    try:
        bsp = bsppp.BSP(path)

        if not bsp.has_lump(40):
            return 0

        sf = io.BytesIO(bsp.get_lump_data(40))
        zf = zipfile.ZipFile(sf)

        count = sum(1 for n in zf.namelist() if n.lower().endswith(".vtf"))

        zf.close()
        return count

    except Exception:
        return 0


def process_bsp_file(
    bsp_src_path,
    bsp_dst_path,
    maretf_path,
    progress_callback=None,
    block_size="4x4",
    timeout=300,
    expected_vtf_count=0,
    quality=1.0,
):
    results = {
        "converted": 0,
        "skipped": 0,
        "already_astc": 0,
        "failed": [],
        "error": None,
        "total": 0,
    }

    def notify(amount=1):
        if progress_callback and amount > 0:
            progress_callback(amount)

    try:
        bsp = bsppp.BSP(bsp_src_path)
    except Exception as e:
        results["error"] = f"Failed to open source BSP: {e}"
        results["total"] = expected_vtf_count
        notify(expected_vtf_count)
        return results

    if not bsp.has_lump(40):
        if expected_vtf_count:
            results["error"] = "No PAK lump found"
            results["total"] = expected_vtf_count
            notify(expected_vtf_count)
        return results

    try:
        pak_data = bsp.get_lump_data(40)
        lump_version = bsp.get_lump_version(40)
    except Exception as e:
        results["error"] = f"Failed to read PAK lump: {e}"
        results["total"] = expected_vtf_count
        notify(expected_vtf_count)
        return results

    try:
        zf = zipfile.ZipFile(io.BytesIO(pak_data), "r")
        all_names = zf.namelist()
        all_entries = {n: zf.read(n) for n in all_names}
        zf.close()
    except Exception as e:
        results["error"] = f"Failed to read PAK ZIP: {e}"
        results["total"] = expected_vtf_count
        notify(expected_vtf_count)
        return results

    vtf_names = [n for n in all_names if n.lower().endswith(".vtf")]

    if not vtf_names:
        if expected_vtf_count:
            results["error"] = "No VTF entries found in PAK lump"
            results["total"] = expected_vtf_count
            notify(expected_vtf_count)
        return results

    results["total"] = len(vtf_names)
    converted = {}

    for entry_name in vtf_names:
        data = all_entries[entry_name]

        try:
            conv_result = convert_vtf_inline(
                data,
                entry_name,
                maretf_path,
                block_size=block_size,
                timeout=timeout,
                quality=quality,
            )
        except Exception as e:
            conv_result = {
                "status": "fail",
                "entry_name": entry_name,
                "error": str(e),
            }

        if conv_result["status"] == "ok":
            converted[entry_name] = conv_result["data"]
            results["converted"] += 1
        elif conv_result["status"] == "already_astc":
            results["already_astc"] += 1
        elif conv_result["status"] == "skip":
            results["skipped"] += 1
        else:
            results["failed"].append(
                f"{entry_name}: {conv_result.get('error', 'unknown error')}"
            )

        notify(1)

    # If nothing was converted, do not create an output BSP.
    if results["converted"] == 0:
        return results

    try:
        out_dir = os.path.dirname(bsp_dst_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        shutil.copy2(bsp_src_path, bsp_dst_path)

        bsp_out = bsppp.BSP(bsp_dst_path)

        new_zip_buf = io.BytesIO()

        # IMPORTANT: Use ZIP_STORED for Source embedded BSP PAK compatibility.
        with zipfile.ZipFile(new_zip_buf, "w", zipfile.ZIP_STORED) as zf_new:
            for name in all_names:
                data = converted.get(name, all_entries[name])

                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o644 << 16

                zf_new.writestr(info, data)

        bsp_out.set_lump(40, lump_version, new_zip_buf.getvalue(), 0)
        bsp_out.bake(bsp_dst_path)

    except Exception as e:
        try:
            if os.path.exists(bsp_dst_path):
                os.remove(bsp_dst_path)
        except OSError:
            pass

        results["error"] = f"Failed to write back PAK lump: {e}"

    return results


def process_bsps(
    game_dir,
    mod_bsp_dir,
    output_dir,
    maretf_path,
    skip_existing,
    threads,
    block_size="4x4",
    timeout=300,
    quality=1.0,
):
    results = {
        "converted": 0,
        "skipped": 0,
        "already_astc": 0,
        "failed": [],
        "total": 0,
    }

    bsp_src_dir = os.path.join(game_dir, mod_bsp_dir)

    if not os.path.isdir(bsp_src_dir):
        print(f"  WARNING: BSP directory not found: {bsp_src_dir}", file=sys.stderr)
        return results

    bsp_files_all = sorted(
        f for f in os.listdir(bsp_src_dir) if f.lower().endswith(".bsp")
    )

    if not bsp_files_all:
        print(f"  No BSP files found in {bsp_src_dir}", file=sys.stderr)
        return results

    print(f"\nFound {len(bsp_files_all)} BSP files, scanning VTFs...", end=" ", flush=True)

    vtf_counts = {}

    for bn in bsp_files_all:
        vtf_counts[bn] = _count_bsp_vtfs(bsp_src_dir, bn)

    bsp_files = [bn for bn in bsp_files_all if vtf_counts[bn] > 0]
    total_vtfs = sum(vtf_counts[bn] for bn in bsp_files)

    print(f"{total_vtfs} embedded VTFs found")

    if not bsp_files:
        return results

    threads = max(1, int(threads))
    bsp_workers = min(threads, len(bsp_files))

    vtf_progress_lock = threading.Lock()
    vtf_progress_bar = tqdm(
        total=total_vtfs,
        desc="  BSP VTFs",
        unit="vtf",
        ncols=80,
    )

    def _progress(amount=1):
        if amount <= 0:
            return
        with vtf_progress_lock:
            vtf_progress_bar.update(amount)

    def _process_one(bsp_name, dst_path):
        src = os.path.join(bsp_src_dir, bsp_name)

        return bsp_name, process_bsp_file(
            src,
            dst_path,
            maretf_path,
            progress_callback=_progress,
            block_size=block_size,
            timeout=timeout,
            expected_vtf_count=vtf_counts.get(bsp_name, 0),
            quality=quality,
        )

    bsp_results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=bsp_workers) as executor:
        futures = {}

        for bn in bsp_files:
            try:
                dst = safe_output_path(output_dir, f"{mod_bsp_dir}/{bn}")
            except Exception as e:
                count = vtf_counts.get(bn, 0)
                results["total"] += count
                results["failed"].append(f"{bn}: {e}")
                _progress(count)
                continue

            if skip_existing and os.path.isfile(dst):
                count = vtf_counts.get(bn, 0)
                results["total"] += count
                results["skipped"] += count
                _progress(count)
                continue

            future = executor.submit(_process_one, bn, dst)
            futures[future] = bn

        for future in concurrent.futures.as_completed(futures):
            bn = futures[future]

            try:
                bsp_results.append(future.result())
            except Exception as e:
                count = vtf_counts.get(bn, 0)
                results["total"] += count
                results["failed"].append(f"{bn}: {e}")
                _progress(count)

    vtf_progress_bar.close()

    for bsp_name, r in bsp_results:
        if r.get("error"):
            results["failed"].append(f"{bsp_name}: {r['error']}")

        results["converted"] += r.get("converted", 0)
        results["skipped"] += r.get("skipped", 0)
        results["already_astc"] += r.get("already_astc", 0)
        results["total"] += r.get("total", 0)

        for failure in r.get("failed", []):
            results["failed"].append(f"{bsp_name}:{failure}")

    return results


# ---------------------------------------------------------------------------
# Summary / main
# ---------------------------------------------------------------------------

def print_summary(name, results):
    c = results["converted"]
    s = results["skipped"]
    a = results["already_astc"]
    f = results["failed"]
    t = results.get("total", c + s + a + len(f))

    print(f"\n{name}: Converted={c} Skipped={s} AlreadyASTC={a} Failed={len(f)} Total={t}")

    if f:
        print(f"  WARNING: {len(f)} failed items:")

        for fe in f[:100]:
            print(f"    -> {fe}")

        if len(f) > 100:
            print(f"    ... and {len(f) - 100} more")


def resolve_maretf_path(path):
    """Resolve maretf path, checking common build locations."""
    if os.path.isfile(path):
        return path

    # Check common build output locations relative to this script (in tools/)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        # Prebuilt binary in same directory (tools/)
        os.path.join(script_dir, "maretf.exe"),
        os.path.join(script_dir, "maretf"),
        # Build output from maretf submodule (tools/maretf/build/)
        os.path.join(script_dir, "maretf", "build", "Release", "maretf.exe"),
        os.path.join(script_dir, "maretf", "build", "Release", "maretf"),
        os.path.join(script_dir, "maretf", "build", "maretf.exe"),
        os.path.join(script_dir, "maretf", "build", "maretf"),
        # Legacy locations (MareTF/ at repo root)
        os.path.join(script_dir, "..", "MareTF", "build", "Release", "maretf.exe"),
        os.path.join(script_dir, "..", "MareTF", "build", "Release", "maretf"),
    ]

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    # Try PATH
    found = shutil.which(path)
    if found:
        return found

    return path


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32":
        default_maretf = os.path.join(script_dir, "maretf.exe")
    else:
        default_maretf = os.path.join(script_dir, "maretf")

    default_threads = os.cpu_count() or 4

    parser = argparse.ArgumentParser(
        prog="vtf_to_astc.py",
        description="Convert Source engine VTF textures to ASTC format for OpenGL ES / Vulkan ports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Convert Portal textures (VPK only, extracts to folder)
  python vtf_to_astc.py --game-dir "C:/Program Files (x86)/Steam/steamapps/common/Portal" --game portal --output-dir ./portal_astc

  # Convert Half-Life 2 textures including BSP embedded textures
  python vtf_to_astc.py --game-dir "C:/Program Files (x86)/Steam/steamapps/common/Half-Life 2" --game hl2 --output-dir ./hl2_astc --process-bsp

  # Repack converted textures back into VPK files (for direct drop-in replacement)
  python vtf_to_astc.py --game-dir "C:/Program Files (x86)/Steam/steamapps/common/Portal" --game portal --output-dir ./portal_astc_vpk --repack-vpk

  # Fast conversion with lower quality (for testing)
  python vtf_to_astc.py --game-dir /path/to/game --game portal --output-dir ./out --quality 0.6

quality presets:
  0.0  = fastest    0.6  = medium     0.99 = very thorough
  0.1  = fast       0.98 = thorough   1.0  = exhaustive (default)
""",
    )

    parser.add_argument(
        "--game-dir",
        required=True,
        metavar="PATH",
        help="Game install directory (read-only, e.g. Steam steamapps/common/Portal)",
    )
    parser.add_argument(
        "--game",
        required=True,
        choices=list(GAME_CONFIGS.keys()),
        help=f"Game to convert: {', '.join(GAME_CONFIGS.keys())}",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        metavar="PATH",
        help="Output directory for converted ASTC textures",
    )
    parser.add_argument(
        "--maretf",
        default=default_maretf,
        metavar="PATH",
        help="Path to maretf executable (default: tools/maretf[.exe])",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=default_threads,
        metavar="N",
        help="Number of parallel workers (default: CPU thread count)",
    )
    parser.add_argument(
        "--block-size",
        default="4x4",
        choices=ASTC_BLOCK_ORDER,
        metavar="SIZE",
        help="ASTC block size: 4x4, 5x4, 5x5, 6x5, 6x6, 8x5, 8x6, 8x8, etc. (default: 4x4)",
    )
    parser.add_argument(
        "--quality",
        type=float,
        default=1.0,
        metavar="0.0-1.0",
        help="ASTC compression quality: 0.0=fastest, 1.0=exhaustive (default: 1.0)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already exist in output directory",
    )
    parser.add_argument(
        "--process-bsp",
        action="store_true",
        help="Also convert VTFs embedded inside BSP map files",
    )
    parser.add_argument(
        "--repack-vpk",
        action="store_true",
        help="Repack converted VTFs back into VPK files instead of extracting to a folder",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Timeout per maretf invocation (default: 300)",
    )

    args = parser.parse_args()

    args.threads = max(1, int(args.threads))
    args.timeout = max(10, int(args.timeout))
    args.maretf = resolve_maretf_path(args.maretf)

    if not os.path.isdir(args.game_dir):
        print(f"ERROR: Game directory not found: {args.game_dir}", file=sys.stderr)
        sys.exit(1)

    # Show selected options
    print("=== VTF to ASTC Converter ===")
    print(f"  Game:        {args.game}")
    print(f"  Game dir:    {args.game_dir}")
    print(f"  Output dir:  {os.path.abspath(args.output_dir)}")
    print(f"  Block size:  {args.block_size}")
    print(f"  Quality:     {args.quality}")
    print(f"  Threads:     {args.threads}")
    print(f"  Process BSP: {'yes' if args.process_bsp else 'no'}")
    print(f"  Repack VPK:  {'yes' if args.repack_vpk else 'no'}")
    print(f"  Skip existing: {'yes' if args.skip_existing else 'no'}")
    print()

    print("=== Checking prerequisites ===")

    check_maretf(args.maretf)
    print("  sourcepp: OK")
    print("  tqdm: OK")

    cfg = GAME_CONFIGS[args.game]
    game_label = args.game
    output_dir = os.path.abspath(args.output_dir)

    # Phase 1: VPK textures
    if args.repack_vpk:
        print(f"\n=== Converting VPK textures for {game_label} (repack mode) ===")

        vpk_results = process_vpks_repack(
            args.game_dir,
            cfg["mod_dir"],
            output_dir,
            args.maretf,
            args.threads,
            block_size=args.block_size,
            timeout=args.timeout,
            quality=args.quality,
        )
    else:
        print(f"\n=== Converting VPK textures for {game_label} (extract mode) ===")

        vpk_results = process_vpks(
            args.game_dir,
            cfg["mod_dir"],
            output_dir,
            args.maretf,
            args.skip_existing,
            args.threads,
            block_size=args.block_size,
            timeout=args.timeout,
            quality=args.quality,
        )

    print_summary(f"VPK textures ({game_label})", vpk_results)

    # Phase 2: BSP embedded textures
    bsp_results = {
        "converted": 0,
        "skipped": 0,
        "already_astc": 0,
        "failed": [],
        "total": 0,
    }

    if args.process_bsp:
        print(f"\n=== Converting BSP embedded textures for {game_label} ===")

        bsp_results = process_bsps(
            args.game_dir,
            cfg["bsp_dir"],
            output_dir,
            args.maretf,
            args.skip_existing,
            args.threads,
            block_size=args.block_size,
            timeout=args.timeout,
            quality=args.quality,
        )

        print_summary(f"BSP textures ({game_label})", bsp_results)

    total_failed = len(vpk_results["failed"]) + len(bsp_results["failed"])

    print("\n=== Done! ===")
    print(f"  Output: {output_dir}")

    if total_failed > 0:
        print(f"  WARNING: {total_failed} total failures occurred.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
