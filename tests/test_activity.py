"""The activity log: what happened in Grembyte, in order (see activity.py)."""

import os
import subprocess
import sys
import threading
import zipfile

import pytest

from grembyte.core import activity, logs

from tests.test_hooks import (  # pylint: disable=unused-import
    _wait_for,
    fixture_qt_app,
    fixture_translations,
    linux_only,
    requires_target,
)


@pytest.fixture(name="activity_file")
def fixture_activity_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMBYTE_LOG_DIR", str(tmp_path))
    path = activity.setup()
    yield path
    activity.teardown()


def _entries(path):
    for handler in activity._logger.handlers:  # pylint: disable=protected-access
        handler.flush()
    return activity.read_entries(path)


def _events(path, *names):
    return [entry for entry in _entries(path) if not names or entry["event"] in names]


def test_nothing_is_written_before_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMBYTE_LOG_DIR", str(tmp_path))
    activity.record("click", text="x")
    handle = activity.rpc_started("agent#1", "scan")
    activity.rpc_ended(handle)
    assert not (tmp_path / activity.ACTIVITY_NAME).exists()
    assert activity.in_flight() == []


@linux_only
def test_an_activity_log_deleted_while_grembyte_runs_is_written_again(activity_file):
    activity.record("click", text="before")
    activity_file.unlink()
    activity.record("click", text="after")
    assert [entry.get("text") for entry in _entries(activity_file)] == ["after"]


def test_the_log_window_says_when_the_activity_log_is_off(tmp_path, monkeypatch, qt_app,
                                                          translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from grembyte.ui.widgets.log_view import LogDialog

    monkeypatch.setenv("GREMBYTE_LOG_DIR", str(tmp_path))
    dialog = LogDialog(translations)
    dialog.source_combo.setCurrentIndex(1)
    text = dialog.text.toPlainText()
    assert text.startswith(translations["log.activity_off"])
    assert "activity.jsonl" in text  # and that there is no file yet
    dialog.close()


def test_a_run_starts_with_what_grembyte_runs_on(activity_file):
    import frida  # pylint: disable=import-outside-toplevel

    activity.record("click", window="Grembyte", text="Prozess öffnen")
    first, click = _entries(activity_file)
    assert first["event"] == "app_started"
    assert first["frida"] == frida.__version__
    assert first["pid"] == os.getpid() and first["python"] and first["qt"]
    assert first["run"] == click["run"] == activity.run_id()
    assert click["text"] == "Prozess öffnen"  # not escaped: the file is read by people too
    assert '"Prozess öffnen"' in activity_file.read_text(encoding="utf-8")
    assert click["s"] >= first["s"]


def test_repeated_rpcs_are_summed_up_failures_are_not(activity_file, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])

    def call(method, ms, error=None, agent="trace#1"):
        handle = activity.rpc_started(agent, method)
        clock[0] += ms / 1000
        activity.rpc_ended(handle, error)

    for _ in range(activity.REPEAT_WRITTEN):
        call("next_scan", 10)    # written one by one
    for _ in range(5):
        clock[0] += 0.1
        call("next_scan", 20)    # summed up
    call("begin", 1)             # another method: written, the sum is not due yet
    call("next_scan", 30, "access violation")  # the sum first, then the failure
    clock[0] += 0.1
    call("next_scan", 5)
    clock[0] += activity.REPEAT_WINDOW_S
    activity.record("click", text="Stopp")      # the sum is due: written before
    rpcs = [entry for entry in _events(activity_file, "rpc", "click")]
    assert [(entry.get("method"), entry.get("repeated"), entry.get("error")) for entry in rpcs] == [
        ("next_scan", None, None),
        ("next_scan", None, None),
        ("next_scan", None, None),
        ("begin", None, None),
        ("next_scan", 5, None),
        ("next_scan", None, "access violation"),
        ("next_scan", 1, None),
        (None, None, None),
    ]
    assert rpcs[4]["ms"] == 100.0 and rpcs[4]["max_ms"] == 20.0
    assert rpcs[0]["ms"] == 10.0


def test_rpcs_in_flight_are_named(activity_file):
    handle = activity.rpc_started("native_trace#2", "finish")
    assert [call["method"] for call in activity.in_flight()] == ["finish"]
    activity.rpc_ended(handle)
    assert activity.in_flight() == []


