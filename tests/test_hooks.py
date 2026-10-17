"""Tests for value transforms, Frida write hooks and the hook UI."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes.hooks.engine import HookSpec, build_agent_source
from hermes.hooks.transforms import (
    ClampTransform,
    FreezeTransform,
    MultiplyTransform,
    ScriptTransform,
    ValueTransform,
    available_transforms,
    create_transform,
    register_transform,
)


def test_builtin_transforms_are_registered():
    assert {"multiply", "freeze", "clamp", "script"} <= set(available_transforms())


def test_create_transform_by_kind():
    transform = create_transform("multiply", factor=10)

    assert isinstance(transform, MultiplyTransform)
    assert transform.to_agent()["params"] == {"factor": 10, "direction": "increase"}
    assert "function" in transform.to_agent()["source"]


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: MultiplyTransform("x"), "factor must be a number"),
        (lambda: MultiplyTransform(2, direction="up"), "direction must be one of"),
        (lambda: FreezeTransform(float("nan")), "value must be finite"),
        (lambda: ClampTransform(), "needs a minimum"),
        (lambda: ClampTransform(minimum=5, maximum=1), "must not be greater"),
        (lambda: ScriptTransform("  "), "needs a function source"),
        (lambda: create_transform("teleport"), "Unknown transform kind"),
    ],
)
def test_transform_validation(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def test_custom_transforms_can_be_registered():
    @register_transform
    class _Double(ValueTransform):
        kind = "test-double"
        source = "ctx => ctx.new * 2"

    try:
        assert isinstance(create_transform("test-double"), _Double)
        with pytest.raises(ValueError, match="already registered"):
            register_transform(_Double)
    finally:
        from hermes.hooks import transforms  # pylint: disable=import-outside-toplevel
        del transforms._TRANSFORMS["test-double"]  # pylint: disable=protected-access


def test_hook_spec_requires_a_code_location():
    with pytest.raises(ValueError, match="trap_pc or a write_pc"):
        HookSpec(0x1000, "int32", MultiplyTransform(2))


def test_hook_spec_serializes_for_agent():
    spec = HookSpec(0x1000, "int32", FreezeTransform(5), trap_pc=0x401006)

    payload = spec.to_agent(7)

    assert payload["id"] == 7
    assert payload["address"] == "0x1000"
    assert payload["trapPc"] == "0x401006"
    assert payload["writePc"] is None
    assert payload["transform"]["kind"] == "freeze"


def test_agent_exports_hook_management():
    source = build_agent_source()

    for name in ("addHook", "removeHook", "setTransform", "setEnabled", "dispose"):
        assert name in source


# --- integration: real process, real hooks --------------------------------

_TARGET_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

typedef struct { int32_t gold; int32_t hp; } Character;
Character g_player = { 100, 50 };
Character g_enemy = { 999, 80 };

__attribute__((noinline)) void add_gold(Character *c, int32_t amount) { c->gold += amount; }
__attribute__((noinline)) void remove_gold(int32_t amount) { g_player.gold -= amount; }

int main(void) {
    char cmd[16];
    int value;
    printf("%d %p\n", getpid(), (void *)&g_player.gold);
    fflush(stdout);
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "add") == 0) add_gold(&g_player, value);
        else if (strcmp(cmd, "enemy") == 0) add_gold(&g_enemy, value);
        else if (strcmp(cmd, "sub") == 0) remove_gold(value);
        printf("%d %d\n", g_player.gold, g_enemy.gold);
        fflush(stdout);
    }
    return 0;
}
"""


def _ptrace_allowed():
    try:
        return Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip() == "0"
    except OSError:
        return True


requires_target = pytest.mark.skipif(
    shutil.which("gcc") is None or not _ptrace_allowed(),
    reason="needs gcc and ptrace_scope=0 to attach to a test process",
)


class _Target:
    def __init__(self, binary):
        self.process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        pid, address = self.process.stdout.readline().split()
        self.pid = int(pid)
        self.address = int(address, 16)

    def send(self, command, value):
        self.process.stdin.write(f"{command} {value}\n")
        self.process.stdin.flush()
        gold, enemy = self.process.stdout.readline().split()
        return int(gold), int(enemy)

    def close(self):
        self.process.kill()
        self.process.wait()


def _trap_pcs(target):
    """Run the real write-watch agent and return the trap PC per command."""
    import frida  # pylint: disable=import-outside-toplevel
    from hermes.core.write_watch import _build_agent_source  # pylint: disable=import-outside-toplevel

    hits = []
    session = frida.attach(target.pid)
    script = session.create_script(_build_agent_source(target.address, 4))
    script.on(
        "message",
        lambda message, _data: hits.extend(
            message.get("payload", {}).get("hits", [])
        ),
    )
    script.load()
    try:
        pcs = {}
        for command in ("add", "sub"):
            hits.clear()
            target.send(command, 1)
            deadline = time.monotonic() + 2
            while not hits and time.monotonic() < deadline:
                time.sleep(0.02)
            assert hits, f"write watch saw no write for {command}"
            pcs[command] = int(hits[0]["pc"], 16)
        return pcs
    finally:
        script.exports_sync.stop()
        script.unload()
        session.detach()


