"""Wine/Proton programs seen from outside: the PE images a Wine process maps.

Linux only at runtime (it reads /proc/<pid>/maps and /proc/<pid>/mem); on
Windows no program runs under Wine, so nobody calls it there.
"""

import os
import struct
from dataclasses import dataclass

_MACHINE_I386 = 0x14C


@dataclass(frozen=True)
class PeImage:
    """A PE file mapped into a Wine process."""

    name: str
    path: str
    base: int
    machine: int

    @property
    def bits(self):
        """32 or 64."""
        return 32 if self.machine == _MACHINE_I386 else 64


class RawMemory:
    """Read and write another process's memory through /proc/<pid>/mem."""

    def __init__(self, pid, writable=True):
        self._fd = os.open(f"/proc/{pid}/mem", os.O_RDWR if writable else os.O_RDONLY)

    def close(self):
        """Close the file."""
        os.close(self._fd)

    def read(self, address, size):
        """Read ``size`` bytes."""
        return os.pread(self._fd, size, address)

    def write(self, address, data):
        """Write ``data`` (ptrace access reaches read-only pages too)."""
        os.pwrite(self._fd, data, address)

    def u16(self, address):
        """Read a little-endian unsigned 16-bit value."""
        return struct.unpack("<H", self.read(address, 2))[0]

    def u32(self, address):
        """Read a little-endian unsigned 32-bit value."""
        return struct.unpack("<I", self.read(address, 4))[0]

    def cstr(self, address, limit=256):
        """Read a NUL-terminated ASCII string."""
        raw = self.read(address, limit)
        return raw.split(b"\0", 1)[0].decode("latin-1")


def _maps(pid):
    """Yield ``(start, end, permissions, offset, path)`` per mapping."""
    with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as maps:
        for line in maps:
            fields = line.split(maxsplit=5)
            if len(fields) < 5:
                continue
            start, end = (int(bound, 16) for bound in fields[0].split("-"))
            path = fields[5].strip() if len(fields) == 6 else ""
            yield start, end, fields[1], int(fields[2], 16), path


def pe_images(pid, memory):
    """Return the PE images (DLLs and EXEs) mapped into ``pid``.

    Wine's PE modules are not reported by Frida's module list, so hermes finds
    them by walking /proc/<pid>/maps for file-backed ranges that begin with an
    ``MZ``/``PE`` header at file offset 0.
    """
    images = []
    for start, _end, _perms, offset, path in _maps(pid):
        if offset or not path.lower().endswith((".dll", ".exe")):
            continue
        try:
            if memory.read(start, 2) != b"MZ":
                continue
            header = start + memory.u32(start + 0x3C)
            if memory.read(header, 4) != b"PE\0\0":
                continue
            machine = memory.u16(header + 4)
        except OSError:
            continue
        images.append(PeImage(os.path.basename(path).lower(), path, start, machine))
    return images


def wine_program(pid):
    """(path, 32 or 64) of the Windows program a Wine process runs, or (None, None)."""
    try:
        memory = RawMemory(pid, writable=False)
    except OSError:
        return None, None
    try:
        images = pe_images(pid, memory)
    finally:
        memory.close()
    main = next((image for image in images if image.name.endswith(".exe")), None)
    return (main.path, main.bits) if main else (None, None)


def wine_bits(pid):
    """Return 32 or 64 for the program a Wine process runs, or None if unknown."""
    return wine_program(pid)[1]


def pe_export(memory, image, name):
    """Resolve an exported function of ``image`` to its address, or None."""
    base = image.base
    header = base + memory.u32(base + 0x3C)
    optional = header + 24
    magic = memory.u16(optional)
    # The export directory RVA sits in data directory 0, whose offset into the
    # optional header differs between PE32 (96) and PE32+ (112).
    directory = optional + (112 if magic == 0x20B else 96)
    export = base + memory.u32(directory)
    count = memory.u32(export + 24)
    functions = base + memory.u32(export + 28)
    names = base + memory.u32(export + 32)
    ordinals = base + memory.u32(export + 36)
    target = name.encode("latin-1")
    for index in range(count):
        entry = base + memory.u32(names + index * 4)
        if memory.cstr(entry, len(target) + 1).encode("latin-1") == target:
            ordinal = memory.u16(ordinals + index * 2)
            return base + memory.u32(functions + ordinal * 4)
    return None
