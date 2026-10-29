"""Workers kept until their thread has ended."""

import time

from PyQt6 import sip  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtCore import QThread  # pylint: disable=import-error,no-name-in-module

from hermes.ui import workers

from tests.test_hooks import _wait_for, fixture_qt_app  # pylint: disable=unused-import


class _Worker(QThread):
    """Runs until asked to stop."""

    def run(self):
        while not self.isInterruptionRequested():
            time.sleep(0.01)


def test_worker_is_released_only_after_its_thread_ended(qt_app):
    worker = _Worker()
    workers.keep_until_finished(worker)
    worker.start()
    assert worker in workers.running_workers()

    assert workers.stop_workers(5000)
    _wait_for(qt_app, lambda: worker not in workers.running_workers())
    assert worker.isFinished() and not worker.isRunning()


def test_stop_workers_without_workers_is_true():
    assert workers.stop_workers(0)


def test_a_worker_deleted_before_it_started_is_dropped():
    worker = _Worker()
    workers.keep_until_finished(worker)
    sip.delete(worker)
    assert workers.stop_workers(0)
    assert not workers.running_workers()
