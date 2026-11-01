"""The scripts tab: its dialog and texts (running scripts: see test_unreal)."""

import itertools
import json
import os
from pathlib import Path

import pytest

from hermes.features.scripts import ScriptSpec
from hermes.ui.localization import language_code, load_translations


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


@pytest.mark.parametrize("language, expected", [("de-DE", "de"), ("en_US", "en"),
                                                  ("fr", "en")])
def test_language_code_resolves_supported_script_languages(language, expected):
    assert language_code(language) == expected


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
        file_menu = window.menuBar().actions()[0].menu()
        file_actions = file_menu.actions()
        assert file_actions[-2].isSeparator()
        assert file_actions[-1] == window.act_check_gadget
        assert window.act_check_gadget.isEnabled() == (main_window.sys.platform != "win32")
        assert not window.act_check_gadget.icon().isNull()
        assert not window._scan_area.isHidden() and window.act_scan_area.isChecked()
        window.btn_scan_area.click()
        assert window._scan_area.isHidden() and not window.act_scan_area.isChecked()
        assert window.btn_scan_area.text() == "Adresssuche einblenden"
        assert not window.btn_scan_area.icon().isNull()

        # The tab of tracked addresses counts them, like those of hooks and scripts.
        tab = window.bottom_tabs.indexOf(window._address_box)
        assert window.bottom_tabs.tabText(tab) == "Gemerkte Adressen"
        address_buttons = (window.btn_write_value, window.btn_freeze, window.btn_remove_address)
        assert not any(button.isEnabled() for button in address_buttons)
        window.addr_table.insertRow(0)
        window.addr_table.insertRow(0)
        assert window.bottom_tabs.tabText(tab) == "Gemerkte Adressen (2)"
        # The address actions need a selected address.
        assert not any(button.isEnabled() for button in address_buttons)
        window.addr_table.selectRow(0)
        assert all(button.isEnabled() for button in address_buttons)
        window.addr_table.removeRow(0)
        assert window.bottom_tabs.tabText(tab) == "Gemerkte Adressen (1)"
        assert not any(button.isEnabled() for button in address_buttons)
        window.addr_table.setRowCount(0)
        assert window.bottom_tabs.tabText(tab) == "Gemerkte Adressen"
        close(window)

        # Started again: still folded away; the menu entry shows it again.
        window = open_window()
        assert window._scan_area.isHidden() and not window.act_scan_area.isChecked()
        window.act_scan_area.trigger()
        assert not window._scan_area.isHidden()
        assert window.btn_scan_area.text() == "Adresssuche ausblenden"
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

    # As ScriptWorker's: unique across workers
    _ids = itertools.count(1)

    def __init__(self, _pid, _parent=None, _language="en"):
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
            methods_listed = pyqtSignal(int, list)
            methods_failed = pyqtSignal(int, str)
            text_fetched = pyqtSignal(int, object)
            text_failed = pyqtSignal(int, str)
            gas_listed = pyqtSignal(int, object)
            gas_failed = pyqtSignal(int, str)
            gas_set = pyqtSignal(int, object)
            gas_set_failed = pyqtSignal(int, str)
            options_listed = pyqtSignal(int, list)
            options_failed = pyqtSignal(int, str)
            finished = pyqtSignal()

        self.signals = Signals()
        for name in ("script_added", "script_updated", "script_removed", "script_failed",
                     "script_ran", "worker_failed", "inspected", "inspect_failed",
                     "evaluated", "evaluate_failed", "methods_listed", "methods_failed",
                     "text_fetched", "text_failed", "gas_listed", "gas_failed", "gas_set",
                     "gas_set_failed", "options_listed", "options_failed", "finished"):
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

    def deleteLater(self):  # pylint: disable=invalid-name
        pass

    def add_script(self, spec):
        self.requests.append(("add", spec))
        return next(self._ids)

    def update_script(self, script_id, spec):
        self.requests.append(("update", script_id, spec))

    def remove_script(self, script_id):
        self.requests.append(("remove", script_id))

    def run_now(self, script_id, args=None):
        self.requests.append(("run", script_id, args))

    def script_options(self, script_id, name):
        self.requests.append(("options", script_id, name))
        return len(self.requests)

    def inspect(self, target):
        self.requests.append(("inspect", target))
        return len(self.requests)

    def gas_attributes(self):
        self.requests.append(("gas_attributes",))
        return len(self.requests)

    def gas_set_base(self, name, value):
        self.requests.append(("gas_set_base", name, value))
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


