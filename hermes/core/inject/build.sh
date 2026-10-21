#!/bin/sh
# Build hermes-inject for 64- and 32-bit Wine games.
# Uses MinGW when installed (dnf install mingw64-gcc mingw32-gcc), else zig
# (pip install ziglang).
set -e
cd "$(dirname "$0")"
FLAGS="-O2 -s -Wall -o"
LIBS="-lshell32"
if command -v x86_64-w64-mingw32-gcc >/dev/null && command -v i686-w64-mingw32-gcc >/dev/null; then
    x86_64-w64-mingw32-gcc inject.c $FLAGS hermes-inject-x86_64.exe $LIBS
    i686-w64-mingw32-gcc inject.c $FLAGS hermes-inject-x86.exe $LIBS
else
    ZIG="${ZIG:-python3 -m ziglang}"
    $ZIG cc -target x86_64-windows-gnu inject.c $FLAGS hermes-inject-x86_64.exe $LIBS
    $ZIG cc -target x86-windows-gnu inject.c $FLAGS hermes-inject-x86.exe $LIBS
fi
