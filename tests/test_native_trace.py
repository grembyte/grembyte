"""The native trace: Stalker follows a test game's threads (see native_trace.py)."""

import subprocess

from hermes.features.native_trace import (
    NativeFunction,
    TraceModule,
    apply_names,
    foreign_code,
    thread_activity,
)

from hermes.platform.linux import parse_maps
from tests.test_hooks import (  # pylint: disable=unused-import
    _Target,
    _wait_for,
    fixture_qt_app,
    fixture_translations,
    requires_target,
)


_TARGET_SOURCE = r"""
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static volatile int gold = 100;
static volatile int frames;

/* noinline: each is a function of its own, as in a real game */
__attribute__((noinline)) void frame(int x) { frames += x; }
__attribute__((noinline)) void spend_gold(int amount) { gold -= amount; }
__attribute__((noinline)) void buy_item(int price) { spend_gold(price); }

static void *render(void *unused) {
    (void)unused;
    for (;;) {
        frame(1);
        usleep(1000);
    }
    return NULL;
}

int main(void) {
    pthread_t thread;
    pthread_create(&thread, NULL, render, NULL);
    printf("%d 0\n", getpid());
    fflush(stdout);
    char cmd[16];
    int value;
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "tick") == 0)
            frame(value);
        else if (strcmp(cmd, "buy") == 0)
            buy_item(value);
        printf("%d 0\n", gold);
        fflush(stdout);
    }
    return 0;
}
"""


def _build(tmp_path):
    (tmp_path / "game.c").write_text(_TARGET_SOURCE)
    binary = tmp_path / "game"
    subprocess.run(["gcc", "-O1", "-pthread", str(tmp_path / "game.c"), "-o", str(binary)],
                   check=True)
    return binary


class _Recorder:
    """A NativeTraceWorker and what its signals said."""

    def __init__(self, qt_app, worker):
        self.app = qt_app
        self.worker = worker
        self.inventory = None
        self.started = []
        self.began = []
        self.recorded = []
        self.stopped = []
        self.failed = []
        worker.inventory_found.connect(
            lambda threads, modules, platform: setattr(self, "inventory",
                                                       (threads, modules, platform)))
        worker.started.connect(self.started.append)
        worker.began.connect(self.began.append)
        worker.recorded.connect(self.recorded.append)
        worker.stopped.connect(lambda: self.stopped.append(True))
        worker.command_failed.connect(lambda action, message: self.failed.append((action, message)))
        worker.worker_failed.connect(lambda message: self.failed.append(("worker", message)))

    def wait(self, predicate):
        _wait_for(self.app, lambda: predicate() or self.failed)
        assert not self.failed

    def record(self, target, phase, *commands):
        self.worker.begin(phase)
        self.wait(lambda: phase in self.began)
        self.began.clear()
        for command, value in commands:
            target.send(command, value)
        count = len(self.recorded)
        self.worker.finish()
        self.wait(lambda: len(self.recorded) > count)
        return self.recorded[-1]


