"""Editors that stay open beside the rest (see grembyte.ui.widgets.editor_window)."""

import itertools
from types import SimpleNamespace

from grembyte.features.scripts import ScriptSpec
from grembyte.ui.localization import load_translations
from tests.test_hooks import fixture_qt_app  # pylint: disable=unused-import
from tests.test_scripts import _FakeScriptWorker


def _answer(monkeypatch, button):
    """Let QMessageBox.question answer ``button``; returns the questions asked."""
    # pylint: disable=import-outside-toplevel
    from PySide6.QtWidgets import QMessageBox  # pylint: disable=import-error,no-name-in-module

    asked = []

    def question(_parent, title, _text, *_args):
        asked.append(title)
        return getattr(QMessageBox.StandardButton, button)

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def test_an_editor_asks_before_discarding_changes(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument
    from grembyte.ui.widgets.scripts import ScriptDialog

    dialog = ScriptDialog(load_translations("en"), spec=ScriptSpec("return 1;"))
    assert not dialog.isModal() and dialog.pin_button is not None
    asked = _answer(monkeypatch, "Cancel")
    dialog.show()
    dialog.reject()  # nothing changed: closes without asking
    assert asked == [] and not dialog.isVisible()

    dialog = ScriptDialog(load_translations("en"), spec=ScriptSpec("return 1;"))
    dialog.show()
    dialog.code_edit.setPlainText("return 2;")
    assert dialog.is_dirty()
    dialog.reject()  # Cancel: it stays open
    assert asked == ["Discard changes?"] and dialog.isVisible()
    assert not dialog.close()
    _answer(monkeypatch, "Discard")
    dialog.reject()
    assert not dialog.isVisible()

    # Saving is not discarding: no question
    dialog = ScriptDialog(load_translations("en"))
    asked = _answer(monkeypatch, "Cancel")
    dialog.show()
    dialog.code_edit.setPlainText("return 3;")
    dialog._accept()  # pylint: disable=protected-access
    assert asked == [] and dialog.spec.source == "return 3;" and not dialog.is_dirty()


def _panel(monkeypatch, stored):
    # pylint: disable=import-outside-toplevel
    from grembyte.ui.widgets import scripts

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    row_ids = itertools.count(7)
    repository = SimpleNamespace(
        list_scripts=lambda _process_id: [],
        add_script=lambda process_id, **fields: (
            stored.append(("add", process_id, fields)) or next(row_ids)),
        update_script=lambda row_id, **fields: stored.append(("update", row_id, fields)))
    panel = scripts.ScriptPanel(load_translations("en"), repository=repository)
    panel.set_process(4242, process_id=1)
    return panel


def test_the_script_editor_is_not_modal_and_one_at_a_time(qt_app, monkeypatch):
    # pylint: disable=unused-argument,protected-access
    stored = []
    panel = _panel(monkeypatch, stored)
    try:
        first = panel.add_script(ScriptSpec("return 1;", label="One"))
        second = panel.add_script(ScriptSpec("return 2;", label="Two"))
        editor = panel._open_editor(first)
        assert editor.isVisible() and not editor.isModal()
        assert panel._open_editor(first) is editor  # the same script: to the front
        # Another script: the open editor closes first, asking about changes
        editor.code_edit.setPlainText("return 10;")
        _answer(monkeypatch, "Cancel")
        assert panel._open_editor(second) is None and panel.editor is editor
        _answer(monkeypatch, "Discard")
        other = panel._open_editor(second)
        assert other is not editor and panel.editor is other
        # Saving changes the running script and the database
        other.code_edit.setPlainText("return 20;")
        other._accept()
        assert panel._scripts[second]["spec"].source == "return 20;"
        assert stored[-1][0:2] == ("update", 8) and stored[-1][2]["source"] == "return 20;"
        assert panel.editor is None
    finally:
        panel.shutdown()
        panel.deleteLater()


def test_a_script_edited_while_the_game_exits_is_kept(qt_app, monkeypatch):
    # pylint: disable=unused-argument,protected-access
    stored = []
    panel = _panel(monkeypatch, stored)
    try:
        script_id = panel.add_script(ScriptSpec("return 1;", label="One"))
        editor = panel._open_editor(script_id)
        panel.set_process(None)  # the game exited
        editor.code_edit.setPlainText("return 5;")
        editor._accept()
        assert stored[-1][0:2] == ("update", 7) and stored[-1][2]["source"] == "return 5;"
        assert "next time" in panel.status_label.text()

        # A new script without a game: kept for the program it was written for
        panel.set_process(4242, process_id=1)
        editor = panel._open_editor(None)
        panel.set_process(None)
        editor.code_edit.setPlainText("return 6;")
        editor._accept()
        assert stored[-1][0:2] == ("add", 1)
    finally:
        panel.shutdown()
        panel.deleteLater()


def test_a_script_editor_refuses_to_save_into_another_game(qt_app, monkeypatch):
    # pylint: disable=unused-argument,protected-access
    stored = []
    panel = _panel(monkeypatch, stored)
    try:
        script_id = panel.add_script(ScriptSpec("return 1;", label="One"))
        editor = panel._open_editor(script_id)
        count = len(stored)
        panel.set_process(5151, process_id=2)  # another game
        editor.code_edit.setPlainText("return 7;")
        editor._accept()
        assert editor.isVisible() and "another game" in editor.error_label.text()
        assert len(stored) == count  # nothing saved, nothing lost
        editor.mark_saved()
        editor.close()
    finally:
        panel.shutdown()
        panel.deleteLater()


def test_the_method_hook_editor_is_not_modal(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.features.catalog import CatalogMethod
    from grembyte.hooks.transforms import example_templates
    from grembyte.ui.widgets.hooks import HookPanel

    stored = []
    repository = SimpleNamespace(
        list_hooks=lambda _process_id: [], list_method_hooks=lambda _process_id: [],
        list_templates=lambda: [],
        add_method_hook=lambda process_id, **fields: stored.append((process_id, fields)) or 3)
    panel = HookPanel(load_translations("en"), repository=repository)
    added = []
    monkeypatch.setattr(panel, "templates", lambda: list(example_templates().values()))
    monkeypatch.setattr(panel, "add_method_hook", added.append)
    monkeypatch.setattr(panel, "set_process", lambda *_args: None)
    panel._pid, panel._process_id = 4242, 1
    method = CatalogMethod("Assembly-CSharp", "Game.Player", "Heal", "(single)", "System.Single",
                           False, 0x06000001, "Game.Player:Heal (single)",
                           argument_names=("amount",))
    try:
        editor = panel.create_method_hook(method)
        assert editor.isVisible() and not editor.isModal() and editor.pin_button is not None
        assert panel.create_method_hook(method) is editor  # the same method: to the front
        editor._accept()
        assert [spec.signature for spec in added] == ["Game.Player:Heal (single)"]
        assert panel.method_editor is None

        # Saved after the game exited: kept for its next start
        editor = panel.create_method_hook(method)
        panel._pid = None
        editor._accept()
        assert len(added) == 1 and stored[-1][0] == 1
        assert "next time" in panel.status_label.text()
    finally:
        panel.deleteLater()
