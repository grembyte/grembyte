"""The general settings: values, storage, the window, where they take effect."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from grembyte.core import settings
from grembyte.db import models  # noqa: F401  pylint: disable=unused-import
from grembyte.db.base import Base
from grembyte.db.repository.settings import SettingsRepository

from tests.test_hooks import fixture_qt_app, fixture_translations  # pylint: disable=unused-import


@pytest.fixture(autouse=True)
def fixture_defaults():
    settings.reset()
    yield
    settings.reset()


@pytest.fixture(name="repository")
def fixture_repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'grembyte.db'}")
    Base.metadata.create_all(engine)
    yield SettingsRepository(sessionmaker(bind=engine))
    engine.dispose()


def test_values_start_at_their_defaults():
    assert settings.get("activity_log") is False
    assert settings.get("wine_trace_limit") == 5000
    assert settings.get("trace_sequence") == 100_000
    assert settings.get("hook_short_jump") is False
    assert settings.get("wine_gadget_download") is False
    assert settings.language() is None  # the system's
    assert set(settings.values()) == {setting.key for setting in settings.SETTINGS}


def test_stored_values_that_do_not_fit_fall_back_or_are_clamped():
    settings.load({"activity_log": "yes", "wine_trace_limit": 10**9, "refresh_interval_ms": 5,
                   "language": "fr", "gone": 1, "large_trace": 7000.0})
    assert settings.get("activity_log") is False       # not a bool: the default
    assert settings.get("wine_trace_limit") == 100_000  # clamped to the maximum
    assert settings.get("refresh_interval_ms") == 100   # and to the minimum
    assert settings.get("language") == "system"
    assert settings.get("large_trace") == 7000


def test_apply_tells_only_what_changed():
    heard = []
    settings.subscribe(heard.append)
    assert settings.apply({"activity_log": True, "wine_trace_limit": 0}) == {
        "activity_log": True, "wine_trace_limit": 0,
    }
    assert settings.apply({"wine_trace_limit": 0}) == {}
    assert heard == [{"activity_log": True, "wine_trace_limit": 0}]
    assert settings.get("wine_trace_limit") == 0


def test_settings_are_stored_and_read_back(repository):
    assert repository.load() == {}
    repository.save({"activity_log": False, "language": "en"})
    repository.save({"language": "de"})
    assert repository.load() == {"activity_log": False, "language": "de"}


def test_the_window_saves_applies_and_resets(repository, qt_app, translations):
    # pylint: disable=unused-argument
    from grembyte.ui.widgets.settings_dialog import (  # pylint: disable=import-outside-toplevel
        SettingsDialog,
        _SETTING_CATEGORIES,
    )

    settings.load({"refresh_interval_ms": 2500})
    dialog = SettingsDialog(translations, repository)
    assert dialog.editors["refresh_interval_ms"].value() == 2500
    assert not dialog.editors["activity_log"].isChecked()
    assert dialog.editors["wine_trace_limit"].specialValueText() == "kein Limit"
    assert {key for _, _, keys in _SETTING_CATEGORIES for key in keys} == set(settings.BY_KEY)
    # A page per category, chosen in the list on the left
    assert dialog.categories.count() == dialog.pages.count() == len(_SETTING_CATEGORIES)
    dialog.categories.setCurrentRow(1)
    assert dialog.pages.currentWidget().objectName() == "settings_category_scan"
    assert dialog.editors["scan_aligned"].isChecked()
    assert "‼" in translations["settings.hook_repatch_warning"]
    dialog.editors["activity_log"].setChecked(True)
    language = dialog.editors["language"]
    assert not dialog.restart_label.isVisibleTo(dialog)
    language.setCurrentIndex(language.findData("en"))
    assert dialog.restart_label.isVisibleTo(dialog)  # only after a restart
    dialog._save()  # pylint: disable=protected-access
    assert dialog.changed == {"activity_log": True, "language": "en"}
    assert repository.load()["activity_log"] is True
    assert settings.get("language") == "en" and settings.language() == "en"

    # The interface size also takes effect only after a restart.
    dialog = SettingsDialog(translations, repository)
    assert not dialog.restart_label.isVisibleTo(dialog)
    dialog.editors["ui_scale"].setValue(120)
    assert dialog.restart_label.isVisibleTo(dialog)
    dialog.editors["ui_scale"].setValue(100)
    assert not dialog.restart_label.isVisibleTo(dialog)

    dialog = SettingsDialog(translations, repository)
    dialog.show_defaults()
    assert dialog.chosen() == {setting.key: setting.default for setting in settings.SETTINGS}


def test_the_window_leaves_its_widgets_to_qt(qt_app, translations, repository):
    # pylint: disable=unused-argument,import-outside-toplevel
    import shiboken6  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtWidgets import QWidget  # pylint: disable=import-error,no-name-in-module
    from grembyte.ui.widgets.settings_dialog import SettingsDialog

    # Widgets PySide thinks Python owns, under a parent, crashed Grembyte at exit.
    parent = QWidget()
    try:
        dialog = SettingsDialog(translations, repository, parent=parent)
        assert not shiboken6.ownedByPython(dialog.categories)
        assert not shiboken6.ownedByPython(dialog.pages)
    finally:
        shiboken6.delete(parent)


def test_the_first_scan_takes_aligned_from_the_settings(qt_app, monkeypatch):
    # pylint: disable=unused-argument,import-outside-toplevel,protected-access
    from grembyte.ui import main_window
    from tests.test_pointer import _delete_window

    window = main_window.MainWindow("de")
    requests = []
    monkeypatch.setattr(window, "_start_scan_worker", requests.append)
    try:
        window._current_pid = 123
        window.input_value.setText("7")
        window._first_scan()
        settings.apply({"scan_aligned": False})
        window._first_scan()
        assert [request.aligned for request in requests] == [True, False]
    finally:
        _delete_window(window)


def test_pointer_scan_alignment_follows_the_pointer_size():
    assert settings.pointer_alignment("pointer_location_step", 8) == 8
    assert settings.pointer_alignment("pointer_value_alignment", 4) == 4
    settings.apply({"pointer_location_step": "4", "pointer_value_alignment": "16"})
    assert settings.pointer_alignment("pointer_location_step", 8) == 4
    assert settings.pointer_alignment("pointer_value_alignment", 4) == 16
    settings.apply({"pointer_value_alignment": "1"})
    assert settings.pointer_alignment("pointer_value_alignment", 8) == 1


def test_trace_agents_get_the_sequence_capacity():
    # pylint: disable=import-outside-toplevel
    from grembyte.features import native_trace, trace

    assert "const EVENT_CAPACITY = 250000;" in trace.build_agent_source(250_000)
    native = native_trace.build_agent_source(500_000)
    assert "const EVENT_CAPACITY = 500000;" in native
    # The table of first calls grows with it: twice the sequence, a power of two.
    assert f"const FIRST_CAPACITY = {1 << 20};" in native
    assert native_trace.first_capacity(100_000) == 1 << 19
    settings.apply({"trace_sequence": 20_000})
    assert "const EVENT_CAPACITY = 20000;" in trace.TraceWorker(1).agent_source()
    assert "const EVENT_CAPACITY = 20000;" in native_trace.NativeTraceWorker(1).agent_source()


def test_the_activity_log_follows_its_setting(tmp_path, monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte import main
    from grembyte.core import activity

    monkeypatch.setenv("GREMBYTE_LOG_DIR", str(tmp_path))
    settings.subscribe(main._follow_activity_setting)
    try:
        settings.apply({"activity_log": True})
        assert activity.enabled()
        settings.apply({"activity_log": True})  # unchanged: nothing happens
        assert activity.enabled()
        settings.apply({"activity_log": False})
        assert not activity.enabled()
        events = [entry["event"] for entry in activity.read_entries(tmp_path / "activity.jsonl")]
        assert events[-1] == "activity_log_off"
        settings.apply({"activity_log": True})
        assert activity.enabled()
    finally:
        activity.teardown()


def test_the_interface_scale_is_set_only_while_qt_starts(monkeypatch):
    # pylint: disable=import-outside-toplevel
    import os
    from grembyte import main

    monkeypatch.delenv(main.SCALE_VARIABLE, raising=False)
    seen = lambda: os.environ.get(main.SCALE_VARIABLE)  # noqa: E731
    assert main.scale_interface(seen) is None  # 100 % leaves Qt alone
    settings.apply({"ui_scale": 130})
    assert main.scale_interface(seen) == "1.3"
    # Restored at once: processes Grembyte starts must not inherit it.
    assert main.SCALE_VARIABLE not in os.environ
    monkeypatch.setenv(main.SCALE_VARIABLE, "2")
    assert main.scale_interface(seen) == "2.6"
    assert os.environ[main.SCALE_VARIABLE] == "2"
    # Steps of 10 % between 100 % and 150 %
    assert settings.BY_KEY["ui_scale"].coerce(200) == 150
    assert settings.BY_KEY["ui_scale"].restart


def test_leaving_saves_qt_settings_before_the_process_ends(tmp_path, monkeypatch):
    # pylint: disable=import-outside-toplevel
    from PySide6.QtCore import QSettings  # pylint: disable=import-error,no-name-in-module
    from grembyte import main

    path = tmp_path / "state.ini"
    kept = QSettings(str(path), QSettings.Format.IniFormat)
    kept.setValue("window/size", 42)
    closed = []
    monkeypatch.setattr(main.syntax, "close_checker", lambda: closed.append(True))
    ended = []

    def end(code):
        # What is in the file when the process ends
        ended.append((code, "size=42" in path.read_text(encoding="utf-8")))
        raise SystemExit(code)

    monkeypatch.setattr(main.os, "_exit", end)
    with pytest.raises(SystemExit):
        main.leave(3)
    assert ended == [(3, True)] and closed == [True]
