"""Tests for the component credits dialog."""

import pytest

from grembyte import __version__
from grembyte.ui.localization import load_translations
from grembyte.ui.widgets.about_dialog import AboutDialog
from PySide6.QtWidgets import QLabel  # pylint: disable=import-error,no-name-in-module
from tests.test_hooks import fixture_qt_app  # pylint: disable=unused-import


@pytest.mark.parametrize("language", ("en", "de"))
def test_about_dialog_lists_runtime_components_and_icon_credits(qt_app, language):
    # pylint: disable=unused-argument
    dialog = AboutDialog(load_translations(language))
    try:
        markup = dialog.credits.toHtml()

        labels = [label.text() for label in dialog.findChildren(QLabel)]
        assert any(__version__ in text for text in labels)
        assert any("MIT" in text for text in labels)
        assert all(component in markup for component in (
            "Frida", "Capstone", "psutil", "NumPy", "PySide6", "Qt 6", "SQLAlchemy"))
        assert "wxWindows Library Licence 3.1" in markup
        assert "Google Material Icons (Outlined)" in markup
        assert "Apache 2.0" in markup
        assert "fonts.google.com/icons" in markup
    finally:
        dialog.close()