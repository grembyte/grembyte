"""Linux: processes through /proc/<pid>/maps, /proc/<pid>/mem and friends."""

import functools
import os
import struct
from pathlib import Path

from grembyte.core.messages import LocalizedOSError
from grembyte.platform.mapping import Mapping

# Kernel mappings that are no memory of the program.
_KERNEL_MAPPINGS = {"[vvar]", "[vvar_vclock]", "[vsyscall]", "[vdso]"}


def _image_size(path):
    """SizeOfImage of the PE file at ``path`` (its extent once loaded), or None."""
    try:
        status = os.stat(path)
    except OSError:
        return None
    return _image_size_of(path, status.st_mtime_ns, status.st_size)


@functools.lru_cache(maxsize=512)
def _image_size_of(path, _mtime, _size):
    try:
        with open(path, "rb") as image:
            dos = image.read(0x40)
            if len(dos) < 0x40 or dos[:2] != b"MZ":
                return None
            image.seek(struct.unpack_from("<I", dos, 0x3C)[0])
            header = image.read(24 + 60)
    except OSError:
        return None
    if len(header) < 24 + 60 or header[:4] != b"PE\0\0":
        return None
    # SizeOfImage: at 56 into the optional header, in PE32 and PE32+ alike
    return struct.unpack_from("<I", header, 24 + 56)[0] or None


def _pe_images(regions, image_size):
    """``[(start, end, path)]`` of the PE images among the regions of /proc/<pid>/maps."""
    images = []
    for start, _end, _permissions, path, offset in regions:
        if offset == 0 and path.startswith("/") and path.lower().endswith((".exe", ".dll")):
            size = image_size(path)
            if size:
                images.append((start, start + size, path))
    return images


_PAGE = 0x1000
_PT_LOAD = 1


def elf_end(read, start):
    """End of the ELF module loaded at ``start``, from its program headers.

    ``read(address, size)`` reads the process' memory (bytes, or None). The
    module ends where its last loadable segment does, .bss included. None if
    no ELF header is there.
    """
    header = read(start, 64)
    if not header or len(header) < 64 or header[:4] != b"\x7fELF":
        return None
    if header[4] == 2:      # 64-bit
        phoff = struct.unpack_from("<Q", header, 0x20)[0]
        entry_size, count = struct.unpack_from("<HH", header, 0x36)
        layout, fields = "<IIQQQQQQ", (3, 6)
    elif header[4] == 1:    # 32-bit
        phoff = struct.unpack_from("<I", header, 0x1C)[0]
        entry_size, count = struct.unpack_from("<HH", header, 0x2A)
        layout, fields = "<IIIIIIII", (2, 5)
    else:
        return None
    table = read(start + phoff, entry_size * count) if count else None
    if not table or len(table) < entry_size * count or entry_size < struct.calcsize(layout):
        return None
    segments = []
    for index in range(count):
        entry = struct.unpack_from(layout, table, index * entry_size)
        if entry[0] == _PT_LOAD:
            segments.append((entry[fields[0]], entry[fields[1]]))
    if not segments:
        return None
    low = min(address for address, _size in segments) & ~(_PAGE - 1)
    high = max(address + size for address, size in segments)
    return start + ((high - low + _PAGE - 1) & ~(_PAGE - 1))