@pytest.mark.parametrize("language, suffix", [("en", "(copy)"), ("de", "(Kopie)")])
def test_context_menu_creates_a_localized_script_copy(qt_app, monkeypatch, language, suffix):
    # pylint: disable=import-outside-toplevel,protected-access,unused-argument
    from PyQt6.QtCore import QPoint  # pylint: disable=import-error,no-name-in-module

    from hermes.features.scripts import ScriptParam
    from hermes.ui.widgets import scripts

    class FakeAction:
        def __init__(self, label, callback):
            self.label = label
            self.callback = callback
            self.enabled = True

        def setEnabled(self, enabled):
            self.enabled = enabled

    class FakeMenu:
        instances = []

        def __init__(self, _parent):
            self.actions = []
            self.instances.append(self)

        def addAction(self, _icon, label, callback):
            action = FakeAction(label, callback)
            self.actions.append(action)
            return action

        def exec(self, _position):
            pass

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(scripts, "QMenu", FakeMenu)
    monkeypatch.setattr(scripts, "add_delete_action", lambda *_args: None)
    monkeypatch.setattr(scripts, "add_copy_actions", lambda *_args: None)
    translations = load_translations(language)
    panel = scripts.ScriptPanel(translations, language=language)
    spec = ScriptSpec("return ctx.lang;", "repeat", 3, True, "Language", (
        ScriptParam("choice", "Choice", "choice", "en", 'return ["en", "de"];'),
    ))
    try:
        panel.set_process(4242)
        original_id = panel.add_script(spec)
        worker = panel._worker
        position = panel.table.visualItemRect(panel.table.item(0, 0)).center()
        panel._show_context_menu(QPoint(position))

        menu = FakeMenu.instances[-1]
        assert [action.label for action in menu.actions] == [
            translations["scripts.edit"], translations["scripts.run"],
            translations["scripts.pause"], translations["scripts.copy"],
        ]
        menu.actions[-1].callback()

        copied_id = panel._selected_id()
        copied = panel._scripts[copied_id]
        assert copied_id != original_id
        assert copied["spec"] == ScriptSpec(
            spec.source, spec.mode, spec.seconds, spec.enabled, f"Language {suffix}", spec.params)
        assert copied["runs"] == 0 and copied["last"] == "" and copied["options"] == {}
        assert worker.requests[-1] == ("add", copied["spec"])
    finally:
        panel.shutdown()
        panel.deleteLater()


