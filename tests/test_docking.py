"""Dialogs docked in the main window as tabs (grembyte.ui.widgets.docking)."""

# pylint: disable=import-outside-toplevel,import-error,no-name-in-module

import pytest

from tests.test_scripts import fixture_qt_app  # pylint: disable=unused-import


@pytest.fixture(name="window")
def _window(qt_app):
    from PySide6.QtWidgets import QMainWindow

    from grembyte.ui.widgets.docking import DockManager

    window = QMainWindow()
    window.dock_manager = DockManager(window)
    window.show()
    qt_app.processEvents()
    yield window
    window.close()


def _dialog(translations, title, parent):
    from PySide6.QtWidgets import QDialog, QVBoxLayout

    from grembyte.ui.widgets.docking import PinButton

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.pin_button = PinButton(translations)
    QVBoxLayout(dialog).addWidget(dialog.pin_button)
    return dialog


@pytest.fixture(name="translations")
def _translations():
    from grembyte.ui.localization import load_translations

    return load_translations("en")


def test_dialogs_dock_as_tabs_and_float_again(window, translations, qt_app):
    from grembyte.ui.widgets.docking import RIGHT

    manager = window.dock_manager
    gold = _dialog(translations, "Gold", window)
    mana = _dialog(translations, "Mana", window)
    manager.add(gold, "pointer_scan", RIGHT)
    assert gold.isWindow() and gold.isVisible()  # nothing remembered yet

    gold.pin_button.click()
    host = manager.host(RIGHT)
    assert host.dialogs() == [gold] and not gold.isWindow() and host.isVisible()
    assert gold.pin_button.isChecked() and gold.pin_button.text() == translations["dock.unpin"]
    assert host.windowTitle() == "Gold"

    manager.add(mana, "pointer_scan", RIGHT)  # the next one follows: docked beside it
    assert host.dialogs() == [gold, mana] and host.dialog() is mana
    assert [host.tabs.tabText(i) for i in range(2)] == ["Gold", "Mana"]
    mana.setWindowTitle("Mana 2")  # the tab follows the title
    assert host.tabs.tabText(1) == "Mana 2" and host.windowTitle() == "Mana 2"

    manager.show(gold)
    assert host.dialog() is gold

    gold.pin_button.click()  # floats again; the next one would, too
    assert host.dialogs() == [mana] and gold.isWindow() and gold.isVisible()
    assert gold.parentWidget() is window and not gold.pin_button.isChecked()
    assert not manager.remembered("pointer_scan")

    mana.reject()  # finished: its tab goes, the empty dock hides
    qt_app.processEvents()
    assert host.dialogs() == [] and not host.isVisible()


def test_tab_and_dock_close_buttons_close_the_dialogs(window, translations):
    from grembyte.ui.widgets.docking import BOTTOM

    manager = window.dock_manager
    dialogs = [_dialog(translations, name, window) for name in ("Trace", "Log", "Monitor")]
    finished, cleaned = [], []
    for dialog in dialogs:
        dialog.finished.connect(lambda _result, d=dialog: finished.append(d))
        # The tools stop their workers in closeEvent
        dialog.closeEvent = lambda event, d=dialog: (cleaned.append(d), event.accept())
        manager.add(dialog, "trace", BOTTOM, dock=True)
    host = manager.host(BOTTOM)
    assert host.dialogs() == dialogs and host.dialog() is dialogs[2]

    host.tabs.tabCloseRequested.emit(1)  # a tab behind: its dialog is hidden
    assert finished == cleaned == [dialogs[1]]
    assert host.dialogs() == [dialogs[0], dialogs[2]]
    host.close()
    assert finished == cleaned == [dialogs[1], dialogs[0], dialogs[2]]
    assert host.dialogs() == []


def test_a_docked_dialog_goes_with_the_window_it_came_from(window, translations, qt_app):
    from PySide6.QtWidgets import QDialog

    from grembyte.ui.widgets.docking import RIGHT, find_dock_manager

    monitor = QDialog(window)
    inspector = _dialog(translations, "Inspector", monitor)
    manager = find_dock_manager(inspector)
    assert manager is window.dock_manager
    finished = []
    inspector.finished.connect(finished.append)
    manager.add(inspector, "inspector", RIGHT, dock=True)
    assert manager.host(RIGHT).dialogs() == [inspector]

    monitor.reject()
    qt_app.processEvents()
    assert finished and manager.host(RIGHT).dialogs() == []


