"""Trace filters: choosing the methods worth tracing (no target process needed)."""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermes.db.base import Base
from hermes.db.repository.trace_filters import TraceFilterRepository
from hermes.features.catalog import CatalogMethod
from hermes.features.trace_filters import (
    BUILTIN_FILTERS,
    TraceFilter,
    has_number,
    is_generated,
    looks_like_change,
    name_parts,
    select_methods,
    word_matches,
)
from hermes.ui.localization import load_translations


def _method(class_name, name, parameters="()", return_type="void", assembly="Assembly-CSharp",
            token=1):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    return CatalogMethod(assembly, class_name, name, parameters, return_type, False, token,
                         f"{class_name}:{name} {parameters}")


_METHODS = [
    _method("Game.Inventory", "AddGold", "(int&,int)", token=1),
    _method("Game.Inventory", "get_Items", "()", "System.Collections.Generic.List`1", token=2),
    _method("Game.Player", "SetStateHealth", "(int)", token=3),
    _method("Game.StateMachine", "Tick", token=4),
    _method("Game.Garden", "Marigold", token=5),
    _method("Game.Player/<>c__DisplayClass1_0", "<Heal>b__0", token=6),
    _method("UI.HPBar", "set_Value", "(single)", token=7),
    _method("Game.Hero", "GetStat", "(int)", "int", token=8),
    _method("Game.Combat", "RollDamage", "(int,single)", "int", token=9),
    _method("AC.GlobalVariables", "SetIntegerValue", "(int,int)", token=10),
]


def _names(methods):
    return [f"{m.class_name}:{m.name}" for m in methods]


def _builtin(key):
    return next(f for f in BUILTIN_FILTERS if f.builtin == key)


@pytest.mark.parametrize("text, parts", [
    ("HPBar", ["hp", "bar"]),
    ("SetStateHealth", ["set", "state", "health"]),
    ("gold_amount", ["gold", "amount"]),
    ("addGold2", ["add", "gold", "2"]),
    ("Game.Inventory", ["game", "inventory"]),
])
def test_name_parts(text, parts):
    assert name_parts(text) == parts


@pytest.mark.parametrize("word, part, matches", [
    ("item", "item", True), ("item", "items", True), ("item", "itemize", False),
    ("stat", "state", False), ("stat", "stats", True),
    ("inventory", "inventoryslot", True), ("gold", "marigold", False),
])
def test_short_words_match_exactly_long_words_as_prefix(word, part, matches):
    assert word_matches(word, part) is matches


def test_signature_rules():
    add_gold, items, set_health = _METHODS[0], _METHODS[1], _METHODS[2]
    assert has_number(add_gold) and not has_number(items)
    assert looks_like_change(add_gold) and looks_like_change(_METHODS[6])  # set_Value
    assert not looks_like_change(items) and looks_like_change(set_health)
    assert is_generated(_METHODS[5]) and not is_generated(add_gold)


def test_builtin_filters_pick_game_values_not_lookalikes():
    assert _names(select_methods(_METHODS, [_builtin("money")])) == ["Game.Inventory:AddGold"]
    assert _names(select_methods(_METHODS, [_builtin("inventory")])) == [
        "Game.Inventory:AddGold", "Game.Inventory:get_Items"]
    stats = _names(select_methods(_METHODS, [_builtin("stats")]))
    assert "Game.Hero:GetStat" in stats and "UI.HPBar:set_Value" in stats
    assert "Game.StateMachine:Tick" not in stats
    assert _names(select_methods(_METHODS, [_builtin("combat")])) == ["Game.Combat:RollDamage"]
    assert "AC.GlobalVariables:SetIntegerValue" in _names(
        select_methods(_METHODS, [_builtin("progress")]))


def test_filters_combine_with_or_and_words_narrow():
    both = select_methods(_METHODS, [_builtin("money"), _builtin("combat")])
    assert _names(both) == ["Game.Inventory:AddGold", "Game.Combat:RollDamage"]
    assert _names(select_methods(_METHODS, [_builtin("money"), _builtin("combat")],
                                 ["roll"])) == ["Game.Combat:RollDamage"]
    # without filters only the words count
    assert _names(select_methods(_METHODS, [], ["inventory"])) == [
        "Game.Inventory:AddGold", "Game.Inventory:get_Items"]