def test_scripts_show_disconnected_when_their_session_ends(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets import scripts

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    panel = scripts.ScriptPanel(load_translations("de"))
    try:
        panel.set_process(4242)
        running = panel.add_script(ScriptSpec("return 1;", label="Läuft"))
        paused = panel.add_script(ScriptSpec("return 2;", label="Pausiert", enabled=False))
        worker = panel._worker
        worker.script_added.emit(running)
        worker.script_added.emit(paused)
        assert panel.table.item(0, 2).text() == "Aktiv"

        # The session is ended in the sessions window: the worker just finishes.
        worker.finished.emit()
        assert panel._worker is None
        assert [panel.table.item(row, 2).text() for row in range(2)] == ["Getrennt"] * 2
        panel.table.selectRow(1)
        assert not panel.btn_run.isEnabled()
        assert panel.btn_toggle.text() == "Fortsetzen"

        # Resuming starts it anew in a new session, running.
        panel.btn_toggle.click()
        restarted = panel._worker
        assert restarted is not worker
        assert restarted.requests[-1][0] == "add" and restarted.requests[-1][1].enabled
        assert panel.table.item(0, 2).text() == "Getrennt"  # the other one stays
    finally:
        panel.shutdown()
        panel.deleteLater()


def test_the_dialog_defines_parameters_and_their_options(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.features.scripts import ScriptParam
    from hermes.ui.widgets.scripts import ScriptDialog

    dialog = ScriptDialog(load_translations("de"))
    dialog.code_edit.setPlainText("return ctx.args.count;")
    editor = dialog.param_editor
    editor.btn_add.click()
    editor.table.item(0, 0).setText("item")
    editor.table.item(0, 1).setText("Item")
    editor.table.cellWidget(0, 2).setCurrentIndex(3)  # a choice: its code shows
    assert editor.options_edit.isEnabled() and "return" in editor.options_edit.toPlainText()
    editor.options_edit.setPlainText('return ["ItFo_Apple", "ItFo_Pear"];')
    editor.btn_add.click()
    editor.table.item(1, 0).setText("count")
    editor.table.cellWidget(1, 2).setCurrentIndex(1)
    editor.table.item(1, 3).setText("x")
    assert not editor.options_edit.isEnabled()  # a number has no options

    # A default that is no number keeps it open, on the parameters' tab.
    dialog.tabs.setCurrentIndex(0)
    dialog._accept()
    assert dialog.spec is None and "count" in dialog.error_label.text()
    assert dialog.tabs.currentIndex() == 1
    editor.table.item(1, 3).setText("20")
    dialog._accept()
    assert dialog.spec.params == (
        ScriptParam("item", "Item", "choice", "", 'return ["ItFo_Apple", "ItFo_Pear"];'),
        ScriptParam("count", "", "number", "20"))

    # Editing shows them again; removing the choice takes its code along.
    dialog = ScriptDialog(load_translations("de"), spec=dialog.spec)
    editor = dialog.param_editor
    assert editor.table.rowCount() == 2
    editor.table.selectRow(0)
    assert editor.options_edit.toPlainText() == 'return ["ItFo_Apple", "ItFo_Pear"];'
    editor.btn_remove.click()
    dialog._accept()
    assert [param.name for param in dialog.spec.params] == ["count"]


def test_running_by_hand_asks_for_the_parameters(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PyQt6.QtWidgets import QDialog  # pylint: disable=import-error,no-name-in-module

    from hermes.features.scripts import ScriptParam
    from hermes.ui.widgets import scripts
    from hermes.ui.widgets.script_params import ScriptRunDialog

    translations = load_translations("de")
    spec = ScriptSpec("return 1;", label="Items", params=(
        ScriptParam("item", "Item", "choice", "ItFo_Apple", "return [];"),
        ScriptParam("count", "Anzahl", "number", "1"),
        ScriptParam("all", "Alle", "bool")))
    worker = _FakeScriptWorker(4242)
    cache = {}

    # The choice's options come from the game; the last run's values are preselected.
    dialog = ScriptRunDialog(translations, spec, worker=worker, script_id=7,
                             options_cache=cache, last={"item": "ItFo_Pear", "count": 5})
    assert worker.requests == [("options", 7, "item")]
    combo = dialog._inputs["item"]
    assert not combo.isEnabled()
    dialog._accept()
    assert dialog.args is None and dialog.error_label.text()  # nothing to pick yet
    worker.options_listed.emit(99, [{"label": "x", "value": "y"}])  # another request's
    assert not combo.isEnabled()
    options = [{"label": "Apfel", "value": "ItFo_Apple"}, {"label": "Birne", "value": "ItFo_Pear"},
               {"label": "ItFo_Plum", "value": "ItFo_Plum"}]
    worker.options_listed.emit(1, options)
    assert [combo.itemText(i) for i in range(3)] == [
        "Apfel  (ItFo_Apple)", "Birne  (ItFo_Pear)", "ItFo_Plum"]
    assert combo.currentText() == "Birne  (ItFo_Pear)"
    assert dialog._inputs["count"].text() == "5"
    dialog._inputs["all"].setChecked(True)
    dialog._accept()
    assert dialog.args == {"item": "ItFo_Pear", "count": 5, "all": True}

    # A text that is no option is refused; the options are asked for only once.
    dialog = ScriptRunDialog(translations, spec, worker=worker, script_id=7, options_cache=cache)
    assert len(worker.requests) == 1
    assert dialog._inputs["item"].currentText() == "Apfel  (ItFo_Apple)"  # the default
    dialog._inputs["item"].setEditText("Bir")
    dialog._accept()
    assert dialog.args is None and "Item" in dialog.error_label.text()
    dialog.close()

    # The panel runs it with the values typed in and keeps them for the next run.
    shown = []

    class _Accepting:  # pylint: disable=too-few-public-methods
        def __init__(self, _translations, spec, **kwargs):
            shown.append((spec, kwargs))
            self.args = {"item": "ItFo_Plum", "count": 2, "all": False}

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(scripts, "ScriptRunDialog", _Accepting)
    panel = scripts.ScriptPanel(translations)
    try:
        panel.set_process(4242)
        script_id = panel.add_script(spec)
        panel._worker.script_added.emit(script_id)
        panel.table.selectRow(0)
        panel.btn_run.click()
        assert panel._worker.requests[-1] == ("run", script_id, shown[0][0].default_args()
                                              | {"item": "ItFo_Plum", "count": 2})
        panel.btn_run.click()
        assert shown[1][1]["last"] == {"item": "ItFo_Plum", "count": 2, "all": False}
        assert shown[1][1]["options_cache"] is shown[0][1]["options_cache"]
    finally:
        panel.shutdown()
        panel.deleteLater()