def _wait_for(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for hook worker")
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module,import-outside-toplevel

    return QApplication.instance() or QApplication([])


@pytest.fixture(name="translations")
def fixture_translations():
    from hermes.ui.localization import load_translations  # pylint: disable=import-outside-toplevel

    return load_translations("de")


def _build_target(tmp_path, optimization="-O2"):
    source = tmp_path / "target.c"
    source.write_text(_TARGET_SOURCE)
    binary = tmp_path / "target"
    subprocess.run(["gcc", optimization, str(source), "-o", str(binary)], check=True)
    return binary


@requires_target
@pytest.mark.parametrize("optimization", ["-O0", "-O2"])
def test_hook_worker_rewrites_values(tmp_path, qt_app, optimization):
    from hermes.hooks.engine import HookWorker  # pylint: disable=import-outside-toplevel

    target = _Target(_build_target(tmp_path, optimization))
    worker = HookWorker(target.pid)
    installed, failed, errors = {}, {}, {}
    worker.hook_installed.connect(lambda hook_id, info: installed.update({hook_id: info}))
    worker.hook_failed.connect(lambda hook_id, message: failed.update({hook_id: message}))
    worker.transform_error.connect(lambda hook_id, message: errors.update({hook_id: message}))
    try:
        trap_pcs = _trap_pcs(target)
        assert target.send("add", 0) == (100, 999)

        worker.start()
        add_id = worker.add_hook(HookSpec(
            target.address, "int32", MultiplyTransform(10), trap_pc=trap_pcs["add"]
        ))
        sub_id = worker.add_hook(HookSpec(
            target.address, "int32", MultiplyTransform(0, direction="decrease"),
            trap_pc=trap_pcs["sub"],
        ))
        _wait_for(qt_app, lambda: {add_id, sub_id} <= set(installed) | set(failed))
        assert not failed

        assert target.send("add", 100) == (100 + 1000, 999)
        assert target.send("sub", 30) == (1100, 999)
        # Shared code: the add hook must not touch the enemy's gold.
        assert target.send("enemy", 100) == (1100, 1099)

        worker.set_transform(add_id, FreezeTransform(5))
        worker.set_enabled(sub_id, False)
        worker.add_hook(HookSpec(
            target.address, "int32", ScriptTransform("ctx => { throw new Error('boom'); }"),
            trap_pc=trap_pcs["add"],
        ))
        _wait_for(qt_app, lambda: len(installed) + len(failed) == 3)
        assert not failed
        assert target.send("sub", 1) == (1099, 1099)
        target.send("add", 7)
        _wait_for(qt_app, lambda: errors)
        assert "boom" in next(iter(errors.values()))

        removed = []
        worker.hook_removed.connect(removed.append)
        worker.remove_hook(add_id)
        _wait_for(qt_app, lambda: removed)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


# --- UI --------------------------------------------------------------------

def test_hook_dialog_lists_all_registered_transforms(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "0x1000", "int32", "game+0x10")

    kinds = [dialog.kind_combo.itemData(i) for i in range(dialog.kind_combo.count())]
    assert kinds == list(available_transforms())
    assert dialog.kind_combo.itemText(0) == "Multiplikator"


def test_hook_dialog_builds_transform_from_form(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "0x1000", "int32", "game+0x10")
    form = dialog.forms.currentWidget()
    form._editors["factor"][1].setText("0x10")  # pylint: disable=protected-access
    dialog.label_input.setText("Gold")

    dialog._accept()  # pylint: disable=protected-access

    assert dialog.result() == dialog.DialogCode.Accepted
    assert isinstance(dialog.transform, MultiplyTransform)
    assert dialog.transform.params == {"factor": 16, "direction": "increase"}
    assert dialog.label == "Gold"


def test_hook_dialog_reports_invalid_input(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "0x1000", "int32", "game+0x10")
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("clamp"))

    dialog._accept()  # pylint: disable=protected-access

    assert dialog.transform is None
    assert "minimum" in dialog.error_label.text()


def test_hook_dialog_prefills_existing_transform(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(
        translations, "0x1000", "int32", "game+0x10",
        transform=ScriptTransform("ctx => 7"),
    )

    assert dialog.kind_combo.currentData() == "script"
    editor = dialog.forms.currentWidget()._editors["source"][1]  # pylint: disable=protected-access
    assert editor.toPlainText() == "ctx => 7"


@requires_target
def test_hook_panel_installs_and_removes_hooks(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel
    from hermes.ui.widgets.hooks import HookPanel

    target = _Target(_build_target(tmp_path))
    panel = HookPanel(translations)
    try:
        trap_pcs = _trap_pcs(target)
        panel.set_process(target.pid)
        hook_id = panel.add_hook(
            HookSpec(target.address, "int32", MultiplyTransform(3),
                     trap_pc=trap_pcs["add"], label="Gold"),
            "target+0x580",
        )
        status = lambda: panel.table.item(0, 4).text()  # pylint: disable=unnecessary-lambda-assignment
        _wait_for(qt_app, lambda: status() != "Wird installiert …")
        assert status() == "Aktiv"
        assert "add" in panel.table.item(0, 2).text()

        assert target.send("add", 10)[0] == 100 + 30
        _wait_for(qt_app, lambda: panel.table.item(0, 5).text() == "1")
        assert panel.table.item(0, 6).text() == "100 → 110 ⇒ 130"

        updated = []
        panel._worker.hook_updated.connect(updated.append)  # pylint: disable=protected-access
        panel.table.selectRow(0)
        panel._toggle_selected()  # pylint: disable=protected-access
        assert status() == "Pausiert"
        _wait_for(qt_app, lambda: updated)
        assert target.send("add", 10)[0] == 140

        panel._remove_selected()  # pylint: disable=protected-access
        _wait_for(qt_app, lambda: panel.table.rowCount() == 0)
        assert hook_id not in panel._hooks  # pylint: disable=protected-access
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()
