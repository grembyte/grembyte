"""Load Frida's Windows gadget into a Wine/Proton game.

``frida.attach`` fails on Wine processes (its Linux agent cannot locate the
program behind ``wine-preloader``). The Windows build of Frida's gadget,
``frida-gadget.dll``, does not: loaded as an ordinary DLL it sees PE modules,
Windows threads and the Win64 ABI the way the game does, and exposes a Frida
session over a local TCP port.

To load it into an already-running game, hermes first runs a small Windows
helper inside the game's wineserver (wine_helper.py), which loads the DLL with
``CreateRemoteThread`` on ``LoadLibraryW``. Should that fail, it falls back to
calling ``LoadLibraryW`` on one of the game's own threads via ptrace (64-bit
games only): it parks a thread at a Windows function the game calls often (a
software ``int3``), points the thread at ``LoadLibraryW`` with the gadget path,
lets it return to an ``int3`` trap, then restores the saved registers and
detaches. Either way hermes then connects with
``add_remote_device("127.0.0.1:<port>")``.
"""

import ctypes
import hashlib
import json
import logging
import lzma
import os
import re
import signal
import socket
import struct
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import frida

from hermes.core import activity, wine_helper
from hermes.platform import process_start_time
from hermes.core.ptrace_engine import (
    _WAIT_ANY,
    _WALL,
    DR6,
    DR7,
    PTRACE_ATTACH,
    PTRACE_CONT,
    PTRACE_DETACH,
    PTRACE_EVENT_CLONE,
    PTRACE_O_TRACECLONE,
    PTRACE_POKEUSER,
    PTRACE_SETOPTIONS,
    _ptrace,
    tgkill,
)

_MACHINE_I386 = 0x14C

log = logging.getLogger("hermes.wine_gadget")

METHOD_AUTO = "auto"
METHOD_HELPER = "helper"
METHOD_PTRACE = "ptrace"

PTRACE_GETREGS = 12
PTRACE_SETREGS = 13

GADGET_PORT_BASE = 27100
GADGET_PORT_COUNT = 64
_GADGET_URL = ("https://github.com/frida/frida/releases/download/"
               "{version}/frida-gadget-{version}-windows-{arch}.dll.xz")
# The release's assets with the SHA-256 GitHub computed for each (``digest``).
_RELEASE_URL = "https://api.github.com/repos/frida/frida/releases/tags/{version}"
# PE machine types of the gadget
_PE_MACHINES = {"x86": 0x14C, "x86_64": 0x8664}

# Functions a game reaches from a clean Windows call boundary, where a borrowed
# thread has a valid context. A windowed game pumps messages every frame, so
# the message-pump entries fire within one frame; the timing and wait calls
# cover games that are between frames or otherwise idle. A software breakpoint
# is set on each that exists, and the first thread to reach one is borrowed.
_HOT_CANDIDATES = (
    ("win32u.dll", "NtUserPeekMessage"),
    ("win32u.dll", "NtUserGetMessage"),
    ("user32.dll", "PeekMessageW"),
    ("user32.dll", "PeekMessageA"),
    ("user32.dll", "GetMessageW"),
    ("kernelbase.dll", "GetSystemTimeAsFileTime"),
    ("kernelbase.dll", "QueryPerformanceCounter"),
    ("kernelbase.dll", "Sleep"),
    ("kernelbase.dll", "WaitForSingleObjectEx"),
)

# ``struct user_regs_struct`` field order on x86-64.
_REG_NAMES = (
    "r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10", "r9", "r8",
    "rax", "rcx", "rdx", "rsi", "rdi", "orig_rax", "rip", "cs", "eflags",
    "rsp", "ss", "fs_base", "gs_base", "ds", "es", "fs", "gs",
)


class _Regs(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in _REG_NAMES]


class GadgetError(RuntimeError):
    """The gadget could not be loaded."""


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


class _Memory:
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
        memory = _Memory(pid, writable=False)
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


# -- gadget file management ---------------------------------------------------

