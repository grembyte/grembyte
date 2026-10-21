"""Frida-backed hardware watchpoints for writes (and reads) of process memory.

x86 watchpoints trap on writes or on any access; there is no read-only
mode. So the agent decodes the instruction before each new trap PC to tell
whether it reads and/or writes the target.

In Mono games (Unity) a hit in JIT code names its C# method (``method``).
"""

import threading

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.core import sessions
from hermes.engines.registry import ENGINE_AGENT_SOURCE
from hermes.core.frida_attach import attach_session
from hermes.core.value_types import VALUE_TYPES
from hermes.core.agent_source import load_agent_source


FRIDA_ERRORS = (
  frida.AddressInUseError,
  frida.ExecutableNotFoundError,
  frida.ExecutableNotSupportedError,
  frida.InvalidArgumentError,
  frida.InvalidOperationError,
  frida.NotSupportedError,
  frida.OperationCancelledError,
  frida.PermissionDeniedError,
  frida.ProcessNotFoundError,
  frida.ProcessNotRespondingError,
  frida.ProtocolError,
  frida.ServerNotRunningError,
  frida.TimedOutError,
  frida.TransportError,
)

# Raised by exports_sync calls when the agent-side RPC handler throws.
CLEANUP_ERRORS = FRIDA_ERRORS + (frida.RPCException, RuntimeError)

_AGENT_SOURCE = load_agent_source(__file__)


def watch_size_for_type(value_type):
    """Return the byte width associated with a supported scan data type."""
    try:
        return VALUE_TYPES[value_type].size
    except KeyError as error:
        raise ValueError(f"Unsupported watch data type: {value_type}") from error


def _build_agent_source(address, size, reads=False):
    return ENGINE_AGENT_SOURCE + (
        _AGENT_SOURCE
        .replace("__ADDRESS__", hex(address))
        .replace("__SIZE__", str(size))
        .replace("__CONDITIONS__", "rw" if reads else "w")
    )


class WriteWatchWorker(QThread):
    """Attach to a process and report writes observed by hardware watchpoints.

    With ``reads`` reads are reported as well. Each hit tells whether its
    instruction ``read`` and/or ``write``-s the target.
    """

    monitor_ready = pyqtSignal(int, str)
    hits_ready = pyqtSignal(list)
    monitor_warning = pyqtSignal(str)
    monitor_failed = pyqtSignal(str)
    monitor_stopped = pyqtSignal()

    def __init__(self, pid, address, size, parent=None, reads=False):
        # pylint: disable=too-many-arguments
        super().__init__(parent)
        self.pid = pid
        self.address = address
        self.size = size
        self.reads = reads
        self._ready_event = threading.Event()
        self._ready_payload = None
        self._agent_error = None
        self._detach_reason = None

    @sessions.tracked(sessions.WRITE_WATCH)
    def run(self):
        session = None
        script = None
        try:
            session = attach_session(self.pid)
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", self._on_session_detached)
            script = session.create_script(
                _build_agent_source(self.address, self.size, self.reads)
            )
            script.on("message", self._on_message)
            script.load()

            if not self._ready_event.wait(5):
                raise RuntimeError("Timed out while installing hardware watchpoints")
            if self._agent_error:
                raise RuntimeError(self._agent_error)
            ready = self._ready_payload or {}
            thread_count = ready.get("threadCount", 0)
            if not thread_count:
                raise RuntimeError(
                    "No thread accepted the hardware watchpoint. "
                    "Check address alignment, watchpoint size, and process permissions."
                )

            self.monitor_ready.emit(thread_count, "")
            for warning in ready.get("warnings", []):
                self.monitor_warning.emit(
                    f"Thread {warning.get('threadId')}: "
                    f"{warning.get('error', '')}"
                )
            while (not self.isInterruptionRequested()
                   and self._detach_reason is None
                   and self._agent_error is None):
                self.msleep(50)

            if self._agent_error and not self.isInterruptionRequested():
                raise RuntimeError(self._agent_error)
            if self._detach_reason and not self.isInterruptionRequested():
                raise RuntimeError(
                    f"Target process detached: {self._detach_reason}"
                )
        except FRIDA_ERRORS + (ImportError, OSError, RuntimeError, ValueError) as error:
            if not self.isInterruptionRequested():
                self.monitor_failed.emit(str(error))
        finally:
            if script is not None:
                try:
                    script.exports_sync.stop()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)
                try:
                    script.unload()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)
            if session is not None:
                try:
                    session.detach()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)
            self.monitor_stopped.emit()

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = message.get("stack", "Frida agent failed")
            self._ready_event.set()
            return

        if message.get("type") != "send":
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready_payload = payload
            self._ready_event.set()
        elif kind == "hits":
            self.hits_ready.emit(payload.get("hits", []))
        elif kind == "thread-error":
            self.monitor_warning.emit(
                f"Thread {payload.get('threadId')}: {payload.get('error', '')}"
            )

    def _on_session_detached(self, reason, _crash):
        self._detach_reason = reason


# Watch backends the UI can switch between.
BACKEND_FRIDA = "frida"
BACKEND_PTRACE = "ptrace"
WATCH_BACKENDS = (BACKEND_FRIDA, BACKEND_PTRACE)


def create_watch_worker(backend, pid, address, size, parent=None, reads=False):
    """Build the watch worker for ``backend`` (``frida`` or ``ptrace``).

    Both workers share the same signals and attributes, so callers treat them
    alike. The ptrace backend is imported lazily so a Frida-only run never
    needs ctypes/capstone on the watch path.
    """
    # pylint: disable=too-many-arguments
    if backend == BACKEND_PTRACE:
        from hermes.core.ptrace_watch import (  # pylint: disable=import-outside-toplevel
            PtraceWriteWatchWorker,
        )
        return PtraceWriteWatchWorker(pid, address, size, parent, reads=reads)
    return WriteWatchWorker(pid, address, size, parent, reads=reads)
