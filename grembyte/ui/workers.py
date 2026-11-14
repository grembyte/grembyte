"""Keep QThreads referenced until their thread has really ended.

A QThread destroyed while its thread still runs aborts the application. Its
``finished`` signal is emitted from the thread just before the thread ends, so
a slot of ``finished`` that drops the last reference may destroy it too early.
Workers of windows that close are kept here until then.

A window that closes while a worker lives on (one of the scripts tab, say)
connects its signals with ``connect_while_alive``: a lambda connected to it
would outlive the window and touch its deleted widgets.
"""

import shiboken6  # pylint: disable=import-error,no-name-in-module
from PySide6.QtCore import QObject, QThread, Signal  # pylint: disable=import-error,no-name-in-module

from grembyte.attach.wine_gadget import ensure_gadget

_workers = set()


class GadgetDownloadWorker(QThread):
    """Download a Wine gadget without blocking the GUI thread."""

    progress = Signal(int, int)
    completed = Signal()

    def __init__(self, bits, parent=None):
        super().__init__(parent)
        self.bits = bits
        self.path = None
        self.error = None

    def run(self):
        try:
            self.path = ensure_gadget(self.bits, progress=self.progress.emit)
        except Exception as error:  # pylint: disable=broad-exception-caught
            self.error = str(error)
        self.completed.emit()


class _Relay(QObject):  # pylint: disable=too-few-public-methods
    """Passes a signal on to ``slot``; a child of the receiver, so Qt drops the
    connection when the receiver is deleted."""

    def __init__(self, slot, parent):
        super().__init__(parent)
        self._slot = slot

    def relay(self, *args):
        """The connected slot."""
        self._slot(*args)


def connect_while_alive(signal, receiver, slot):
    """Connect ``signal`` to ``slot`` (any callable) for as long as QObject
    ``receiver`` lives; returns the relay, whose ``deleteLater`` disconnects
    it earlier."""
    relay = _Relay(slot, receiver)
    signal.connect(relay.relay)
    return relay


def keep_until_finished(worker):
    """Keep ``worker`` referenced until its thread has ended, also after its window closed."""
    _workers.add(worker)
    worker.finished.connect(lambda: _release(worker))


def _release(worker):
    if QThread.currentThread() is not worker:
        worker.wait()  # past run(): only the end of the thread is left
    _workers.discard(worker)


def running_workers():
    """The workers kept right now."""
    # One deleted by Qt (e.g. never started, with its parent gone) runs no
    # more; nor does one never started or whose thread ended before its
    # ``finished`` slot ran (no event loop ran since).
    _workers.difference_update([worker for worker in _workers
                                if not getattr(shiboken6, "isValid")(worker)
                                or not worker.isRunning()])
    return set(_workers)


def stop_workers(timeout_ms):
    """Ask every kept worker to stop and wait for each; True if all have ended."""
    workers = list(running_workers())
    for worker in workers:
        worker.requestInterruption()
    # A list, not a generator: wait for each, also after one timed out.
    return all([worker.wait(timeout_ms) for worker in workers])  # pylint: disable=use-a-generator
