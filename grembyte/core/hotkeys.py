"""Hotkeys that run a script while the game has the focus.

A hotkey is written as Qt's QKeySequence writes it in portable text,
``Ctrl+Shift+G``: modifiers (Ctrl, Shift, Alt, Meta) and one key (KEYS).
HotkeyListener asks the keyboard state of the system (see
grembyte.platform.keyboard) a few dozen times a second and reports a hotkey
when its key goes down while its modifiers, and no others, are held, and a
window of the game has the focus. The game gets the keys as well: Grembyte
only reads them.
"""

import string
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal  # pylint: disable=import-error,no-name-in-module

from grembyte.core import logs, messages

# The modifiers in the order Qt writes them
MODIFIERS = ("ctrl", "shift", "alt", "meta")
_MODIFIER_NAMES = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "meta": "Meta"}
# The keys a hotkey can have, as Qt names them
KEYS = (tuple(string.ascii_uppercase) + tuple(string.digits)
        + tuple(f"F{number}" for number in range(1, 13))
        + ("Ins", "Del", "Home", "End", "PgUp", "PgDown", "Pause", "Space"))
_KEYS = {key.lower(): key for key in KEYS}
# How often the keyboard is read
POLL_MS = 30


@dataclass(frozen=True)
class Hotkey:
    """A key with the modifiers that have to be held with it."""

    key: str
    modifiers: frozenset = frozenset()

    def text(self):
        """As Qt writes it: ``Ctrl+Shift+G``."""
        return "+".join([_MODIFIER_NAMES[name] for name in MODIFIERS if name in self.modifiers]
                        + [self.key])


def parse(text):
    """The Hotkey of ``text`` (``Ctrl+Shift+G``); LocalizedValueError if it is none."""
    words = [word.strip() for word in (text or "").split("+")]
    if not words or not all(words):
        raise messages.LocalizedValueError("error.hotkeys.invalid", text=text)
    modifiers = set()
    for word in words[:-1]:
        name = word.lower().replace("control", "ctrl")
        if name not in MODIFIERS:
            raise messages.LocalizedValueError("error.hotkeys.invalid", text=text)
        modifiers.add(name)
    key = _KEYS.get(words[-1].lower())
    if key is None:
        raise messages.LocalizedValueError("error.hotkeys.key", name=words[-1])
    return Hotkey(key, frozenset(modifiers))


def normalized(text):
    """``text`` as parse reads it and Qt writes it, or "" for no hotkey."""
    return parse(text).text() if (text or "").strip() else ""


class HotkeyListener(QObject):
    """Report the hotkeys pressed while a window of the processes ``pids`` has the focus.

    ``reader`` reads the keyboard (grembyte.platform.keyboard.open_reader);
    without one the listener reports nothing (``available`` is False).
    """

    # the hotkey as Hotkey.text() writes it
    triggered = Signal(str)

    def __init__(self, pids, reader, parent=None, interval_ms=POLL_MS):
        super().__init__(parent)
        self.pids = set(pids)
        self.reader = reader
        self._hotkeys = {}
        # hotkeys whose keys are held: they do not fire again until let go;
        # None until the keyboard was read (keys held then do not count)
        self._held = None
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.poll)

    @property
    def available(self):
        return self.reader is not None

    def set_hotkeys(self, texts):
        """Listen for these hotkeys (texts parse reads); none stops listening."""
        self._hotkeys = {}
        for text in texts:
            hotkey = parse(text)
            self._hotkeys[hotkey.text()] = hotkey
        # Keys held while the hotkeys change do not count as pressed
        self._held = None
        if self._hotkeys and self.reader is not None:
            self._timer.start()
        else:
            self._timer.stop()

    def poll(self):
        """Read the keyboard once and report the hotkeys that went down."""
        if not self._hotkeys or self.reader is None:
            return
        try:
            focused = self.reader.focused_pid()
            if focused is not None and focused not in self.pids:
                # Keys held while the focus comes back do not count as pressed
                self._held = None
                return
            names = set(MODIFIERS)
            names.update(hotkey.key for hotkey in self._hotkeys.values())
            down = self.reader.pressed(names)
        except OSError as error:
            logs.log.warning("hotkeys: cannot read the keyboard: %s", error)
            self.close()
            return
        modifiers = down & set(MODIFIERS)
        active = {text for text, hotkey in self._hotkeys.items()
                  if hotkey.key in down and modifiers == hotkey.modifiers}
        held, self._held = self._held, active
        if held is None:
            return
        for text in self._hotkeys:
            if text in active and text not in held:
                self.triggered.emit(text)

    def close(self):
        """Stop listening and let go of the keyboard."""
        self._timer.stop()
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.close()
