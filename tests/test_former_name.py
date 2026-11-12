"""Tests for taking over the files of Hermes Hook, Grembyte's former name."""

from PySide6.QtCore import QSettings  # pylint: disable=import-error,no-name-in-module

from grembyte.core.former_name import adopt_folders, adopt_settings


def _old_tree(base):
    old = base / "hermes-hook"
    (old / "gadget").mkdir(parents=True)
    (old / "gadget" / "frida-gadget.dll").write_bytes(b"dll")
    (old / "hermes.db").write_bytes(b"db")
    (old / "hermes.log").write_text("log", encoding="utf-8")
    (old / "hermes.log.1").write_text("older", encoding="utf-8")
    (old / "themes").mkdir()
    (old / "themes" / "hermes_themes_Mine.json").write_text("{}", encoding="utf-8")
    return old


def test_the_old_folder_moves_and_its_files_get_new_names(tmp_path):
    old = _old_tree(tmp_path)
    moved = adopt_folders([tmp_path])
    new = tmp_path / "grembyte"
    assert not old.exists()
    assert (new / "grembyte.db").read_bytes() == b"db"
    assert (new / "grembyte.log").read_text(encoding="utf-8") == "log"
    assert (new / "grembyte.log.1").read_text(encoding="utf-8") == "older"
    assert (new / "gadget" / "frida-gadget.dll").read_bytes() == b"dll"
    assert (new / "themes" / "grembyte_themes_Mine.json").is_file()
    assert (old, new) in moved
    # Nothing left to do on the next start.
    assert adopt_folders([tmp_path]) == []


def test_what_exists_under_the_new_name_is_kept(tmp_path):
    _old_tree(tmp_path)
    new = tmp_path / "grembyte"
    new.mkdir()
    (new / "grembyte.db").write_bytes(b"new")
    adopt_folders([tmp_path])
    assert (new / "grembyte.db").read_bytes() == b"new"
    # The old database moved along but kept its name, so nothing is lost.
    assert (new / "hermes.db").read_bytes() == b"db"
    assert (new / "gadget" / "frida-gadget.dll").is_file()
    assert not (tmp_path / "hermes-hook").exists()


def test_without_an_old_folder_nothing_happens(tmp_path):
    assert adopt_folders([tmp_path]) == []
    assert not (tmp_path / "grembyte").exists()


def test_qt_settings_are_copied_only_into_empty_ones(tmp_path):
    def ini(name):
        return QSettings(str(tmp_path / name), QSettings.Format.IniFormat)

    old, new = ini("old.ini"), ini("new.ini")
    old.setValue("ui_theme", "Github-Light")
    old.setValue("windows/MainWindow/geometry", "abc")
    old.sync()
    assert adopt_settings(old, new)
    assert new.value("ui_theme") == "Github-Light"
    assert new.value("windows/MainWindow/geometry") == "abc"
    old.setValue("ui_theme", "Solarized-Dark")
    assert not adopt_settings(old, new)
    assert new.value("ui_theme") == "Github-Light"