def test_sessions_know_their_thread(activity_file):
    class Worker:  # pylint: disable=too-few-public-methods
        pid = 42

    seen = []

    def run():
        number = activity.session_started("native_trace", Worker(), 42)
        seen.append(activity.current_session())
        activity.session_ended(number, "native_trace", 42, 1.5)
        seen.append(activity.current_session())

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert seen[0][1] == "native_trace" and seen[1] == (None, None)
    assert activity.current_session() == (None, None)  # another thread
    started, ended = _events(activity_file, "session_started", "session_ended")
    assert started["worker"] == "Worker" and started["session"] == ended["session"]
    assert ended["seconds"] == 1.5


@linux_only
def test_a_native_process_is_described(tmp_path):
    details = activity.describe_process(os.getpid())
    assert details["exe"] == os.path.realpath(sys.executable)
    assert details["wine"] is False
    assert details["bits"] == details["loader_bits"] == 64
    assert "proton" not in details


def test_proton_names_its_version(tmp_path):
    proton = tmp_path / "Proton 9.0 (Beta)"
    (proton / "files" / "bin").mkdir(parents=True)
    (proton / "version").write_text("1718000000 proton-9.0-2\n")
    assert activity.proton_version(proton / "files" / "bin" / "wine64-preloader") \
        == "1718000000 proton-9.0-2"
    assert activity.proton_version("/usr/bin/wine64-preloader") is None


def test_the_report_holds_the_logs(activity_file, tmp_path):
    logs.setup(tmp_path)
    try:
        logs.log.warning("something went wrong")
        target = tmp_path / "report.zip"
        names = activity.export_report(target, tmp_path)
    finally:
        logs.teardown()
    assert set(names) >= {"activity.jsonl", "grembyte.log", "crash.log"}
    with zipfile.ZipFile(target) as archive:
        assert "something went wrong" in archive.read("grembyte.log").decode()
    assert _events(activity_file, "report_exported")[0]["files"] == names


def _wait_for_sleeping(pid, timeout=5.0):
    # pylint: disable=import-outside-toplevel
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with open(f"/proc/{pid}/syscall", encoding="ascii") as syscall:
            if syscall.read().split()[0] in ("35", "230"):  # nanosleep, clock_nanosleep
                return
        time.sleep(0.01)
    raise AssertionError("sleep did not start sleeping")


@requires_target
def test_frida_sessions_note_agents_rpcs_and_their_end(activity_file):
    # pylint: disable=import-outside-toplevel
    from grembyte.attach.frida_attach import attach_session

    process = subprocess.Popen(["sleep", "30"])
    try:
        # Not while it starts up: Frida's agent in a half-initialised libc kills it.
        _wait_for_sleeping(process.pid)
        session = attach_session(process.pid)
        script = session.create_script(
            "rpc.exports = { add(a, b) { return a + b; }, fail() { throw new Error('no'); } };")
        script.on("message", lambda _message, _data: None)
        script.load()
        assert script.exports_sync.add(2, 3) == 5
        with pytest.raises(Exception, match="no"):
            script.exports_sync.fail()
        script.unload()
        session.detach()
    finally:
        process.kill()
        process.wait()
    loaded = next(entry for entry in _events(activity_file, "agent_loaded")
                  if entry["pid"] == process.pid)
    # Only this process: workers of earlier tests may still be unloading.
    entries = [entry for entry in _events(activity_file) if entry.get("pid") == process.pid
               or entry.get("agent") == loaded["agent"]]
    names = [entry["event"] for entry in entries]
    assert names[names.index("attached"):] == [
        "attached", "agent_loaded", "rpc", "rpc", "agent_unloaded", "detach", "detached"]
    attached = entries[names.index("attached")]
    assert attached["via"] == "frida.attach" and attached["pid"] == process.pid
    rpcs = [entry for entry in entries if entry["event"] == "rpc"]
    assert [(entry["method"], "error" in entry) for entry in rpcs] == [("add", False),
                                                                      ("fail", True)]
    assert rpcs[0]["agent"] == entries[names.index("agent_loaded")]["agent"]
    assert entries[-1]["reason"] == "application-requested"


