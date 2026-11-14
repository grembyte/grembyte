"""The GAS attributes window (see grembyte.ui.widgets.gas_attributes)."""

import pytest

from grembyte.ui.localization import load_translations
from grembyte.ui.widgets.gas_attributes import (
    format_value,
    held_value,
    hold_label,
    hold_spec,
    parse_value,
    script_set_name,
)

from tests.test_scripts import _FakeScriptWorker, fixture_qt_app  # pylint: disable=unused-import


_STATE = {
    "owner": "BPC_AlabamaPlayer_C_1", "ownerClass": "ABPC_AlabamaPlayer_C",
    "ownerAddress": "0x7ff612340000", "setter": "Library:SetFloatAttributeBase",
    "component": True,
    "sets": [{"name": "AttributeSetCore", "className": "UAlabamaAttributeSetCore",
              "attributes": [{"name": "Might", "base": 4, "current": 5},
                             {"name": "DamageOutgoingMult", "base": 1, "current": 1.399999976},
                             {"name": "Health", "base": 200, "current": 200}]}],
}


def test_values_read_short_and_type_in_either_locale():
    assert format_value(1.399999976158142) == "1.4"
    assert format_value(3.0) == "3"
    assert parse_value(" 2,5 ") == 2.5 and parse_value("7") == 7
    with pytest.raises(ValueError):
        parse_value("viel")
    assert hold_label("Set.Might", 14.0) == "GAS: Set.Might = 14"
    assert held_value("GAS: Set.Might = 14") == 14 and held_value("GAS: kaputt") is None
    spec = hold_spec("Set.Might", 14)
    assert (spec.mode, spec.enabled, spec.label) == ("repeat", True, "GAS: Set.Might = 14")
    assert 'const name = "Set.Might", value = 14.0;' in spec.source


def test_scripts_name_sets_without_their_object_number():
    names = ["AttributeSet_Health_2147445233", "AttributeSetCore",
             "Twin_7", "Twin_8", "Stat_2", "Stat"]
    assert script_set_name(names[0], names) == "AttributeSet_Health"
    assert script_set_name("AttributeSetCore", names) == "AttributeSetCore"
    # The number tells these apart: kept
    assert script_set_name("Twin_7", names) == "Twin_7"
    assert script_set_name("Stat_2", names) == "Stat_2"
    assert script_set_name("_5", ["_5"]) == "_5"


