"""Frida-backed hardware watchpoints for writes (and reads) of process memory.

x86 watchpoints trap on writes or on any access; there is no read-only
mode. So the agent decodes the instruction before each new trap PC to tell
whether it reads and/or writes the target.

In Mono games (Unity) a hit in JIT code names its C# method (``method``).
"""

import threading

from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.attach.backends import BACKEND_PTRACE
from hermes.core import logs, messages
from hermes.core import sessions
from hermes.engines.registry import ENGINE_AGENT_SOURCE
from hermes.attach.frida_attach import CLEANUP_ERRORS, FRIDA_ERRORS, attach_session
from hermes.agent.source import load_agent_source


_AGENT_SOURCE = load_agent_source(__file__)


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

    monitor_ready = Signal(int, str)
    hits_ready = Signal(object)
    monitor_warning = Signal(str)
    monitor_failed = Signal(str)
    monitor_stopped = Signal()

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
                raise messages.LocalizedRuntimeError("error.write_watch.install_timeout")
            if self._agent_error:
                raise RuntimeError(self._agent_error)
            ready = self._ready_payload or {}
            thread_count = ready.get("threadCount", 0)
            if not thread_count:
                raise messages.LocalizedRuntimeError("error.write_watch.no_thread")

            self.monitor_ready.emit(thread_count, "")
            for warning in ready.get("warnings", []):
                self.monitor_warning.emit(messages.text(
                    "error.write_watch.thread", thread=warning.get("threadId"),
                    error=warning.get("error", "")))
            while (not self.isInterruptionRequested()
                   and self._detach_reason is None
                   and self._agent_error is None):
                self.msleep(50)

            if self._agent_error and not self.isInterruptionRequested():
                raise RuntimeError(self._agent_error)
            if self._detach_reason and not self.isInterruptionRequested():
                raise messages.LocalizedRuntimeError("error.agent.detached",
                                                     reason=self._detach_reason)
        except FRIDA_ERRORS + (ImportError, OSError, RuntimeError, ValueError) as error:
            if not self.isInterruptionRequested():
                self.monitor_failed.emit(messages.describe(error))
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
            self._agent_error = messages.describe(
                message.get("stack") or messages.text("error.write_watch.agent_failed"))
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
            self.monitor_warning.emit(messages.text(
                "error.write_watch.thread", thread=payload.get("threadId"),
                error=payload.get("error", "")))

    def _on_session_detached(self, reason, _crash):
        self._detach_reason = reason


def create_watch_worker(backend, pid, address, size, parent=None, reads=False):
    """Build the watch worker for ``backend`` (``frida`` or ``ptrace``).

    Both workers share the same signals and attributes, so callers treat them
    alike. The ptrace backend is imported lazily so a Frida-only run never
    needs ctypes/capstone on the watch path.
    """
    # pylint: disable=too-many-arguments
    if backend == BACKEND_PTRACE:
        from hermes.features.ptrace_watch import (  # pylint: disable=import-outside-toplevel
            PtraceWriteWatchWorker,
        )
        return PtraceWriteWatchWorker(pid, address, size, parent, reads=reads)
    return WriteWatchWorker(pid, address, size, parent, reads=reads)
