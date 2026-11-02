"""Window sizes, splitters and column widths kept between runs (hermes.ui.window_state)."""

import os

import pytest

from hermes.ui.localization import load_translations


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module
    return QApplication.instance() or QApplication([])


@pytest.fixture(name="keeper")
def fixture_keeper(qt_app):
    from hermes.ui.window_state import keep_window_state  # pylint: disable=import-outside-toplevel

    keeper = keep_window_state(qt_app)
    yield keeper
    qt_app.removeEventFilter(keeper)


def _settle(qt_app):
    # Column sizes are adjusted and saved on timers.
    # pylint: disable=import-outside-toplevel
    import time

    from hermes.ui.widgets.table import SAVE_DELAY_MS
    deadline = time.monotonic() + (SAVE_DELAY_MS + 200) / 1000
    while time.monotonic() < deadline:
        qt_app.processEvents()


def test_windows_get_their_size_splitters_and_column_widths_back(qt_app, keeper):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets.scripts import ScriptPanel
    from PySide6.QtWidgets import QDialog, QVBoxLayout  # pylint: disable=import-error,no-name-in-module

    translations = load_translations("en")

    def open_window():
        # A window of Hermes' own (the filter keeps only those) with the
        # scripts panel: a table and a splitter.
        window = QDialog()
        window.__class__ = type("ScriptsWindow", (QDialog,), {"__module__": "hermes.test"})
        layout = QVBoxLayout(window)
        panel = ScriptPanel(translations)
        layout.addWidget(panel)
        window.show()
        _settle(qt_app)
        return window, panel

    window, panel = open_window()
    window.resize(760, 480)  # wider than the panel's buttons, within the 800 px offscreen screen
    splitter = panel.table.parentWidget()
    splitter.setSizes([100, 300])
    header = panel.table.horizontalHeader()
    header.resizeSection(1, 211)  # as if the user dragged it
    _settle(qt_app)
    sizes = splitter.sizes()
    window.close()
    _settle(qt_app)

    window, panel = open_window()
    header = panel.table.horizontalHeader()
    assert (window.width(), window.height()) == (760, 480)
    assert panel.table.parentWidget().sizes() == sizes
    assert header.sectionSize(1) == 211
    window.close()


def test_modal_dialogs_get_only_their_size_back(qt_app, keeper):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QSize  # pylint: disable=import-error,no-name-in-module

    from hermes.ui import window_state
    from hermes.ui.widgets.scripts import ScriptDialog

    store = window_state.settings()
    store.setValue("windows/ScriptDialog/size", QSize(812, 633))
    dialog = ScriptDialog(load_translations("de"))
    dialog.setModal(True)
    dialog.show()
    qt_app.processEvents()
    assert (dialog.width(), dialog.height()) == (812, 633)
    dialog.resize(700, 600)
    dialog.close()
    qt_app.processEvents()
    assert window_state.settings().value("windows/ScriptDialog/size") == QSize(700, 600)


def test_column_widths_of_another_layout_are_ignored(qt_app):
    # pylint: disable=unused-argument,import-outside-toplevel
    from hermes.ui import window_state

    window_state.save_columns("W/T0", 3, {1: 150})
    assert window_state.load_columns("W/T0", 3) == {1: 150}
    assert window_state.load_columns("W/T0", 4) == {}
    window_state.settings().setValue("columns/W/T1", "not json")
    assert window_state.load_columns("W/T1", 3) == {}
