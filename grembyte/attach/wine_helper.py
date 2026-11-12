"""Load a DLL into a Wine/Proton game with a small Windows helper program.

``grembyte-inject.exe`` (source in ``inject/inject.c``) loads a DLL the
standard Windows way: ``VirtualAllocEx``, ``WriteProcessMemory`` and
``CreateRemoteThread`` on ``LoadLibraryW``. Those calls only reach processes of
the same wineserver, so grembyte runs the helper with the game's own Wine build
and ``WINEPREFIX``, both read from ``/proc/<pid>``. Neither ptrace nor a
breakpoint is involved, and a 32-bit game gets the 32-bit helper.

Wine names a prefix's server socket after the prefix directory's device and
inode. grembyte checks that this socket exists before it runs anything, because
a Wine started on a prefix without a running server would start a server of
its own and never see the game.
"""

import logging
import ntpath
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from grembyte.core.messages import LocalizedError
from grembyte.platform import process_start_time

# The helper's source and build script; build.sh puts the executables into
# build/inject/ of the repository (BUILD_DIR), or $GREMBYTE_INJECT_DIR.
HELPER_DIR = Path(__file__).resolve().parent / "inject"
BUILD_DIR = Path(__file__).resolve().parents[2] / "build" / "inject"
HELPER_TIMEOUT = 60.0
BUILD_TIMEOUT = 120.0

log = logging.getLogger("grembyte.wine_helper")
_build_lock = threading.Lock()
_build_tried = False

# Of the game's Wine variables only those naming its prefix and those the
# server must agree on (esync, fsync, ntsync) are copied. Others are Wine's
# own between parent and child (WINESERVERSOCKET is an inherited descriptor,
# WINEPRELOADRESERVE the game's address range) or paths that in a Steam
# runtime container need not exist on the host.
_COPIED_WINE_VARS = {"WINEPREFIX", "WINEARCH"}


class HelperError(LocalizedError, RuntimeError):
    """The helper could not load the DLL."""


@dataclass(frozen=True)
class WineProcess:
    """A process as the helper's ``list`` command reports it."""

    pid: int
    created_ms: int
    name: str
    bits: int = 0  # 32 or 64, 0 if unknown


def helper_dir():
    """Where the helper executables are: ``$GREMBYTE_INJECT_DIR`` or BUILD_DIR."""
    override = os.environ.get("GREMBYTE_INJECT_DIR")
    return Path(override).expanduser() if override else BUILD_DIR


def helper_path(bits, build=True):
    """Return the helper executable for a 32- or 64-bit target.

    With ``build`` a missing helper is built once with build.sh (MinGW or zig
    needed); raises HelperError if it is still missing.
    """
    name = "grembyte-inject-x86.exe" if bits == 32 else "grembyte-inject-x86_64.exe"
    path = helper_dir() / name
    if not path.exists() and build:
        build_helpers(path.parent)
    if not path.exists():
        raise HelperError("error.helper.not_built", path=path,
                          script=HELPER_DIR / "build.sh")
    return path