def test_generated_code_is_left_out_unless_asked_for():
    healing = TraceFilter.from_text("Heilung", "heal")
    assert select_methods(_METHODS, [healing]) == []
    assert _names(select_methods(_METHODS, [healing], generated=True)) == [
        "Game.Player/<>c__DisplayClass1_0:<Heal>b__0"]


def test_user_filter_from_text_with_exclusions_and_rules():
    trace_filter = TraceFilter.from_text("Spieler", "Player, hero;  STAT", "state",
                                         numbers=True)
    assert trace_filter.words == ("player", "hero", "stat")
    assert trace_filter.exclude == ("state",)
    assert _names(select_methods(_METHODS, [trace_filter])) == ["Game.Hero:GetStat"]
    rebuilt = TraceFilter.from_json("Spieler", trace_filter.to_json())
    assert rebuilt == trace_filter
    assert not TraceFilter("leer").matches(_METHODS[0])


def test_copy_of_a_builtin_filter_is_editable():
    copy = _builtin("money").copy_as("Geld (Kopie)")
    assert copy.builtin == "" and copy.filter_id is None and copy.words == _builtin("money").words


@pytest.fixture(name="repository")
def fixture_repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    return TraceFilterRepository(sessionmaker(bind=engine))


def test_repository_keeps_user_filters(repository):
    gold_id = repository.save_filter(TraceFilter.from_text("Gold", "gold coin"))
    repository.save_filter(TraceFilter.from_text("Alpha", "alpha", numbers=True))
    assert [f.name for f in repository.list_filters()] == ["Alpha", "Gold"]
    with pytest.raises(ValueError):
        repository.save_filter(TraceFilter.from_text("Gold", "other"))
    repository.save_filter(TraceFilter.from_text("Gold", "gold coin money", filter_id=gold_id))
    gold = next(f for f in repository.list_filters() if f.name == "Gold")
    assert gold.words == ("gold", "coin", "money") and gold.filter_id == gold_id
    repository.delete_filter(gold_id)
    assert [f.name for f in repository.list_filters()] == ["Alpha"]


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    return QApplication.instance() or QApplication([])


def test_panel_counts_and_selects(qt_app, repository):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from hermes.ui.widgets.trace_filters import TraceFilterPanel

    translations = load_translations("de")
    repository.save_filter(TraceFilter.from_text("Gold", "gold"))
    panel = TraceFilterPanel(translations, repository)
    assert panel.selection() is None  # no method list yet
    panel.set_candidates(_METHODS)
    texts = [panel.list.item(row).text() for row in range(panel.list.count())]
    assert "Geld (1)" in texts and "Gold (1)" in texts and "Kampf (1)" in texts
    # nothing ticked: every method but the generated one
    assert len(panel.selection()) == len(_METHODS) - 1
    combat = next(row for row in range(panel.list.count())
                  if panel.list.item(row).text().startswith("Kampf"))
    panel.list.item(combat).setCheckState(Qt.CheckState.Checked)
    assert _names(panel.selection()) == ["Game.Combat:RollDamage"]
    # built-in filters cannot be deleted
    panel.list.setCurrentRow(combat)
    assert panel.edit_button.isEnabled() and not panel.delete_button.isEnabled()


def test_editor_saves_a_new_filter_without_repository(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets.trace_filters import TraceFilterDialog, TraceFilterPanel

    translations = load_translations("de")
    panel = TraceFilterPanel(translations)
    panel.set_candidates(_METHODS)
    dialog = TraceFilterDialog(translations, TraceFilter(""), panel.candidates, panel._save)
    dialog._accept()
    assert dialog.error_label.text() == translations["trace.filter_error_name"]
    dialog.name_input.setText("Würfel")
    dialog.words_input.setText("roll")
    assert dialog.preview_label.text() == translations["trace.filter_preview"].format(count=1)
    dialog._accept()
    assert dialog.result_filter is not None
    assert [f.name for f in panel._user_filters] == ["Würfel"]
    duplicate = TraceFilterDialog(translations, TraceFilter(""), panel.candidates, panel._save)
    duplicate.name_input.setText("Würfel")
    duplicate.words_input.setText("dice")
    duplicate._accept()
    assert duplicate.result_filter is None and "Würfel" in duplicate.error_label.text()
