"""The activity log: what happened in Hermes, in order, one JSON object a line.

Beside ``hermes.log`` (logs.py), ``activity.jsonl`` records the course of a
run rather than its problems, to tell afterwards what led up to a game
crashing: the program attached with its details (32/64 bit, native or
Wine/Proton, engine, how the gadget came in), the workers and agents in the
order they started and ended, every RPC to an agent with its duration and
outcome, the clicks in the window, and what Frida said when a session
ended (with the crash report Frida has for a target that crashed).

Each entry holds ``t`` (local time), ``s`` (seconds since the start),
``run`` (one id per start of Hermes) and ``event``; the rest depends on the
event. Values of the game's memory are not recorded, only the course.

Until ``setup`` nothing is written (tests, tools importing hermes). Once
``REPEAT_WRITTEN`` calls of the same RPC were written within
``REPEAT_WINDOW_S``, the next ones are summed up into one entry with
``repeated``, so scans and live views do not flood the
file; failed calls always get an entry of their own. Such a sum is written
once it is due, when the method fails, or when the agent is unloaded, the
session ends or detaches: other entries may come before it, ``within_s``
says how long it covers.
"""

import itertools
import json
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path


ACTIVITY_NAME = "activity.jsonl"
MAX_BYTES = 2_000_000
BACKUPS = 4
REPEAT_WINDOW_S = 2.0
# Calls of a method written one by one within REPEAT_WINDOW_S before they are summed up
REPEAT_WRITTEN = 3
# Environment variables of a Wine game worth knowing (no paths, no user data).
WINE_VARIABLES = ("SteamAppId", "SteamGameId", "WINEDLLOVERRIDES", "WINEESYNC", "WINEFSYNC",
                  "WINE_NTSYNC", "PROTON_USE_WINED3D", "PROTON_NO_ESYNC", "PROTON_NO_FSYNC")

_logger = logging.getLogger("hermes.activity")
_logger.propagate = False
_lock = threading.Lock()
_handler = None
_run = None
_started = time.monotonic()
_local = threading.local()
_sessions = itertools.count(1)
_agents = itertools.count(1)
# (agent, method) -> {"count", "ms", "max", "since"}: RPCs not yet written
_pending = {}
# (agent, method) -> time.monotonic() of the entries last written for it
_last_written = {}
# id -> (agent, method, time.monotonic()): RPCs that have not returned yet
_in_flight = {}
_calls = itertools.count(1)


def enabled():
    return _handler is not None


def activity_path(directory=None):
    # pylint: disable=import-outside-toplevel
    from hermes.core import logs

    return (Path(directory) if directory is not None else logs.log_dir()) / ACTIVITY_NAME


def setup(directory=None):
    """Start writing ``activity.jsonl`` into ``directory`` (default: the log's)."""
    # pylint: disable=import-outside-toplevel
    from hermes.core import logs

    global _handler, _run  # pylint: disable=global-statement
    teardown()
    directory = Path(directory) if directory is not None else logs.log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        directory / ACTIVITY_NAME, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    with _lock:
        _handler = handler
        _run = uuid.uuid4().hex[:8]
    record("app_started", **environment())
    return directory / ACTIVITY_NAME


def teardown():
    """Stop writing (for tests and the end of the run)."""
    global _handler  # pylint: disable=global-statement
    flush()
    with _lock:
        handler, _handler = _handler, None
        _pending.clear()
        _last_written.clear()
        _in_flight.clear()
    if handler is not None:
        _logger.removeHandler(handler)
        handler.close()


def run_id():
    return _run


def record(event, **fields):
    """Write an entry; does nothing before ``setup``."""
    if _handler is None:
        return
    _flush_due(time.monotonic())
    _write(event, fields)


def _write(event, fields):
    now = time.time()
    entry = {"t": datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
             "s": round(time.monotonic() - _started, 3), "run": _run, "event": event}
    entry.update(fields)
    _logger.info(json.dumps(entry, ensure_ascii=False, default=str))


# --- Workers and agents ---------------------------------------------------------

def session_started(kind, worker, pid):
    """Note the worker of this thread; returns its session number."""
    number = next(_sessions)
    _local.session = (number, kind)
    record("session_started", session=number, kind=kind, worker=type(worker).__name__, pid=pid)
    return number


def session_ended(number, kind, pid, seconds):
    _local.session = None
    flush()
    record("session_ended", session=number, kind=kind, pid=pid, seconds=round(seconds, 3))


def current_session():
    """(session number, kind) of the worker running in this thread, or (None, None)."""
    return getattr(_local, "session", None) or (None, None)


def new_agent():
    """A number for the next agent loaded."""
    return next(_agents)


def rpc_started(agent, method):
    """Note an RPC in flight; returns the handle for ``rpc_ended``."""
    handle = next(_calls)
    with _lock:
        _in_flight[handle] = (agent, method, time.monotonic())
    return handle


