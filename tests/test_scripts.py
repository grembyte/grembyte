"""The scripts tab: its dialog and texts (running scripts: see test_unreal)."""

import json
import os
from pathlib import Path

import pytest

from hermes.features.scripts import ScriptSpec
from hermes.ui.localization import load_translations


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module
    return QApplication.instance() or QApplication([])


def test_both_languages_name_the_same_script_texts():
    folder = Path(__file__).parent.parent / "hermes" / "ui" / "localization"
    keys = {language: {key for key in json.loads((folder / f"{language}.json").read_text("utf-8"))
                       if key.startswith("scripts.")}
            for language in ("en", "de")}
    assert keys["en"] == keys["de"]


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_dialog_builds_a_script_and_rejects_busy_loops(qt_app, language):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.scripts import ScriptDialog, timing_text

    translations = load_translations(language)
    dialog = ScriptDialog(translations)
    dialog.label_edit.setText("Heal")
    dialog.code_edit.setPlainText("ctx.find('AlabamaCheatManager').call('GiveHealth', 50);")
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("repeat"))
    dialog.seconds_spin.setValue(0)
    dialog._accept()  # pylint: disable=protected-access
    assert dialog.spec is None and dialog.error_label.text()

    dialog.seconds_spin.setValue(30)
    dialog._accept()  # pylint: disable=protected-access
    assert dialog.spec == ScriptSpec("ctx.find('AlabamaCheatManager').call('GiveHealth', 50);",
                                     "repeat", 30, True, "Heal")
    assert "30" in timing_text(translations, dialog.spec)

    # Editing keeps whether the script is paused; manual scripts have no delay.
    paused = ScriptSpec("return 1;", "once", 5, False, "Later")
    dialog = ScriptDialog(translations, spec=paused)
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("manual"))
    assert not dialog.seconds_spin.isEnabled()
    dialog._accept()  # pylint: disable=protected-access
    assert dialog.spec == ScriptSpec("return 1;", "manual", 0.0, False, "Later")


