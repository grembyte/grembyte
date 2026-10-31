"""The GAS attributes window (see hermes.ui.widgets.gas_attributes)."""

import pytest

from hermes.ui.localization import load_translations
from hermes.ui.widgets.gas_attributes import (
    format_value,
    held_value,
    hold_label,
    hold_spec,
    parse_value,
)

from tests.test_scripts import _FakeScriptWorker, fixture_qt_app  # pylint: disable=unused-import


_STATE = {
    "owner": "BPC_AlabamaPlayer_C_1", "setter": "Library:SetFloatAttributeBase",
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


def test_window_lists_sets_and_holds_values_by_scripts(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets import scripts
    from hermes.ui.widgets.gas_attributes import GasAttributesDialog

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
        assert dialog.owner_label.text() == "Spielfigur: BPC_AlabamaPlayer_C_1"
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

        # In the main menu there is no player: said so, the list is empty
        dialog.refresh()
        worker.gas_listed.emit(len(worker.requests), None)
        assert table.rowCount() == 0
        assert "Keine Spielfigur" in dialog.status_label.text()
    finally:
        dialog.close()
        panel.shutdown()
        panel.deleteLater()
