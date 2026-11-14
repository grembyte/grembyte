"""The hotkey sounds (see grembyte.core.sounds)."""

from grembyte.core import sounds


def test_a_run_sounds_by_its_outcome():
    assert sounds.feedback("Gold +1000") == sounds.SUCCESS
    assert sounds.feedback("") == sounds.SUCCESS
    assert sounds.feedback(" TRUE ") == sounds.ON
    assert sounds.feedback("false") == sounds.OFF
    assert sounds.feedback("true", error="TypeError: x") == sounds.ERROR


def test_a_player_with_the_file_first_then_the_desktop_theme(tmp_path):
    (tmp_path / "bell.oga").write_bytes(b"")
    everything = lambda name: f"/usr/bin/{name}"  # noqa: E731
    # Not libcanberra first: it plays as a notification, silent when those are muted
    assert sounds.command(sounds.SUCCESS, everything, tmp_path) == [
        "pw-play", str(tmp_path / "bell.oga")]
    pulse = lambda name: "/usr/bin/paplay" if name == "paplay" else None  # noqa: E731
    assert sounds.command(sounds.SUCCESS, pulse, tmp_path) == [
        "paplay", str(tmp_path / "bell.oga")]
    # Without the file or a player: libcanberra by the sound's name
    assert sounds.command(sounds.ERROR, everything, tmp_path) == [
        "canberra-gtk-play", "-i", "dialog-warning", "-d", "Grembyte"]
    canberra = lambda name: "/usr/bin/canberra-gtk-play" if name == "canberra-gtk-play" else None  # noqa: E731,E501
    assert sounds.command(sounds.SUCCESS, canberra, tmp_path)[0] == "canberra-gtk-play"
    # Nothing that plays: silent
    assert sounds.command(sounds.SUCCESS, lambda _name: None, tmp_path) is None
    pipewire = lambda name: "/usr/bin/pw-play" if name == "pw-play" else None  # noqa: E731
    assert sounds.command(sounds.ERROR, pipewire, tmp_path) is None
