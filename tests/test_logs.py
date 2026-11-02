"""The application log: files, counting, hooks for what nothing caught."""

import subprocess
import sys
import threading

import pytest
from PySide6.QtCore import QObject, QThread, Signal, qWarning  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.ui.widgets.log_view import read_log

from tests.test_hooks import _wait_for, fixture_qt_app, linux_only  # pylint: disable=unused-import


@pytest.fixture(name="log_file")
def fixture_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LOG_DIR", str(tmp_path))
    path = logs.setup()
    hooks = sys.excepthook, threading.excepthook
    yield path
    sys.excepthook, threading.excepthook = hooks
    logs.teardown()


def _text(path):
    for handler in logs.log.handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


def test_setup_writes_to_the_state_directory(log_file, tmp_path):
    assert log_file == tmp_path / "hermes.log" == logs.log_path()
    logs.log.getChild("test").info("hello %s", "log")
    text = _text(log_file)
    assert "Hermes started" in text and "INFO     [MainThread] hermes.test: hello log" in text
    assert "--- Hermes started" in (tmp_path / "crash.log").read_text(encoding="utf-8")


def test_errors_are_counted_and_announced(log_file):
    heard = []
    logs.subscribe(lambda: heard.append(logs.error_count()))
    logs.log.warning("only a warning")
    assert logs.error_count() == 0
    logs.log.error("a real one")
    assert logs.error_count() == 1 and heard == [1]
    assert "ERROR    [MainThread] hermes: a real one" in _text(log_file)


def test_exceptions_nothing_caught_are_logged_not_fatal(log_file, qt_app):
    reported = []
    logs.on_unhandled(lambda summary, details: reported.append((summary, details)))
    logs.install_exception_hooks()

    class Failing(QThread):
        def run(self):
            raise ValueError("broken worker")

    worker = Failing()
    worker.start()
    assert worker.wait(2000)
    thread = threading.Thread(target=lambda: 1 / 0, name="helper")
    thread.start()
    thread.join()
    summaries = [summary for summary, _details in reported]
    assert summaries == ["ValueError: broken worker", "ZeroDivisionError: division by zero"]
    assert "raise ValueError" in reported[0][1]
    text = _text(log_file)
    assert "CRITICAL [helper] hermes: Unhandled exception in thread helper" in text
    assert "Traceback" in text and logs.error_count() == 2


def test_worker_failures_are_logged_once_per_interval(log_file):
    class Worker(QObject):
        worker_failed = Signal(str)
        hook_failed = Signal(int, str)
        progress = Signal(int)

        pid = 4711

    worker = Worker()
    logs.log_failures(worker)
    worker.worker_failed.emit("target gone")
    worker.worker_failed.emit("target gone")  # the same: once is enough
    worker.hook_failed.emit(3, "bad script")
    worker.progress.emit(5)
    text = _text(log_file)
    assert text.count("worker_failed (pid 4711): target gone") == 1
    assert "hook_failed (pid 4711): 3 | bad script" in text
    assert "progress" not in text


def test_qt_messages_go_to_the_log(log_file):
    qWarning("something odd")
    assert "WARNING  [MainThread] hermes.qt: something odd" in _text(log_file)


def test_a_crash_leaves_python_stacks_and_is_reported_next_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LOG_DIR", str(tmp_path))
    # Not dumpable (prctl PR_SET_DUMPABLE): the crash leaves no core dump
    # and no crash report of the desktop at every test run.
    no_core_dump = ("import ctypes; ctypes.CDLL(None).prctl(4, 0); "
                    if sys.platform == "linux" else "")
    crash = subprocess.run(
        [sys.executable, "-c",
         no_core_dump + "import os; from hermes.core import logs; logs.setup(); os.abort()"],
        capture_output=True, text=True, check=False)
    assert crash.returncode != 0
    stacks = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "Fatal Python error: Aborted" in stacks and "<string>" in stacks

    path = logs.setup()
    try:
        assert logs.error_count() == 1
        assert "The previous run of Hermes crashed" in _text(path)
    finally:
        logs.teardown()
    # This run did not crash: the next start is quiet.
    logs.setup()
    try:
        assert logs.error_count() == 0
    finally:
        logs.teardown()


