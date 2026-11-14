"""Workers kept until their thread has ended."""

import time

import shiboken6  # pylint: disable=import-error,no-name-in-module
from PySide6.QtCore import QObject, QThread, Signal  # pylint: disable=import-error,no-name-in-module

from grembyte.ui import workers

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
    shiboken6.delete(worker)
    assert workers.stop_workers(0)
    assert not workers.running_workers()


class _Emitter(QThread):
    """Emits ``answer`` from its own thread, as a worker does."""

    answer = Signal(int, str)

    def run(self):
        self.answer.emit(7, "seven")


def test_connections_end_with_their_receiver(qt_app):
    receiver = QObject()
    emitter = _Emitter()
    got = []
    relay = workers.connect_while_alive(emitter.answer, receiver,
                                        lambda number, text: got.append((number, text)))
    try:
        emitter.start()
        emitter.wait(5000)
        _wait_for(qt_app, lambda: got)
        assert got == [(7, "seven")]

        # The receiver deleted (a window closed): its relay goes with it
        shiboken6.delete(receiver)
        assert not shiboken6.isValid(relay)
        got.clear()
        emitter.start()
        emitter.wait(5000)
        qt_app.processEvents()
        emitter.answer.emit(8, "eight")
        assert not got
    finally:
        emitter.wait(5000)
