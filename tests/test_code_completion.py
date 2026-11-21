"""Completion in the code fields (see grembyte.ui.widgets.code_completion)."""

import pytest

from grembyte.ui.localization import load_translations
from grembyte.ui.widgets import code_completion
from grembyte.ui.widgets.code_completion import HOOK, NATIVE, SCRIPT, completions
from tests.test_hooks import fixture_qt_app  # pylint: disable=unused-import


def _offered(source, context=SCRIPT, engine=None):
    prefix, entries = completions(source, source.split("\n")[-1], 0, context, engine)
    return prefix, [entry.name for entry in entries]


def test_ctx_offers_only_what_the_engine_has():
    _prefix, unreal = _offered("ctx.", engine="unreal")
    assert {"isPlaying", "findClass", "gas", "find", "native", "enum"} <= set(unreal)
    assert "static" not in unreal
    _prefix, mono = _offered("ctx.", engine="mono")
    assert {"static", "find", "enum"} <= set(mono)
    assert not {"isPlaying", "findClass", "gas", "outerName"} & set(mono)
    _prefix, il2cpp = _offered("ctx.", engine="il2cpp")
    assert "static" in il2cpp and "enum" not in il2cpp
    # A game without an adapter: only what works on machine code
    _prefix, native = _offered("ctx.", engine=NATIVE)
    assert native == ["native", "args", "lang", "log", "state", "runs", "scriptId"]
    # Not attached: everything
    _prefix, unknown = _offered("ctx.")
    assert {"isPlaying", "static"} <= set(unknown)


def test_hook_fields_have_their_own_ctx():
    _prefix, names = _offered("return ctx.", HOOK, "mono")
    assert {"old", "new", "delta", "this", "result", "find", "static"} <= set(names)
    assert not {"runs", "native", "isPlaying"} & set(names)


def test_frida_pointer_chains_are_followed():
    prefix, names = _offered('ctx.find("BP_PlayerCharacter_C").address.add(0x17d0).rea')
    assert prefix == "rea"
    assert {"readPointer", "readU32", "readFloat", "add", "isNull"} <= set(names)
    assert _offered('ptr("0x147D94408").')[1] == _offered("NULL.")[1]
    assert "readPointer" in _offered("new NativePointer(5).")[1]
    assert "readS32" in _offered("ctx.native.module().base.add(0x10).readPointer().")[1]
    assert "readS32" in _offered('ctx.native.scan("48 8B ??").')[1]


def test_a_variable_is_known_by_its_assignment():
    source = ('const pawn = ctx.find("A");  // the player\n'
              "let component = pawn.address.add(0x17d0).readPointer();\n"
              "component.")
    assert "readPointer" in _offered(source)[1]
    assert "className" in _offered(source + "isNull();\npawn.")[1]
    assert "path" in _offered("const m = ctx.native.module();\nm.")[1]


def test_wrappers_offer_their_own_names():
    _prefix, names = _offered('ctx.find("Player").', engine="unreal")
    assert names == ["name", "className", "address", "field", "get", "call"]
    assert "component" in _offered('ctx.find("Player").', engine="mono")[1]
    assert "className" in _offered("ctx.this.", HOOK)[1]


def test_frida_objects_and_ctx_parts():
    assert _offered("ctx.native.")[1] == ["module", "scan", "scanAll", "rip", "fn"]
    assert _offered("ctx.gas.", engine="unreal")[1] == ["attributes", "get", "setBase",
                                                        "findSet"]
    assert "alloc" in _offered("Memory.")[1]
    assert "path" in _offered("Process.mainModule.")[1]
    assert "findGlobalExportByName" in _offered("Module.")[1]


def test_nothing_where_it_is_not_known():
    for source in ("player.Stats.", "list.find(x => x).", "x = [1].", "1.", "const a = 1.5",
                   '"ctx.', "// ctx.", "/* ctx.", "`${ctx.find('A')} ctx.", "[...a, ...",
                   "ctx.find.", "ctx.isPlaying()."):
        assert _offered(source)[1] == [], source


def test_names_on_their_own_are_keywords_and_frida_globals():
    prefix, names = _offered("  cons")
    assert prefix == "cons"
    assert {"const", "continue", "ctx", "ptr", "Memory", "NativePointer", "hexdump"} <= set(names)
    # TypeScript's words are no use in a script
    assert "interface" not in names


def test_a_block_comment_from_an_earlier_line():
    # pylint: disable=protected-access
    from grembyte.ui.widgets.code_editor import _BLOCK_COMMENT  # pylint: disable=import-outside-toplevel

    assert completions("/*\nctx.", "ctx.", _BLOCK_COMMENT)[1] == ()


@pytest.mark.parametrize("language", ["en", "de"])
def test_every_entry_has_a_description(language):
    translations = load_translations(language)
    groups = (code_completion.SCRIPT_CONTEXT, code_completion.HOOK_CONTEXT,
              code_completion.WRAPPER, code_completion.NATIVE_CONTEXT, code_completion.GAS,
              code_completion.POINTER, code_completion.MODULE, code_completion.MEMORY,
              code_completion.PROCESS, code_completion.MODULE_API, code_completion.GLOBALS,
              code_completion.KEYWORDS)
    for entry in (entry for group in groups for entry in group):
        assert code_completion.description(entry, translations).strip(), entry.name
    read = next(entry for entry in code_completion.POINTER if entry.name == "readU16")
    assert "16" in code_completion.description(read, translations)
    gas_get = next(entry for entry in code_completion.GAS if entry.name == "get")
    assert code_completion.description(gas_get, translations).startswith("{base, current}")


def _typed(editor, keys):
    # pylint: disable=import-outside-toplevel
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module

    for number, line in enumerate(keys.split("\n")):
        if number:  # QTest.keyClicks cannot type a line break
            QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, line)


def _editor_at_end(text, engine="unreal", context=SCRIPT):
    # pylint: disable=import-outside-toplevel
    from PySide6.QtGui import QTextCursor  # pylint: disable=import-error,no-name-in-module
    from grembyte.ui.widgets.code_editor import CodeEditor

    editor = CodeEditor(text)
    editor.set_completion(load_translations("en"), context, engine)
    editor.show()
    editor.moveCursor(QTextCursor.MoveOperation.End)
    editor.setFocus()
    return editor


