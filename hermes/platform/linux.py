"""Linux: processes through /proc/<pid>/maps, /proc/<pid>/mem and friends."""

import os
from pathlib import Path

from hermes.core.messages import LocalizedOSError
from hermes.platform.mapping import Mapping

# Kernel mappings that are no memory of the program.
_KERNEL_MAPPINGS = {"[vvar]", "[vvar_vclock]", "[vsyscall]", "[vdso]"}


def parse_maps(lines):
    """Parse the lines of /proc/<pid>/maps into Mappings.

    A module is a file with executable code. All its mappings belong to it,
    and so does an anonymous writable mapping right behind one of them (.bss,
    if it does not fit the file). Other mapped files, e.g.
    /dev/dri/renderD128 or game assets, are no modules; Frida does not know
    them either.
    """
    regions = []
    for line in lines:
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            continue
        start, end = (int(bound, 16) for bound in fields[0].split("-"))
        path = fields[5].strip() if len(fields) == 6 else ""
        regions.append((start, end, fields[1], path))
    modules = {
        path for _start, _end, permissions, path in regions
        if path.startswith("/") and "x" in permissions
    }
    mappings = []
    previous = None
    for start, end, permissions, path in sorted(regions):
        writable = (
            all(flag in permissions for flag in "rwp")
            and not path.startswith("/dev/") and path not in _KERNEL_MAPPINGS
        )
        module = None
        if path in modules:
            module = os.path.basename(path)
        elif writable and not path and previous is not None \
                and previous.path in modules and previous.end == start:
            module = previous.module
        previous = Mapping(start, end, permissions, path, module, writable)
        mappings.append(previous)
    return mappings


def read_mappings(pid):
    """Return the Mappings of process ``pid``."""
    with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as maps_file:
        return parse_maps(maps_file)


def main_module(pid):
    """Return the module name of the program itself, or None if unknown."""
    try:
        path = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None
    return os.path.basename(path.removesuffix(" (deleted)"))


class ProcessMemory:
    """Read, and if ``writable`` also write, a process' memory via /proc/<pid>/mem."""

    def __init__(self, pid, writable=False):
        self.pid = pid
        self._file = open(  # pylint: disable=consider-using-with
            f"/proc/{pid}/mem", "r+b" if writable else "rb", buffering=0
        )

    def close(self):
        """Close the memory file."""
        self._file.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def read_available(self, address, size):
        """Return up to ``size`` bytes at ``address``.

        Fewer if readable memory ends earlier, ``b""`` if ``address`` itself
        cannot be read.
        """
        if address < 0:
            return b""
        try:
            self._file.seek(address)
            return self._file.read(size)
        except (OSError, OverflowError, ValueError):
            return b""

    def read(self, address, size):
        """Return ``size`` bytes or None if the memory cannot be read."""
        data = self.read_available(address, size)
        return data if len(data) == size else None

    def read_pointer(self, address, size=8):
        """Return the pointer at ``address`` or None."""
        data = self.read(address, size)
        return None if data is None else int.from_bytes(data, "little")

    def write(self, address, data):
        """Write ``data`` to ``address``; raises OSError unless all of it was written.

        Read-only pages are written too: the kernel writes through them.
        """
        self._file.seek(address)
        written = self._file.write(data)
        if written != len(data):
            raise LocalizedOSError("error.platform.incomplete_write_linux", written=written,
                                   size=len(data))


def process_start_time(pid, proc_root="/proc"):
    """Return when ``pid`` was started (clock ticks after boot), or None.

    None means the process has exited, including zombies that wait for their
    parent. Comparing start times tells a restarted program that got the same
    PID apart from the original one.
    """
    try:
        stat = (Path(proc_root) / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces and parentheses; fields follow the
    # last ')'. They start with field 3 (state); starttime is field 22.
    fields = stat[stat.rindex(")") + 2:].split()
    if fields[0] in {"Z", "X", "x"}:
        return None
    return int(fields[19])


def program_bits(pid, proc_root="/proc"):
    """Return 32 or 64 for the ELF program ``pid`` runs, or None if unknown.

    Under Wine that is the loader's; the Windows program's own is
    ``wine_bits`` (hermes.platform.wine).
    """
    try:
        with open(f"{proc_root}/{pid}/exe", "rb") as exe:
            ident = exe.read(5)
    except OSError:
        return None
    if ident[:4] != b"\x7fELF":
        return None
    # EI_CLASS: ELFCLASS32 or ELFCLASS64
    return {1: 32, 2: 64}.get(ident[4])


def is_wine_process(pid, proc_root="/proc"):
    """Return True if ``pid`` runs under Wine or Proton.

    Wine starts Windows programs through its static ``wine-preloader``
    (``wine64-preloader``), or through the ``wine`` loader itself.
    """
    try:
        name = (Path(proc_root) / str(pid) / "exe").readlink().name
    except OSError:
        return False
    return name in {"wine", "wine64"} or (
        name.startswith("wine") and name.endswith("-preloader"))
