"""Registry of the workers attached to a target process.

Frida workers (catalog, method monitor, trace, hooks, write watch) and
ptrace workers register while their ``run`` lasts (``@tracked``). The
window shows them, can ask one to stop, and can cut a Frida session that no
longer answers: detaching the session ends the worker's pending RPC call.

``SESSIONS`` is plain Python and thread-safe: it outlives any QApplication.
Workers report to it from their own threads and it calls the ``subscribe``d
callbacks there; ``SessionSignals`` turns that into a Qt signal for widgets.
"""

import functools
import sys
import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.core import activity, logs
from hermes.platform import read_mappings


# Kinds of workers (translation keys "sessions.kind_<kind>")
CATALOG = "catalog"
MONITOR = "monitor"
TRACE = "trace"
NATIVE_TRACE = "native_trace"
NATIVE_MONITOR = "native_monitor"
METHOD_HOOKS = "method_hooks"
SCRIPTS = "scripts"
HOOKS = "hooks"
WRITE_WATCH = "write_watch"
PTRACE_WATCH = "ptrace_watch"
PTRACE_HOOKS = "ptrace_hooks"

# A worker asked to stop that still runs after this long is shown as stuck.
STUCK_AFTER_S = 5.0


@dataclass(eq=False)
class Session:
    """A running worker."""

    worker: object
    kind: str
    pid: int
    started: float = field(default_factory=time.monotonic)
    # time.monotonic() of the stop request, or None
    stopping: float | None = None
    # the Frida session, or None (ptrace, or not attached yet)
    frida_session: object = None
    detaching: bool = False

    def stuck(self, now=None):
        """Whether it was asked to stop long ago and still runs."""
        now = time.monotonic() if now is None else now
        return self.stopping is not None and now - self.stopping >= STUCK_AFTER_S


class SessionRegistry:
    """The running workers; thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}
        # Workers that have left the registry but whose thread may still be
        # finishing: Python must not destroy a running QThread, which it
        # would if the registry dropped its last reference from that thread.
        self._ending = []
        self._callbacks = []

    def subscribe(self, callback):
        """Call ``callback()`` on every change, from whichever thread made it."""
        with self._lock:
            self._callbacks.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def _changed(self):
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except RuntimeError:  # its Qt object is gone
                self.unsubscribe(callback)

    # --- Called by the workers ---------------------------------------------
    def add(self, worker, kind, pid):
        with self._lock:
            self._prune()
            self._sessions[id(worker)] = Session(worker, kind, pid)
        self._changed()

    def remove(self, worker):
        with self._lock:
            if self._sessions.pop(id(worker), None) is not None:
                self._ending.append(worker)
        self._changed()

    def _prune(self):
        # Under the lock: release the workers whose thread has finished.
        def finished(worker):
            try:
                return worker.isFinished()
            except RuntimeError:  # the Qt object is gone
                return True
        self._ending = [worker for worker in self._ending if not finished(worker)]

    def set_frida_session(self, worker, frida_session):
        """Remember the Frida session of a running worker (to cut it)."""
        with self._lock:
            session = self._sessions.get(id(worker))
            if session is not None:
                session.frida_session = frida_session

    # --- Called by the window ------------------------------------------------
    def sessions(self, pid=None):
        """The running workers (of ``pid``), oldest first."""
        with self._lock:
            self._prune()
            found = list(self._sessions.values())
        return sorted((s for s in found if pid is None or s.pid == pid),
                      key=lambda s: s.started)

    def stop(self, session):
        """Ask a worker to stop; it removes its hooks and session first."""
        if session.stopping is None:
            session.stopping = time.monotonic()
        session.worker.requestInterruption()
        self._changed()

    def stop_all(self):
        for session in self.sessions():
            self.stop(session)

    def disconnect(self, session):
        """Cut a Frida session that does not answer; False for ptrace.

        Detaching runs on a thread of its own: Frida may take a while, and
        the window must not freeze for it. The agent in the target removes
        the session's scripts once it notices.
        """
        frida_session = session.frida_session
        if frida_session is None:
            return False
        logs.log.warning("cutting the Frida session of %s (pid %s)", session.kind, session.pid)
        self.stop(session)
        session.detaching = True
        threading.Thread(target=_detach, args=(frida_session, session.pid), daemon=True,
                         name="hermes-detach").start()
        self._changed()
        return True

    def disconnect_all(self):
        for session in self.sessions():
            if not self.disconnect(session):
                self.stop(session)

    def wait_all(self, timeout_s):
        """Wait up to ``timeout_s`` for all workers to end; True if they have."""
        deadline = time.monotonic() + timeout_s
        for session in self.sessions():
            remaining = max(0.0, deadline - time.monotonic())
            session.worker.wait(int(remaining * 1000))
        return not self.sessions()


class SessionSignals(QObject):
    """``changed`` of ``SESSIONS`` as a Qt signal, delivered in the GUI thread."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        emit = self.changed.emit  # one object, to unsubscribe it again
        SESSIONS.subscribe(emit)
        self.destroyed.connect(lambda _object=None: SESSIONS.unsubscribe(emit))


