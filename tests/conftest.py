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


@pytest.fixture(autouse=True, scope="session")
def _no_garbage_collection_in_other_threads():
    """Collect garbage in the main thread only, as the app does (see hermes.ui.garbage).

    Collected in a hook or Frida thread, Qt objects in reference cycles are
    deleted by PySide6 later in the GUI thread, and widgets can crash the
    interpreter then.
    """
    # pylint: disable=import-outside-toplevel
    import gc

    gc.disable()
    yield
    gc.enable()


@pytest.fixture(autouse=True)
def _collect_garbage_after_each_test():
    """Free what a test leaves in reference cycles (see the fixture above)."""
    # pylint: disable=import-outside-toplevel
    import gc

    yield
    gc.collect()


@pytest.fixture(autouse=True, scope="session")
def _end_qt_before_python():
    """Stop workers and delete the windows tests leave, before Python ends.

    Otherwise PySide6 deletes what is left while the interpreter shuts down,
    in an order that crashes (a running QThread aborts, a widget is deleted
    twice), and every test run leaves a core dump.
    """
    yield
    # pylint: disable=import-outside-toplevel
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    from hermes.ui import workers

    application = QApplication.instance()
    if application is None:
        return
    workers.stop_workers(5000)
    for widget in application.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()
    gc.collect()