@requires_target
def test_large_agents_reach_the_gadget_in_parts(activity_file):
    # pylint: disable=import-outside-toplevel
    import frida

    from grembyte.attach.frida_attach import GADGET_SOURCE_LIMIT, RecordedSession

    process = subprocess.Popen(["sleep", "30"])
    try:
        _wait_for_sleeping(process.pid)
        # The loader path as under Wine, on a native process
        session = RecordedSession(frida.attach(process.pid), process.pid, 0, "test", gadget=True)
        source = ("const base = 40;\n"
                  "rpc.exports = { add(a) { return base + a; } };\n"
                  "send({ kind: 'ready', text: 'ä\\u2028\"' });\n"
                  + "// padding \\ \" ä\n" * (GADGET_SOURCE_LIMIT // 10))
        messages = []
        script = session.create_script(source)
        script.on("message", lambda message, _data: messages.append(message))
        script.load()
        assert script.exports_sync.add(2) == 42
        assert [message["payload"] for message in messages] == [
            {"kind": "ready", "text": "ä\N{LINE SEPARATOR}\""}]
        script.unload()
        session.detach()
    finally:
        process.kill()
        process.wait()
    loaded = next(entry for entry in _events(activity_file, "agent_loaded")
                  if entry["pid"] == process.pid)
    assert loaded["size"] == len(source)


def test_clicks_actions_and_windows_are_recorded(activity_file, qt_app):
    # pylint: disable=import-outside-toplevel
    from PySide6.QtWidgets import QComboBox, QDialog, QMenu, QPushButton, QVBoxLayout  # pylint: disable=no-name-in-module

    from grembyte.ui.activity_recorder import UiRecorder

    recorder = UiRecorder()
    try:
        dialog = QDialog()
        dialog.setWindowTitle("Nativer Trace")
        layout = QVBoxLayout(dialog)
        button = QPushButton("&Verfolgung starten")
        # as the windows do: their slot, connected first, changes the text
        button.clicked.connect(lambda: button.setText("Verfolgung beenden"))
        layout.addWidget(button)
        combo = QComboBox()
        combo.setObjectName("phase")
        combo.addItems(["Grundrauschen", "Szenario"])
        layout.addWidget(combo)
        menu = QMenu(dialog)
        action = menu.addAction("Adresse kopieren")
        dialog.show()
        qt_app.processEvents()
        recorder.scan()  # what the timer does
        button.click()
        combo.activated.emit(1)
        action.trigger()
        dialog.close()
        qt_app.processEvents()
        recorder.scan()
    finally:
        recorder.stop()
    events = [(entry["event"], entry.get("text") or entry.get("window"))
              for entry in _events(activity_file, "click", "choice", "action",
                                   "window_shown", "window_hidden")]
    assert events == [("window_shown", "Nativer Trace"), ("click", "Verfolgung starten"),
                      ("choice", "Szenario"), ("action", "Adresse kopieren"),
                      ("window_hidden", "Nativer Trace")]
    click = _events(activity_file, "click")[0]
    assert click["window"] == "Nativer Trace" and click["widget"] == "QPushButton"


def test_clearing_a_filter_with_its_clear_button_is_recorded(activity_file, qt_app):
    # The clear button's action is created by Qt; asking its tool button for
    # defaultAction() invalidated the action's wrapper and the click raised.
    # pylint: disable=import-outside-toplevel
    from PySide6.QtGui import QAction  # pylint: disable=no-name-in-module
    from PySide6.QtWidgets import QLineEdit  # pylint: disable=no-name-in-module

    from grembyte.ui.activity_recorder import UiRecorder

    recorder = UiRecorder()
    try:
        line = QLineEdit()
        line.setClearButtonEnabled(True)
        line.setText("Gold")
        line.show()
        qt_app.processEvents()
        recorder.scan()
        line.findChild(QAction).trigger()
        qt_app.processEvents()
        line.close()
    finally:
        recorder.stop()
    assert _events(activity_file, "action")


def test_the_log_window_shows_the_activity(activity_file, qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from grembyte.ui.widgets.log_view import LogDialog

    activity.record("process_selected", pid=4711, name="Baldur.exe", wine=True, bits=64)
    activity.flush()
    dialog = LogDialog(translations)
    dialog.source_combo.setCurrentIndex(1)
    text = dialog.text.toPlainText()
    assert f"run {activity.run_id()}" in text
    assert "process_selected" in text and "name=Baldur.exe" in text and "wine=true" in text
    assert not dialog.problems_check.isEnabled()
    dialog.close()
