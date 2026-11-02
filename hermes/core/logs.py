"""The application log: a file of what went wrong, and hooks that fill it.

``setup`` writes the ``hermes`` loggers to a rotating ``hermes.log`` in
``hermes-hook`` in the user's state directory (``$XDG_STATE_HOME`` or
``~/.local/state`` on Linux; ``logs`` in ``hermes-hook`` in ``%LOCALAPPDATA%``
on Windows; or ``HERMES_LOG_DIR``), lets ``faulthandler`` write the Python stack of every
thread to ``crash.log`` should the process crash or abort, and routes Qt's
messages into the log.

``install_exception_hooks`` catches exceptions nothing else caught, in slots,
in ``QThread.run`` (PySide hands them to ``sys.excepthook``) and in Python
threads. They are logged and reported to ``on_unhandled`` callbacks instead
of PySide aborting the application.

``log_failures(worker)`` logs what a worker reports through its ``…_failed``
and ``…_error`` signals. Counting: ``error_count()`` is the number of
records of level ERROR and up since the start; ``subscribe`` callbacks hear
of each (from the thread that logged it).
"""

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QtMsgType, SignalInstance, qInstallMessageHandler  # pylint: disable=import-error,no-name-in-module


LOG_NAME = "hermes.log"
CRASH_NAME = "crash.log"
MAX_BYTES = 1_000_000
BACKUPS = 4
FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"
# The same failure of the same worker kind is logged once in this many seconds.
REPEAT_INTERVAL_S = 60.0

log = logging.getLogger("hermes")

_lock = threading.Lock()
_errors = 0
_callbacks = []
_unhandled_callbacks = []
_crash_file = None
_last_failures = {}


def log_dir():
    """Where the log files go (not created here)."""
    if os.environ.get("HERMES_LOG_DIR"):
        return Path(os.environ["HERMES_LOG_DIR"])
    if sys.platform == "win32":
        # Qt's state location there is %LOCALAPPDATA%\State, shared by all
        # programs; keep the logs next to the database instead.
        data = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
        return Path(data) / "hermes-hook" / "logs"
    state = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericStateLocation)
    return Path(state) / "hermes-hook"


def log_path():
    return log_dir() / LOG_NAME


def crash_path():
    return log_dir() / CRASH_NAME


class ReopeningFileHandler(logging.handlers.RotatingFileHandler):
    """A RotatingFileHandler that opens its file again once it was deleted or moved.

    Otherwise a log deleted while Hermes runs keeps being written to the
    deleted file and shows up again only at the next start. The check is
    the one of logging's WatchedFileHandler, before every record.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stat_stream()

    def _stat_stream(self):
        self._identity = None
        if self.stream is not None:
            status = os.fstat(self.stream.fileno())
            self._identity = (status.st_dev, status.st_ino)

    def _reopen_if_needed(self):
        if self.stream is None:
            return
        try:
            status = os.stat(self.baseFilename)
            if (status.st_dev, status.st_ino) == self._identity:
                return
        except FileNotFoundError:
            pass
        self.stream.flush()
        self.stream.close()
        self.stream = None  # should opening fail, the next record tries again
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        self.stream = self._open()
        self._stat_stream()

    def emit(self, record):
        try:
            self._reopen_if_needed()
        except OSError:
            self.handleError(record)
            return
        super().emit(record)

    def doRollover(self):
        super().doRollover()
        self._stat_stream()


class _Counter(logging.Handler):
    """Counts records of level ERROR and up and tells the subscribers."""

    def __init__(self):
        super().__init__(logging.ERROR)

    def emit(self, record):
        global _errors  # pylint: disable=global-statement
        with _lock:
            _errors += 1
            callbacks = list(_callbacks)
        for callback in callbacks:
            try:
                callback()
            except RuntimeError:  # its Qt object is gone
                unsubscribe(callback)


def setup(directory=None):
    """Start logging to files; returns the log file's path. Safe to repeat."""
    global _crash_file  # pylint: disable=global-statement
    directory = Path(directory) if directory is not None else log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    file_handler = ReopeningFileHandler(
        directory / LOG_NAME, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(FORMAT))
    file_handler.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(FORMAT))
    console.setLevel(logging.WARNING)
    log.addHandler(file_handler)
    log.addHandler(console)
    log.addHandler(_Counter())
    log.setLevel(logging.DEBUG)
    log.propagate = False

    crashed = _previous_run_crashed(directory / CRASH_NAME)
    if _crash_file is not None:
        faulthandler.disable()
        _crash_file.close()
    # faulthandler writes to the file descriptor directly, also when the
    # process is already falling apart; the file stays open for that.
    _crash_file = open(directory / CRASH_NAME, "a", encoding="utf-8")  # pylint: disable=consider-using-with
    _crash_file.write(f"--- Hermes started {time.strftime('%Y-%m-%d %H:%M:%S')}, "
                      f"pid {os.getpid()} ---\n")
    _crash_file.flush()
    faulthandler.enable(file=_crash_file, all_threads=True)

    qInstallMessageHandler(_qt_message)
    log.info("Hermes started (pid %d), logging to %s", os.getpid(), directory / LOG_NAME)
    if crashed:
        log.error("The previous run of Hermes crashed; its Python stacks are in %s",
                  directory / CRASH_NAME)
    return directory / LOG_NAME