def build_helpers(out_dir):
    """Run build.sh into ``out_dir``, once per run of grembyte; returns whether it succeeded."""
    global _build_tried  # pylint: disable=global-statement
    with _build_lock:
        if _build_tried:
            return False
        _build_tried = True
        log.info("building the Windows helper into %s", out_dir)
        try:
            result = subprocess.run(
                ["sh", str(HELPER_DIR / "build.sh")], env={**os.environ, "OUT_DIR": str(out_dir)},
                capture_output=True, text=True, timeout=BUILD_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            log.warning("building the Windows helper failed: %s", error)
            return False
        if result.returncode != 0:
            log.warning("building the Windows helper failed:\n%s%s",
                        result.stdout, result.stderr)
            return False
        return True


def read_environ(pid, proc_root="/proc"):
    """Return the environment ``pid`` was started with."""
    try:
        raw = (Path(proc_root) / str(pid) / "environ").read_bytes()
    except OSError as error:
        raise HelperError("error.helper.environment", error=error) from error
    environ = {}
    for entry in raw.split(b"\0"):
        name, sep, value = entry.partition(b"=")
        if sep:
            environ[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return environ


def wine_prefix(environ):
    """Return the prefix directory the game runs in."""
    prefix = environ.get("WINEPREFIX")
    if prefix:
        return Path(prefix)
    home = environ.get("HOME") or str(Path.home())
    return Path(home) / ".wine"


def server_socket(prefix, uid=None):
    """Return the socket path of the wineserver that serves ``prefix``.

    Wine keeps it in ``/tmp/.wine-<uid>/server-<dev>-<inode>/`` with the
    prefix directory's device and inode in hex.
    """
    info = os.stat(prefix)
    uid = os.getuid() if uid is None else uid
    return Path(f"/tmp/.wine-{uid}/server-{info.st_dev:x}-{info.st_ino:x}/socket")


def _wine_in(directory):
    for name in ("wine", "wine64"):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def wine_loader(pid, environ, proc_root="/proc"):
    """Find the ``wine`` program of the build the game runs on.

    The game's executable is that build's preloader, e.g.
    ``…/files/lib/wine/x86_64-unix/wine64-preloader`` (Proton) or
    ``/usr/bin/wine64-preloader``; ``wine`` sits beside it or in a ``bin``
    directory up to three levels above. A path only the game's container sees
    is reached through ``/proc/<pid>/root``.
    """
    try:
        exe = (Path(proc_root) / str(pid) / "exe").readlink()
    except OSError as error:
        raise HelperError("error.helper.executable", error=error) from error
    loader = environ.get("WINELOADER")
    roots = (Path("/"), Path(proc_root) / str(pid) / "root")
    for root in roots:
        if loader:
            candidate = root / loader.lstrip("/")
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        directory = root / str(exe.parent).lstrip("/")
        for _level in range(4):
            found = _wine_in(directory) or _wine_in(directory / "bin")
            if found:
                return found
            directory = directory.parent
    raise HelperError("error.helper.no_wine", exe=exe)


def helper_environment(environ, base=None):
    """Build the helper's environment: grembyte's own plus the game's Wine settings."""
    env = dict(os.environ if base is None else base)
    for name in [key for key in env if key.startswith("WINE")]:
        del env[name]
    for name, value in environ.items():
        if name in _COPIED_WINE_VARS or (name.startswith("WINE") and "SYNC" in name):
            env[name] = value
    env["WINEPREFIX"] = str(wine_prefix(environ))
    env["WINEDEBUG"] = "-all"
    return env


def parse_list(output):
    """Parse the helper's ``list`` output into :class:`WineProcess` entries."""
    processes = []
    for line in output.splitlines():
        fields = line.rstrip("\r").split("\t")
        if len(fields) not in (3, 4):
            continue
        try:
            bits = int(fields[3]) if len(fields) == 4 else 0
            processes.append(WineProcess(int(fields[0]), int(fields[1]), fields[2], bits))
        except ValueError:
            continue
    return processes


def start_time_ms(pid, proc_root="/proc"):
    """Return when ``pid`` started, in milliseconds since the Unix epoch, or None."""
    ticks = process_start_time(pid, proc_root)
    if ticks is None:
        return None
    try:
        with open(f"{proc_root}/stat", encoding="ascii") as stat:
            boot = next(int(line.split()[1]) for line in stat if line.startswith("btime"))
    except (OSError, StopIteration, ValueError):
        return None
    return boot * 1000 + ticks * 1000 // os.sysconf("SC_CLK_TCK")


def sibling_start_times(pid, exe_name, proc_root="/proc"):
    """Return when the Unix processes running ``exe_name`` in ``pid``'s prefix started.

    In milliseconds since the Unix epoch, ``pid``'s own start among them. A
    Wine process's command line begins with its Windows program.
    """
    prefix = wine_prefix(read_environ(pid, proc_root))
    exe_name = exe_name.lower()
    times = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        try:
            program = (Path(proc_root) / entry / "cmdline").read_bytes().split(b"\0", 1)[0]
        except OSError:
            continue
        if ntpath.basename(program.decode("utf-8", "replace").strip()).lower() != exe_name:
            continue
        try:
            if wine_prefix(read_environ(int(entry), proc_root)) != prefix:
                continue
        except HelperError:
            continue
        started = start_time_ms(int(entry), proc_root)
        if started is not None:
            times.append(started)
    return times


def match_process(processes, exe_name, started_ms, bits=None, siblings_ms=()):
    """Pick the Windows process that is the game.

    Matches on the executable name and, if given, the architecture (a 32- and a
    64-bit program may share a name). Among several left, the order of their
    creation decides when ``siblings_ms``, the starts of the Unix processes
    of that name, has as many entries: Wine registers a process some way off
    its Unix start (Gothic 1 Remake: 0.8 s later), more than lies between an
    Unreal bootstrap and the game it starts, but in the same order. Otherwise
    the one whose creation time is closest to the Unix process's start wins.
    """
    exe_name = exe_name.lower()
    candidates = [proc for proc in processes if proc.name.lower() == exe_name
                  and (bits is None or proc.bits in (0, bits))]
    if not candidates:
        raise HelperError("error.helper.no_windows_process", name=exe_name)
    if len(candidates) == 1 or started_ms is None:
        return candidates[0]
    if len(siblings_ms) == len(candidates) and started_ms in siblings_ms:
        rank = sorted(siblings_ms).index(started_ms)
        return sorted(candidates, key=lambda proc: proc.created_ms)[rank]
    return min(candidates, key=lambda proc: abs(proc.created_ms - started_ms))


class Helper:
    """Run ``grembyte-inject.exe`` inside the wineserver of game ``pid``."""

    def __init__(self, pid, bits, proc_root="/proc"):
        environ = read_environ(pid, proc_root)
        self.pid = pid
        self.bits = bits
        self.exe = helper_path(bits)
        self.wine = wine_loader(pid, environ, proc_root)
        self.env = helper_environment(environ)
        prefix = Path(self.env["WINEPREFIX"])
        try:
            socket = server_socket(prefix)
        except OSError as error:
            raise HelperError("error.helper.prefix", prefix=prefix, error=error) from error
        if not socket.exists():
            raise HelperError("error.helper.no_server", prefix=prefix, socket=socket)

    def run(self, *args):
        """Run the helper with ``args``; return ``(exit code, stdout)``."""
        try:
            done = subprocess.run(
                [str(self.wine), str(self.exe), *map(str, args)],
                env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=HELPER_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HelperError("error.helper.run", error=error) from error
        return done.returncode, done.stdout.decode("utf-8", "replace")

    def windows_pid(self, exe_name):
        """Return the game's Windows process ID."""
        code, output = self.run("list")
        if code != 0:
            raise HelperError("error.helper.list", output=output.strip())
        return match_process(parse_list(output), exe_name, start_time_ms(self.pid),
                             self.bits, sibling_start_times(self.pid, exe_name)).pid

    def inject(self, windows_pid, dll_path, load_library=None):
        """Load ``dll_path`` (a Windows path) into ``windows_pid``."""
        args = ["inject", windows_pid, dll_path]
        if load_library:
            args.append(f"{load_library:x}")
        code, output = self.run(*args)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        result = lines[-1] if lines else ""
        if code != 0 or not result.startswith("ok"):
            detail = result.removeprefix("error ")
            if not detail:
                raise HelperError("error.helper.failed_silent", code=code)
            raise HelperError("error.helper.failed", code=code, detail=detail)