def test_inspectors_open_next_to_a_docked_one(window, translations, qt_app):
    from grembyte.ui.widgets.docking import RIGHT
    from grembyte.ui.widgets.inspection import Inspections
    from tests.test_scripts import _FakeScriptWorker

    inspections = Inspections(translations, _FakeScriptWorker(1), window)
    inspections.inspect({"address": "0x1000"})
    first = inspections.inspectors[-1]
    assert first.isWindow()  # nothing remembered: a window of its own
    host = window.dock_manager.host(RIGHT)

    first.pin_button.click()
    assert host.dialogs() == [first]
    inspections.inspect({"address": "0x2000"}, origin=first)  # a field's object
    second = inspections.inspectors[-1]
    assert host.dialogs() == [first, second] and host.dialog() is second
    assert second.depth == 2

    second.pin_button.click()  # floated; the next one opens floating too
    inspections.inspect({"address": "0x3000"}, origin=second)
    third = inspections.inspectors[-1]
    assert third.isWindow() and host.dialogs() == [first]
    for inspector in (first, second, third):
        inspector.reject()
    qt_app.processEvents()
    assert host.dialogs() == []


def test_the_tools_that_stay_in_use_can_be_docked():
    import inspect

    from grembyte.ui.widgets import (gas_attributes, log_view, memory_view, method_monitor,
                                   native_monitor, native_trace, object_browser,
                                   object_inspector, pointer_scan, trace, write_watch)

    dialogs = (pointer_scan.PointerScanDialog, object_inspector.ObjectInspectorDialog,
               object_browser.ObjectBrowserDialog, write_watch.WriteWatchDialog,
               memory_view.MemoryViewerDialog, gas_attributes.GasAttributesDialog,
               trace.TraceDialog, native_trace.NativeTraceDialog,
               method_monitor.MethodMonitorDialog, native_monitor.NativeMonitorDialog,
               log_view.LogDialog)
    for dialog in dialogs:
        source = inspect.getsource(dialog)
        assert "self.pin_button = PinButton(" in source, dialog.__name__


def test_docked_inspectors_show_short_tab_titles(window, translations):
    from grembyte.ui.widgets.docking import RIGHT
    from grembyte.ui.widgets.inspection import Inspections
    from grembyte.ui.widgets.object_inspector import short_type_name
    from tests.test_scripts import _FakeScriptWorker

    assert short_type_name("Game.Combat+Health") == "Health"
    assert short_type_name("System.Collections.Generic.List<Game.Item>") == "List<Game.Item>"
    assert short_type_name("ABP_Hero_C") == "ABP_Hero_C"

    inspections = Inspections(translations, _FakeScriptWorker(1), window)
    inspections.inspect({"address": "0x1000"}, name="this")
    inspector = inspections.inspectors[-1]
    window.dock_manager.add(inspector, "inspector", RIGHT, dock=True)
    tabs = window.dock_manager.host(RIGHT).tabs
    assert tabs.tabText(0) == "this"  # nothing read yet

    inspector.show_contents({"name": "Game.Combat+Health", "address": "0x1000", "fields": []})
    assert tabs.tabText(0) == "Health"
    assert tabs.tabToolTip(0) == inspector.windowTitle()
    inspector.show_contents({"name": "Game.Combat+Health", "address": "0x1000", "fields": [],
                             "instanceName": "Companion_Eder(Clone)_0"})
    assert tabs.tabText(0) == "Companion_Eder(Clone)_0"
    inspector.reject()


def test_tools_close_with_the_game_but_log_and_pointer_scans_stay(qt_app, translations):
    # pylint: disable=protected-access
    from grembyte.ui import main_window
    from grembyte.ui.widgets.docking import BOTTOM, RIGHT

    window = main_window.MainWindow("en")
    try:
        manager = window.dock_manager
        window._open_log()
        log = window._log_dialog

        class PointerScan:  # pylint: disable=too-few-public-methods
            pid = "game"

            def set_process(self, pid):
                self.pid = pid

        pointer = _dialog(translations, "Pointer scan", window)
        pointer.set_process = PointerScan.set_process.__get__(pointer)
        window._pointer_dialogs[1] = pointer
        tools = {}
        for name, area in (("_objects_dialog", RIGHT), ("_gas_dialog", RIGHT),
                           ("_memory_viewer", RIGHT), ("_native_monitor", BOTTOM)):
            tools[name] = _dialog(translations, name, window)
            setattr(window, name, tools[name])
            manager.add(tools[name], name, area, dock=True)
        manager.add(pointer, "pointer_scan", RIGHT, dock=True)
        manager.add(log, "log", BOTTOM, dock=True)
        closed = []
        for name, dialog in tools.items():
            dialog.finished.connect(lambda _result, n=name: closed.append(n))

        window._detach_process()
        qt_app.processEvents()
        assert sorted(closed) == sorted(tools)
        assert all(getattr(window, name) is None for name in tools)
        assert manager.host(RIGHT).dialogs() == [pointer] and pointer.pid is None
        assert manager.host(BOTTOM).dialogs() == [log]
    finally:
        window.close()
