"""Keep QThreads referenced until their thread has really ended.

A QThread destroyed while its thread still runs aborts the application. Its
``finished`` signal is emitted from the thread just before the thread ends, so
a slot of ``finished`` that drops the last reference may destroy it too early.
Workers of windows that close are kept here until then.
"""

from PyQt6 import sip  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtCore import QThread  # pylint: disable=import-error,no-name-in-module

_workers = set()


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
    # One deleted by Qt (e.g. never started, with its parent gone) runs no more.
    _workers.difference_update([worker for worker in _workers if sip.isdeleted(worker)])
    return set(_workers)


def stop_workers(timeout_ms):
    """Ask every kept worker to stop and wait for each; True if all have ended."""
    workers = list(running_workers())
    for worker in workers:
        worker.requestInterruption()
    # A list, not a generator: wait for each, also after one timed out.
    return all([worker.wait(timeout_ms) for worker in workers])  # pylint: disable=use-a-generator