def _detach(frida_session, pid=None):
    # A session cut by hand: the game may be stuck, note where its threads are.
    if pid is not None:
        activity.record_threads(pid, "session cut")
    try:
        frida_session.detach()
    except Exception as error:  # pylint: disable=broad-exception-caught
        logs.cleanup_failed(error)  # gone already, or the target is: either way it is cut


SESSIONS = SessionRegistry()


def tracked(kind):
    """Decorate a worker's ``run``: registered while it runs.

    The thread is named ``<kind>-<session number>`` meanwhile, so the log
    tells the workers apart: Python calls a QThread "Dummy-<n>", in the order
    it first saw the threads.
    """
    def decorate(run):
        @functools.wraps(run)
        def wrapper(self, *args, **kwargs):
            if not getattr(self, "_failures_logged", False):
                logs.log_failures(self)
                self._failures_logged = True
            began = time.monotonic()
            number = activity.session_started(kind, self, self.pid)
            thread = threading.current_thread()
            name, thread.name = thread.name, f"{kind}-{number}"
            SESSIONS.add(self, kind, self.pid)
            logs.log.info("session %s started (pid %s)", kind, self.pid)
            try:
                return run(self, *args, **kwargs)
            finally:
                SESSIONS.remove(self)
                logs.log.info("session %s ended (pid %s)", kind, self.pid)
                activity.session_ended(number, kind, self.pid, time.monotonic() - began)
                thread.name = name
        return wrapper
    return decorate


def frida_agents(pid, proc_root="/proc"):
    """How many Frida agents the process has loaded (0 if unknown).

    Every Hermes process uses one agent for all its sessions; one left by an
    earlier Hermes that did not end cleanly stays until the target exits.
    Frida's gadget in a Wine game counts as an agent too.
    """
    return _mapped_files(pid, ("frida-agent", "frida-gadget"), proc_root)


def frida_gadgets(pid, proc_root="/proc"):
    """How many Frida gadgets a Wine game has loaded (0 if unknown).

    Unlike an agent, the gadget is never stale: every Hermes run reuses it.
    """
    return _mapped_files(pid, ("frida-gadget",), proc_root)


def _mapped_files(pid, markers, proc_root):
    # Count the files mapped from offset 0 whose path holds one of ``markers``.
    if sys.platform == "win32":
        # The loaded modules (frida-agent.dll), each from a folder of its own.
        try:
            return len({mapping.path for mapping in read_mappings(pid)
                        if mapping.module is not None
                        and any(marker in mapping.path for marker in markers)})
        except OSError:
            return 0
    try:
        with open(f"{proc_root}/{pid}/maps", encoding="utf-8", errors="replace") as maps:
            return sum(1 for line in maps
                       if any(marker in line for marker in markers)
                       and line.split()[2] == "00000000")
    except (OSError, IndexError):
        return 0
