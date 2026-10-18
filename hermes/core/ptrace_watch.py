"""A ptrace-backed hardware watchpoint monitor for process memory.

This is an alternative to the Frida watch backend (see write_watch.py). Instead
of injecting an agent it drives the Linux ``ptrace`` API directly, so it works
on targets Frida cannot instrument, most notably Wine/Proton processes. The
low-level ptrace machinery lives in :mod:`hermes.core.ptrace_engine`; this
module turns watchpoint traps into the hit records the UI shows.
"""

import time

from PyQt6.QtCore import pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import sessions
from hermes.core.memory import read_memory
from hermes.core.ptrace_engine import (
    MAX_INSTRUCTION_SIZE,
    SYNC_WINDOW,
    PtraceWatchpointEngine,
    build_dr7,
    decode_access,
)

__all__ = ["PtraceWriteWatchWorker", "build_dr7", "decode_access"]


class PtraceWriteWatchWorker(PtraceWatchpointEngine):
    """Report writes (and, with ``reads``, reads) via ptrace watchpoints.

    Matches :class:`hermes.core.write_watch.WriteWatchWorker`'s interface so
    the two backends are interchangeable. Each hit tells whether its
    instruction ``read`` and/or ``write``-s the target.
    """

    monitor_ready = pyqtSignal(int, str)
    hits_ready = pyqtSignal(list)
    monitor_warning = pyqtSignal(str)
    monitor_failed = pyqtSignal(str)
    monitor_stopped = pyqtSignal()

    FLUSH_INTERVAL = 0.1

    def __init__(self, pid, address, size, parent=None, reads=False):
        # pylint: disable=too-many-arguments
        super().__init__(pid, parent)
        self.address = address
        self.size = size
        self.reads = reads
        self._slots = {0: (address, size, reads)}
        self._pending = {}
        self._access_cache = {}

    @sessions.tracked(sessions.PTRACE_WATCH)
    def run(self):
        """Attach, report hits until stopped, then detach cleanly."""
        try:
            self._monitor()
        except OSError as error:
            if not self.isInterruptionRequested():
                self.monitor_failed.emit(self._explain(error))
        finally:
            self._detach_all()
            self.monitor_stopped.emit()

    def _monitor(self):
        self._load_mappings()
        warnings = self._attach_all()
        if not self._armed:
            raise OSError(
                1,
                "No thread accepted the hardware watchpoint. Check address "
                "alignment, watchpoint size, and ptrace permissions.",
            )
        self.monitor_ready.emit(len(self._armed), "")
        for message in warnings:
            self.monitor_warning.emit(message)

        last_flush = time.monotonic()
        while not self.isInterruptionRequested():
            if not self._pump():
                if time.monotonic() - last_flush >= self.FLUSH_INTERVAL:
                    self._flush()
                    last_flush = time.monotonic()
                time.sleep(0.002)
        self._flush()
        if self._target_gone:
            self.monitor_failed.emit("The target process exited.")

    def _handle_trap(self, tid, slot, pc, bits):
        read, write = self._access_of(pc, bits)
        key = hex(pc)
        hit = self._pending.get(key)
        now = int(time.time() * 1000)
        if hit is not None:
            hit["count"] += 1
            hit["read"] = hit["read"] or read
            hit["write"] = hit["write"] or write
            hit["threadId"] = tid
            hit["time"] = now
            return
        self._pending[key] = {
            "pc": key,
            "site": self._site(pc),
            "value": None,
            "read": read,
            "write": write,
            "threadId": tid,
            "count": 1,
            "time": now,
        }

    def _access_of(self, pc, bits):
        cached = self._access_cache.get(pc)
        if cached is not None:
            return cached
        start = max(0, pc - (SYNC_WINDOW + MAX_INSTRUCTION_SIZE))
        data, readable = read_memory(self.pid, start, pc - start)
        if not all(readable):
            access = (self.reads, True)
        else:
            access = decode_access(bytes(data), len(data), bits, self.reads)
        self._access_cache[pc] = access
        return access

    def _read_value(self):
        data, readable = read_memory(self.pid, self.address, self.size)
        if not all(readable):
            return "unreadable"
        return " ".join(f"{byte:02x}" for byte in data)

    def _flush(self):
        if not self._pending:
            return
        hits = list(self._pending.values())
        self._pending.clear()
        value = self._read_value()
        for hit in hits:
            hit["value"] = value
        self.hits_ready.emit(hits)