def test_the_address_search_folds_away_and_stays_so(qt_app, tmp_path, monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access,unused-argument
    from PyQt6 import sip  # pylint: disable=import-error,no-name-in-module
    from PyQt6.QtCore import QSettings  # pylint: disable=import-error,no-name-in-module
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from hermes.db import models  # noqa: F401  pylint: disable=unused-import
    from hermes.db.base import Base
    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.db.repository.scripts import ScriptRepository
    from hermes.ui import main_window

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(main_window, "HookRepository", lambda: HookRepository(sessions))
    monkeypatch.setattr(main_window, "AddressRepository", lambda: AddressRepository(sessions))
    monkeypatch.setattr(main_window, "ScriptRepository", lambda: ScriptRepository(sessions))
    settings_file = str(tmp_path / "settings.ini")
    monkeypatch.setattr(main_window, "QSettings",
                        lambda *_args: QSettings(settings_file, QSettings.Format.IniFormat))

    def open_window():
        window = main_window.MainWindow("de")
        window._refresh_timer.stop()
        return window

    def close(window):
        window.close()
        sip.delete(window)

    try:
        window = open_window()
        assert not window._scan_area.isHidden() and window.act_scan_area.isChecked()
        window.btn_scan_area.click()
        assert window._scan_area.isHidden() and not window.act_scan_area.isChecked()
        assert window.btn_scan_area.text() == "▼ Adresssuche einblenden"

        # The tab of tracked addresses counts them, like those of hooks and scripts.
        tab = window.bottom_tabs.indexOf(window._address_box)
        assert window.bottom_tabs.tabText(tab) == "Gemerkte Adressen"
        window.addr_table.insertRow(0)
        window.addr_table.insertRow(0)
        assert window.bottom_tabs.tabText(tab) == "Gemerkte Adressen (2)"
        window.addr_table.removeRow(0)
        assert window.bottom_tabs.tabText(tab) == "Gemerkte Adressen (1)"
        window.addr_table.setRowCount(0)
        assert window.bottom_tabs.tabText(tab) == "Gemerkte Adressen"
        close(window)

        # Started again: still folded away; the menu entry shows it again.
        window = open_window()
        assert window._scan_area.isHidden() and not window.act_scan_area.isChecked()
        window.act_scan_area.trigger()
        assert not window._scan_area.isHidden()
        assert window.btn_scan_area.text() == "▲ Adresssuche ausblenden"
        close(window)
        assert QSettings(settings_file, QSettings.Format.IniFormat).value(
            "scan_area_visible", type=bool) is True
    finally:
        engine.dispose()


def test_native_tools_ask_first_in_mono_games(qt_app, tmp_path, monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access,unused-argument
    from PyQt6 import sip  # pylint: disable=import-error,no-name-in-module
    from PyQt6.QtWidgets import QMessageBox  # pylint: disable=import-error,no-name-in-module
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from hermes.db import models  # noqa: F401  pylint: disable=unused-import
    from hermes.db.base import Base
    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.db.repository.scripts import ScriptRepository
    from hermes.engines.registry import engine_by_id
    from hermes.ui import main_window

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(main_window, "HookRepository", lambda: HookRepository(sessions))
    monkeypatch.setattr(main_window, "AddressRepository", lambda: AddressRepository(sessions))
    monkeypatch.setattr(main_window, "ScriptRepository", lambda: ScriptRepository(sessions))
    answers, asked = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes], []

    def warning(*args):
        asked.append(args[1])
        return answers.pop(0)

    monkeypatch.setattr(main_window.QMessageBox, "warning", warning)
    window = main_window.MainWindow("de")
    window._refresh_timer.stop()
    try:
        window._current_pid = 4242
        window._engine = engine_by_id("unreal")
        assert window._confirm_native_tools("Trace") and not asked

        window._engine = engine_by_id("mono")
        window._update_catalog_button()
        assert window.act_native_trace.isEnabled()
        assert "Shadowrun Returns" in window.act_native_trace.toolTip()
        assert not window._confirm_native_tools("Trace")  # "No"
        assert window._confirm_native_tools("Trace")      # "Yes"
        assert window._confirm_native_tools("Monitor")    # not asked again
        assert asked == ["Trace", "Trace"]
    finally:
        window.close()
        sip.delete(window)
        engine.dispose()


class _FakeScriptWorker:
    """Stands in for ScriptWorker: records the requests; the test answers."""

    def __init__(self, _pid, _parent=None):
        # pylint: disable=import-outside-toplevel
        from PyQt6.QtCore import QObject, pyqtSignal  # pylint: disable=import-error,no-name-in-module

        class Signals(QObject):  # pylint: disable=too-few-public-methods
            script_added = pyqtSignal(int)
            script_updated = pyqtSignal(int)
            script_removed = pyqtSignal(int)
            script_failed = pyqtSignal(int, str)
            script_ran = pyqtSignal(int, dict)
            worker_failed = pyqtSignal(str)
            inspected = pyqtSignal(int, dict)
            inspect_failed = pyqtSignal(int, str)
            evaluated = pyqtSignal(int, list)
            evaluate_failed = pyqtSignal(int, str)
            text_fetched = pyqtSignal(int, object)
            text_failed = pyqtSignal(int, str)

        self.signals = Signals()
        for name in ("script_added", "script_updated", "script_removed", "script_failed",
                     "script_ran", "worker_failed", "inspected", "inspect_failed",
                     "evaluated", "evaluate_failed", "text_fetched", "text_failed"):
            setattr(self, name, getattr(self.signals, name))
        self.requests = []

    def start(self):
        pass

    def isRunning(self):  # pylint: disable=invalid-name
        return True

    def requestInterruption(self):  # pylint: disable=invalid-name
        pass

    def wait(self, _timeout):
        return True

    def add_script(self, spec):
        self.requests.append(("add", spec))
        return 1

    def run_now(self, script_id):
        self.requests.append(("run", script_id))

    def inspect(self, target):
        self.requests.append(("inspect", target))
        return len(self.requests)


def test_objects_scripts_return_open_in_the_inspector_from_the_log(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PyQt6.QtCore import QUrl  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.widgets import scripts

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    panel = scripts.ScriptPanel(load_translations("de"))
    try:
        panel.set_process(4242)
        script_id = panel.add_script(ScriptSpec('return ctx.call("GameState:get_PlayerCharacter");',
                                                label="Spieler"))
        worker = panel._worker
        worker.script_added.emit(script_id)
        target = {"address": "0x7f82508577e0"}
        player = {"runs": 1, "time": 0, "result": 'Player "Player_New_Game(Clone)_1"@0x7f82508577e0',
                  "target": target, "logs": [], "error": None}

        # Neither a timed run nor one by hand opens a window: the log links it.
        worker.script_ran.emit(script_id, player)
        panel.table.selectRow(0)
        panel.btn_run.click()
        worker.script_ran.emit(script_id, {**player, "runs": 2})
        assert not panel.inspections.inspectors
        assert panel.log_view.toPlainText().count("Inspizieren") == 2

        panel.log_view.anchorClicked.emit(QUrl("inspect:1"))
        assert len(panel.inspections.inspectors) == 1
        assert worker.requests[-1] == ("inspect", target)
        assert panel.table.item(0, 4).text().startswith("Player")
    finally:
        for inspector in list(panel.inspections.inspectors if panel.inspections else []):
            inspector.close()
        panel.shutdown()
        panel.deleteLater()
