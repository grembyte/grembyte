"""The session registry, its window and the shutdown dialog, with fake workers."""

import subprocess
import sys
import threading
import time

from PyQt6.QtCore import QThread  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import QDialog  # pylint: disable=import-error,no-name-in-module

from hermes.core import sessions
from hermes.core.sessions import SESSIONS, frida_agents
from hermes.ui.widgets.sessions import SessionsDialog, ShutdownDialog, duration_text

from tests.test_hooks import _wait_for, fixture_qt_app, fixture_translations, linux_only  # pylint: disable=unused-import


class _FakeFridaSession:
    def __init__(self):
        self.detached = threading.Event()

    def detach(self):
        self.detached.set()


class _Worker(QThread):
    """Runs until asked to stop, or (``hangs``) until its session is cut."""

    def __init__(self, pid, hangs=False):
        super().__init__()
        self.pid = pid
        self.hangs = hangs
        self.frida = _FakeFridaSession()

    @sessions.tracked(sessions.HOOKS)
    def run(self):
        SESSIONS.set_frida_session(self, self.frida)
        while not self.frida.detached.is_set():
            if self.isInterruptionRequested() and not self.hangs:
                return
            time.sleep(0.01)


def _start(qt_app, worker):
    worker.start()
    _wait_for(qt_app, lambda: any(s.worker is worker and s.frida_session
                                  for s in SESSIONS.sessions()))
    return next(s for s in SESSIONS.sessions() if s.worker is worker)


def test_registry_tracks_running_workers_only(qt_app):
    worker = _Worker(4242)
    assert not SESSIONS.sessions(4242)
    session = _start(qt_app, worker)
    assert (session.kind, session.pid, session.stopping) == (sessions.HOOKS, 4242, None)
    assert SESSIONS.sessions(1) == []
    SESSIONS.stop(session)
    assert worker.wait(2000)
    assert not SESSIONS.sessions(4242)


def test_window_lists_sessions_and_cuts_a_hanging_one(qt_app, translations):
    worker = _Worker(4243, hangs=True)
    session = _start(qt_app, worker)
    # A process without Frida agents (the tests' own may have one: the
    # syntax check's, see hermes.hooks.syntax)
    clean = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    dialog = SessionsDialog(translations, lambda: clean.pid)
    try:
        assert dialog.table.rowCount() == 1
        assert dialog.table.item(0, 0).text() == "Hooks (Frida)"
        assert dialog.table.item(0, 3).text() == "aktiv"
        assert "0 Frida-Agenten" in dialog.agents_label.text()

        dialog.table.selectRow(0)
        dialog.stop_button.click()
        assert not worker.wait(200)
        session.stopping -= 10
        dialog.refresh()
        assert dialog.table.item(0, 3).text().startswith("reagiert nicht (10 s)")
        dialog.table.selectRow(0)
        assert dialog.disconnect_button.isEnabled()
        dialog.disconnect_button.click()
        assert worker.wait(2000)
        _wait_for(qt_app, lambda: dialog.table.rowCount() == 0)
    finally:
        dialog.close()
        clean.kill()
        clean.wait()
        worker.frida.detach()
        worker.wait(2000)


def test_shutdown_waits_then_cuts_what_hangs(qt_app, translations):
    polite, stubborn = _Worker(4244), _Worker(4245, hangs=True)
    _start(qt_app, polite)
    _start(qt_app, stubborn)
    began = time.monotonic()
    # The polite one ends when asked; the stubborn one only when cut.
    result = ShutdownDialog(translations, timeout_s=0.5, grace_s=2).exec()
    assert result == QDialog.DialogCode.Accepted
    assert stubborn.frida.detached.is_set() and not polite.frida.detached.is_set()
    assert time.monotonic() - began < 2.5
    assert polite.wait(1000) and stubborn.wait(1000)


def test_shutdown_gives_up_on_a_worker_that_ignores_everything(qt_app, translations):
    class _Deaf(_Worker):
        @sessions.tracked(sessions.TRACE)
        def run(self):
            SESSIONS.set_frida_session(self, self.frida)
            while not self.release.is_set():
                time.sleep(0.01)

    deaf = _Deaf(4246)
    deaf.release = threading.Event()
    _start(qt_app, deaf)
    try:
        result = ShutdownDialog(translations, timeout_s=0.2, grace_s=0.3).exec()
        assert result == QDialog.DialogCode.Rejected
        assert SESSIONS.sessions(4246)
    finally:
        deaf.release.set()
        deaf.wait(2000)
        qt_app.processEvents()


@linux_only
def test_counts_frida_agents_in_maps(tmp_path):
    maps = tmp_path / "77" / "maps"
    maps.parent.mkdir()
    maps.write_text(
        "7f00-7f10 r--p 00000000 00:01 5 /memfd:frida-agent-64.so (deleted)\n"
        "7f10-7f20 r-xp 00010000 00:01 5 /memfd:frida-agent-64.so (deleted)\n"
        "8f00-8f10 r--p 00000000 00:01 6 /memfd:frida-agent-64.so (deleted)\n"
        "9f00-9f10 r--p 00000000 08:01 7 /usr/lib/libc.so.6\n")
    assert frida_agents(77, proc_root=str(tmp_path)) == 2
    assert frida_agents(78, proc_root=str(tmp_path)) == 0


def test_duration_text():
    assert [duration_text(s) for s in (4.9, 75, 3725)] == ["4 s", "1:15 min", "1:02 h"]


def test_workers_log_under_the_name_of_their_session(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    import logging

    from hermes.core import logs

    class Named(QThread):
        pid = 4711

        def __init__(self):
            super().__init__()
            self.names = []

        @sessions.tracked(sessions.NATIVE_TRACE)
        def run(self):
            self.names.append(threading.current_thread().name)
            logs.log.info("inside")

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logs.log.addHandler(handler)
    previous = logs.log.level
    logs.log.setLevel(logging.INFO)
    worker = Named()
    try:
        worker.start()
        assert worker.wait(5000)
    finally:
        logs.log.removeHandler(handler)
        logs.log.setLevel(previous)
    name = worker.names[0]
    assert name.startswith("native_trace-") and name.split("-")[1].isdigit()
    assert [record.threadName for record in records
            if record.getMessage() in ("inside", "session native_trace started (pid 4711)",
                                       "session native_trace ended (pid 4711)")] == [name] * 3