def test_the_popup_completes_while_typing(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module

    editor = _editor_at_end('const pawn = ctx.find("BP_PlayerCharacter_C");\n')
    popup = editor.completion_popup
    _typed(editor, "pawn.address.add(0x17d0).readP")
    assert popup.isVisible()
    assert popup.current_entry().name == "readPointer"
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert not popup.isVisible()
    assert editor.toPlainText().endswith("pawn.address.add(0x17d0).readPointer()")
    assert editor.textCursor().atEnd()  # no parameters: after the parentheses

    # A method with parameters: the cursor between them; Tab takes the entry too
    _typed(editor, ";\nctx.isP")
    assert popup.current_entry().name == "isPlaying"
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert editor.toPlainText().endswith("ctx.isPlaying()")
    _typed(editor, ";\nctx.fin")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText().endswith("ctx.find()")
    assert editor.document().characterAt(editor.textCursor().position()) == ")"

    # Down and Up move the selection, stopping at the ends
    editor.moveCursor(editor.textCursor().MoveOperation.End)
    _typed(editor, ";\nctx.")
    assert popup.current_entry().name == "find"
    QTest.keyClick(editor, Qt.Key.Key_Up)
    assert popup.current_entry().name == "find"
    QTest.keyClick(editor, Qt.Key.Key_Down)
    QTest.keyClick(editor, Qt.Key.Key_Down)
    assert popup.current_entry().name == "forEach"
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText().endswith("ctx.forEach()")

    # Escape closes it and keeps the text; an unknown chain opens nothing
    editor.setPlainText("")
    _typed(editor, "player.Stats.")
    assert not popup.isVisible()
    _typed(editor, "Gold;\nctx.")
    assert popup.isVisible()
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    assert not popup.isVisible()
    assert editor.toPlainText() == "player.Stats.Gold;\nctx."
    editor.close()


def test_keywords_after_two_letters_and_ctrl_space(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module

    editor = _editor_at_end("", engine=NATIVE)
    popup = editor.completion_popup
    _typed(editor, "r")
    assert not popup.isVisible()
    _typed(editor, "e")
    assert popup.isVisible()
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == "return"
    # Ctrl+Space opens it without a letter; a native game has no ctx.find
    _typed(editor, " ctx.")
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    QTest.keyClick(editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    assert popup.isVisible()
    model = popup.model()
    shown = {model.index(row, 0).data(Qt.ItemDataRole.UserRole).name
             for row in range(model.rowCount())}
    assert "native" in shown and "find" not in shown
    editor.close()


def test_fields_without_completion_keep_tab_and_enter(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module
    from grembyte.ui.widgets.code_editor import CodeEditor

    editor = CodeEditor("")
    assert editor.completion_popup is None
    _typed(editor, "ctx.")
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == "ctx.  \n"


def test_the_script_dialog_completes_for_the_engine(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from grembyte.ui.widgets.scripts import ScriptDialog

    dialog = ScriptDialog(load_translations("en"), engine="mono")
    for editor in (dialog.code_edit, dialog.param_editor.options_edit):
        editor.setPlainText("ctx.")
        editor.moveCursor(editor.textCursor().MoveOperation.End)
        names = [entry.name for entry in editor.completion_entries()[1]]
        assert "static" in names and "isPlaying" not in names


def test_write_and_method_hooks_have_their_own_ctx():
    _prefix, value = _offered("ctx.", code_completion.VALUE_HOOK, "unreal")
    assert value == ["old", "new", "delta", "params", "state", "valueType", "address",
                     "writePc", "threadId", "hookId", "log"]
    _prefix, method = _offered("ctx.", code_completion.METHOD_HOOK, "mono")
    assert {"phase", "args", "result", "this", "find", "static", "params", "log"} <= set(method)
    assert not {"old", "new", "writePc", "isPlaying"} & set(method)
    assert "isPlaying" in _offered("ctx.", code_completion.METHOD_HOOK, "unreal")[1]
    assert code_completion.hook_context("value") == code_completion.VALUE_HOOK
    assert code_completion.hook_context("method") == code_completion.METHOD_HOOK


def _ctx_names(editor):
    editor.setPlainText("ctx.")
    editor.moveCursor(editor.textCursor().MoveOperation.End)
    return {entry.name for entry in editor.completion_entries()[1]}


def test_the_template_editor_follows_the_kind_of_hook(qt_app, tmp_path):
    # pylint: disable=import-outside-toplevel,unused-argument
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from grembyte.db import models  # noqa: F401  pylint: disable=unused-import
    from grembyte.db.base import Base
    from grembyte.db.repository.hooks import HookRepository
    from grembyte.hooks.transforms import example_templates
    from grembyte.ui.widgets.templates import TemplateDialog

    engine = create_engine(f"sqlite:///{tmp_path / 'grembyte.db'}")
    Base.metadata.create_all(engine)
    repository = HookRepository(sessionmaker(bind=engine))
    repository.ensure_templates(example_templates())
    dialog = TemplateDialog(load_translations("en"), repository)
    combo = dialog.hook_kind_combo
    combo.setCurrentIndex(combo.findData("value"))
    names = _ctx_names(dialog.source_input)
    assert "old" in names and "phase" not in names
    combo.setCurrentIndex(combo.findData("method"))
    names = _ctx_names(dialog.source_input)
    assert "phase" in names and "old" not in names
    dialog.close()
    engine.dispose()


def test_a_method_hooks_code_completes_for_the_engine(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from grembyte.hooks.transforms import example_templates
    from grembyte.ui.widgets.code_editor import CodeEditor
    from grembyte.ui.widgets.hooks import HookDialog

    templates = [template for template in example_templates().values()
                 if template.kind == "method"]
    dialog = HookDialog(load_translations("en"), "", "", "", templates=templates,
                        method="Game.Player:Heal (single)", engine="mono")
    editors = [editor for editor in dialog.findChildren(CodeEditor)
               if editor.completion_popup is not None]
    assert editors
    for editor in editors:
        names = _ctx_names(editor)
        assert {"phase", "static", "find"} <= names
        assert not {"isPlaying", "old"} & names
    dialog.mark_saved()  # what was typed here is not to be kept
    dialog.close()


# What the agent's classMembers answers for two classes of a made-up game
_MEMBERS = {
    "CharacterStats": {
        "name": "CharacterStats",
        "fields": [{"name": "m_health", "type": "Health", "static": False},
                   {"name": "Level", "type": "System.Int32", "static": False},
                   {"name": "s_count", "type": "System.Int32", "static": True},
                   {"name": "<Name>k__BackingField", "type": "System.String", "static": False}],
        "properties": [{"name": "IsPartyMember", "type": "System.Boolean", "static": False},
                       {"name": "Owner", "type": "UnityEngine.GameObject", "static": False}],
        "methods": [{"name": "Heal", "params": [["System.Single", "amount"]],
                     "returns": "System.Single", "static": False},
                    {"name": "GetHealth", "params": [], "returns": "Health", "static": False},
                    {"name": ".ctor", "params": [], "returns": "System.Void", "static": False}],
    },
    "Health": {
        "name": "Health",
        "fields": [{"name": "m_current", "type": "System.Single", "static": False}],
        "properties": [], "methods": [],
    },
}
_ROLL = "DamagePacket:RollDamage (CharacterStats roller,single scale,int& roll)"


def _lookup(asked=None):
    def lookup(name):
        if asked is not None:
            asked.append(name)
        found = _MEMBERS.get(name)
        return None if found is None else code_completion.ClassMembers.from_agent(found)
    return lookup


def _scoped(source, scope, context=code_completion.METHOD_HOOK, engine="mono"):
    prefix, entries = completions(source, source.split("\n")[-1], 0, context, engine, scope)
    return prefix, [entry.name for entry in entries]


def test_method_parameters_from_the_hook_dialogs_signature():
    assert code_completion.method_parameters(_ROLL) == (
        "DamagePacket", {"roller": "CharacterStats", "scale": "single", "roll": "int&"})
    assert code_completion.method_parameters(
        "Game.Combat:RollDamage (System.Int32 min,System.Collections.Generic.Dictionary`2"
        "<System.String, System.Int32> table)") == (
        "Game.Combat", {"min": "System.Int32",
                        "table": "System.Collections.Generic.Dictionary`2<System.String, "
                                 "System.Int32>"})
    # Without names (a catalog entry of Unreal): no arguments to offer
    assert code_completion.method_parameters("A:B (int32,bool)") == ("A", {})
    assert code_completion.method_parameters("") == (None, {})


def test_class_types_that_are_followed():
    assert code_completion.class_type("CharacterStats&") == "CharacterStats"
    assert code_completion.class_type("ULStatComponent*") == "ULStatComponent"
    assert code_completion.class_type("FLStatData") == "FLStatData"
    for plain in ("single", "System.Single", "int&", "System.String", "FString", "bool",
                  "TArray<FLStatData>", "System.Int32[]", "List`1", "EPrimaryAttribute", "?"):
        assert code_completion.class_type(plain) is None, plain


def test_ctx_args_lists_the_method_hooks_arguments():
    _owner, args = code_completion.method_parameters(_ROLL)
    scope = code_completion.Scope(_lookup(), args, "DamagePacket")
    assert _scoped("ctx.args.", scope)[1] == ["roller", "scale", "roll"]
    # Only a method hook has them, and only with names
    assert _scoped("ctx.args.", code_completion.Scope(_lookup()))[1] == []


def test_an_arguments_fields_properties_and_methods():
    _owner, args = code_completion.method_parameters(_ROLL)
    asked = []
    scope = code_completion.Scope(_lookup(asked), args)
    names = _scoped("ctx.args.roller.", scope)[1]
    assert names[:5] == ["IsPartyMember", "Level", "m_health", "Owner", "s_count"]
    assert names[5:7] == ["GetHealth", "Heal"]
    assert names[7:] == ["name", "className", "address", "field", "get", "call", "component"]
    assert asked == ["CharacterStats"]
    # Chains through fields, method results and variables
    assert _scoped("ctx.args.roller.m_health.", scope)[1][0] == "m_current"
    assert _scoped("ctx.args.roller.GetHealth().", scope)[1][0] == "m_current"
    assert _scoped('ctx.args.roller.field("m_health").', scope)[1][0] == "m_current"
    assert _scoped("const s = ctx.args.roller;\nconst h = s.m_health;\nh.", scope)[1][0] == \
        "m_current"
    # Numbers are JavaScript numbers; unknown members and Frida pointers
    assert _scoped("ctx.args.scale.", scope)[1] == ["toFixed", "toPrecision", "toString"]
    assert _scoped("ctx.args.roller.Level.", scope)[1] == ["toFixed", "toPrecision", "toString"]
    assert _scoped("ctx.args.roller.Missing.", scope)[1] == []
    assert "readPointer" in _scoped("ctx.args.roller.address.", scope)[1]


def test_find_and_this_name_their_class():
    scope = code_completion.Scope(_lookup(), {}, "CharacterStats")
    assert "Heal" in _scoped('ctx.find("CharacterStats").', scope, SCRIPT)[1]
    assert "Heal" in _scoped("ctx.this.", scope)[1]
    # A class the game does not have: the wrapper's own names
    assert _scoped('ctx.find("Nothing").', scope, SCRIPT, "unreal")[1] == [
        "name", "className", "address", "field", "get", "call"]


def test_members_being_read_are_pending():
    scope = code_completion.Scope(lambda name: code_completion.PENDING)
    assert _scoped('ctx.find("CharacterStats").', scope, SCRIPT, "unreal")[1] == [
        "name", "className", "address", "field", "get", "call"]
    assert scope.pending
    assert _scoped("ctx.", scope, SCRIPT)[1] and not scope.pending


def test_the_editor_shows_members_once_the_game_answered(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

    class Lookup(QObject):
        changed = Signal()

        def __init__(self):
            super().__init__()
            self.answered = False

        def __call__(self, name):
            if not self.answered:
                return code_completion.PENDING
            return code_completion.ClassMembers.from_agent(_MEMBERS[name])

    lookup = Lookup()
    editor = _editor_at_end("", engine="mono", context=code_completion.METHOD_HOOK)
    editor.set_completion(load_translations("en"), code_completion.METHOD_HOOK, "mono",
                          lookup, _ROLL)
    popup = editor.completion_popup
    _typed(editor, "ctx.args.ro")
    assert popup.current_entry().name == "roller"
    _typed(editor, "ller.He")
    assert not popup.isVisible()  # nothing yet: the wrapper names start with no "He"
    lookup.answered = True
    lookup.changed.emit()
    assert popup.isVisible() and popup.current_entry().name == "Heal"
    _typed(editor, "\t")
    assert editor.toPlainText() == "ctx.args.roller.Heal()"
    assert editor.document().characterAt(editor.textCursor().position()) == ")"
    editor.close()


def test_a_wide_popup_starts_at_the_cursor_and_ends_at_the_screens_edge(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

    long_params = [[f"System.Collections.Generic.Dictionary`2<System.String,Type{n}>",
                    f"argument{n}"] for n in range(6)]
    members = code_completion.ClassMembers.from_agent({
        "name": "Wide", "fields": [], "properties": [],
        "methods": [{"name": f"GetComponentFastPath{n}", "params": long_params,
                     "returns": "System.Object", "static": False} for n in range(3)]})

    class Lookup(QObject):
        changed = Signal()

        def __call__(self, name):
            return members

    editor = _editor_at_end("", engine="mono")
    editor.set_completion(load_translations("en"), SCRIPT, "mono", Lookup())
    editor.resize(500, 200)
    editor.move(200, 100)
    _typed(editor, 'ctx.find("Wide").GetC')
    popup = editor.completion_popup
    assert popup.isVisible()
    screen = editor.screen().availableGeometry()
    cursor = editor.viewport().mapToGlobal(editor.cursorRect().bottomLeft())
    assert popup.geometry().left() == cursor.x()
    assert popup.geometry().right() <= screen.right()
    # The description keeps a column of its own
    assert popup.columnWidth(1) > 50
    editor.close()


_CLASSES = ["BP_EnemyBase_C", "BP_PlayerCharacter_C", "PlayerController", "PlayerState"]


class _NameLookup:
    """A lookup with class names, as MemberLookup gives them."""

    def __init__(self, names=_CLASSES):
        self.names = names

    def __call__(self, name):
        return None

    def class_names(self):
        return self.names


def test_class_names_inside_find_and_for_each():
    scope = code_completion.Scope(_NameLookup())
    prefix, names = _scoped('const p = ctx.find("Play', scope, SCRIPT, "unreal")
    assert prefix == "Play"
    # Those starting with it first, then those containing it
    assert names == ["PlayerController", "PlayerState", "BP_PlayerCharacter_C"]
    assert _scoped("ctx.forEach('enemy", scope, SCRIPT, "unreal")[1] == ["BP_EnemyBase_C"]
    assert _scoped('ctx.find(`', scope, SCRIPT, "unreal")[1] == sorted(
        _CLASSES, key=str.lower)
    # Only the class name: not the object's name, not other calls or strings
    for source in ('ctx.find("A", "Play', 'ctx.call("Play', 'const s = "Play',
                   '// ctx.find("Play'):
        assert _scoped(source, scope, SCRIPT, "unreal")[1] == [], source
    # A native game has no ctx.find; class names being read are pending
    assert _scoped('ctx.find("', scope, SCRIPT, NATIVE)[1] == []
    pending = code_completion.Scope(_NameLookup(code_completion.PENDING))
    assert _scoped('ctx.find("', pending, SCRIPT, "unreal")[1] == [] and pending.pending


def test_the_popup_completes_a_class_name_and_closes_the_string(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module

    editor = _editor_at_end("", engine="unreal")
    editor.set_completion(load_translations("en"), SCRIPT, "unreal", _NameLookup())
    popup = editor.completion_popup
    _typed(editor, 'const p = ctx.find("')
    assert popup.isVisible()  # at once after the quote
    _typed(editor, "Character")
    assert popup.current_entry().name == "BP_PlayerCharacter_C"
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == 'const p = ctx.find("BP_PlayerCharacter_C"'
    # A closing quote already there is kept, not doubled
    editor.setPlainText('ctx.forEach("Enemy", e => e)')
    cursor = editor.textCursor()
    cursor.setPosition(len('ctx.forEach("Enemy'))
    editor.setTextCursor(cursor)
    editor.show_completions(typed=False)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == 'ctx.forEach("BP_EnemyBase_C", e => e)'
    editor.close()


_STATICS = [["GameState.s_playerCharacter", "CharacterStats"],
            ["GameState.s_instance", "GameState"],
            ["UIManager.Instance", "UIManager"]]


class _StaticLookup(_NameLookup):
    """Class names, static fields and the members of _MEMBERS."""

    def __call__(self, name):
        found = _MEMBERS.get(name)
        return None if found is None else code_completion.ClassMembers.from_agent(found)

    def static_fields(self):
        return _STATICS


def test_static_fields_inside_ctx_static():
    scope = code_completion.Scope(_StaticLookup())
    prefix, names = _scoped('const p = ctx.static("player', scope, SCRIPT, "mono")
    assert prefix == "player" and names == ["GameState.s_playerCharacter"]
    _prefix, entries = completions('ctx.static("Game', 'ctx.static("Game', 0, SCRIPT, "mono",
                                   scope)
    assert [entry.name for entry in entries] == ["GameState.s_instance",
                                                 "GameState.s_playerCharacter"]
    assert code_completion.description(entries[1], load_translations("en")) == \
        "Static field: CharacterStats"
    assert "UIManager.Instance" in _scoped("ctx.static('", scope, SCRIPT, "il2cpp")[1]
    # ctx.static is for Unity games only
    assert _scoped('ctx.static("', scope, SCRIPT, "unreal")[1] == []
    assert _scoped('ctx.static("', scope, SCRIPT, NATIVE)[1] == []
    # The same lookup still gives class names to ctx.find
    assert "PlayerState" in _scoped('ctx.find("', scope, SCRIPT, "mono")[1]


def test_a_static_fields_object_offers_its_members():
    scope = code_completion.Scope(_StaticLookup())
    names = _scoped('ctx.static("CharacterStats.m_health").', scope, SCRIPT, "mono")[1]
    assert names[0] == "m_current"
    assert _scoped('ctx.static("CharacterStats:m_health").', scope, SCRIPT, "mono")[1][0] == \
        "m_current"
    assert _scoped('const h = ctx.static("CharacterStats.m_health");\nh.', scope, SCRIPT,
                   "mono")[1][0] == "m_current"
    # A number field: a JavaScript number; an unknown field: nothing
    assert _scoped('ctx.static("CharacterStats.Level").', scope, SCRIPT, "mono")[1][0] == \
        "toFixed"
    assert _scoped('ctx.static("Nothing.Here").', scope, SCRIPT, "mono")[1] == []


def test_names_only_where_an_expression_can_start():
    for source in ("", "  ", "ct", "const p = ", "if (!", "foo(a, ", "return ", "x => ",
                   "a ? ", "{ ", "typeof ", "const s = await ", "[1, "):
        assert "ctx" in _offered(source)[1], source
    # After a closed bracket, a string, a number or a name only an operator fits
    for source in ('ctx.static("GameState.s_isInMainMenu") ', 'ctx.static("A")', "x[0] ",
                   '"text" ', "42 ", "const p ", "player ", "returned "):
        assert _offered(source)[1] == [], source
    assert code_completion.expression_starts("   ")
    assert not code_completion.expression_starts("a.return")


def test_the_string_under_the_mouse():
    line = 'const p = ctx.find("BP_Hero_C"); ctx.static(\'GameState.s_x\');'
    assert code_completion.string_at(line, line.index("Hero")) == ("find", "BP_Hero_C")
    assert code_completion.string_at(line, line.index('"BP')) == ("find", "BP_Hero_C")
    assert code_completion.string_at(line, line.index("s_x")) == ("static", "GameState.s_x")
    assert code_completion.string_at(line, line.index("const")) is None
    assert code_completion.string_at('ctx.find("A", "Name")', 15) is None  # the object's name


class _TooltipLookup:
    """Answers as MemberLookup gives them for tooltips."""

    def __init__(self, answers):
        self.answers = answers

    def class_info(self, name):
        return self.answers.get(("class", name), code_completion.MISSING)

    def static_value(self, name):
        return self.answers.get(("static", name), code_completion.MISSING)

    def static_fields(self):
        return [["GameState.s_menu", "System.Boolean"]]


def test_tooltips_of_classes_and_static_fields():
    translations = load_translations("en")
    scope = code_completion.Scope(_TooltipLookup({
        ("class", "BP_Hero_C"): {"name": "BP_Hero_C", "parents": ["Character", "Pawn"],
                                 "more": 2, "objects": 3},
        ("class", "Game.Combat"): {"name": "Game.Combat", "parents": ["UnityEngine.Object"],
                                   "more": 0, "objects": None},
        ("static", "GameState.s_menu"): {"name": "s_menu", "type": "System.Boolean",
                                         "value": {"text": "false"}},
        ("static", "GameState.s_wait"): code_completion.PENDING,
    }))
    assert code_completion.tooltip("find", "BP_Hero_C", scope, translations, "unreal") == (
        "Class BP_Hero_C\nDerives from Character → Pawn → …\n"
        "Live objects (with subclasses): 3")
    assert code_completion.tooltip("forEach", "Game.Combat", scope, translations, "mono") == (
        "Class Game.Combat\nDerives from UnityEngine.Object")
    assert code_completion.tooltip("find", "BP_Typo_C", scope, translations, "unreal") == (
        "The game has no class named BP_Typo_C")
    assert code_completion.tooltip("static", "GameState.s_menu", scope, translations,
                                   "mono") == "GameState.s_menu: System.Boolean\nValue now: false"
    assert code_completion.tooltip("static", "GameState.s_nope", scope, translations,
                                   "il2cpp") == "The game has no static field GameState.s_nope"
    # Asking the game: it says so and waits
    assert code_completion.tooltip("static", "GameState.s_wait", scope, translations,
                                   "mono") == "Reading from the game …"
    assert scope.pending
    # ctx.static only in Unity games; without a game the type from the list
    assert code_completion.tooltip("static", "GameState.s_menu", scope, translations,
                                   "unreal") is None
    offline = code_completion.Scope(_StaticLookup())
    offline.lookup.static_value = lambda name: None
    assert code_completion.tooltip("static", "GameState.s_playerCharacter", offline,
                                   translations, "mono") == (
        "GameState.s_playerCharacter: CharacterStats")


def test_the_editor_shows_a_tooltip_and_updates_it_when_the_game_answers(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

    class Lookup(QObject):
        changed = Signal()

        def __init__(self):
            super().__init__()
            self.value = code_completion.PENDING

        def static_value(self, name):
            return self.value

        def __call__(self, name):
            return None

    lookup = Lookup()
    editor = _editor_at_end("", engine="mono")
    editor.set_completion(load_translations("en"), SCRIPT, "mono", lookup)
    editor.setPlainText('// ctx.static("GameState.s_menu")\nreturn ctx.static("GameState.s_menu");')
    editor.resize(600, 200)
    qt_app.processEvents()
    block = editor.document().findBlockByNumber(1)
    cursor = editor.textCursor()
    cursor.setPosition(block.position() + block.text().index("s_menu"))
    over = editor.cursorRect(cursor).center()
    global_position = editor.viewport().mapToGlobal(over)
    assert editor.show_string_tooltip(over, global_position) == "Reading from the game …"
    lookup.value = {"name": "s_menu", "type": "System.Boolean", "value": {"text": "true"}}
    assert editor.show_string_tooltip(over, global_position) == (
        "GameState.s_menu: System.Boolean\nValue now: true")
    # Not in a comment, not past the end of a line, not over other code
    cursor.setPosition(editor.document().findBlockByNumber(0).position() + 20)
    assert editor.show_string_tooltip(editor.cursorRect(cursor).center(), global_position) is None
    cursor.setPosition(block.position() + 2)  # "return"
    assert editor.show_string_tooltip(editor.cursorRect(cursor).center(), global_position) is None
    editor.close()


def _signature(source, scope=None, context=SCRIPT, engine="mono"):
    found = code_completion.signature_help(source, context, engine, scope)
    if found is None:
        return None
    signatures, index = found
    return [(signature.name, signature.params, signature.returns) for signature in signatures], index


def test_the_signature_of_the_call_being_typed():
    assert _signature("ctx.find(") == ([("find", ("className", "[objectName]"), None)], 0)
    # Arguments are counted past strings, nested calls and brackets
    assert _signature('ctx.find("A, (b", ')[1] == 1
    assert _signature('ctx.native.fn(at, "void", ["int", f(1, 2)], ')[1] == 3
    assert _signature('ctx.native.fn(')[0][0][1] == ("address", "returnType", "argTypes",
                                                     "[abi]")
    assert _signature('ctx.log(a, b, ')[0][0][1] == ("...items",)
    assert _signature("ptr(")[0][0][0] == "ptr"
    assert _signature('const p = ctx.find("A").address;\np.add(0x10).writeS32(')[0][0][0] == \
        "writeS32"
    # Inside the string being typed as the first argument
    assert _signature('ctx.find("BP_Pla')[1] == 0
    # No call, a closed one, a comment, an unknown function, another engine's
    for source in ("ctx.find", 'ctx.find("x")', "// ctx.find(", "/* ctx.find(", "foo(",
                   "if (", "function heal(", "f()("):
        assert _signature(source) is None, source
    assert _signature("ctx.isPlaying(", engine="mono") is None
    assert _signature("ctx.isPlaying(", engine="unreal")[0][0][1] == ()


def test_the_signatures_of_a_game_methods_overloads():
    members = code_completion.ClassMembers.from_agent({
        "name": "Inventory", "fields": [], "properties": [],
        "methods": [
            {"name": "Add", "params": [["System.Int32", "count"]], "returns": "System.Void",
             "static": False},
            {"name": "Add", "params": [["Item", "item"], ["System.Int32", "count"]],
             "returns": "System.Boolean", "static": False}]})
    scope = code_completion.Scope(lambda name: members)
    signatures, index = _signature('ctx.find("Inventory").Add(item, ', scope)
    assert index == 1
    assert signatures == [("Add", ("System.Int32 count",), "System.Void"),
                          ("Add", ("Item item", "System.Int32 count"), "System.Boolean")]
    # The wrapper's own methods where the game has none of that name
    assert _signature('ctx.find("Inventory").call(', scope)[0][0][1] == ("name", "...args")
    # The game is still asked
    pending = code_completion.Scope(lambda name: code_completion.PENDING)
    assert code_completion.signature_help('ctx.find("Inventory").Heal(', SCRIPT, "mono",
                                          pending) is None
    assert pending.pending


def test_the_editor_shows_the_signature_with_the_current_argument(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module

    editor = _editor_at_end("", engine="unreal")
    hint = editor.signature_hint
    _typed(editor, "ctx.find(")
    assert hint.isVisible()
    assert "<b>className</b>" in hint.text() and "Find a live object" not in hint.text()
    assert "A live object of the class" in hint.text()  # the description
    _typed(editor, '"BP_Hero_C", ')
    assert "<b>[objectName]</b>" in hint.text()
    _typed(editor, '"Hero")')
    assert not hint.isVisible()  # the call is closed
    # Escape closes it; Ctrl+Shift+Space opens it again
    _typed(editor, ";\nctx.log(")
    assert hint.isVisible()
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    assert not hint.isVisible()
    QTest.keyClick(editor, Qt.Key.Key_Space,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert hint.isVisible() and "<b>...items</b>" in hint.text()
    # Moving the cursor out of the call hides it
    editor.moveCursor(editor.textCursor().MoveOperation.Start)
    assert not hint.isVisible()
    # A method chosen in the popup shows its signature at once
    editor.setPlainText("")
    _typed(editor, "ctx.fin")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == "ctx.find()" and hint.isVisible()
    editor.close()


# The classes of the Lies of P script: an equipment, its use slots (an Unreal
# array of structs with an FString) and an item
_LIES_OF_P = {
    "LPlayerEquipment": {"name": "LPlayerEquipment", "fields": [], "properties": [], "methods": [
        {"name": "GetUseSlotsLine_AllSlot", "params": [["bool", "FirstLine"]],
         "returns": "TArray<FLUseSlotData>", "static": False},
        {"name": "GetUseItem_SpecifiedLine", "params": [["int32", "SlotIndex"],
                                                        ["bool", "FirstLine"]],
         "returns": "ULItem*", "static": False}]},
    "FLUseSlotData": {"name": "LUseSlotData", "properties": [], "methods": [], "fields": [
        {"name": "ItemCodeName", "type": "FString", "static": False},
        {"name": "SlotIndex", "type": "int32", "static": False}]},
    "ULItem": {"name": "LItem", "fields": [], "properties": [], "methods": [
        {"name": "SetCount", "params": [["int32", "Count"]], "returns": "void",
         "static": False}]},
}
_SCRIPT_HEAD = ('if (!ctx.isPlaying()) return;\n\n'
                'const equipment = ctx.find("LPlayerEquipment");\n'
                'const slots = equipment?.GetUseSlotsLine_AllSlot(true);\n')


def _lies_of_p(source):
    found = {name: code_completion.ClassMembers.from_agent(members)
             for name, members in _LIES_OF_P.items()}
    scope = code_completion.Scope(found.get)
    return _scoped(_SCRIPT_HEAD + source, scope, SCRIPT, "unreal")


def test_unreal_arrays_their_elements_and_callback_parameters():
    array = ["length", "find", "filter", "map", "forEach", "some", "every", "findIndex"]
    assert _lies_of_p("slots?.")[1][:8] == array
    # The element: in a callback, through find, an index, at and for … of
    for source in ("const bp = slots?.find(u => u.", "slots.filter((u, i) => u.",
                   'const bp = slots?.find(u => u.SlotIndex > 0);\nbp.',
                   "const bp = slots[0];\nbp.", "slots.at(-1).", "for (const s of slots) s.",
                   "slots.filter(u => u).find(u => u."):
        assert _lies_of_p(source)[1][:2] == ["ItemCodeName", "SlotIndex"], source
    # Texts and numbers are JavaScript's
    names = _lies_of_p("const bp = slots?.find(u => u.ItemCodeName?.")[1]
    assert {"startsWith", "includes", "toLowerCase", "length"} <= set(names)
    assert _lies_of_p("slots.length.")[1][0] == "toFixed"
    assert _lies_of_p("slots[0].ItemCodeName.split(',').")[1][1] == "find"
    # The item the method returns
    assert _lies_of_p("const item = equipment.GetUseItem_SpecifiedLine(0, true);\n"
                      "item.")[1][0] == "SetCount"
    # A callback of something not known gives its parameter nothing
    assert _lies_of_p("list.find(u => u.")[1] == []


def test_the_scripts_own_variables_come_first():
    names = _lies_of_p("const bp = 1;\nfor (const s of slots) {}\nslots.map((v, i) => v);\n"
                       "function heal() {}\n  ")[1]
    assert names[:7] == ["equipment", "slots", "bp", "s", "v", "i", "heal"]
    assert names[7] == "ctx"  # then the globals and keywords
    assert code_completion.declared_names("const ctx = 1; let return_ = 2") == ["return_"]


def test_tooltips_of_the_scripts_variables():
    translations = load_translations("en")
    found = {name: code_completion.ClassMembers.from_agent(members)
             for name, members in _LIES_OF_P.items()}
    scope = code_completion.Scope(found.get)
    source = (_SCRIPT_HEAD + 'const bp = slots?.find(u => u.ItemCodeName?.startsWith("C"));\n'
              "const code = bp.ItemCodeName;\nconst count = bp.SlotIndex + 1;\n"
              "const at = bp.address;\nconst x = foo();")

    def tip(name):
        return code_completion.variable_tooltip(name, source, scope, translations)

    assert tip("equipment") == "Variable equipment: LPlayerEquipment"
    assert tip("slots") == "Variable slots: array of FLUseSlotData"
    assert tip("bp") == "Variable bp: FLUseSlotData"
    assert tip("u") == "Variable u: FLUseSlotData"
    assert tip("code") == "Variable code: text (string)"
    assert tip("at") == "Variable at: NativePointer (an address)"
    assert tip("x") == "Variable x: type not known"
    assert tip("ctx") is None and tip("nothing") is None
    pending = code_completion.Scope(lambda name: code_completion.PENDING)
    assert code_completion.variable_tooltip("slots", source, pending, translations) == (
        "Reading from the game …")
    assert code_completion.identifier_at("  item.SetCount(3)", 3) == ("item", 2)


def test_the_editor_shows_a_variables_tooltip(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    found = {name: code_completion.ClassMembers.from_agent(members)
             for name, members in _LIES_OF_P.items()}

    class Lookup:
        def __call__(self, name):
            return found.get(name)

    editor = _editor_at_end("", engine="unreal")
    editor.set_completion(load_translations("en"), SCRIPT, "unreal", Lookup())
    editor.setPlainText(_SCRIPT_HEAD + "// slots\nreturn slots.length;")
    editor.resize(700, 300)
    qt_app.processEvents()

    def tip_over(line_number, word):
        block = editor.document().findBlockByNumber(line_number)
        cursor = editor.textCursor()
        cursor.setPosition(block.position() + block.text().index(word) + 1)
        point = editor.cursorRect(cursor).center()
        return editor.show_string_tooltip(point, editor.viewport().mapToGlobal(point))

    assert tip_over(3, "slots") == "Variable slots: array of FLUseSlotData"  # its declaration
    assert tip_over(5, "slots") == "Variable slots: array of FLUseSlotData"
    assert tip_over(4, "slots") is None  # in a comment
    assert tip_over(5, "length") is None  # a member
    assert tip_over(5, "return") is None  # a keyword
    editor.close()


def test_a_variables_entry_names_what_it_holds():
    translations = load_translations("en")
    found = {name: code_completion.ClassMembers.from_agent(members)
             for name, members in _LIES_OF_P.items()}
    scope = code_completion.Scope(found.get)
    source = _SCRIPT_HEAD + "let test = foo();\n  "
    _prefix, entries = completions(source, "  ", 0, SCRIPT, "unreal", scope)
    described = {entry.name: code_completion.description(entry, translations)
                 for entry in entries[:3]}
    assert described == {"equipment": "Variable of this script: LPlayerEquipment",
                         "slots": "Variable of this script: array of FLUseSlotData",
                         "test": "Variable of this script"}


def test_names_are_bold_and_descriptions_quieter(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtGui import QFont, QFontInfo  # pylint: disable=import-error,no-name-in-module

    editor = _editor_at_end("", engine="unreal")
    large = QFont(qt_app.font())
    large.setPixelSize(QFontInfo(editor.font()).pixelSize() + 6)
    editor.completion_popup.setFont(large)  # an interface font larger than the code
    _typed(editor, "ctx.")
    model = editor.completion_popup.model()
    name, detail = model.item(0, 0), model.item(0, 1)
    code = QFontInfo(editor.font()).pixelSize()
    assert QFontInfo(name.font()).pixelSize() == code and name.font().bold()
    # A little smaller than the code, not as large as the interface font
    assert QFontInfo(detail.font()).pixelSize() == round(code * 0.9)
    assert detail.foreground().color().alphaF() < 1
    for item in (name, detail):
        assert item.textAlignment() & Qt.AlignmentFlag.AlignVCenter
    editor.close()


def test_cached_unreal_methods_without_names_still_load():
    # Entries cached before the Unreal adapter added the names end with the flags
    from grembyte.features.catalog import catalog_methods  # pylint: disable=import-outside-toplevel

    old = ["Rename", "(FString,bool)", "void", False, 7, "Hero:Rename (FString,bool)", 0, 0]
    new = old + [["NewName", "bLoud"]]
    [before], [after] = (catalog_methods("/Script/Game", [["Hero", [entry]]], "unreal")
                         for entry in (old, new))
    assert before.argument_names == () and before.display_signature == before.signature
    assert after.display_signature == "Hero:Rename (FString NewName,bool bLoud)"
    # A new method hook's ctx.args from that signature
    assert code_completion.method_parameters(after.display_signature) == (
        "Hero", {"NewName": "FString", "bLoud": "bool"})


# --- ctx.call ---------------------------------------------------------------------------

_STATIC_METHODS = [
    ["Game.Combat:RollDamage", "System.Int32 min, System.Int32 max", "System.Single"],
    ["Game.Combat:RollDamage", "System.Int32 max", "System.Single"],
    ["Game.Combat:get_Count", "", "System.Int32"],
    ["GameState:GetPlayer", "", "CharacterStats"],
]


class _CallLookup(_StaticLookup):
    """Static methods besides class names, static fields and _MEMBERS."""

    def static_methods(self):
        return _STATIC_METHODS


def test_static_methods_inside_ctx_call():
    scope = code_completion.Scope(_CallLookup())
    prefix, names = _scoped('ctx.call("roll', scope, SCRIPT, "mono")
    # One entry per overload; matched anywhere in the name
    assert prefix == "roll" and names == ["Game.Combat:RollDamage"] * 2
    _prefix, entries = completions('ctx.call("Game.', 'ctx.call("Game.', 0, SCRIPT, "unreal",
                                   scope)
    assert [entry.label for entry in entries][:2] == [
        "Game.Combat:get_Count()", "Game.Combat:RollDamage(System.Int32 min, System.Int32 max)"]
    assert code_completion.description(entries[0], load_translations("de")) == \
        "Statische Methode → System.Int32"
    assert "GameState:GetPlayer" in _scoped("ctx.call('", scope, SCRIPT, "il2cpp")[1]
    # Also in a method hook; not in a native game, not after the method's name
    assert _scoped('ctx.call("GetP', scope)[1] == ["GameState:GetPlayer"]
    assert _scoped('ctx.call("', scope, SCRIPT, NATIVE)[1] == []
    assert "ctx" in _scoped('ctx.call("Game.Combat:get_Count", ', scope, SCRIPT)[1]


def test_what_ctx_call_returns_and_its_signature():
    scope = code_completion.Scope(_CallLookup())
    # The return type comes from the class's members (as ctx.static's field type)
    assert _scoped('ctx.call("CharacterStats:GetHealth").', scope, SCRIPT)[1][0] == "m_current"
    assert _scoped('const h = ctx.call("CharacterStats.GetHealth ()");\nh.', scope,
                   SCRIPT)[1][0] == "m_current"
    assert _scoped('ctx.call("CharacterStats:Nope").', scope, SCRIPT)[1] == []
    # The arguments after the name: the method's parameters, per overload
    assert _signature('ctx.call("Game.Combat:RollDamage", 1, ', scope) == ([
        ("ctx.call", ('"Game.Combat:RollDamage"', "System.Int32 min", "System.Int32 max"),
         "System.Single"),
        ("ctx.call", ('"Game.Combat:RollDamage"', "System.Int32 max"), "System.Single")], 2)
    # A dot or a parameter list in the name, other case: the same method
    assert _signature('ctx.call("game.combat.rolldamage (int,int)", ', scope)[0][0][2] == \
        "System.Single"
    # The name itself, or an unknown method: ctx.call's own signature
    assert _signature('ctx.call("Game.', scope)[0][0][0] == "call"
    assert _signature('ctx.call("Nowhere:Thing", ', scope)[0][0][0] == "call"


def test_the_tooltip_of_a_method_in_ctx_call():
    translations = load_translations("en")
    scope = code_completion.Scope(_CallLookup())
    line = 'ctx.call("GameState:GetPlayer");'
    assert code_completion.string_at(line, line.index("Get")) == ("call", "GameState:GetPlayer")
    assert code_completion.tooltip("call", "Game.Combat:RollDamage", scope, translations,
                                   "mono") == (
        "Game.Combat:RollDamage(System.Int32 min, System.Int32 max) → System.Single\n"
        "Game.Combat:RollDamage(System.Int32 max) → System.Single")
    assert code_completion.tooltip("call", "Game.Combat:Nope", scope, translations,
                                   "mono") == "The game has no static method Game.Combat:Nope"
    assert code_completion.tooltip("call", "GameState:GetPlayer", scope, translations,
                                   NATIVE) is None
    # Without a game nothing is known: no tooltip; while reading it says so
    assert code_completion.tooltip("call", "A:B", code_completion.Scope(), translations) is None
    reading = code_completion.Scope(_NameLookup(code_completion.PENDING))
    reading.lookup.static_methods = lambda: code_completion.PENDING
    assert code_completion.tooltip("call", "A:B", reading, translations) == \
        "Reading from the game …"


def test_choosing_a_static_method_goes_on_to_its_arguments(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module

    editor = _editor_at_end("", engine="mono")
    editor.set_completion(load_translations("en"), SCRIPT, "mono", _CallLookup())
    _typed(editor, 'ctx.call("Roll')
    assert editor.completion_popup.current_entry().name == "Game.Combat:RollDamage"
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == 'ctx.call("Game.Combat:RollDamage", '
    assert editor.signature_hint.isVisible()
    # Without parameters only the string is closed
    editor.setPlainText("")
    _typed(editor, 'ctx.call("get_C')
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == 'ctx.call("Game.Combat:get_Count"'
    editor.close()


def test_the_member_lookup_reads_the_static_methods_once(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets.member_lookup import MemberLookup

    class Worker(QObject):
        members_read = members_failed = Signal(int, object)
        names_read = Signal(int, object)
        names_failed = Signal(int, str)
        info_read = statics_read = Signal(int, object)
        info_failed = statics_failed = Signal(int, str)
        asked = 0

        def static_method_names(self):
            self.asked += 1
            return 5

    worker = Worker()
    lookup = MemberLookup(lambda: worker)
    assert lookup.static_methods() is code_completion.PENDING
    assert lookup.static_methods() is code_completion.PENDING and worker.asked == 1
    worker.names_read.emit(5, _STATIC_METHODS)
    assert lookup.static_methods() == _STATIC_METHODS and worker.asked == 1
    assert MemberLookup(lambda: None).static_methods() is None  # no game


def test_a_scripts_parameters_after_ctx_args(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from grembyte.features.scripts import ScriptParam, ScriptSpec
    from grembyte.ui.widgets.scripts import ScriptDialog

    spec = ScriptSpec("return 1;", "manual", 0.0, True, "Give item", (
        ScriptParam("item", "Item", "choice", "", "return [1];"),
        ScriptParam("count", "Count", "number", "5"),
        ScriptParam("notify", "Note it", "bool", "true")))
    dialog = ScriptDialog(load_translations("en"), spec=spec, engine="mono")
    editor = dialog.code_edit

    def offered(source):
        _prefix, entries = completions(source, source.split("\n")[-1], 0, SCRIPT, "mono",
                                      editor._scope)  # pylint: disable=protected-access
        return [(entry.name, dict(entry.values).get("type")) for entry in entries]

    assert offered("ctx.args.") == [("item", "choice"), ("count", "number"),
                                    ("notify", "boolean")]
    # A number parameter is a JavaScript number, a text one a string
    assert "toFixed" in [name for name, _type in offered("ctx.args.count.")]
    # Added and renamed in the table before saving: offered at once
    dialog.param_editor.btn_add.click()
    dialog.param_editor.table.item(0, 0).setText("itemName")
    assert [name for name, _type in offered("ctx.args.")] == [
        "itemName", "count", "notify", "param1"]
    dialog.param_editor.table.selectRow(3)
    dialog.param_editor.btn_remove.click()
    assert "param1" not in [name for name, _type in offered("ctx.args.")]
    dialog.mark_saved()
    dialog.close()
    dialog.deleteLater()


# --- Ctrl+click -------------------------------------------------------------------------

_LINK_SOURCE = """\
const health = ctx.find("Health", "Companion_Eder(Clone)_0");
const stats = health.m_stats;
const first = ctx.static("GameState.s_instance").Party[0].field("m_name");
let count = 1;
count = 2;
const total = health.GetHealth();
const amount = ctx.args.amount;
for (const item of health.items) { item; }
// ctx.find("InComment")
"""


def _link(line_number, text, token="text", engine="mono", occurrence=0):
    line = _LINK_SOURCE.split("\n")[line_number]
    column = line.index(text)
    for _ in range(occurrence):
        column = line.index(text, column + 1)
    source = "\n".join(_LINK_SOURCE.split("\n")[:line_number + 1])
    return code_completion.inspect_link(line, column, token, source, engine)


def test_the_lookups_ctrl_click_inspects():
    # The strings of ctx.find: the class or the object's name, the whole call evaluated
    link = _link(0, "Health", "string")
    assert link.expression == 'ctx.find("Health", "Companion_Eder(Clone)_0")'
    assert link.name == "Companion_Eder(Clone)_0" and (link.start, link.end) == (24, 32)
    assert _link(0, "Companion", "string").start == 34
    # A variable: its lookup, with the variables it uses put in
    assert _link(0, "health").expression == link.expression
    stats = _link(1, "stats")
    assert stats.expression == '(ctx.find("Health", "Companion_Eder(Clone)_0")).m_stats'
    assert stats.name == "stats" and (stats.start, stats.end) == (6, 11)
    assert _link(1, "health").name == "health"  # used, not declared, on this line
    first = _link(2, "first")
    assert first.expression == 'ctx.static("GameState.s_instance").Party[0].field("m_name")'
    # ctx.static is for Unity games; nothing in games without an adapter
    assert _link(2, "first", engine="unreal") is None
    assert _link(0, "Health", "string", engine=NATIVE) is None
    # Not: assigned twice, a method call, ctx.args, a loop's variable, a member, a comment
    assert _link(4, "count") is None
    assert _link(5, "total") is None
    assert _link(6, "amount") is None
    assert _link(7, "item", occurrence=1) is None
    assert _link(1, "m_stats") is None
    assert _link(8, "InComment", "comment") is None


def test_ctrl_shows_links_and_ctrl_click_opens_them(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PySide6.QtCore import QPoint, Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module

    editor = _editor_at_end(_LINK_SOURCE, engine="mono")
    editor.resize(900, 400)
    editor.moveCursor(editor.textCursor().MoveOperation.Start)  # every line in view
    clicked = []
    editor.set_link_handler(lambda link, position: clicked.append(link))
    block = editor.document().findBlockByNumber(1)
    cursor = editor.textCursor()
    cursor.setPosition(block.position() + 8)  # in "stats"
    point = editor.cursorRect(cursor).center()
    assert editor.link_at(point)[1].name == "stats"
    # Ctrl held: underlined in the link colour, a pointing hand
    editor._update_link(point, held=True)
    [selection] = editor.extraSelections()
    assert selection.format.fontUnderline() and selection.cursor.selectedText() == "stats"
    assert editor.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor
    QTest.keyRelease(editor, Qt.Key.Key_Control)
    assert editor.extraSelections() == [] and editor._link is None
    # A plain click moves the cursor; Ctrl+click calls the handler and keeps it
    QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     point)
    assert clicked == []
    before = editor.textCursor().position()
    QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier, point)
    assert [link.name for link in clicked] == ["stats"]
    assert editor.textCursor().position() == before
    # Without a handler nothing is a link
    editor.set_link_handler(None)
    assert editor.link_at(point) is None
    assert editor.link_at(QPoint(5000, 5)) is None
    editor.close()


def test_ctrl_click_in_the_script_editor_opens_the_inspector(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PySide6.QtCore import QPoint  # pylint: disable=import-error,no-name-in-module

    from grembyte.features.scripts import ScriptSpec
    from grembyte.ui.widgets import scripts
    from tests.test_scripts import _FakeScriptWorker

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    panel = scripts.ScriptPanel(load_translations("en"))
    messages = []
    try:
        panel.set_process(4242)
        script_id = panel.add_script(ScriptSpec(_LINK_SOURCE, label="Eder"))
        editor = panel._open_editor(script_id)
        monkeypatch.setattr(editor.code_edit, "show_link_message",
                            lambda text, _position: messages.append(text))
        inspected = []
        monkeypatch.setattr(panel.inspections, "inspect",
                            lambda target, name="": inspected.append((target, name)))
        worker = panel._worker

        link = _link(1, "stats")
        editor.code_edit._link_handler(link, QPoint(1, 1))
        assert worker.requests[-1] == ("inspect_expression", link.expression)
        request = len(worker.requests)
        worker.expression_inspected.emit(request, {"text": "CharacterStats@0x10",
                                                   "target": {"address": "0x10"}})
        assert inspected == [({"address": "0x10"}, "stats")] and messages == []
        # Found nothing, or a failure: said where the click was
        editor.code_edit._link_handler(link, QPoint(1, 1))
        worker.expression_inspected.emit(len(worker.requests), {"text": "null"})
        assert messages == ["stats is no object of the game now: null"]
        editor.code_edit._link_handler(link, QPoint(1, 1))
        worker.expression_failed.emit(len(worker.requests), "no class Health")
        assert messages[-1] == "no class Health"
        # The game gone: nothing is asked
        count = len(worker.requests)
        panel.set_process(None)
        editor.code_edit._link_handler(link, QPoint(1, 1))
        assert len(worker.requests) == count and "attached" in messages[-1]
        editor.mark_saved()
        editor.close()
    finally:
        panel.shutdown()
        panel.deleteLater()
