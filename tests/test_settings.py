"""The general settings: values, storage, the window, where they take effect."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermes.core import settings
from hermes.db import models  # noqa: F401  pylint: disable=unused-import
from hermes.db.base import Base
from hermes.db.repository.settings import SettingsRepository

from tests.test_hooks import fixture_qt_app, fixture_translations  # pylint: disable=unused-import


@pytest.fixture(autouse=True)
def fixture_defaults():
    settings.reset()
    yield
    settings.reset()


@pytest.fixture(name="repository")
def fixture_repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
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
    from hermes.ui.widgets.settings_dialog import (  # pylint: disable=import-outside-toplevel
        SettingsDialog,
        _SETTING_CATEGORIES,
    )

    settings.load({"refresh_interval_ms": 2500})
    dialog = SettingsDialog(translations, repository)
    assert dialog.editors["refresh_interval_ms"].value() == 2500
    assert not dialog.editors["activity_log"].isChecked()
    assert dialog.editors["wine_trace_limit"].specialValueText() == "kein Limit"
    assert {key for _, keys in _SETTING_CATEGORIES for key in keys} == set(settings.BY_KEY)
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

    dialog = SettingsDialog(translations, repository)
    dialog.show_defaults()
    assert dialog.chosen() == {setting.key: setting.default for setting in settings.SETTINGS}


def test_trace_agents_get_the_sequence_capacity():
    # pylint: disable=import-outside-toplevel
    from hermes.features import native_trace, trace

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
    from hermes import main
    from hermes.core import activity

    monkeypatch.setenv("HERMES_LOG_DIR", str(tmp_path))
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
