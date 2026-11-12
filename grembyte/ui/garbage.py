"""Python's garbage collector, run in the GUI thread only.

Left automatic, the collector runs in whatever thread allocates when a
threshold is crossed, often a hook or Frida thread. Qt objects in the cycles
it frees are then deleted by PySide6 later in the GUI thread, and with
widgets that can crash the interpreter (a widget deleted twice). Here the
automatic collector is off and a timer in the GUI thread collects instead,
by the same thresholds.
"""

import gc

from PySide6.QtCore import QObject, QTimer  # pylint: disable=import-error,no-name-in-module

INTERVAL_MS = 500


class GarbageCollector(QObject):
    """Collects garbage in the thread it lives in; see the module docstring."""

    def __init__(self, parent=None, interval_ms=INTERVAL_MS):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.check)
        self._was_enabled = None

    def start(self):
        self._was_enabled = gc.isenabled()
        gc.disable()
        self._timer.start()

    def stop(self):
        """Hand collecting back to Python, if it collected before ``start``."""
        self._timer.stop()
        if self._was_enabled:
            gc.enable()

    @staticmethod
    def check():
        """Collect the oldest generation whose count is over its threshold."""
        counts = gc.get_count()
        thresholds = gc.get_threshold()
        if counts[0] <= thresholds[0]:
            return
        generation = 0
        for older in (1, 2):
            if thresholds[older] and counts[older] > thresholds[older]:
                generation = older
        gc.collect(generation)
