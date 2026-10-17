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
    TemplateTransform,
    TransformDefinition,
    TransformParameter,
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
    with pytest.raises(ValueError, match="needs a trap_pc, a write_pc or a module"):
        HookSpec(0x1000, "int32", MultiplyTransform(2))
    with pytest.raises(ValueError, match="given together"):
        HookSpec(None, "int32", MultiplyTransform(2), module="game")
    with pytest.raises(ValueError, match="only be resolved with an address"):
        HookSpec(None, "int32", MultiplyTransform(2), trap_pc=0x401006)


def test_hook_spec_validates_callers():
    with pytest.raises(ValueError, match="caller_depth must be between"):
        HookSpec(0x1000, "int32", MultiplyTransform(2), write_pc=0x401000, caller_depth=5)
    with pytest.raises(ValueError, match="needs caller_depth frames"):
        HookSpec(0x1000, "int32", MultiplyTransform(2), write_pc=0x401000,
                 caller_depth=2, callers=[[("game", 0x10)]])

    payload = HookSpec(
        None, "int32", MultiplyTransform(2), module="game", module_offset=0x10,
        callers=[[("game", 0x20)]],
    ).to_agent(1)

    assert (payload["callerDepth"], payload["callers"]) == (1, [[["game", 0x20]]])


def test_hook_spec_serializes_module_location():
    spec = HookSpec(
        None, "int32", FreezeTransform(5),
        module="game", module_offset=0x1139, instruction_bytes="0137", enabled=False,
    )

    payload = spec.to_agent(3)

    assert payload["address"] is None
    assert (payload["module"], payload["moduleOffset"]) == ("game", 0x1139)
    assert payload["instructionBytes"] == "0137"
    assert payload["enabled"] is False


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
volatile int g_events;

__attribute__((noinline)) void add_gold(Character *c, int32_t amount) { c->gold += amount; }
__attribute__((noinline)) void remove_gold(int32_t amount) { g_player.gold -= amount; }
/* Shared helper with two callers; g_events keeps the calls from becoming jumps. */
__attribute__((noinline)) void give(Character *c, int32_t amount) { add_gold(c, amount); g_events++; }
__attribute__((noinline)) void loot(int32_t amount) { give(&g_player, amount); g_events++; }
__attribute__((noinline)) void bounty(int32_t amount) { give(&g_enemy, amount); g_events++; }

int main(void) {
    char cmd[16];
    int value;
    printf("%d %p\n", getpid(), (void *)&g_player.gold);
    fflush(stdout);
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "add") == 0) add_gold(&g_player, value);
        else if (strcmp(cmd, "enemy") == 0) add_gold(&g_enemy, value);
        else if (strcmp(cmd, "sub") == 0) remove_gold(value);
        else if (strcmp(cmd, "loot") == 0) loot(value);
        else if (strcmp(cmd, "bounty") == 0) bounty(value);
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


def _build_target(tmp_path, optimization="-O2", *flags):
    source = tmp_path / "target.c"
    source.write_text(_TARGET_SOURCE)
    binary = tmp_path / "target"
    subprocess.run(
        ["gcc", optimization, *flags, str(source), "-o", str(binary)], check=True
    )
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

def _template(template_id=1, source="ctx => ctx.new * ctx.params.factor", default=2):
    return TransformDefinition(
        "Faktor", source, (TransformParameter("factor", default=default, label="Faktor"),),
        template_id=template_id,
    )


def test_hook_dialog_lists_example_templates(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "0x1000", "int32", "game+0x10")

    names = [dialog.template_combo.itemText(i) for i in range(dialog.template_combo.count())]
    assert names == ["Multiplikator", "Fester Wert", "Begrenzen"]
    assert not dialog.template_notice.isVisibleTo(dialog)


