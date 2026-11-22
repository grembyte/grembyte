"""Tests for write-watchpoint configuration."""

import pytest

from grembyte.core.value_types import watch_size_for_type
from grembyte.features.write_watch import _build_agent_source

from tests.test_hooks import _Target, requires_target


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

    from grembyte.ui.localization import load_translations
    from grembyte.ui.widgets.write_watch import WriteWatchDialog

    _app = QApplication.instance() or QApplication([])
    dialog = WriteWatchDialog("0x1000", 4, load_translations("de"))
    first = datetime(2026, 10, 17, 14, 3, 27, 418000).timestamp() * 1000
    hit = {"pc": "0x401077", "site": "game+0x1077", "value": "01", "threadId": 7}

    dialog.add_hits([{**hit, "time": first}])
    dialog.add_hits([{**hit, "count": 2, "time": first + 1500}])

    table = dialog.results_table
    assert table.horizontalHeaderItem(8).text() == "Zuletzt"
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "3"
    assert table.item(0, 8).text() == "14:03:28.918"


def test_dialog_shows_reads_and_writes_and_hooks_only_writes():
    # pylint: disable=import-outside-toplevel,protected-access
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.localization import load_translations
    from grembyte.ui.widgets.write_watch import WriteWatchDialog

    _app = QApplication.instance() or QApplication([])
    dialog = WriteWatchDialog("0x1000", 4, load_translations("de"), reads=True)
    dialog.add_hits([
        {"pc": "0x401077", "site": "game+0x1077", "read": True, "write": True},
        {"pc": "0x402000", "site": "game+0x2000", "read": True, "write": False},
    ])

    table = dialog.results_table
    assert dialog.windowTitle() == "Lese- und Schreibzugriffe auf 0x1000"
    assert [table.horizontalHeaderItem(column).text() for column in (4, 5)] == [
        "Lesend", "Schreibend"]
    checked = lambda row, column: (  # pylint: disable=unnecessary-lambda-assignment
        table.item(row, column).checkState() == Qt.CheckState.Checked)
    assert [checked(0, 4), checked(0, 5), checked(1, 4), checked(1, 5)] == [
        True, True, True, False]
    assert not table.item(0, 4).flags() & Qt.ItemFlag.ItemIsUserCheckable
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

    from grembyte.ui.localization import load_translations
    from grembyte.ui.widgets.write_watch import WriteWatchDialog

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


def test_dialog_counts_callers_and_points_from_a_copy_routine_to_them():
    # pylint: disable=import-outside-toplevel
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.localization import load_translations
    from grembyte.ui.widgets.write_watch import WriteWatchDialog

    _app = QApplication.instance() or QApplication([])
    dialog = WriteWatchDialog("0x1000", 4, load_translations("en"))
    requested = []
    dialog.hook_requested.connect(lambda pc, site: requested.append(pc))
    copy = {"pc": "0x7000dc1e", "site": "VCRUNTIME140.dll+0x1dc1e", "write": True,
            "copyRoutine": "memcpy"}
    dialog.add_hits([
        {**copy, "callers": [{"address": "0x140001234", "site": "game.exe+0x1234", "count": 2}]},
        {"pc": "0x401077", "site": "game+0x1077", "write": True,
         "callers": [{"address": "0x402000", "site": "game+0x2000", "count": 1}]},
    ])
    dialog.add_hits([{**copy, "count": 3, "callers": [
        {"address": "0x140005678", "site": "game.exe+0x5678", "count": 3},
        {"address": "0x140001234", "site": "game.exe+0x1234", "count": 1},
        {"address": "0x140009000", "site": "game.exe+0x9000", "count": 1, "checked": False},
    ]}])

    table = dialog.results_table
    assert table.horizontalHeaderItem(3).text() == "Called from"
    assert table.item(0, 2).text() == "VCRUNTIME140.dll+0x1dc1e (memcpy)"
    assert table.item(0, 3).text() == "game.exe+0x1234 (+2)"
    assert table.item(0, 3).toolTip() == (
        "3× game.exe+0x1234\n3× game.exe+0x5678\n1× game.exe+0x9000 (?)")
    assert table.item(1, 3).text() == "game+0x2000"

    # A copy routine cannot be hooked; the hint points to the caller.
    table.selectRow(0)
    assert not dialog.hook_button.isEnabled()
    assert dialog.monitor_button.isEnabled()
    assert not dialog.copy_routine_label.isHidden()
    assert "memcpy" in dialog.copy_routine_label.text()
    dialog._request_hook()  # pylint: disable=protected-access
    assert not requested
    table.selectRow(1)
    assert dialog.hook_button.isEnabled()
    assert dialog.copy_routine_label.isHidden()


