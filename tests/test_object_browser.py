"""The object browser window (see grembyte.ui.widgets.object_browser)."""

from grembyte.ui.localization import load_translations
from grembyte.ui.widgets.object_browser import (
    GAME_PACKAGES,
    filter_words,
    is_singleton,
    name_matches,
    object_text,
)

from tests.test_scripts import _FakeScriptWorker, fixture_qt_app  # pylint: disable=unused-import


def _entry(name, count, package, role=None, matched=0, klass=None):
    return {"klass": klass or f"0x{abs(hash(name)) % 0xffffff:x}", "name": name,
            "cppName": f"U{name}", "package": package, "count": count, "role": role,
            "matched": matched}


_CLASSES = {"objects": 5009, "classes": [
    _entry("AbilitySystemComponent", 1, "/Script/GameplayAbilities"),
    _entry("BP_Hero_C", 3, "/Game/Blueprints", klass="0x1000"),
    _entry("HeroAttributeSet", 2, "/Script/FakeGame"),
    _entry("Object", 5000, "/Script/CoreUObject"),
    _entry("PlayerController", 1, "/Script/Engine", role="PlayerController"),
    _entry("GameManager", 1, "/Script/FakeGame"),
    _entry("WidgetTree", 1, "/Script/Engine"),
]}


def _is_framework(name):
    return name.startswith(("/Script/Engine", "/Script/CoreUObject", "/Script/Gameplay"))


def test_names_and_singletons():
    assert filter_words("  Hero  SIDE ") == ["hero", "side"]
    hero = _entry("BP_Hero_C", 3, "/Game/Blueprints")
    assert name_matches(hero, ["hero", "ubp"]) and not name_matches(hero, ["side"])
    assert object_text({"name": "BP_Hero_C_0", "address": "0x10"}) == '"BP_Hero_C_0"@0x10'
    # Plain C# objects have no name; strings may have line breaks
    assert object_text({"name": None, "address": "0x20"}, "Game.Inventory") == \
        "Game.Inventory@0x20"
    assert object_text({"name": "a\nb", "address": "0x30"}) == '"a b"@0x30'
    # An engine single-instance class, or a game class with one object
    assert is_singleton(_entry("PlayerController", 1, "/Script/Engine", "PlayerController"),
                        _is_framework)
    assert is_singleton(_entry("GameManager", 1, "/Script/FakeGame"), _is_framework)
    assert not is_singleton(_entry("WidgetTree", 1, "/Script/Engine"), _is_framework)
    assert not is_singleton(_entry("Manager", 2, "/Script/FakeGame"), _is_framework)


def _names(dialog):
    tree = dialog.tree
    return [tree.topLevelItem(row).text(0) for row in range(tree.topLevelItemCount())]


def test_window_lists_classes_and_opens_their_objects(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import scripts
    from grembyte.ui.widgets.object_browser import ObjectBrowserDialog

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    dialog = ObjectBrowserDialog(translations, panel, is_framework=_is_framework)
    try:
        worker = panel._worker
        assert worker.requests[-1] == ("browse_classes", "", False)
        worker.classes_browsed.emit(len(worker.requests), _CLASSES)
        assert len(_names(dialog)) == 7
        assert dialog.status_label.text() == "7 Klassen, 5009 Objekte"
        combo = dialog.package_combo
        assert [combo.itemText(i) for i in range(3)] == [
            "Alle Pakete (5009)", "Nur Spiel (6)", "/Game/Blueprints (3)"]

        # Packages: the game's own, or one
        combo.setCurrentIndex(combo.findData(GAME_PACKAGES))
        assert _names(dialog) == ["BP_Hero_C", "HeroAttributeSet", "GameManager"]
        combo.setCurrentIndex(combo.findData("/Script/Engine"))
        assert _names(dialog) == ["PlayerController", "WidgetTree"]
        combo.setCurrentIndex(0)

        # Singletons: engine roles and game classes with one object
        dialog.singleton_check.setChecked(True)
        assert _names(dialog) == ["PlayerController", "GameManager"]
        assert dialog.tree.topLevelItem(0).text(3) == "PlayerController"
        assert dialog.tree.topLevelItem(1).text(3) == "ein Objekt"
        dialog.singleton_check.setChecked(False)

        # Opening a class asks for its objects; one opens in the inspector
        hero = dialog.tree.topLevelItem(1)
        hero.setExpanded(True)
        assert worker.requests[-1] == ("browse_instances", "0x1000", "", False)
        worker.instances_browsed.emit(len(worker.requests), {
            "count": 3, "truncated": False, "instances": [
                {"address": "0x7f00", "name": "BP_Hero_C_0", "outer": "/Game/Maps/Start"},
                {"address": "0x7f80", "name": "BP_Hero_C_1", "outer": "/Game/Maps/Start"},
                {"address": "0x7fc0", "name": "Sidekick", "outer": "WidgetTree_7"}]})
        assert [hero.child(i).text(0) for i in range(hero.childCount())] == [
            '"BP_Hero_C_0"@0x7f00', '"BP_Hero_C_1"@0x7f80', '"Sidekick"@0x7fc0']
        assert hero.child(2).toolTip(0) == "Sidekick in WidgetTree_7"
        assert not dialog.inspect_button.isEnabled()
        dialog.tree.setCurrentItem(hero.child(0))
        assert dialog.inspect_button.isEnabled()
        opened = []
        monkeypatch.setattr(panel.inspections, "inspect",
                            lambda target, name="", instance_name=None: opened.append(
                                (target, name, instance_name)))
        dialog.inspect_button.click()
        assert opened == [({"address": "0x7f00"}, "BP_Hero_C_0", "BP_Hero_C_0")]

        # The filter: class names at once, objects' names once the agent counted
        dialog.filter_input.setText("side")
        assert _names(dialog) == []
        dialog._filter_timer.timeout.emit()
        assert worker.requests[-1] == ("browse_classes", "side", False)
        matched = {"objects": 5009, "classes": [
            {**entry, "matched": 1 if entry["name"] == "BP_Hero_C" else 0}
            for entry in _CLASSES["classes"]]}
        worker.classes_browsed.emit(len(worker.requests), matched)
        assert _names(dialog) == ["BP_Hero_C"]
        hero = dialog.tree.topLevelItem(0)
        assert hero.text(1) == "1 / 3"
        # Found by its objects only: opened by itself, its objects filtered
        assert hero.isExpanded()
        assert worker.requests[-1] == ("browse_instances", "0x1000", "side", False)
        worker.instances_browsed.emit(len(worker.requests), {
            "count": 1, "truncated": False, "instances": [
                {"address": "0x7fc0", "name": "Sidekick", "outer": "WidgetTree_7"}]})
        assert hero.childCount() == 1

        # A class matching by name shows with all its objects
        dialog.filter_input.setText("hero")
        assert _names(dialog) == ["BP_Hero_C", "HeroAttributeSet"]
        assert dialog.tree.topLevelItem(0).text(1) == "3"

        # Templates and reflection: read anew
        dialog.internal_check.setChecked(True)
        assert worker.requests[-1] == ("browse_classes", "hero", True)
    finally:
        dialog.close()
        panel.set_process(None)
