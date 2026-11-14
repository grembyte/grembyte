"""The Steam launch option for grembyte-cli (see grembyte.core.launch_option)."""

from grembyte.core import launch_option

from tests.test_hooks import fixture_qt_app  # pylint: disable=unused-import


def _installation(folder):
    """A virtual environment's bin folder with Python and grembyte-cli."""
    bin_folder = folder / ".venv" / "bin"
    bin_folder.mkdir(parents=True)
    (bin_folder / "python").write_text("")
    cli = bin_folder / "grembyte-cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    return bin_folder / "python", cli


def test_the_full_path_unless_steams_folders_hold_this_cli(tmp_path):
    python, cli = _installation(tmp_path / "my games")
    steam = tmp_path / "usr-local-bin"
    steam.mkdir()
    # Not in Steam's folders: the full path, quoted for the space in it
    assert launch_option.launch_option(python, (str(steam),)) == f"'{cli}' %command%"
    # A link in /usr/local/bin to this very grembyte-cli: its name is enough
    (steam / "grembyte-cli").symlink_to(cli)
    assert launch_option.launch_option(python, (str(steam),)) == "grembyte-cli %command%"


def test_another_installation_on_steams_path_is_not_taken(tmp_path):
    python, cli = _installation(tmp_path / "grembyte")
    _other_python, other = _installation(tmp_path / "old")
    steam = tmp_path / "bin"
    steam.mkdir()
    (steam / "grembyte-cli").symlink_to(other)
    assert launch_option.launch_option(python, (str(steam),)) == f"{cli} %command%"


def test_no_grembyte_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_option.shutil, "which", lambda *_args, **_kwargs: None)
    assert launch_option.launch_option(tmp_path / "python", ()) is None


def test_the_menu_copies_the_launch_option(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PySide6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
    from grembyte.ui import main_window
    from tests.test_pointer import _delete_window

    window = main_window.MainWindow("de")
    try:
        monkeypatch.setattr(launch_option, "launch_option", lambda: "grembyte-cli %command%")
        window.act_launch_option.trigger()
        assert QGuiApplication.clipboard().text() == "grembyte-cli %command%"
        assert window.lbl_status.text().startswith("Kopiert: grembyte-cli %command%")
        monkeypatch.setattr(launch_option, "launch_option", lambda: None)
        window.act_launch_option.trigger()
        assert "nicht gefunden" in window.lbl_status.text()
    finally:
        _delete_window(window)
