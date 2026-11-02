"""Settings every test shares."""

import pytest


@pytest.fixture(autouse=True)
def _private_catalog_cache(tmp_path, monkeypatch):
    """Keep the catalog cache (see hermes.features.catalog_cache) out of the user's."""
    monkeypatch.setenv("HERMES_CACHE_DIR", str(tmp_path / "catalog-cache"))


@pytest.fixture(autouse=True)
def _private_window_state(tmp_path, monkeypatch):
    """Keep window sizes and column widths (see hermes.ui.window_state) out of the user's."""
    # pylint: disable=import-outside-toplevel
    from PySide6.QtCore import QSettings  # pylint: disable=import-error,no-name-in-module

    from hermes.ui import window_state

    path = str(tmp_path / "window-state.ini")
    monkeypatch.setattr(window_state, "settings",
                        lambda: QSettings(path, QSettings.Format.IniFormat))


@pytest.fixture(autouse=True)
def _english_messages():
    """Undo the language a window chose for error messages (see hermes.core.messages)."""
    # pylint: disable=import-outside-toplevel
    from hermes.core import messages

    yield
    messages.use(None)
