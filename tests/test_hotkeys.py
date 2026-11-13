"""Hotkeys that run scripts while the game has the focus (see grembyte.core.hotkeys)."""

import os

import pytest

from grembyte.core import hotkeys
from grembyte.core.messages import LocalizedValueError


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module
    return QApplication.instance() or QApplication([])


class FakeKeyboard:
    """Stands in for grembyte.platform.keyboard: the test says what is held."""

    def __init__(self, focused=4242):
        self.down = set()
        self.focused = focused
        self.closed = False

    def pressed(self, names):
        return self.down & set(names)

    def focused_pid(self):
        return self.focused

    def close(self):
        self.closed = True


def test_hotkeys_are_read_and_written_as_qt_writes_them():
    assert hotkeys.parse("Ctrl+Shift+G") == hotkeys.Hotkey("G", frozenset({"ctrl", "shift"}))
    assert hotkeys.normalized("shift+control+g") == "Ctrl+Shift+G"
    assert hotkeys.normalized("F9") == "F9"
    assert hotkeys.normalized("Alt+PgDown") == "Alt+PgDown"
    assert hotkeys.normalized("  ") == ""
    for text in ("Ctrl+", "Hyper+G", "Ctrl++"):
        with pytest.raises(LocalizedValueError):
            hotkeys.parse(text)
    with pytest.raises(LocalizedValueError) as error:
        hotkeys.parse("Ctrl+Tab")
    assert error.value.key == "error.hotkeys.key"


def test_a_hotkey_fires_once_per_press_with_exactly_its_modifiers(qt_app):
    # pylint: disable=unused-argument
    keyboard = FakeKeyboard()
    listener = hotkeys.HotkeyListener({4242}, keyboard)
    fired = []
    listener.triggered.connect(fired.append)
    try:
        listener.set_hotkeys(["Ctrl+G", "F9"])
        listener.poll()  # the first reading only notes what is held
        keyboard.down = {"ctrl", "G"}
        listener.poll()
        listener.poll()  # held: not again
        assert fired == ["Ctrl+G"]
        keyboard.down = {"ctrl"}
        listener.poll()
        keyboard.down = {"ctrl", "shift", "G"}  # another modifier as well: not Ctrl+G
        listener.poll()
        keyboard.down = {"F9"}
        listener.poll()
        assert fired == ["Ctrl+G", "F9"]
    finally:
        listener.close()
    assert keyboard.closed


def test_hotkeys_count_only_while_the_game_has_the_focus(qt_app):
    # pylint: disable=unused-argument
    keyboard = FakeKeyboard(focused=1)
    listener = hotkeys.HotkeyListener({4242}, keyboard)
    fired = []
    listener.triggered.connect(fired.append)
    try:
        listener.set_hotkeys(["F9"])
        keyboard.down = {"F9"}
        listener.poll()
        # Held while the focus comes back: only a new press counts
        keyboard.focused = 4242
        listener.poll()
        assert fired == []
        keyboard.down = set()
        listener.poll()
        keyboard.down = {"F9"}
        listener.poll()
        # A window whose process cannot be told does not stop it
        keyboard.down = set()
        listener.poll()
        keyboard.focused = None
        keyboard.down = {"F9"}
        listener.poll()
        assert fired == ["F9", "F9"]
    finally:
        listener.close()


def test_keys_held_when_the_hotkeys_change_do_not_fire(qt_app):
    # pylint: disable=unused-argument
    keyboard = FakeKeyboard()
    keyboard.down = {"F9"}
    listener = hotkeys.HotkeyListener({4242}, keyboard)
    fired = []
    listener.triggered.connect(fired.append)
    try:
        listener.set_hotkeys(["F9"])
        listener.poll()
        assert fired == []
    finally:
        listener.close()


def test_without_a_keyboard_the_listener_reports_nothing(qt_app):
    # pylint: disable=unused-argument
    listener = hotkeys.HotkeyListener({4242}, None)
    listener.set_hotkeys(["F9"])
    listener.poll()
    assert not listener.available


@pytest.mark.skipif(not os.environ.get("DISPLAY") or not os.sys.platform.startswith("linux"),
                    reason="needs an X display")
def test_the_x11_keyboard_knows_the_keys_of_every_hotkey():
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte.platform.keyboard import X11Keyboard

    keyboard = X11Keyboard()
    try:
        missing = [key for key in hotkeys.KEYS + ("ctrl", "shift", "alt")
                   if not keyboard._keycodes.get(key)]
        assert not missing
        assert keyboard.pressed({"F12"}) <= {"F12"}
        focused = keyboard.focused_pid()
        assert focused is None or focused >= 0
    finally:
        keyboard.close()
