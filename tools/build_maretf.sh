#!/bin/bash
# MareTF build script for Linux (aarch64 native or cross-compilation)
# Usage: ./build_maretf.sh [--gui] [--clean] [--cross]
#
# Options:
#   --gui     Build with GUI (requires Qt6)
#   --clean   Clean build directory first
#   --cross   Cross-compile for aarch64 (uses aarch64-linux-gnu toolchain)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARETF_DIR="$SCRIPT_DIR/maretf"
BUILD_DIR="$MARETF_DIR/build"
BUILD_GUI=OFF
CLEAN_BUILD=0
CROSS_COMPILE=0

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --gui)
            BUILD_GUI=ON
            shift
            ;;
        --clean)
            CLEAN_BUILD=1
            shift
            ;;
        --cross)
            CROSS_COMPILE=1
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: ./build_maretf.sh [--gui] [--clean] [--cross]"
            exit 1
            ;;
    esac
done

echo "=== MareTF Build Script (Linux) ==="
echo "Build GUI: $BUILD_GUI"
echo "Cross-compile: $CROSS_COMPILE"
echo

# Clean if requested
if [[ $CLEAN_BUILD -eq 1 ]] && [[ -d "$BUILD_DIR" ]]; then
    echo "Cleaning build directory..."
    rm -rf "$BUILD_DIR"
fi

# Create build directory
mkdir -p "$BUILD_DIR"

# Initialize MareTF submodules if needed
echo "=== Checking submodules ==="
git -C "$MARETF_DIR" submodule update --init --recursive --quiet 2>/dev/null || \
    echo "WARNING: Failed to initialize some submodules"

# Set up cross-compilation environment
CMAKE_ARGS=(
    -S "$MARETF_DIR"
    -B "$BUILD_DIR"
    -DCMAKE_BUILD_TYPE=Release
    -DMARETF_BUILD_CLI=ON
    -DMARETF_BUILD_GUI=$BUILD_GUI
    -DMARETF_BUILD_THUMBNAILER=OFF
    -DMARETF_BUILD_INSTALLER=OFF
)

if [[ $CROSS_COMPILE -eq 1 ]]; then
    echo "=== Configuring for aarch64 cross-compilation ==="
    export PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig
    export CC=aarch64-linux-gnu-gcc
    export CXX=aarch64-linux-gnu-g++
    CMAKE_ARGS+=(
        -DCMAKE_SYSTEM_NAME=Linux
        -DCMAKE_SYSTEM_PROCESSOR=aarch64
    )
else
    echo "=== Configuring for native build ==="
fi

# Configure
cmake "${CMAKE_ARGS[@]}"

# Build
echo
echo "=== Building ==="
cmake --build "$BUILD_DIR" --config Release --target maretf -j$(nproc)

echo
echo "=== Build Complete ==="
echo "Binary: $BUILD_DIR/maretf"

# Copy to tools directory
cp "$BUILD_DIR/maretf" "$SCRIPT_DIR/maretf" 2>/dev/null || true
if [[ -f "$SCRIPT_DIR/maretf" ]]; then
    echo "Copied to: $SCRIPT_DIR/maretf"
fi