def test_hook_dialog_builds_transform_from_form(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "0x1000", "int32", "game+0x10")
    form = dialog.forms.currentWidget()
    form._editors["factor"][1].setText("0x10")  # pylint: disable=protected-access
    dialog.label_input.setText("Gold")

    dialog._accept()  # pylint: disable=protected-access

    assert dialog.result() == dialog.DialogCode.Accepted
    assert isinstance(dialog.transform, TemplateTransform)
    assert dialog.transform.definition.name == "Multiplikator"
    assert dialog.transform.params == {"factor": 16, "direction": "increase"}
    assert dialog.transform.source == MultiplyTransform.source.strip()
    assert dialog.label == "Gold"


def test_hook_dialog_sets_caller_depth_only_when_creating(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "0x1000", "int32", "game+0x10")
    dialog.caller_depth_input.setValue(3)
    dialog._accept()
    assert dialog.caller_depth == 3

    editing = HookDialog(
        translations, "0x1000", "int32", "game+0x10", transform=FreezeTransform(1)
    )
    assert not hasattr(editing, "caller_depth_input")


def test_hook_dialog_reports_invalid_input(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "0x1000", "int32", "game+0x10")
    dialog.forms.currentWidget()._editors["factor"][1].setText("viel")

    dialog._accept()

    assert dialog.transform is None
    assert "Faktor" in dialog.error_label.text()