def test_window_lists_sets_and_holds_values_by_scripts(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import scripts
    from grembyte.ui.widgets.gas_attributes import GasAttributesDialog

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    dialog = GasAttributesDialog(translations, panel)
    try:
        worker = panel._worker
        assert worker.requests[-1] == ("gas_attributes",)
        worker.gas_listed.emit(len(worker.requests), _STATE)
        table = dialog.table
        assert [[table.item(row, column).text() for column in range(5)]
                for row in range(table.rowCount())] == [
            ["AttributeSetCore", "Might", "4", "5", ""],
            ["AttributeSetCore", "DamageOutgoingMult", "1", "1.4", ""],
            ["AttributeSetCore", "Health", "200", "200", ""]]
        assert dialog.owner_label.text() == "Spielfigur: BPC_AlabamaPlayer_C_1 (ABPC_AlabamaPlayer_C)"
        # Effects change these (current differs from base)
        dialog.changed_check.setChecked(True)
        assert table.rowCount() == 2
        dialog.changed_check.setChecked(False)
        dialog.search_input.setText("damage")
        assert table.rowCount() == 1
        dialog.search_input.clear()

        # A base value typed in (with a decimal comma) is set through the game
        table.item(1, 2).setText("2,5")
        assert worker.requests[-1] == ("gas_set_base", "AttributeSetCore.DamageOutgoingMult",
                                       2.5)
        table.item(1, 2).setText("viel")
        assert "keine Zahl" in dialog.status_label.text()
        assert table.item(1, 2).text() == "1"  # shown as read again

        # Held: a repeating script in the scripts tab, saved with the game's scripts
        table.selectRow(0)
        assert not dialog.release_button.isEnabled()
        dialog.hold_button.click()
        held = panel.scripts_labelled("GAS: ")
        assert [spec.label for spec in held.values()] == ["GAS: AttributeSetCore.Might = 4"]
        assert [spec.mode for spec in held.values()] == ["repeat"]
        assert table.item(0, 4).text() == "✓ 4"
        assert dialog.release_button.isEnabled()

        # Setting a held value holds the new one: the script would set it back
        dialog.set_base("AttributeSetCore.Might", 14)
        assert [spec.label for spec in panel.scripts_labelled("GAS: ").values()] == [
            "GAS: AttributeSetCore.Might = 14"]
        assert len(panel._scripts) == 1

        dialog.release_button.click()
        assert not panel.scripts_labelled("GAS: ")

        # A set numbered anew on every load (Gothic 1 Remake): shown as it
        # is, set and held without the number, which the next load changes
        dialog.refresh()
        worker.gas_listed.emit(len(worker.requests), {**_STATE, "sets": [
            {"name": "AttributeSet_Health_2147445233", "className": "UAttributeSet_Health",
             "attributes": [{"name": "Health", "base": 40, "current": 40}]}]})
        assert table.item(0, 0).text() == "AttributeSet_Health_2147445233"
        table.item(0, 2).setText("64")
        assert worker.requests[-1] == ("gas_set_base", "AttributeSet_Health.Health", 64)
        table.selectRow(0)
        dialog.hold_button.click()
        assert [spec.label for spec in panel.scripts_labelled("GAS: ").values()] == [
            "GAS: AttributeSet_Health.Health = 40"]
        assert table.item(0, 4).text() == "✓ 40"
        dialog.release_button.click()

        # A game without GAS (Queen's Domain): no sets and no setter. Said
        # is that there are no sets; the player's own fields hold its values,
        # one click opens them in the object inspector.
        dialog.refresh()
        worker.gas_listed.emit(len(worker.requests), {**_STATE, "setter": None,
                                                      "component": False, "sets": []})
        assert table.rowCount() == 0
        assert "keine Attribut-Sets" in dialog.status_label.text()
        assert dialog.inspect_button.isEnabled()
        dialog.inspect_button.click()
        assert ("inspect", {"address": "0x7ff612340000"}) in worker.requests
        assert [inspector.name for inspector in panel.inspections.inspectors] == [
            "BPC_AlabamaPlayer_C_1"]

        # In the main menu there is no player: said so, the list is empty
        dialog.refresh()
        worker.gas_listed.emit(len(worker.requests), None)
        assert table.rowCount() == 0
        assert "Keine Spielfigur" in dialog.status_label.text()
        assert not dialog.inspect_button.isEnabled()
    finally:
        for inspector in list(panel.inspections.inspectors):
            inspector.close()
        dialog.close()
        panel.shutdown()
        panel.deleteLater()


def test_answers_after_the_window_closed_reach_it_no_more(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access
    import sys

    import shiboken6  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtCore import QCoreApplication, QEvent, Qt  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets import scripts
    from grembyte.ui.widgets.gas_attributes import GasAttributesDialog
    from grembyte.ui.widgets.object_browser import ObjectBrowserDialog

    # The worker of the scripts tab outlives the windows; a request may still
    # be answered after one closed (Fatekeeper: "GUObjectArray not found"
    # once a second, with "Live" on). An exception in a slot only reaches
    # the excepthook.
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    try:
        gas = GasAttributesDialog(translations, panel)
        browser = ObjectBrowserDialog(translations, panel, is_framework=lambda _name: False)
        worker = panel._worker
        for dialog in (gas, browser):
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(gas) and not shiboken6.isValid(browser)
        for request in range(1, len(worker.requests) + 1):
            worker.gas_failed.emit(request, "the object table (GUObjectArray) was not found")
            worker.gas_listed.emit(request, _STATE)
            worker.browse_failed.emit(request, "the object table (GUObjectArray) was not found")
        qt_app.processEvents()
        assert not errors, errors
    finally:
        panel.shutdown()
        panel.deleteLater()
