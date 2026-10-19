"""The native function monitor: calls of a test game's functions (see native_monitor.py)."""

import subprocess

import pytest

from hermes.features.native_monitor import (
    argument_name,
    change_numbers,
    parse_location,
    signed,
)

from tests.test_hooks import (  # pylint: disable=unused-import
    _Target,
    _wait_for,
    fixture_qt_app,
    fixture_translations,
    requires_target,
)


_TARGET_SOURCE = r"""
#include <stdio.h>
#include <string.h>
#include <unistd.h>

struct Hero {
    char name[16];
    int level;
    unsigned char strength;
    unsigned char dexterity;
    short hit_points;
};

static struct Hero hero = { "Imoen", 1, 10, 10, 8 };

/* noinline: each is a function of its own, as in a real game */
__attribute__((noinline)) int roll_abilities(struct Hero *self, int seed) {
    self->strength = (unsigned char)seed;
    self->dexterity = (unsigned char)(seed + 1);
    return self->strength + self->dexterity;
}

__attribute__((noinline)) void rename_hero(struct Hero *self, const char *name) {
    strncpy(self->name, name, sizeof self->name - 1);
}

int main(void) {
    printf("%d 0\n", getpid());
    fflush(stdout);
    char cmd[16];
    int value;
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "roll") == 0)
            roll_abilities(&hero, value);
        else if (strcmp(cmd, "rename") == 0)
            rename_hero(&hero, value ? "Minsc" : "Boo");
        printf("%d 0\n", hero.strength);
        fflush(stdout);
    }
    return 0;
}
"""


def _build(tmp_path):
    (tmp_path / "game.c").write_text(_TARGET_SOURCE)
    binary = tmp_path / "game"
    subprocess.run(["gcc", "-O1", str(tmp_path / "game.c"), "-o", str(binary)], check=True)
    return binary


def test_locations_are_addresses_module_offsets_or_names():
    assert parse_location(" 0x55AAe4095790 ") == {"address": "0x55aae4095790"}
    assert parse_location("BaldursGate+0x606e50") == {"module": "BaldursGate",
                                                      "offset": 0x606e50}
    assert parse_location("libc.so.6 + 16") == {"module": "libc.so.6", "offset": 16}
    assert parse_location("_ZN13CDerivedStatsaSERKS_") == {"name": "_ZN13CDerivedStatsaSERKS_"}
    with pytest.raises(ValueError):
        parse_location("  ")


def test_arguments_values_and_changes_read_as_numbers():
    assert [argument_name(i, "linux") for i in (0, 5, 6)] == ["rdi", "r9", "arg6"]
    assert [argument_name(i, "windows") for i in (0, 3, 4)] == ["rcx", "r9", "arg4"]
    assert signed("0xffffffffffffffff") == -1
    assert signed("0x11") == 17
    assert change_numbers("0e", "11") == (14, 17)
    assert change_numbers("0800", "1000") == (8, 16)
    assert change_numbers("00" * 9, "11" * 9) is None


