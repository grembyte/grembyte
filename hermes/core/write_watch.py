"""Frida-backed hardware watchpoints for writes to process memory."""

import threading

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module


_VALUE_SIZES = {
    "int32": 4,
    "int64": 8,
    "float": 4,
    "double": 8,
}

_FRIDA_ERRORS = (
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
_CLEANUP_ERRORS = _FRIDA_ERRORS + (frida.RPCException, RuntimeError)

_AGENT_SOURCE = r"""
const targetAddress = ptr('__ADDRESS__');
const targetSize = __SIZE__;
const watchpointId = 0;
const armedThreads = new Map();
const pendingHits = new Map();
const threadErrors = [];
let initializing = true;
let threadObserver = null;

function readTargetBytes() {
  try {
    return Array.from(new Uint8Array(targetAddress.readByteArray(targetSize)))
      .map(value => value.toString(16).padStart(2, '0'))
      .join(' ');
  } catch (_error) {
    return 'unreadable';
  }
}

function recordHit(threadId, context) {
  const pc = context.pc;
  const module = Process.findModuleByAddress(pc);
  const site = module
    ? `${module.name}+${pc.sub(module.base)}`
    : pc.toString();
  const key = pc.toString();
  const hit = pendingHits.get(key);
  if (hit) {
    hit.count += 1;
    hit.value = readTargetBytes();
    hit.threadId = threadId;
    return;
  }
  pendingHits.set(key, {
    pc: key,
    site,
    value: readTargetBytes(),
    threadId,
    count: 1
  });
}

Process.setExceptionHandler(details => {
  // Linux reports hardware data watchpoint traps as 'single-step'.
  if (details.type !== 'breakpoint' && details.type !== 'single-step')
    return false;

  const threadId = Process.getCurrentThreadId();
  if (!armedThreads.has(threadId))
    return false;

  recordHit(threadId, details.context);
  return true;
});

function armThread(thread) {
  if (armedThreads.has(thread.id))
    return;
  try {
    thread.setHardwareWatchpoint(watchpointId, targetAddress, targetSize, 'w');
    armedThreads.set(thread.id, thread);
  } catch (error) {
    const warning = {
      kind: 'thread-error',
      threadId: thread.id,
      error: String(error)
    };
    if (initializing)
      threadErrors.push(warning);
    else
      send(warning);
  }
}

for (const thread of Process.enumerateThreads())
  armThread(thread);

threadObserver = Process.attachThreadObserver({
  onAdded(thread) {
    armThread(thread);
  },
  onRemoved(thread) {
    armedThreads.delete(thread.id);
  }
});
initializing = false;

function flushHits() {
  if (pendingHits.size === 0)
    return;
  const hits = Array.from(pendingHits.values());
  pendingHits.clear();
  send({ kind: 'hits', hits });
}

const flushTimer = setInterval(flushHits, 100);
send({
  kind: 'ready',
  threadCount: armedThreads.size,
  warnings: threadErrors
});

rpc.exports = {
  stop() {
    clearInterval(flushTimer);
    if (threadObserver !== null)
      threadObserver.detach();
    for (const thread of armedThreads.values()) {
      try {
        thread.unsetHardwareWatchpoint(watchpointId);
      } catch (_error) {
      }
    }
    armedThreads.clear();
    Process.setExceptionHandler(null);
    flushHits();
    return true;
  }
};
"""


def watch_size_for_type(value_type):
    """Return the byte width associated with a supported scan data type."""
    try:
        return _VALUE_SIZES[value_type]
    except KeyError as error:
        raise ValueError(f"Unsupported watch data type: {value_type}") from error


def _build_agent_source(address, size):
    return (
        _AGENT_SOURCE
        .replace("__ADDRESS__", hex(address))
        .replace("__SIZE__", str(size))
    )


class WriteWatchWorker(QThread):
    """Attach to a process and report writes observed by hardware watchpoints."""

    monitor_ready = pyqtSignal(int, str)
    hits_ready = pyqtSignal(list)
    monitor_warning = pyqtSignal(str)
    monitor_failed = pyqtSignal(str)
    monitor_stopped = pyqtSignal()

    def __init__(self, pid, address, size, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.address = address
        self.size = size
        self._ready_event = threading.Event()
        self._ready_payload = None
        self._agent_error = None
        self._detach_reason = None

    def run(self):
        session = None
        script = None
        try:
            session = frida.attach(self.pid)
            session.on("detached", self._on_session_detached)
            script = session.create_script(
                _build_agent_source(self.address, self.size)
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
        except _FRIDA_ERRORS + (ImportError, OSError, RuntimeError, ValueError) as error:
            if not self.isInterruptionRequested():
                self.monitor_failed.emit(str(error))
        finally:
            if script is not None:
                try:
                    script.exports_sync.stop()
                except _CLEANUP_ERRORS:
                    pass
                try:
                    script.unload()
                except _CLEANUP_ERRORS:
                    pass
            if session is not None:
                try:
                    session.detach()
                except _CLEANUP_ERRORS:
                    pass
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