@requires_target
def test_native_trace_finds_what_a_scenario_calls(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.native_trace import NativeTraceWorker
    from hermes.features.trace import NEW, SAME, analyze, recording_calls

    target = _Target(_build(tmp_path))
    worker = NativeTraceWorker(target.pid)
    recorder = _Recorder(qt_app, worker)
    try:
        worker.start()
        worker.inventory()
        recorder.wait(lambda: recorder.inventory is not None)
        threads, modules, platform = recorder.inventory
        assert platform == "linux"
        assert {thread.id for thread in threads} >= {target.pid}
        assert all(thread.load is not None for thread in threads)
        # The render thread draws frames; the main thread waits for commands.
        active = {thread.id: thread.active for thread in threads}
        assert active[target.pid] is False
        assert list(active.values()).count(True) == 1
        assert [module.name for module in modules if module.game] == ["game"]

        worker.trace([thread.id for thread in threads], ["game"])
        recorder.wait(lambda: recorder.started)
        assert recorder.started[0] == {"threads": len(threads), "failed": 0, "modules": 1}

        functions = []
        baseline = recorder.record(target, "baseline", ("tick", 1), ("tick", 2))
        apply_names(functions, baseline)
        assert baseline["events"] == []
        scenario = recorder.record(target, "scenario", ("tick", 3), ("buy", 7))
        apply_names(functions, scenario)
        assert target.send("tick", 0) == (93, 0)  # the game still runs, and bought
        assert (baseline["saturated"], scenario["saturated"]) == (0, 0)  # no calls lost

        rows = [row for row in analyze(scenario, baseline) if functions[row.method].module == "game"]
        verdicts = {functions[row.method].label: (row.verdict, row.calls) for row in rows}
        assert verdicts["buy_item"] == (NEW, 1)
        assert verdicts["spend_gold"] == (NEW, 1)
        assert verdicts["frame"][0] == SAME
        # The first calls of what is new, on the thread that bought
        calls = [call for call in recording_calls(scenario)
                 if functions[call.method].module == "game"]
        assert sorted(functions[call.method].label for call in calls) == ["buy_item",
                                                                         "spend_gold"]
        assert {call.thread for call in calls} == {target.pid}
        buy = next(function for function in functions if function.label == "buy_item")
        assert buy.location.startswith("game+0x")

        # Another trace after this one: the agent is loaded afresh.
        worker.stop()
        recorder.wait(lambda: recorder.stopped)
        worker.trace([target.pid], ["game"])
        recorder.wait(lambda: len(recorder.started) == 2)
        assert recorder.started[1]["threads"] == 1
        scenario = recorder.record(target, "scenario", ("buy", 1))
        worker.stop()
        recorder.wait(lambda: len(recorder.stopped) == 2)
        # The main thread waits for commands in code Stalker made: once it
        # has returned from there, the agents unload without waiting.
        assert target.send("tick", 0) == (92, 0)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_native_trace_window_shows_what_the_scenario_called(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.widgets.native_trace import NativeTraceDialog

    target = _Target(_build(tmp_path))
    dialog = NativeTraceDialog(translations, target.pid)
    shown = []
    dialog.memory_requested.connect(shown.append)

    def record(button, *commands):
        button.click()
        _wait_for(qt_app, lambda: dialog.recording_label.text()
                  != translations["trace.recording_starting"])
        for command, value in commands:
            target.send(command, value)
        button.click()
        _wait_for(qt_app, lambda: dialog._state == 3)  # ready again

    try:
        _wait_for(qt_app, lambda: dialog.module_list.count() > 0)
        assert dialog.module_list.item(0).text() == "game"
        # Only the render thread is active; buying happens on the main thread.
        threads = dialog.thread_list
        checked = [threads.item(row).checkState() == Qt.CheckState.Checked
                   for row in range(threads.count())]
        assert checked.count(True) == 1
        for row in range(threads.count()):
            threads.item(row).setCheckState(Qt.CheckState.Checked)
        assert not dialog.baseline_button.isEnabled()
        dialog.trace_button.click()
        _wait_for(qt_app, lambda: dialog._state == 3 or "Fehler" in dialog.status_label.text())
        assert dialog.trace_button.text() == translations["native_trace.stop"]

        record(dialog.baseline_button, ("tick", 1))
        record(dialog.scenario_button, ("buy", 5))
        labels = [dialog.row_model.data(dialog.row_model.index(row, 1))
                  for row in range(dialog.row_model.rowCount())]
        assert sorted(labels) == ["buy_item", "spend_gold"]
        assert dialog.row_model.data(dialog.row_model.index(0, 5)) == "neu"
        assert dialog.sequence_model.data(dialog.sequence_model.index(0, 3)).startswith("game+0x")

        dialog.rows_view.selectRow(0)
        function = dialog.selected_function()
        assert dialog.selection_label.text() == function.signature
        dialog.memory_button.click()
        assert shown == [function.address]

        dialog.trace_button.click()
        _wait_for(qt_app, lambda: dialog._state == 1)  # idle
        assert dialog.status_label.text() == translations["native_trace.stopped"]
        assert target.send("buy", 1) == (94, 0)  # also lets the main thread leave Stalker's code
    finally:
        dialog.close()
        target.close()


def test_functions_are_named_by_symbol_or_location():
    functions = []
    apply_names(functions, {"firstFunction": 0, "functions": [
        ["0x401000", "game", 0x1000], ["0x7f0000", "", None]], "names": {"0": "buy_item"}})
    assert [function.label for function in functions] == ["buy_item", "0x7f0000"]
    assert functions[0].location == "game+0x1000"
    assert functions[0].signature == "buy_item  (game+0x1000, 0x401000)"
    # The next answer only brings the functions seen since.
    apply_names(functions, {"firstFunction": 2, "functions": [["0x401200", "game", 0x1200]],
                            "names": {"1": "jit"}})
    assert [function.label for function in functions] == ["buy_item", "jit", "game+0x1200"]
    assert functions[2] == NativeFunction(0x401200, "game", 0x1200)


def test_wine_unix_code_outside_the_followed_modules_runs_natively():
    mappings = parse_maps([
        "140000000-140400000 r-xp 00000000 08:01 1 /games/BG/Baldur.exe",
        "6fff0000-70000000 r-xp 00000000 08:01 2 /proton/lib/wine/x86_64-windows/user32.dll",
        "7f0000000000-7f0000100000 r-xp 00000000 08:01 3 /proton/lib/wine/x86_64-unix/ntdll.so",
        "7f0000200000-7f0000300000 rw-p 00000000 08:01 3 /proton/lib/wine/x86_64-unix/ntdll.so",
        "7f0000400000-7f0000500000 rwxp 00000000 00:00 0 ",
    ])
    followed = [TraceModule("Baldur.exe", "Z:\\games\\BG\\Baldur.exe", 0x140000000, 0x400000, True)]
    assert foreign_code(mappings, followed) == [
        ["0x6fff0000", 0x10000], ["0x7f0000000000", 0x100000]]


def test_thread_activity_is_cpu_share_and_wakeups(tmp_path):
    # pylint: disable=import-outside-toplevel
    import os

    ticks = os.sysconf("SC_CLK_TCK")
    task = tmp_path / "7" / "task"

    def thread(tid, total, switches=0):
        (task / str(tid)).mkdir(parents=True, exist_ok=True)
        # utime and stime are fields 14 and 15; the name may hold spaces and ")"
        fields = ["S"] + ["0"] * 10 + [str(total), "0"] + ["0"] * 5
        (task / str(tid) / "stat").write_text(f"{tid} (Main) Thread) " + " ".join(fields))
        (task / str(tid) / "status").write_text(
            f"Name:\tMain\nvoluntary_ctxt_switches:\t{switches}\n"
            "nonvoluntary_ctxt_switches:\t1\n")

    def later(_seconds):
        thread(7, ticks)          # half a CPU over two seconds
        thread(8, 0, switches=3)  # woke up, too briefly for a tick
    for tid in (7, 8, 9):
        thread(tid, 0)
    activity = thread_activity(7, [7, 8, 9, 10], seconds=2, proc_root=str(tmp_path), sleep=later)
    assert activity == {7: (0.5, True), 8: (0.0, True), 9: (0.0, False)}


def test_calls_filter_reads_counts_ranges_bounds_and_multiples():
    # pylint: disable=import-outside-toplevel
    import pytest

    from hermes.features.trace import calls_filter

    assert calls_filter("") is None
    assert calls_filter(" , ") is None
    assert [calls for calls in range(13) if calls_filter("%3")(calls)] == [0, 3, 6, 9, 12]
    assert [calls for calls in range(9) if calls_filter("2-4, >=7")(calls)] == [2, 3, 4, 7, 8]
    assert [calls for calls in range(6) if calls_filter("<2, 5")(calls)] == [0, 1, 5]
    for text in ("x", "%0", "3-", ">"):
        with pytest.raises(ValueError):
            calls_filter(text)


def test_rows_sort_by_column_and_filter_by_name_and_calls(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module

    from hermes.features.trace import NEW, SAME, TraceRow
    from hermes.ui.widgets.native_trace import NativeRowModel
    from hermes.ui.widgets.trace import RowFilterBar

    functions = [NativeFunction(0x401000, "game", 0x1000, "roll_dice"),
                 NativeFunction(0x402000, "game", 0x2000, "draw_ui"),
                 NativeFunction(0x7f0000, "", None)]
    rows = [TraceRow(0, 3, 1.0, None, NEW, 0), TraceRow(1, 600, 200.0, 190.0, SAME, None),
            TraceRow(2, 7, 2.3, None, NEW, 1)]
    model = NativeRowModel(translations)

    def labels():
        return [model.data(model.index(row, 1)) for row in range(model.rowCount())]

    model.set_items(functions, rows)
    assert labels() == ["roll_dice", "draw_ui", "0x7f0000"]  # as analyze ordered them
    model.sort(2, Qt.SortOrder.DescendingOrder)
    assert labels() == ["draw_ui", "0x7f0000", "roll_dice"]
    model.set_items(functions, rows[:1] + rows[2:])  # the sort stays
    assert labels() == ["0x7f0000", "roll_dice"]
    model.sort(1)
    assert labels() == ["0x7f0000", "roll_dice"]

    bar = RowFilterBar(translations)
    changes = []
    bar.changed.connect(lambda: changes.append(True))

    def shown():
        return [row.method for row in rows if bar.accepts(row, functions[row.method].signature)]

    bar.calls_input.setText("%3")
    assert shown() == [0, 1]
    bar.calls_input.setText("%3x")  # invalid: the last valid filter stays
    assert shown() == [0, 1] and len(changes) == 1
    bar.calls_input.setText("")
    bar.name_input.setText("GAME+0x")
    assert shown() == [0, 1]
    bar.name_input.setText("roll game")
    assert shown() == [0]