def test_read_log_keeps_problems_with_their_tracebacks(tmp_path):
    path = tmp_path / "hermes.log"
    path.write_text(
        "2026-10-18 10:00:00,000 INFO     [MainThread] hermes: started\n"
        "2026-10-18 10:00:01,000 CRITICAL [MainThread] hermes: Unhandled exception\n"
        "Traceback (most recent call last):\n"
        "ValueError: x\n"
        "2026-10-18 10:00:02,000 INFO     [MainThread] hermes: more\n"
        "2026-10-18 10:00:03,000 WARNING  [w] hermes.HookWorker: hook_failed (pid 1): y\n",
        encoding="utf-8")
    assert len(read_log(path)) == 6
    assert read_log(path, problems_only=True) == [
        "2026-10-18 10:00:01,000 CRITICAL [MainThread] hermes: Unhandled exception",
        "Traceback (most recent call last):", "ValueError: x",
        "2026-10-18 10:00:03,000 WARNING  [w] hermes.HookWorker: hook_failed (pid 1): y"]
    assert read_log(path, max_lines=2) == read_log(path)[-2:]
    assert read_log(tmp_path / "missing.log") is None


def test_error_dialog_shows_one_box_and_counts_the_rest(log_file, qt_app):
    # pylint: disable=import-outside-toplevel,protected-access
    import json
    from pathlib import Path
    from hermes.ui.widgets.log_view import ErrorReporter

    translations = json.loads(
        (Path(logs.__file__).parent.parent / "ui" / "localization" / "de.json").read_text("utf-8"))
    opened = []
    reporter = ErrorReporter(translations, lambda: opened.append(True))
    logs.report_unhandled(*_exc_info(KeyError("first")))
    _wait_for(qt_app, lambda: reporter._box is not None)
    box = reporter._box
    assert "KeyError: 'first'" in box.text() and "Traceback" in box.detailedText()
    logs.report_unhandled(*_exc_info(KeyError("second")))
    qt_app.processEvents()
    assert reporter._box is box and "1 weitere" in box.informativeText()
    next(button for button in box.buttons()
         if button.text() == translations["log.open"]).click()
    _wait_for(qt_app, lambda: reporter._box is None)
    assert opened == [True]


def _exc_info(error):
    try:
        raise error
    except KeyError:
        return sys.exc_info()


@linux_only
def test_a_log_deleted_while_hermes_runs_is_written_again(log_file):
    logs.log.warning("before")
    log_file.unlink()
    logs.log.warning("after the file was deleted")
    assert "after the file was deleted" in _text(log_file)


@linux_only
def test_a_log_whose_folder_was_deleted_is_written_again(tmp_path):
    # pylint: disable=import-outside-toplevel
    import logging
    import shutil

    folder = tmp_path / "logs"
    folder.mkdir()
    handler = logs.ReopeningFileHandler(folder / "hermes.log", encoding="utf-8")
    logger = logging.getLogger("hermes.test.folder")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.warning("before")
        shutil.rmtree(folder)
        logger.warning("after the folder was deleted")
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert (folder / "hermes.log").read_text(encoding="utf-8") == "after the folder was deleted\n"


def test_a_reopening_log_still_rotates(tmp_path):
    # pylint: disable=import-outside-toplevel
    import logging

    handler = logs.ReopeningFileHandler(tmp_path / "small.log", maxBytes=200, backupCount=2,
                                        encoding="utf-8")
    logger = logging.getLogger("hermes.test.rotation")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for number in range(30):
            logger.warning("line %02d of a log that is rotated now and then", number)
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "small.log", "small.log.1", "small.log.2"]
    assert "line 29" in (tmp_path / "small.log").read_text(encoding="utf-8")
