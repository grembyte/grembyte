"""The scripts tab: its dialog and texts (running scripts: see test_unreal)."""

import json
import os
from pathlib import Path

import pytest

from hermes.features.scripts import ScriptSpec
from hermes.ui.localization import load_translations


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module
    return QApplication.instance() or QApplication([])


def test_both_languages_name_the_same_script_texts():
    folder = Path(__file__).parent.parent / "hermes" / "ui" / "localization"
    keys = {language: {key for key in json.loads((folder / f"{language}.json").read_text("utf-8"))
                       if key.startswith("scripts.")}
            for language in ("en", "de")}
    assert keys["en"] == keys["de"]


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_dialog_builds_a_script_and_rejects_busy_loops(qt_app, language):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.scripts import ScriptDialog, timing_text

    translations = load_translations(language)
    dialog = ScriptDialog(translations)
    dialog.label_edit.setText("Heal")
    dialog.code_edit.setPlainText("ctx.find('AlabamaCheatManager').call('GiveHealth', 50);")
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("repeat"))
    dialog.seconds_spin.setValue(0)
    dialog._accept()  # pylint: disable=protected-access
    assert dialog.spec is None and dialog.error_label.text()

    dialog.seconds_spin.setValue(30)
    dialog._accept()  # pylint: disable=protected-access
    assert dialog.spec == ScriptSpec("ctx.find('AlabamaCheatManager').call('GiveHealth', 50);",
                                     "repeat", 30, True, "Heal")
    assert "30" in timing_text(translations, dialog.spec)

    # Editing keeps whether the script is paused; manual scripts have no delay.
    paused = ScriptSpec("return 1;", "once", 5, False, "Later")
    dialog = ScriptDialog(translations, spec=paused)
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("manual"))
    assert not dialog.seconds_spin.isEnabled()
    dialog._accept()  # pylint: disable=protected-access
    assert dialog.spec == ScriptSpec("return 1;", "manual", 0.0, False, "Later")