def rpc_ended(handle, error=None):
    """Write (or sum up) the RPC ``handle``; ``error``: its message if it failed."""
    now = time.monotonic()
    with _lock:
        call = _in_flight.pop(handle, None)
    if _handler is None or call is None:
        return
    agent, method, began = call
    ms = (now - began) * 1000
    key = (agent, method)
    _flush_due(now)
    if error is not None:
        _flush_keys([key])
        _write("rpc", {"agent": agent, "method": method, "ms": round(ms, 1),
                       "error": error})
        return
    with _lock:
        pending = _pending.get(key)
        if pending is not None:
            pending["count"] += 1
            pending["ms"] += ms
            pending["max"] = max(pending["max"], ms)
            return
        recent = [written for written in _last_written.get(key, ())
                  if now - written < REPEAT_WINDOW_S]
        if len(recent) >= REPEAT_WRITTEN:
            _pending[key] = {"count": 1, "ms": ms, "max": ms, "since": now}
            return
        _last_written[key] = recent + [now]
    _write("rpc", {"agent": agent, "method": method, "ms": round(ms, 1)})


def in_flight():
    """``agent.method`` and seconds of the RPCs that have not returned, oldest first."""
    now = time.monotonic()
    with _lock:
        calls = sorted(_in_flight.values(), key=lambda call: call[2])
    return [{"agent": agent, "method": method, "seconds": round(now - began, 3)}
            for agent, method, began in calls]


def flush(agent=None):
    """Write the summed-up RPCs (of ``agent``, or all)."""
    with _lock:
        keys = [key for key in _pending if agent is None or key[0] == agent]
    _flush_keys(keys)


def _flush_due(now):
    with _lock:
        keys = [key for key, pending in _pending.items()
                if now - pending["since"] >= REPEAT_WINDOW_S]
    _flush_keys(keys)


def _flush_keys(keys):
    written = []
    now = time.monotonic()
    with _lock:
        for key in keys:
            pending = _pending.pop(key, None)
            if pending is not None:
                # still frequent: what follows within the window is summed up again
                _last_written[key] = [now] * REPEAT_WRITTEN
                written.append((key, pending))
    if _handler is None:
        return
    for (agent, method), pending in written:
        _write("rpc", {"agent": agent, "method": method, "repeated": pending["count"],
                       "ms": round(pending["ms"], 1), "max_ms": round(pending["max"], 1),
                       "within_s": round(now - pending["since"], 3)})


# --- What Hermes runs on and attaches to ----------------------------------------------