def gadget_dir():
    """Directory where hermes keeps downloaded gadget DLLs.

    It lives under the user's data home (never /tmp), so the Steam/Proton
    container can map it into the game's Z: drive.
    """
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    path = Path(data_home) / "hermes-hook" / "gadget"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_gadget(bits, version=None, download=True):
    """Return the path to the Windows gadget DLL for ``bits``, fetching it once.

    The DLL is named for the Frida version so an upgrade pulls a matching one.
    ``download=False`` returns an existing file or raises, never reaching out.
    """
    version = version or frida.__version__
    arch = "x86" if bits == 32 else "x86_64"
    target = gadget_dir() / f"frida-gadget-{version}-windows-{arch}.dll"
    if target.exists():
        return target
    if not download:
        raise GadgetError(f"Gadget not present: {target}")
    url = _GADGET_URL.format(version=version, arch=arch)
    try:
        digest = _published_digest(version, url.rsplit("/", 1)[1])
        with urllib.request.urlopen(url, timeout=60) as response:
            compressed = response.read()
        if digest is not None and hashlib.sha256(compressed).hexdigest() != digest:
            raise GadgetError(f"The gadget from {url} does not match its SHA-256 {digest}")
        dll = lzma.decompress(compressed)
        if not _is_pe(dll, arch):
            raise GadgetError(f"The gadget from {url} is no {arch} DLL")
        # Written in full or not at all: a cut-off file would be taken as the gadget.
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(dll)
        os.replace(partial, target)
    except (OSError, ValueError, lzma.LZMAError) as error:
        raise GadgetError(f"Could not download the gadget from {url}: {error}") from error
    return target