def parse_maps(lines, image_size=_image_size, module_end=None):
    # pylint: disable=too-many-locals,too-many-branches
    """Parse the lines of /proc/<pid>/maps into Mappings.

    A module is a file with executable code. All its mappings belong to it,
    and so does an anonymous writable mapping right behind one of them (.bss,
    if it does not fit the file): up to where the module ends,
    ``module_end(start)`` of the module loaded at ``start`` (see
    ``elf_end``). The kernel merges .bss with anonymous memory allocated
    right behind it, heap of the game, which is no module's then. Unknown
    (None) the mapping is no module's; without ``module_end`` all of it is.
    Other mapped files, e.g. /dev/dri/renderD128 or game assets, are no
    modules; Frida does not know them either.

    A PE image under Wine (an .exe or .dll mapped from offset 0) is a module
    over its whole extent (``image_size(path)``, its SizeOfImage): Wine
    copies sections whose file layout does not fit pages into anonymous
    memory, often all the code of a game's executable, so no mapping of the
    file itself may be executable.
    """
    regions = []
    for line in lines:
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            continue
        start, end = (int(bound, 16) for bound in fields[0].split("-"))
        path = fields[5].strip() if len(fields) == 6 else ""
        regions.append((start, end, fields[1], path, int(fields[2], 16)))
    modules = {
        path for _start, _end, permissions, path, _offset in regions
        if path.startswith("/") and "x" in permissions
    }
    images = _pe_images(regions, image_size)
    modules.update(path for _start, _end, path in images)
    mappings = []
    previous = None
    # path -> load address of the module, and the end ``module_end`` gave it
    loaded, ends = {}, {}
    for start, end, permissions, path, _offset in sorted(regions):
        writable = (
            all(flag in permissions for flag in "rwp")
            and not path.startswith("/dev/") and path not in _KERNEL_MAPPINGS
        )
        module = None
        image = next((path for low, high, path in images if low <= start and end <= high),
                     None) if not path else None
        if path in modules:
            module = os.path.basename(path)
            loaded.setdefault(path, start)
        elif image is not None:
            module = os.path.basename(image)
        elif writable and not path and previous is not None \
                and previous.path in modules and previous.end == start:
            limit = end
            if module_end is not None:
                if previous.path not in ends:
                    ends[previous.path] = module_end(loaded[previous.path])
                limit = min(end, ends[previous.path] or start)
            if limit > start:
                mappings.append(Mapping(start, limit, permissions, path, previous.module,
                                        writable))
                start = limit
            if start == end:
                previous = mappings[-1]
                continue
        previous = Mapping(start, end, permissions, path, module, writable)
        mappings.append(previous)
    return mappings


def read_mappings(pid):
    """Return the Mappings of process ``pid``.

    Where modules end is read from their ELF headers in the process' memory,
    not from their files: those may be gone (Frida's agent is a memfd) or,
    inside Steam's runtime container, other files on the host.

    Only Wine loads PE images; a native program that maps a .dll, as Mono
    does its assemblies, reads it as data.
    """
    with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as maps_file:
        lines = maps_file.readlines()
    image_size = _image_size if is_wine_process(pid) else (lambda _path: None)
    try:
        memory = open(f"/proc/{pid}/mem", "rb", buffering=0)  # pylint: disable=consider-using-with
    except OSError:
        return parse_maps(lines, image_size, module_end=lambda _start: None)

    def read(address, size):
        try:
            memory.seek(address)
            return memory.read(size)
        except (OSError, ValueError, OverflowError):
            return None

    with memory:
        return parse_maps(lines, image_size, module_end=lambda start: elf_end(read, start))


def main_module(pid, proc_root="/proc"):
    """Return the module name of the program itself, or None if unknown.

    Under Wine /proc/<pid>/exe is Wine's loader (wine64-preloader), not the
    game: there it is the .exe the process maps, named as ``parse_maps``
    names modules.
    """
    if is_wine_process(pid, proc_root):
        # pylint: disable-next=import-outside-toplevel
        from grembyte.platform.wine import wine_program
        path = wine_program(pid)[0]
        return os.path.basename(path) if path else None
    try:
        path = os.readlink(f"{proc_root}/{pid}/exe")
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


def thread_stack_pointers(pid, proc_root="/proc", attempts=3):
    """Return the stack pointer of each thread of ``pid`` that could be read.

    /proc/<pid>/task/<tid>/syscall ends with the stack and instruction
    pointer of a thread that is not running: waiting in a system call, or
    "-1 sp pc" outside one. Wine's threads are Linux threads too. A thread
    that is running just then reads as "running" and is tried again, up to
    ``attempts`` times.
    """
    tasks = Path(proc_root) / str(pid) / "task"
    try:
        pending = [entry.name for entry in tasks.iterdir()]
    except OSError:
        return []
    pointers = []
    for _attempt in range(attempts):
        running = []
        for tid in pending:
            try:
                fields = (tasks / tid / "syscall").read_text(encoding="ascii").split()
            except (OSError, UnicodeDecodeError):
                continue
            if fields and fields[0] == "running":
                running.append(tid)
            elif len(fields) >= 3:
                try:
                    pointers.append(int(fields[-2], 16))
                except ValueError:
                    continue
        if not running:
            break
        pending = running
    return pointers


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
    ``wine_bits`` (grembyte.platform.wine).
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