def _version(package):
    # pylint: disable=import-outside-toplevel
    from importlib import metadata

    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _os_release(path="/etc/os-release"):
    try:
        with open(path, encoding="utf-8") as file:
            for line in file:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def environment():
    """What Hermes runs on: versions, system, session."""
    # pylint: disable=import-outside-toplevel
    import frida
    from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR  # pylint: disable=import-error,no-name-in-module

    return {
        "pid": os.getpid(),
        "hermes": _version("hermes-hook"),
        "python": platform.python_version(),
        "frida": frida.__version__,
        "qt": QT_VERSION_STR,
        "pyqt": PYQT_VERSION_STR,
        "system": _os_release() or platform.platform(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "session": os.environ.get("XDG_SESSION_TYPE"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "argv": sys.argv[1:],
    }


def _environ(pid, names, proc_root):
    try:
        with open(f"{proc_root}/{pid}/environ", "rb") as file:
            pairs = file.read().split(b"\0")
    except OSError:
        return {}
    wanted = set(names)
    found = {}
    for pair in pairs:
        name, _, value = pair.decode("utf-8", "replace").partition("=")
        if name in wanted:
            found[name] = value
    return found


def proton_version(loader_path):
    """The ``version`` file of the Proton build that ``loader_path`` belongs to, or None.

    Proton keeps Wine in ``<Proton>/files/bin`` (``dist`` in older builds)
    and its name and build in ``<Proton>/version``.
    """
    path = Path(loader_path)
    for parent in path.parents:
        if parent.name in ("files", "dist"):
            try:
                return (parent.parent / "version").read_text(encoding="utf-8").strip()
            except OSError:
                return None
    return None


def describe_process(pid, proc_root="/proc"):
    """What is known of ``pid`` without attaching: program, bits, Wine, Frida in it."""
    # pylint: disable=import-outside-toplevel
    from hermes.core import sessions, wine_gadget
    from hermes.platform import is_wine_process, program_bits

    details = {"pid": pid}
    try:
        details["exe"] = os.readlink(f"{proc_root}/{pid}/exe")
    except OSError:
        details["exe"] = None
    wine = is_wine_process(pid, proc_root)
    details["wine"] = wine
    details["loader_bits"] = program_bits(pid, proc_root)
    if wine:
        program, bits = wine_gadget.wine_program(pid)
        details.update(program=program, bits=bits,
                       proton=proton_version(details["exe"]) if details["exe"] else None,
                       variables=_environ(pid, WINE_VARIABLES, proc_root),
                       gadget_port=wine_gadget.gadget_port(pid, proc_root),
                       gadget_loader=wine_gadget.gadget_loader(pid))
    else:
        details["bits"] = details["loader_bits"]
    details["frida_agents"] = sessions.frida_agents(pid, proc_root)
    return details


def _task_sample(task):
    """(name, state, CPU ticks, syscall text, wchan) of a /proc task directory, or None."""
    try:
        stat = (task / "stat").read_text()
        name = stat[stat.index("(") + 1:stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2:].split()
    except (OSError, ValueError):
        return None
    texts = []
    for entry in ("syscall", "wchan"):
        try:
            texts.append((task / entry).read_text().strip())
        except OSError:
            texts.append(None)  # gone, or not ours to read
    # fields[0] is the state (field 3), utime and stime fields 14 and 15
    return name, fields[0], int(fields[11]) + int(fields[12]), texts[0], texts[1]


def _syscall(text):
    """{"syscall": number or "running", "pc": hex} of /proc/…/syscall."""
    if not text:
        return {}
    parts = text.split()
    if parts[0] == "running":
        return {"syscall": "running"}
    found = {"syscall": int(parts[0])}
    if len(parts) >= 3:
        found["pc"] = parts[-1]
    return found


def thread_states(pid, seconds=0.2, proc_root="/proc", sleep=None):
    """What each Linux thread of ``pid`` does: state, CPU share over ``seconds``, where it waits.

    Under Wine every Windows thread is one of them. A thread that froze in
    a loop runs (``R``, a CPU share near 1); one in a deadlock sleeps
    (``S``) in a system call (``futex`` is 202 on x86-64) with ``pc`` the
    address it called from.
    """
    sleep = sleep or time.sleep
    root = Path(proc_root) / str(pid) / "task"
    try:
        tids = sorted(int(name) for name in os.listdir(root))
    except OSError:
        return []
    before = {tid: _task_sample(root / str(tid)) for tid in tids}
    sleep(seconds)
    ticks = os.sysconf("SC_CLK_TCK") * seconds
    states = []
    for tid in tids:
        start, end = before[tid], _task_sample(root / str(tid))
        if start is None or end is None:
            continue
        name, state, cpu, syscall, wchan = end
        entry = {"tid": tid, "name": name, "state": state,
                 "cpu": round((cpu - start[2]) / ticks, 2), **_syscall(syscall)}
        if wchan and wchan != "0":
            entry["wchan"] = wchan
        states.append(entry)
    return states


def record_threads(pid, why, **fields):
    """Write what the threads of ``pid`` do (see ``thread_states``)."""
    if _handler is None:
        return
    states = thread_states(pid)
    record("threads", pid=pid, why=why, running=sum(entry["state"] == "R" for entry in states),
           threads=states, **fields)


# --- Bug report ---------------------------------------------------------------

def export_report(target, directory=None):
    """Zip the logs (activity, hermes.log, crash.log, with their backups) into ``target``."""
    # pylint: disable=import-outside-toplevel
    from hermes.core import logs

    directory = Path(directory) if directory is not None else logs.log_dir()
    flush()
    for handler in list(logs.log.handlers) + list(_logger.handlers):
        handler.flush()
    names = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for base in (ACTIVITY_NAME, logs.LOG_NAME, logs.CRASH_NAME):
            for suffix in [""] + [f".{number}" for number in range(1, BACKUPS + 1)]:
                path = directory / f"{base}{suffix}"
                if path.is_file():
                    archive.write(path, path.name)
                    names.append(path.name)
    record("report_exported", files=names)
    return names


# --- Reading it back ------------------------------------------------------------------

def read_entries(path=None, max_entries=None):
    """The entries of ``activity.jsonl`` (oldest first), or None without the file."""
    path = activity_path() if path is None else Path(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            lines = file.read().splitlines()
    except OSError:
        return None
    entries = []
    for line in lines[-max_entries:] if max_entries else lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def describe_entry(entry):
    """A line for people: ``12:01:02.345  event  key=value …``."""
    time_text = entry.get("t", "")[11:]
    rest = {key: value for key, value in entry.items() if key not in ("t", "s", "run", "event")}
    fields = "  ".join(f"{key}={json.dumps(value, ensure_ascii=False, default=str)}"
                       if not isinstance(value, str) else f"{key}={value}"
                       for key, value in rest.items())
    return f"{time_text}  {entry.get('event', '?'):<18} {fields}".rstrip()