# A direct write in add_gold() and one through memcpy() in transfer().
_COPY_TARGET_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int32_t g_gold = 100;
int32_t g_source = 0;
volatile size_t g_size = sizeof(int32_t);
volatile int g_events;

__attribute__((noinline)) void add_gold(int32_t amount) { g_gold += amount; }
__attribute__((noinline)) void transfer(int32_t amount) {
    g_source = g_gold + amount;
    memcpy(&g_gold, &g_source, g_size);
    g_events++;  /* a call, not a jump */
}

int main(void) {
    char cmd[16];
    int value;
    printf("%d %p\n", getpid(), (void *)&g_gold);
    fflush(stdout);
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "add") == 0) add_gold(value);
        else transfer(value);
        printf("%d 0\n", g_gold);
        fflush(stdout);
    }
    return 0;
}
"""


# With -fno-plt gcc calls memcpy through its GOT entry, "call [rip+…]", as
# MSVC calls a DLL's memcpy through the import table (Lies of P).
@requires_target
@pytest.mark.parametrize("plt", ["-fplt", "-fno-plt"])
def test_agent_names_the_caller_and_copy_routines(tmp_path, plt):
    # pylint: disable=import-outside-toplevel,too-many-locals
    import subprocess
    import time

    import frida

    (tmp_path / "target.c").write_text(_COPY_TARGET_SOURCE)
    binary = tmp_path / "target"
    subprocess.run(["gcc", "-O2", "-fomit-frame-pointer", "-fno-builtin", plt, "-fPIE", "-pie",
                    str(tmp_path / "target.c"), "-o", str(binary)], check=True)
    listing = subprocess.run(["nm", "-S", str(binary)], check=True, capture_output=True,
                             text=True).stdout
    # name -> (start, size)
    symbols = {fields[-1]: (int(fields[0], 16), int(fields[1], 16))
               for fields in map(str.split, listing.splitlines()) if len(fields) == 4}

    target = _Target(binary)
    hits = []
    session = frida.attach(target.pid)
    try:
        script = session.create_script(_build_agent_source(target.address, 4))
        script.on("message", lambda message, _data: hits.extend(
            message.get("payload", {}).get("hits", [])))
        script.load()
        found = {}
        for command in ("add", "copy"):
            hits.clear()
            target.send(command, 1)
            deadline = time.monotonic() + 2
            while not hits and time.monotonic() < deadline:
                time.sleep(0.02)
            assert hits, f"write watch saw no write for {command}"
            found[command] = hits[0]
        script.exports_sync.stop()
    finally:
        session.detach()
        target.close()

    def caller_offset(hit):
        [caller] = hit["callers"]
        assert caller["checked"]
        module, offset = caller["site"].split("+")
        assert module == "target"
        return int(offset, 16)

    def inside(offset, name):
        start, size = symbols[name]
        return start < offset <= start + size

    assert "copyRoutine" not in found["add"]
    assert inside(caller_offset(found["add"]), "main")
    assert "mem" in found["copy"]["copyRoutine"]
    assert inside(caller_offset(found["copy"]), "transfer")