def test_hook_dialog_prefills_transform_without_template(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(
        translations, "0x1000", "int32", "game+0x10",
        transform=FreezeTransform(7),
    )

    assert dialog.template_combo.currentIndex() == 0
    assert dialog.template_combo.currentText() == "freeze"
    editor = dialog.forms.currentWidget()._editors["value"][1]  # pylint: disable=protected-access
    assert editor.text() == "7"
    assert dialog.template_state() is None


def test_hook_dialog_uses_hook_copy_of_unchanged_template(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(
        translations, "0x1000", "int32", "game+0x10",
        transform=TemplateTransform(_template(), factor=5),
        templates=[_template(), _template(2)],
    )

    assert dialog.template_combo.count() == 2
    assert dialog.template_state() is None
    assert not dialog.template_notice.isVisibleTo(dialog)


def test_hook_dialog_offers_changed_template(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets.hooks import HookDialog

    changed = TransformDefinition(
        "Faktor neu", "ctx => ctx.old + ctx.delta * ctx.params.factor",
        (
            TransformParameter("factor", default=2, label="Faktor"),
            TransformParameter("limit", optional=True, label="Grenze"),
        ),
        template_id=1,
    )
    dialog = HookDialog(
        translations, "0x1000", "int32", "game+0x10",
        transform=TemplateTransform(_template(), factor=5),
        templates=[changed],
    )

    assert dialog.template_state() == "changed"
    assert dialog.template_notice.isVisibleTo(dialog)
    assert "Faktor neu" in dialog.template_notice_label.text()

    dialog._adopt_template()

    assert dialog.template_state() is None
    assert not dialog.template_notice.isVisibleTo(dialog)
    assert dialog.template_combo.currentText() == "Faktor neu"
    dialog._accept()
    assert dialog.transform.definition == changed
    assert dialog.transform.definition.template_id == 1
    assert dialog.transform.params == {"factor": 5, "limit": None}


def test_hook_dialog_reports_deleted_template(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(
        translations, "0x1000", "int32", "game+0x10",
        transform=TemplateTransform(_template(7), factor=5),
        templates=[_template(2)],
    )

    assert dialog.template_state() == "deleted"
    assert dialog.template_combo.count() == 2
    assert dialog.template_notice.isVisibleTo(dialog)
    assert not dialog.btn_adopt_template.isVisibleTo(dialog)

    dialog.template_combo.setCurrentIndex(1)
    assert not dialog.template_notice.isVisibleTo(dialog)


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


@requires_target
def test_saved_hooks_survive_a_restart(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access,too-many-locals,too-many-statements
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from hermes.db import models  # noqa: F401  pylint: disable=unused-import
    from hermes.db.base import Base
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui.widgets.hooks import HookPanel

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    repository = HookRepository(sessionmaker(bind=engine))
    binary = _build_target(tmp_path, "-O2", "-fPIE", "-pie")
    process_id = repository.get_or_create_process("target", str(binary))
    status = lambda panel: panel.table.item(0, 4).text()  # pylint: disable=unnecessary-lambda-assignment

    target = _Target(binary)
    panel = HookPanel(translations, repository=repository)
    try:
        trap_pcs = _trap_pcs(target)
        panel.set_process(target.pid, process_id)
        panel.add_hook(
            HookSpec(target.address, "int32", MultiplyTransform(3),
                     trap_pc=trap_pcs["add"], caller_depth=2, label="Gold"),
            "target+?",
        )
        _wait_for(qt_app, lambda: status(panel) != "Wird installiert …")
        assert status(panel) == "Aktiv"
        [saved] = repository.list_hooks(process_id)
        assert saved.module == "target"
        assert saved.label == "Gold"
        assert saved.callers == []

        # loot() and bounty() share give(); two levels tell them apart.
        assert target.send("loot", 10) == (130, 999)
        assert target.send("bounty", 1) == (130, 1000)
        _wait_for(qt_app, lambda: repository.list_hooks(process_id)[0].callers)
        [[give_frame, loot_frame]] = repository.list_hooks(process_id)[0].callers
        assert give_frame[0] == loot_frame[0] == "target"
        assert "ambiguous" not in next(iter(panel._hooks.values()))
        first_address = target.address
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()

    target = _Target(binary)
    panel = HookPanel(translations, repository=repository)
    try:
        if Path("/proc/sys/kernel/randomize_va_space").read_text().strip() != "0":
            assert target.address != first_address  # ASLR moved the module
        panel.set_process(target.pid, process_id)
        assert panel.table.rowCount() == 1
        _wait_for(qt_app, lambda: status(panel) != "Wird installiert …")
        assert status(panel) == "Aktiv"
        assert panel.table.item(0, 1).text() == "dynamisch · 1 Aufrufer"

        assert target.send("loot", 10) == (130, 999)
        assert target.send("bounty", 1) == (130, 1000)
        # Unknown callers of add_gold are left alone as well.
        assert target.send("add", 1) == (131, 1000)

        panel.table.selectRow(0)
        panel._toggle_selected()
        assert not repository.list_hooks(process_id)[0].enabled
        panel._remove_selected()
        assert repository.list_hooks(process_id) == []
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()
        engine.dispose()


@requires_target
def test_changed_code_is_not_patched(tmp_path, qt_app):
    from hermes.hooks.engine import HookWorker  # pylint: disable=import-outside-toplevel

    target = _Target(_build_target(tmp_path))
    worker = HookWorker(target.pid)
    failed = {}
    worker.hook_failed.connect(lambda hook_id, message: failed.update({hook_id: message}))
    try:
        worker.start()
        hook_id = worker.add_hook(HookSpec(
            None, "int32", MultiplyTransform(3),
            module="target", module_offset=0x1000, instruction_bytes="deadbeef",
        ))
        _wait_for(qt_app, lambda: hook_id in failed)
        assert "has changed" in failed[hook_id]
        assert target.send("add", 10) == (110, 999)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_shared_callers_are_reported_as_ambiguous(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.ui.widgets.hooks import HookPanel

    target = _Target(_build_target(tmp_path))
    panel = HookPanel(translations)
    try:
        trap_pcs = _trap_pcs(target)
        panel.set_process(target.pid)
        hook_id = panel.add_hook(
            HookSpec(target.address, "int32", MultiplyTransform(3),
                     trap_pc=trap_pcs["add"], caller_depth=1, label="Gold"),
            "target+?",
        )
        _wait_for(qt_app, lambda: panel.table.item(0, 4).text() == "Aktiv")

        assert target.send("loot", 10) == (130, 999)
        assert target.send("bounty", 1) == (130, 1000)
        _wait_for(qt_app, lambda: panel._hooks[hook_id].get("ambiguous"))
        assert "Gold" in panel.status_label.text()
        assert len(panel._hooks[hook_id]["spec"].callers) == 1
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()