def _published_digest(version, name):
    """The SHA-256 GitHub lists for asset ``name`` of the release, or None if none.

    Raises OSError or ValueError when the release cannot be read: no gadget is
    used that could not be checked.
    """
    request = urllib.request.Request(_RELEASE_URL.format(version=version),
                                     headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            digest = asset.get("digest") or ""
            if digest.startswith("sha256:"):
                return digest.removeprefix("sha256:").lower()
            # Releases from before GitHub computed digests (2025) list none.
            log.warning("GitHub lists no SHA-256 for %s", name)
            return None
    raise ValueError(f"release {version} has no asset {name}")


def _is_pe(data, arch):
    """Whether ``data`` starts like a PE image for ``arch``."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return False
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset + 6 > len(data) or data[offset:offset + 4] != b"PE\0\0":
        return False
    return struct.unpack_from("<H", data, offset + 4)[0] == _PE_MACHINES[arch]


def _windows_path(unix_path):
    """Map a Unix path under the gadget dir to the Z: drive Wine exposes."""
    return "Z:" + str(unix_path).replace("/", "\\")


def write_config(dll_path, port):
    """Write the gadget's .config beside the DLL; return its path.

    Listen mode with ``on_load: resume`` lets the game keep running until
    hermes connects.
    """
    config = Path(str(dll_path)[:-4] + ".config")
    config.write_text(json.dumps({
        "interaction": {
            "type": "listen",
            "address": "127.0.0.1",
            "port": port,
            "on_load": "resume",
        }
    }), encoding="utf-8")
    return config


def _free_port():
    """Pick a loopback port the gadget can bind, from a stable range."""
    for port in range(GADGET_PORT_BASE, GADGET_PORT_BASE + GADGET_PORT_COUNT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise GadgetError("No free port for the gadget listener")


# -- ptrace loader ------------------------------------------------------------

def _get_regs(tid):
    regs = _Regs()
    _ptrace(PTRACE_GETREGS, tid, 0, ctypes.addressof(regs))
    return regs


def _set_regs(tid, regs):
    _ptrace(PTRACE_SETREGS, tid, 0, ctypes.addressof(regs))


def _list_threads(pid):
    try:
        return [int(entry) for entry in os.listdir(f"/proc/{pid}/task")]
    except OSError:
        return []


class _Tracee:
    """A ptrace attachment to every thread of a Wine process.

    Only a convenience wrapper for the one-shot loader below: it attaches all
    threads (and those born meanwhile), lets the caller plant breakpoints and
    single out the thread that hits one, and always detaches cleanly so the
    game keeps running.

    While the caller waits for that thread, the others run: stopping them
    could stop one that holds a lock LoadLibraryW needs. Whatever they report
    goes through ``service``: their own signals are delivered (Wine's suspend
    requests and exceptions must not get lost), and one that hits an ``int3``
    of ours is rewound to it and kept stopped until the ``int3`` is gone.
    """

    def __init__(self, pid):
        self.pid = pid
        self._attached = set()
        self._stopped = set()   # attached threads stopped right now
        self._pending = {}      # tid -> signal to deliver when it goes on
        self._breakpoints = {}  # address -> original byte, int3 there now
        self._planted = set()   # every address an int3 of ours was at
        self._parked = set()    # rewound at an int3 of ours, kept stopped

    def attach(self):
        """Attach and stop every thread; new threads are traced from birth."""
        for _ in range(2):  # a second pass catches threads born while attaching
            for tid in _list_threads(self.pid):
                if tid not in self._attached:
                    self._attach_thread(tid)
        if not self._attached:
            raise GadgetError(
                "Could not ptrace the game. Set "
                "/proc/sys/kernel/yama/ptrace_scope to 0 and detach any other "
                "debugger (including a running Frida session).")

    def _attach_thread(self, tid):
        try:
            _ptrace(PTRACE_ATTACH, tid, 0, 0)
            _waited, status = os.waitpid(tid, _WALL)
        except (OSError, ChildProcessError):
            return
        if not os.WIFSTOPPED(status):
            return
        # The first stop may be the game's own signal: delivered when the
        # thread goes on; the attach's SIGSTOP follows later.
        if os.WSTOPSIG(status) != signal.SIGSTOP:
            self._pending[tid] = os.WSTOPSIG(status)
        try:
            _ptrace(PTRACE_SETOPTIONS, tid, 0, PTRACE_O_TRACECLONE)
        except OSError:
            pass
        self._attached.add(tid)
        self._stopped.add(tid)

    def set_breakpoints(self, memory, addresses):
        """Plant software ``int3`` breakpoints at ``addresses``.

        One byte in shared code traps whichever thread reaches it, so this does
        not depend on hardware debug registers (which do not fire under Wine).
        """
        for address in addresses:
            if address in self._breakpoints:
                continue
            self._breakpoints[address] = memory.read(address, 1)
            self._planted.add(address)
            memory.write(address, b"\xCC")

    def clear_breakpoints(self, memory, addresses=None):
        """Restore the bytes the breakpoints overwrote (all, or ``addresses``).

        Threads kept stopped at one of them may go on then: they run the
        original instruction.
        """
        for address in list(self._breakpoints if addresses is None else addresses):
            original = self._breakpoints.pop(address, None)
            if original is not None:
                memory.write(address, original)
        for tid in list(self._parked):
            self._parked.discard(tid)
            self.resume(tid)

    def resume(self, tid, signal_number=None):
        """Let a stopped thread go on, with its pending signal unless one is given.

        A thread takes one signal per stop: a pending one stays for its next
        stop (or the detach) when another is given.
        """
        if signal_number is None or (signal_number == 0 and tid in self._pending):
            signal_number = self._pending.pop(tid, 0)
        try:
            _ptrace(PTRACE_CONT, tid, 0, signal_number)
        except OSError:
            pass
        self._stopped.discard(tid)

    def mark_borrowed(self, tid, stopped):
        """Note whether the thread the caller runs itself is stopped now."""
        if stopped:
            self._stopped.add(tid)
        else:
            self._stopped.discard(tid)

    def next_event(self):
        """Return the next ``(tid, status)`` of a thread of ours, or None if none is due."""
        try:
            tid, status = os.waitpid(-1, _WAIT_ANY)
        except ChildProcessError:
            return None
        return None if tid == 0 else (tid, status)

    def service(self, tid, status):
        """Deal with an event of a thread nobody waits for.

        Returns the breakpoint address the thread was rewound to and is kept
        stopped at, or None.
        """
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            for threads in (self._attached, self._stopped, self._parked):
                threads.discard(tid)
            self._pending.pop(tid, None)
            return None
        if not os.WIFSTOPPED(status):
            return None
        self._stopped.add(tid)
        if status >> 16 == PTRACE_EVENT_CLONE:
            self.resume(tid, 0)  # the new thread reports its own first stop
            return None
        stop_signal = os.WSTOPSIG(status)
        if stop_signal == signal.SIGSTOP:
            self._attached.add(tid)  # a late stop of ours, or a new thread
            self.resume(tid, 0)
            return None
        if stop_signal == signal.SIGTRAP:
            regs = _get_regs(tid)
            address = regs.rip - 1  # int3 leaves rip one past the trap
            if address in self._planted:
                regs.rip = address
                _set_regs(tid, regs)
                if address in self._breakpoints:
                    self._parked.add(tid)
                    return address
                self.resume(tid, 0)  # the int3 is gone: run the original byte
                return None
        self.resume(tid, stop_signal)  # the game's own signal
        return None

    def catch_breakpoint(self, timeout):
        """Resume all threads and return a thread stopped at a breakpoint.

        Returns ``(tid, address)`` with the thread's instruction pointer rewound
        to the breakpoint, so it reads as a clean call into that function, or
        ``(None, None)`` on timeout. Other threads that trip a breakpoint are
        rewound and kept stopped until the breakpoints are cleared.
        """
        for tid in list(self._stopped - self._parked):
            self.resume(tid)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.next_event()
            if event is None:
                time.sleep(0.003)
                continue
            address = self.service(*event)
            if address is not None:
                self._parked.discard(event[0])  # the caller's now
                return event[0], address
        return None, None

    def detach(self):
        """Detach from every thread, clearing any stale debug registers.

        PTRACE_DETACH needs a stopped thread: running ones get a SIGSTOP, and
        only that stop counts (one left pending would stop the whole game after
        the detach). The loader sets no debug registers, but a crashed earlier
        Frida session may have left some armed; clearing them keeps a detach
        from leaving a watchpoint behind that would trap into no tracer.
        """
        waiting = self._attached - self._stopped
        for tid in waiting:
            try:
                tgkill(self.pid, tid, signal.SIGSTOP)
            except OSError:
                pass
        deadline = time.monotonic() + 3.0
        while waiting and time.monotonic() < deadline:
            event = self.next_event()
            if event is None:
                time.sleep(0.002)
                continue
            tid, status = event
            if os.WIFSTOPPED(status) and status >> 16 != PTRACE_EVENT_CLONE \
                    and os.WSTOPSIG(status) == signal.SIGSTOP:
                self._attached.add(tid)
                self._stopped.add(tid)
                waiting.discard(tid)
                continue
            self.service(tid, status)
            if not os.WIFSTOPPED(status):
                waiting.discard(tid)
        if waiting:
            log.warning("Threads %s did not stop; they are released when this "
                        "thread ends", sorted(waiting))
        for tid in self._stopped & self._attached:
            for offset in (DR7, DR6):
                try:
                    _ptrace(PTRACE_POKEUSER, tid, offset, 0)
                except OSError:
                    pass
            try:
                _ptrace(PTRACE_DETACH, tid, 0, self._pending.pop(tid, 0))
            except OSError:
                pass
        self._attached.clear()
        self._stopped.clear()
        self._parked.clear()


# -- orchestration ------------------------------------------------------------

def _resolve(memory, images, module, name):
    image = next((img for img in images if img.name == module), None)
    return pe_export(memory, image, name) if image else None


def _trap_site(memory, images, load_library):
    """Pick an executable Windows byte to use as the call's return trap.

    Any exported function entry works: the borrowed thread reaches it only
    through the return address hermes plants, so overwriting its first byte
    with ``int3`` for the duration of the call is safe. It must differ from
    ``LoadLibraryW`` (whose own code runs during the call).
    """
    for module, name in (
        ("ntdll.dll", "DbgBreakPoint"),
        ("kernelbase.dll", "SwitchToThread"),
        ("kernel32.dll", "GetCurrentThreadId"),
        ("ntdll.dll", "RtlGetCurrentPeb"),
    ):
        address = _resolve(memory, images, module, name)
        if address and address != load_library:
            return address
    raise GadgetError("No function to anchor the return trap")


def _load_library_address(memory, images):
    for module in ("kernelbase.dll", "kernel32.dll"):
        address = _resolve(memory, images, module, "LoadLibraryW")
        if address:
            return address
    raise GadgetError("Could not find LoadLibraryW in the target")


def _hot_points(memory, images, extra=()):
    """Resolve the hot functions where a thread can be borrowed."""
    points = []
    for module, name in tuple(extra) + _HOT_CANDIDATES:
        address = _resolve(memory, images, module, name)
        if address and address not in points:
            points.append(address)
    return points


def load_gadget(pid, timeout=20.0, download=True, hot_points=(), method=METHOD_AUTO):
    """Load frida-gadget.dll into Wine process ``pid``; return its device URL.

    Returns ``"127.0.0.1:<port>"`` for ``frida.get_device_manager()
    .add_remote_device(...)``. Raises :class:`GadgetError` on failure. The game
    keeps running throughout.

    ``method`` picks the loader: ``"helper"`` (the Windows helper, see
    wine_helper.py), ``"ptrace"`` (a borrowed thread, 64-bit only) or
    ``"auto"``, the helper with ptrace as fallback. ``hot_points`` adds
    ``(module, function)`` pairs the ptrace loader tries before its built-in
    set.
    """
    memory = _Memory(pid)
    try:
        images = pe_images(pid, memory)
        if not images:
            raise GadgetError("No PE modules found; is this really a Wine process?")
        main = next((img for img in images if img.name.endswith(".exe")), images[0])
        # A 32-bit game under the new WoW64 also maps 64-bit system DLLs of the
        # same names; only the game's own architecture is of use.
        images = [img for img in images if img.bits == main.bits]

        dll = ensure_gadget(main.bits, download=download)
        port = _free_port()
        write_config(dll, port)
        url = f"127.0.0.1:{port}"

        helper_error = None
        if method in (METHOD_AUTO, METHOD_HELPER):
            try:
                _load_with_helper(pid, memory, images, main, dll)
                _remember_loader(pid, METHOD_HELPER)
                return url
            except wine_helper.HelperError as error:
                if method == METHOD_HELPER:
                    raise GadgetError(str(error)) from error
                helper_error = error
                log.warning("Windows helper could not load the gadget, "
                            "trying ptrace: %s", error)
        if main.bits != 64:
            raise GadgetError(
                "The Frida gadget could not be loaded into this 32-bit game "
                f"({helper_error}). Use the ptrace backend.")
        _load_with_ptrace(pid, memory, images, dll, timeout, hot_points)
        _remember_loader(pid, METHOD_PTRACE)
    finally:
        memory.close()
    return url


# The loader that put the gadget into a process, by (pid, start time) so a
# later process with a reused PID is not mistaken for it.
_LOADERS = {}


def _remember_loader(pid, method):
    _LOADERS[(pid, process_start_time(pid))] = method


def gadget_loader(pid):
    """Return how this hermes loaded the gadget into ``pid``.

    ``"helper"`` or ``"ptrace"``; None if it did not load one, for instance
    when the gadget came from an earlier hermes run.
    """
    return _LOADERS.get((pid, process_start_time(pid)))


def _load_with_helper(pid, memory, images, main, dll):
    """Load ``dll`` through the Windows helper in the game's wineserver."""
    helper = wine_helper.Helper(pid, main.bits)
    windows_pid = helper.windows_pid(main.name)
    try:
        load_library = _load_library_address(memory, images)
    except GadgetError:
        load_library = None  # the helper falls back to its own LoadLibraryW
    helper.inject(windows_pid, _windows_path(dll), load_library)


def _load_with_ptrace(pid, memory, images, dll, timeout, hot_points):
    """Load ``dll`` on a game thread borrowed via ptrace.

    Neither hardware watchpoints nor execution breakpoints are usable here --
    the former would need Frida, the latter do not fire under Wine -- so the
    thread is caught with a software ``int3`` on a hot function and its return
    with another, the way gdb breakpoints Wine processes.
    """
    load_library = _load_library_address(memory, images)
    trap = _trap_site(memory, images, load_library)
    hot = _hot_points(memory, images, hot_points)
    if not hot:
        raise GadgetError("No hot function found to borrow a thread at")
    wide = (_windows_path(dll) + "\0").encode("utf-16-le")

    tracee = _Tracee(pid)
    tracee.attach()
    try:
        tracee.set_breakpoints(memory, hot)
        tid, _addr = tracee.catch_breakpoint(timeout)
        tracee.clear_breakpoints(memory)
        if tid is None:
            raise GadgetError(
                "No thread reached a hot function in time. The game may be "
                "idle; try again once it is interacting.")
        _call_load_library(memory, tracee, tid, load_library, trap, wide, timeout)
    finally:
        tracee.clear_breakpoints(memory)
        tracee.detach()


def _call_load_library(memory, tracee, tid, load_library, trap, wide, timeout):
    """Divert thread ``tid`` through ``LoadLibraryW(path)`` and restore it.

    The thread is stopped at a hot function entry, a clean Windows call boundary
    with a valid thread context. hermes saves its full register set, points it
    at ``LoadLibraryW`` with the gadget path and a return address of ``trap``
    (an executable byte temporarily overwritten with ``int3``), and lets it run.
    The ``int3`` raises a SIGTRAP the tracer catches on return; then the byte,
    the registers and the thread's original course are all restored. The other
    threads run meanwhile (see _Tracee); one that reaches ``trap`` itself is
    kept stopped until the byte is back.

    Should the call not return in time, the thread is left in it: its
    registers cannot be restored while it runs, and rewinding it inside
    LoadLibraryW would leave the loader lock held.
    """
    saved = _get_regs(tid)
    tracee.set_breakpoints(memory, [trap])
    returned = False
    try:
        scratch = (saved.rsp - 0x2000) & ~0xF
        memory.write(scratch, wide)

        regs = _get_regs(tid)
        # A Win64 call wants (rsp + 8) % 16 == 0 at the callee's first
        # instruction, with 32 bytes of shadow space above the return address.
        new_rsp = (scratch - len(wide) - 0x200) & ~0xF
        new_rsp -= 8  # the return address a CALL would have pushed
        memory.write(new_rsp, struct.pack("<Q", trap))
        regs.rsp = new_rsp
        regs.rip = load_library
        regs.rcx = scratch  # LoadLibraryW(LPCWSTR): first arg in rcx (Win64)
        _set_regs(tid, regs)
        # A signal pending from the attach waits until the thread is restored.
        _ptrace(PTRACE_CONT, tid, 0, 0)
        tracee.mark_borrowed(tid, stopped=False)

        deadline = time.monotonic() + timeout
        result = None
        while time.monotonic() < deadline:
            event = tracee.next_event()
            if event is None:
                time.sleep(0.002)
                continue
            waited, status = event
            if waited != tid:
                tracee.service(waited, status)
                continue
            if not os.WIFSTOPPED(status):
                returned = True  # nothing left to restore
                raise GadgetError("The borrowed thread exited during the call")
            if status >> 16 == PTRACE_EVENT_CLONE:
                _ptrace(PTRACE_CONT, tid, 0, 0)
                continue
            stop_signal = os.WSTOPSIG(status)
            if stop_signal == signal.SIGTRAP:
                stopped = _get_regs(tid)
                # int3 leaves rip one byte past the trap.
                if stopped.rip in (trap, trap + 1):
                    returned = True
                    result = stopped.rax  # HMODULE, 0 on failure
                    break
            # Its own signals reach it, e.g. a guard page fault Wine handles.
            _ptrace(PTRACE_CONT, tid, 0, 0 if stop_signal == signal.SIGSTOP else stop_signal)
        if result is None:
            raise GadgetError("LoadLibraryW did not return within the timeout; "
                              "the borrowed thread is left in the call")
        if not result:
            raise GadgetError("LoadLibraryW returned NULL; the gadget did not load")
    finally:
        tracee.clear_breakpoints(memory, [trap])
        if returned:
            try:
                _set_regs(tid, saved)
            except OSError:
                pass  # it exited
        # The thread stays stopped for detach either way: returned at the
        # trap, or left in its call (then detach stops it).
        tracee.mark_borrowed(tid, stopped=returned)


# -- sessions -----------------------------------------------------------------

_LOCK = threading.Lock()
_DEVICES = {}


def gadget_port(pid, proc_root="/proc"):
    """Return the port of a hermes gadget listening in ``pid``, or None.

    The gadget's listening socket belongs to the game, so its port is found
    from the process's socket inodes and the kernel's TCP table, without any
    state of hermes'. This also finds a gadget an earlier hermes loaded.
    """
    inodes = set()
    try:
        for fd in os.listdir(f"{proc_root}/{pid}/fd"):
            try:
                target = os.readlink(f"{proc_root}/{pid}/fd/{fd}")
            except OSError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match:
                inodes.add(match.group(1))
        with open(f"{proc_root}/{pid}/net/tcp", encoding="ascii") as table:
            rows = table.readlines()[1:]
    except OSError:
        return None
    for row in rows:
        fields = row.split()
        if len(fields) < 10 or fields[3] != "0A" or fields[9] not in inodes:
            continue  # 0A: LISTEN
        address, port = fields[1].split(":")
        port = int(port, 16)
        if address == "0100007F" and (
                GADGET_PORT_BASE <= port < GADGET_PORT_BASE + GADGET_PORT_COUNT):
            return port
    return None


def _wait_listening(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = gadget_port(pid)
        if port is not None:
            return port
        time.sleep(0.05)
    raise GadgetError("The gadget loaded but does not listen for Frida")


def _device(url, fresh=False):
    manager = frida.get_device_manager()
    if fresh and url in _DEVICES:
        del _DEVICES[url]
        try:
            manager.remove_remote_device(url)
        except frida.InvalidArgumentError:
            pass
    if url not in _DEVICES:
        _DEVICES[url] = manager.add_remote_device(url)
    return _DEVICES[url]


def connect(pid, download=True):
    """Return a Frida session on Wine process ``pid`` through the gadget.

    The gadget is loaded on first use and reused afterwards, by every worker
    and across hermes restarts; each caller gets a session of its own.
    """
    with _LOCK:
        port = gadget_port(pid)
        if port is None:
            began = time.monotonic()
            try:
                load_gadget(pid, download=download)
                port = _wait_listening(pid)
            except (OSError, GadgetError) as error:
                activity.record("gadget_failed", pid=pid, error=str(error),
                                seconds=round(time.monotonic() - began, 3))
                if isinstance(error, GadgetError):
                    raise
                raise GadgetError(
                    f"Could not load the Frida gadget into the game: {error}") from error
            activity.record("gadget_loaded", pid=pid, port=port, method=gadget_loader(pid),
                            seconds=round(time.monotonic() - began, 3))
        url = f"127.0.0.1:{port}"
        try:
            return _attach_gadget(_device(url))
        except (frida.ServerNotRunningError, frida.TransportError,
                frida.InvalidOperationError):
            # The cached device belonged to a gadget that is gone (a game
            # restarted on the same port): connect afresh.
            return _attach_gadget(_device(url, fresh=True))


def _attach_gadget(device):
    # The gadget's device lists exactly one process: the game.
    return device.attach(device.enumerate_processes()[0].pid)
