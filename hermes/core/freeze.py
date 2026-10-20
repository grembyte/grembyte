"""Keep frozen address-list values in place by writing them back periodically."""

import threading
import time
from dataclasses import dataclass

from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.core.pointer import PointerChain, module_bases, resolve_chain
from hermes.platform import ProcessMemory, read_mappings


@dataclass(frozen=True)
class FrozenValue:
    """Bytes to keep at ``address``, or wherever ``pointer`` leads right now."""

    data: bytes
    address: int | None = None
    pointer: PointerChain | None = None


class FreezeWorker(QThread):
    """Write frozen values to a process every ``INTERVAL_MS``.

    Unlike a hook with a fixed value this needs no write instruction: the game
    may change the value between two rounds, so it can flicker briefly.
    Pointer chains are resolved before every write; module bases are re-read
    at most every ``MAPPINGS_INTERVAL`` seconds. A failing entry is reported
    once via ``write_failed`` and retried silently.
    """

    write_failed = pyqtSignal(str)

    INTERVAL_MS = 100
    MAPPINGS_INTERVAL = 1.0

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        logs.log_failures(self)
        self.pid = pid
        self._lock = threading.Lock()
        self._entries = {}
        self._reported = set()
        self._bases = None
        self._bases_time = 0.0

    def set_entry(self, key, entry):
        """Freeze ``entry`` (a FrozenValue) under ``key``, replacing an older one."""
        with self._lock:
            self._entries[key] = entry
            self._reported.discard(key)

    def remove_entry(self, key):
        with self._lock:
            self._entries.pop(key, None)
            self._reported.discard(key)

    def write_once(self, memory):
        """Write all frozen values once to ``memory``, a writable ProcessMemory."""
        with self._lock:
            entries = list(self._entries.items())
        if not entries:
            return
        if any(entry.pointer is not None for _key, entry in entries):
            now = time.monotonic()
            if self._bases is None or now - self._bases_time >= self.MAPPINGS_INTERVAL:
                self._bases = module_bases(read_mappings(self.pid))
                self._bases_time = now
        for key, entry in entries:
            address = entry.address
            if entry.pointer is not None:
                address = resolve_chain(memory, self._bases, entry.pointer)
            if address is None:
                continue
            try:
                memory.write(address, entry.data)
            except (OSError, OverflowError, ValueError) as error:
                self._report(key, f"0x{address:X}: {error}")

    def _report(self, key, message):
        with self._lock:
            if key in self._reported or key not in self._entries:
                return
            self._reported.add(key)
        self.write_failed.emit(message)

    def run(self):
        try:
            with ProcessMemory(self.pid, writable=True) as memory:
                while not self.isInterruptionRequested():
                    self.write_once(memory)
                    self.msleep(self.INTERVAL_MS)
        except OSError as error:
            if not self.isInterruptionRequested():
                self.write_failed.emit(str(error))