@requires_target
def test_native_monitor_logs_arguments_result_caller_and_changes(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.native_monitor import NativeMonitorWorker

    target = _Target(_build(tmp_path))
    worker = NativeMonitorWorker(target.pid)
    added, removed, calls, counts, failed = {}, [], [], {}, []
    worker.added.connect(added.__setitem__)
    worker.removed.connect(removed.append)
    worker.calls.connect(lambda logged, numbers: (calls.extend(logged), counts.update(numbers)))
    worker.add_failed.connect(lambda monitor_id, message: failed.append((monitor_id, message)))
    worker.command_failed.connect(lambda action, message: failed.append((action, message)))
    worker.worker_failed.connect(lambda message: failed.append(("worker", message)))

    def wait(predicate):
        _wait_for(qt_app, lambda: predicate() or failed)

    try:
        worker.start()
        worker.add(1, {"name": "roll_abilities"})
        worker.add(3, {"name": "no_such_function"})
        _wait_for(qt_app, lambda: 1 in added and failed)
        assert [monitor_id for monitor_id, _message in failed] == [3]
        assert added[1]["symbol"] == "roll_abilities"
        assert added[1]["where"].startswith("game+0x")
        failed.clear()
        # The same function by module and offset, a second time; then removed
        worker.add(2, parse_location(added[1]["where"]), arguments=2, dump=0)
        wait(lambda: 2 in added)
        assert added[2]["address"] == added[1]["address"]
        worker.remove(2)
        wait(lambda: removed)

        assert target.send("roll", 17) == (17, 0)
        wait(lambda: calls)
        call = calls[0]
        assert worker.platform == "linux"
        assert call["monitorId"] == 1
        assert call["thread"] == target.pid
        assert len(call["args"]) == 4
        assert call["args"][0]["text"] == "Imoen"  # the hero's name starts the struct
        assert int(call["args"][1]["value"], 16) == 17
        assert int(call["result"]["value"], 16) & 0xffffffff == 17 + 18
        assert call["callerWhere"].startswith("game+0x")
        assert call["callerSymbol"].startswith("main")
        # strength and dexterity are side by side, after the name and the level
        assert [(change["arg"], change["offset"], change["before"], change["after"])
                for change in call["changes"]] == [(0, 20, "0a0a", "1112")]
        assert int(call["changes"][0]["address"], 16) == int(call["args"][0]["value"], 16) + 20
        _wait_for(qt_app, lambda: counts.get(1, {}).get("calls") == 1)

        worker.add(4, {"name": "rename_hero"})
        wait(lambda: 4 in added)
        calls.clear()
        target.send("rename", 1)
        wait(lambda: calls)
        assert calls[0]["args"][1]["text"] == "Minsc"
        assert calls[0]["changes"][0]["offset"] == 0
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_native_monitor_window_shows_calls_and_changed_fields(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.features.native_trace import NativeFunction
    from hermes.ui.widgets.native_monitor import NativeMonitorDialog

    target = _Target(_build(tmp_path))
    dialog = NativeMonitorDialog(translations, target.pid)
    shown = []
    dialog.memory_requested.connect(shown.append)
    try:
        dialog.location_input.setText("roll_abilities")
        dialog.add_button.click()
        dialog.add_button.click()  # monitored once only
        _wait_for(qt_app, lambda: dialog.functions_table.item(0, 4).text()
                  == translations["native_monitor.status_function"])
        assert dialog.functions_table.rowCount() == 1
        location = dialog.functions_table.item(0, 1).text().split()[0]
        # The trace hands over NativeFunctions: the same function, not twice
        module, offset = location.split("+")
        dialog.add_function(NativeFunction(0, module, int(offset, 16), "roll_abilities"))
        assert dialog.functions_table.rowCount() == 2
        _wait_for(qt_app, lambda: dialog.functions_table.rowCount() == 1)
        assert dialog.status_label.text() == translations["native_monitor.already"].format(
            function="roll_abilities")

        dialog.location_input.setText("rename_hero")
        dialog.add_button.click()
        _wait_for(qt_app, lambda: dialog.functions_table.rowCount() == 2
                  and dialog.functions_table.item(1, 4).text()
                  == translations["native_monitor.status_function"])
        target.send("roll", 15)
        target.send("tick", 0)  # no function of interest
        _wait_for(qt_app, lambda: dialog.log.rowCount() == 1)
        assert dialog.functions_table.item(0, 2).text() == "1"
        assert dialog.log.data(dialog.log.index(0, 2)) == "roll_abilities"
        assert 'rdi=0x' in dialog.log.data(dialog.log.index(0, 3))
        assert 'rsi=15' in dialog.log.data(dialog.log.index(0, 3))
        assert dialog.log.data(dialog.log.index(0, 4)) == "31"
        assert dialog.log.data(dialog.log.index(0, 6)) == "1"

        dialog.log_view.selectRow(0)
        assert dialog.values_table.item(0, 0).text() == "rdi"
        assert dialog.values_table.item(0, 3).text() == '"Imoen"'
        assert dialog.changes_table.rowCount() == 1
        assert dialog.changes_table.item(0, 1).text() == "+0x14"
        assert dialog.changes_table.item(0, 5).text() == "10 10 → 15 16"
        dialog.changes_table.selectRow(0)
        dialog.memory_button.click()
        assert shown == [int(dialog.changes_table.item(0, 2).text(), 16)]

        # Removing a function stops its log; the other one goes on.
        dialog.functions_table.selectRow(0)
        dialog.remove_button.click()
        _wait_for(qt_app, lambda: dialog.functions_table.rowCount() == 1)
        target.send("roll", 3)
        target.send("rename", 0)
        _wait_for(qt_app, lambda: dialog.log.rowCount() == 2)
        assert dialog.log.data(dialog.log.index(1, 2)) == "rename_hero"
        dialog.changes_check.setChecked(True)
        assert dialog.log.rowCount() == 2  # both changed the hero
    finally:
        dialog.close()
        target.close()


def _second_instruction(binary, function):
    """Distance of the second instruction of ``function`` in ``binary`` from its start."""
    listing = subprocess.run(["objdump", "-d", "--no-show-raw-insn", str(binary)],
                             check=True, capture_output=True, text=True).stdout
    body = listing.split(f"<{function}>:\n", 1)[1].split("\n\n", 1)[0]
    first, second = (int(line.split(":")[0].strip(), 16) for line in body.splitlines()[:2])
    return second - first


@requires_target
def test_an_address_inside_a_function_is_probed_as_an_instruction(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.native_monitor import FUNCTION, INSTRUCTION, NativeMonitorWorker

    binary = _build(tmp_path)
    distance = _second_instruction(binary, "roll_abilities")
    target = _Target(binary)
    worker = NativeMonitorWorker(target.pid)
    added, calls, failed = {}, [], []
    worker.added.connect(added.__setitem__)
    worker.calls.connect(lambda logged, _counts: calls.extend(logged))
    worker.add_failed.connect(lambda monitor_id, message: failed.append((monitor_id, message)))
    worker.worker_failed.connect(lambda message: failed.append(("worker", message)))
    try:
        worker.start()
        # Where the function is: its first instruction, chosen as an instruction
        worker.add(0, {"name": "roll_abilities"}, kind=INSTRUCTION)
        _wait_for(qt_app, lambda: 0 in added or failed)
        assert added[0]["kind"] == INSTRUCTION and "function" not in added[0]
        inside = int(added[0]["address"], 16) + distance
        # Right behind a hooked address: Frida would patch into its jump
        worker.add(5, {"address": hex(inside)})
        _wait_for(qt_app, lambda: failed)
        assert failed[0][0] == 5 and "too close" in failed[0][1]
        failed.clear()
        worker.remove(0)
        worker.add(1, {"address": hex(inside)})
        _wait_for(qt_app, lambda: 1 in added or failed)
        assert not failed
        assert added[1]["kind"] == INSTRUCTION
        assert added[1]["functionName"] == "roll_abilities"
        assert added[1]["symbol"].startswith("roll_abilities+0x")
        # The game computes as before: its stack was left alone.
        for value in (12, 13, 14):
            assert target.send("roll", value) == (value, 0)
        _wait_for(qt_app, lambda: len(calls) == 3)
        registers = dict(calls[0]["registers"])
        assert int(registers["rsi"]["value"], 16) == 12
        assert registers["rdi"]["text"] == "Imoen"
        assert "args" not in calls[0]

        # The enclosing function, as the answer names it: its start is
        # patched as far as the probe (a short function), so not both.
        worker.add(2, {"address": added[1]["function"]}, kind=FUNCTION)
        _wait_for(qt_app, lambda: failed)
        assert failed[0][0] == 2 and "too close" in failed[0][1]
        failed.clear()
        worker.remove(1)
        worker.add(3, {"address": added[1]["function"]}, kind=FUNCTION)
        _wait_for(qt_app, lambda: 3 in added or failed)
        assert added[3]["kind"] == FUNCTION and "function" not in added[3]
        calls.clear()
        assert target.send("roll", 9) == (9, 0)
        _wait_for(qt_app, lambda: calls)
        assert int(calls[0]["result"]["value"], 16) & 0xffffffff == 9 + 10
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_window_offers_the_function_around_an_instruction(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel
    from hermes.ui.widgets.native_monitor import NativeMonitorDialog

    binary = _build(tmp_path)
    distance = _second_instruction(binary, "roll_abilities")
    target = _Target(binary)
    dialog = NativeMonitorDialog(translations, target.pid)
    table = dialog.functions_table

    def status(row):
        item = table.item(row, 4)
        return item.text() if item is not None else ""

    try:
        # Where the function starts: a probe there, removed again
        dialog.kind_combo.setCurrentIndex(2)  # a single instruction
        dialog.location_input.setText("rename_hero")
        dialog.add_button.click()
        _wait_for(qt_app, lambda: status(0) == translations["native_monitor.status_instruction"])
        table.selectRow(0)
        assert not dialog.function_button.isEnabled()  # the function's start
        dialog.location_input.setText("roll_abilities")
        dialog.add_button.click()
        _wait_for(qt_app, lambda: status(1) == translations["native_monitor.status_instruction"])
        start = int(table.item(1, 1).text().split("(")[1].rstrip(")"), 16)
        table.selectRow(1)
        dialog.remove_button.click()
        _wait_for(qt_app, lambda: table.rowCount() == 1)

        dialog.kind_combo.setCurrentIndex(0)  # automatic
        dialog.location_input.setText(hex(start + distance))
        dialog.add_button.click()
        _wait_for(qt_app, lambda: status(1) == translations["native_monitor.status_instruction"])
        assert table.item(1, 0).text().startswith("roll_abilities+0x")
        assert dialog.status_label.text() == translations[
            "native_monitor.inside_function"].format(function="roll_abilities")
        table.selectRow(1)
        assert dialog.function_button.isEnabled()
        # The function's start is too close to the probe right behind it
        dialog.function_button.click()
        _wait_for(qt_app, lambda: status(2) == translations["native_monitor.status_failed"])
        table.selectRow(1)
        dialog.remove_button.click()
        _wait_for(qt_app, lambda: table.rowCount() == 2)
        dialog.location_input.setText("roll_abilities")
        dialog.add_button.click()
        _wait_for(qt_app, lambda: status(2) == translations["native_monitor.status_function"])
        assert table.item(2, 0).text() == "roll_abilities"

        target.send("roll", 5)
        target.send("rename", 0)
        _wait_for(qt_app, lambda: dialog.log.rowCount() == 2)
        results = [dialog.log.data(dialog.log.index(row, 4)) for row in range(2)]
        assert results == ["11", "—"]  # the function returned 5 + 6; a probe has no result
        probe = 1
        dialog.log_view.selectRow(probe)
        names = [dialog.values_table.item(row, 0).text()
                 for row in range(dialog.values_table.rowCount())]
        assert names[:6] == ["rax", "rbx", "rcx", "rdx", "rsi", "rdi"]
    finally:
        dialog.close()
        target.close()
