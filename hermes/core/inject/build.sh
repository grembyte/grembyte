#!/bin/sh
# Build hermes-inject for 64- and 32-bit Wine games.
# Uses MinGW when installed (sudo dnf install mingw64-gcc mingw32-gcc), else zig
# (pip install ziglang).
set -e
cd "$(dirname "$0")"
FLAGS="-O2 -s -Wall -o"
LIBS="-lshell32"
FAILED=0

# build <output> <compiler command...>
build() {
    out=$1
    shift
    if "$@" inject.c $FLAGS "$out" $LIBS; then
        echo "OK:     $out"
    else
        echo "FAILED: $out" >&2
        FAILED=1
    fi
}

if command -v x86_64-w64-mingw32-gcc >/dev/null && command -v i686-w64-mingw32-gcc >/dev/null; then
    echo "Toolchain: MinGW"
    build hermes-inject-x86_64.exe x86_64-w64-mingw32-gcc
    build hermes-inject-x86.exe i686-w64-mingw32-gcc
else
    ZIG="${ZIG:-python3 -m ziglang}"
    echo "Toolchain: zig ($ZIG)"
    build hermes-inject-x86_64.exe $ZIG cc -target x86_64-windows-gnu
    build hermes-inject-x86.exe $ZIG cc -target x86-windows-gnu
fi

if [ "$FAILED" -eq 0 ]; then
    echo "Build succeeded."
else
    echo "Build failed." >&2
    exit 1
fi
