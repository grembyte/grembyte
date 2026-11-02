"""Garbage is collected in the GUI thread only (see hermes.ui.garbage)."""

import gc
import threading
import weakref

from hermes.ui.garbage import GarbageCollector
from tests.test_hooks import fixture_qt_app  # pylint: disable=unused-import


class _Cycle:  # pylint: disable=too-few-public-methods
    """Refers to itself; tells in which thread it was freed."""

    freed_in = []

    def __init__(self):
        self.me = self

    def __del__(self):
        self.freed_in.append(threading.current_thread())


def test_cycles_are_freed_in_the_gui_thread(qt_app):
    was_enabled = gc.isenabled()
    collector = GarbageCollector(qt_app)
    collector.start()
    try:
        assert not gc.isenabled()

        def leave_garbage():
            alive.append(weakref.ref(_Cycle()))
            # Far over the threshold: Python itself would collect in this thread
            kept.extend([] for _ in range(3 * gc.get_threshold()[0]))

        alive, kept = [], []
        worker = threading.Thread(target=leave_garbage)
        worker.start()
        worker.join()
        assert alive[0]() is not None

        filler = [[] for _ in range(gc.get_threshold()[0] + 1)]
        collector.check()
        assert alive[0]() is None
        assert _Cycle.freed_in == [threading.main_thread()]
        del filler
    finally:
        collector.stop()
    assert gc.isenabled() == was_enabled
