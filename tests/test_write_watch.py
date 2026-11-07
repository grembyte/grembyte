"""Tests for write-watchpoint configuration."""

import pytest

from hermes.features.write_watch import (
    _build_agent_source,
    watch_size_for_type,
)


@pytest.mark.parametrize(
    ("value_type", "expected_size"),
    [
        ("int8", 1),
        ("uint16", 2),
        ("int32", 4),
        ("float", 4),
        ("int64", 8),
        ("double", 8),
    ],
)
def test_watch_size_matches_scan_value_type(value_type, expected_size):
    assert watch_size_for_type(value_type) == expected_size


def test_watch_size_rejects_unknown_value_type():
    with pytest.raises(ValueError, match="Unsupported watch data type"):
        watch_size_for_type("string")


def test_agent_installs_exception_handler_before_watchpoints():
    source = _build_agent_source(0x1234, 4)

    assert "ptr('0x1234')" in source
    assert "const targetSize = 4;" in source
    assert source.index("Process.setExceptionHandler") < source.index(
        "thread.setHardwareWatchpoint"
    )


def test_dialog_shows_the_time_of_the_last_write():
    # pylint: disable=import-outside-toplevel
    import os
    from datetime import datetime

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.write_watch import WriteWatchDialog

    _app = QApplication.instance() or QApplication([])
    dialog = WriteWatchDialog("0x1000", 4, load_translations("de"))
    first = datetime(2026, 10, 17, 14, 3, 27, 418000).timestamp() * 1000
    hit = {"pc": "0x401077", "site": "game+0x1077", "value": "01", "threadId": 7}

    dialog.add_hits([{**hit, "time": first}])
    dialog.add_hits([{**hit, "count": 2, "time": first + 1500}])

    table = dialog.results_table
    assert table.horizontalHeaderItem(7).text() == "Zuletzt"
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "3"
    assert table.item(0, 7).text() == "14:03:28.918"


def test_dialog_shows_reads_and_writes_and_hooks_only_writes():
    # pylint: disable=import-outside-toplevel,protected-access
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.write_watch import WriteWatchDialog

    _app = QApplication.instance() or QApplication([])
    dialog = WriteWatchDialog("0x1000", 4, load_translations("de"), reads=True)
    dialog.add_hits([
        {"pc": "0x401077", "site": "game+0x1077", "read": True, "write": True},
        {"pc": "0x402000", "site": "game+0x2000", "read": True, "write": False},
    ])

    table = dialog.results_table
    assert dialog.windowTitle() == "Lese- und Schreibzugriffe auf 0x1000"
    assert [table.horizontalHeaderItem(column).text() for column in (3, 4)] == [
        "Lesend", "Schreibend"]
    checked = lambda row, column: (  # pylint: disable=unnecessary-lambda-assignment
        table.item(row, column).checkState() == Qt.CheckState.Checked)
    assert [checked(0, 3), checked(0, 4), checked(1, 3), checked(1, 4)] == [
        True, True, True, False]
    assert not table.item(0, 3).flags() & Qt.ItemFlag.ItemIsUserCheckable
    table.selectRow(0)
    assert dialog.hook_button.isEnabled()
    table.selectRow(1)
    assert not dialog.hook_button.isEnabled()
    # Reads and writes alike can be monitored: the instruction after the access
    assert dialog.monitor_button.isEnabled()
    monitored = []
    dialog.monitor_requested.connect(monitored.append)
    dialog.monitor_button.click()
    assert monitored == [0x402000]


def test_dialog_names_jit_code_by_its_csharp_method():
    # pylint: disable=import-outside-toplevel
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.write_watch import WriteWatchDialog

    _app = QApplication.instance() or QApplication([])
    dialog = WriteWatchDialog("0x1000", 4, load_translations("de"))
    requested = []
    dialog.hook_requested.connect(lambda pc, site: requested.append((pc, site)))
    dialog.add_hits([{
        "pc": "0x7f0012345678", "site": "0x7f0012345678", "write": True,
        "method": "PlayerInventory:AddCurrency+0x3a",
    }])

    site = dialog.results_table.item(0, 2)
    assert site.text() == "PlayerInventory:AddCurrency+0x3a"
    assert site.toolTip() == "0x7f0012345678"
    dialog.results_table.selectRow(0)
    dialog._request_hook()  # pylint: disable=protected-access
    assert requested == [(0x7f0012345678, "PlayerInventory:AddCurrency+0x3a")]