def teardown():
    """Undo ``setup`` (for tests): no files, no handlers, Qt's own messages."""
    global _crash_file, _errors  # pylint: disable=global-statement
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    if _crash_file is not None:
        faulthandler.disable()
        _crash_file.close()
        _crash_file = None
    qInstallMessageHandler(None)
    with _lock:
        _errors = 0
        _last_failures.clear()
        _callbacks.clear()
        _unhandled_callbacks.clear()


_RUN_HEADER = "--- Hermes started "


def _previous_run_crashed(path):
    """Whether faulthandler wrote anything after the last run's header."""
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            file.seek(max(0, os.path.getsize(path) - 200_000))
            text = file.read()
    except OSError:
        return False
    start = text.rfind(_RUN_HEADER)
    return start >= 0 and "Fatal Python error" in text[start:]


_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def _qt_message(kind, context, message):
    where = f" ({context.file}:{context.line})" if context.file else ""
    logging.getLogger("hermes.qt").log(_QT_LEVELS.get(kind, logging.WARNING),
                                        "%s%s", message, where)


# --- Exceptions nothing caught -------------------------------------------------

def on_unhandled(callback):
    """Call ``callback(summary, details)`` for each unhandled exception."""
    with _lock:
        _unhandled_callbacks.append(callback)


def report_unhandled(kind, value, trace, thread_name=None):
    """Log an exception nothing caught and tell the ``on_unhandled`` callbacks."""
    if issubclass(kind, KeyboardInterrupt):
        return
    thread_name = thread_name or threading.current_thread().name
    log.critical("Unhandled exception in thread %s", thread_name,
                 exc_info=(kind, value, trace))
    summary = f"{kind.__name__}: {value}"
    details = "".join(traceback.format_exception(kind, value, trace))
    with _lock:
        callbacks = list(_unhandled_callbacks)
    for callback in callbacks:
        try:
            callback(summary, details)
        except RuntimeError:
            pass  # its Qt object is gone


def install_exception_hooks():
    """Report exceptions nothing caught instead of letting PySide abort."""
    sys.excepthook = report_unhandled
    threading.excepthook = lambda args: report_unhandled(
        args.exc_type, args.exc_value, args.exc_traceback,
        args.thread.name if args.thread is not None else None)


# --- Errors so far ---------------------------------------------------------------

def error_count():
    with _lock:
        return _errors


def subscribe(callback):
    """Call ``callback()`` for each error logged, from the logging thread."""
    with _lock:
        _callbacks.append(callback)


def unsubscribe(callback):
    with _lock:
        if callback in _callbacks:
            _callbacks.remove(callback)


# --- What workers report -----------------------------------------------------------

def _is_failure_signal(name):
    return name.endswith("failed") or name.endswith("_error") or name == "agent_warning"


def log_failures(worker):
    """Log what ``worker`` reports through its failure signals.

    Connected directly: the log is written in the thread that emits, which
    may run no event loop. The same message of the same signal of a worker
    kind is logged once per REPEAT_INTERVAL_S.
    """
    kind = type(worker).__name__
    logger = logging.getLogger(f"hermes.{kind}")
    for name in dir(type(worker)):
        if not _is_failure_signal(name):
            continue
        signal = getattr(worker, name, None)
        if not isinstance(signal, SignalInstance):
            continue

        def write(*args, name=name):
            text = " | ".join(str(arg) for arg in args)
            key = (kind, name, text)
            now = time.monotonic()
            with _lock:
                last = _last_failures.get(key)
                if last is not None and now - last < REPEAT_INTERVAL_S:
                    return
                _last_failures[key] = now
            logger.warning("%s (pid %s): %s", name, getattr(worker, "pid", "?"), text)

        signal.connect(write, Qt.ConnectionType.DirectConnection)


_AGENT_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
                 "error": logging.ERROR}


def agent_log(source, level, text):
    """Log a ``console.log``/``warn``/``error`` of a Frida script."""
    log.log(_AGENT_LEVELS.get(level, logging.INFO), "%s: %s", source, text)


def cleanup_failed(error):
    """Note an error while tearing something down (expected: the target may be gone)."""
    logging.getLogger("hermes.cleanup").debug("ignored while cleaning up: %r", error)
