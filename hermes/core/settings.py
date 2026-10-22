"""The user's general settings: what they are, their values in this process.

``SETTINGS`` defines each setting with its default and bounds; the values
live in the table ``settings`` of hermes.db (hermes.db.repository.settings).
At start ``load`` takes the stored values, ``get`` returns the value of a
setting anywhere in Hermes, and ``apply`` takes new ones from the settings
window and tells the ``subscribe``d callbacks which changed.

Values that do not fit their setting (an old version's, a hand-edited
database) fall back to the default or are clamped into the bounds.
"""

import threading
from dataclasses import dataclass, field


BOOL, INT, CHOICE = "bool", "int", "choice"


@dataclass(frozen=True)
class Setting:
    """A setting: translation keys ``settings.<key>`` and ``settings.<key>_tooltip``."""

    key: str
    kind: str
    default: object
    minimum: int | None = None
    maximum: int | None = None
    step: int = 1
    # CHOICE: the values to choose from (translation keys settings.<key>_<value>)
    choices: tuple = field(default_factory=tuple)
    # takes effect only after Hermes was started again
    restart: bool = False

    def coerce(self, value):
        """``value`` as this setting holds it, or the default if it cannot."""
        if self.kind == BOOL:
            return value if isinstance(value, bool) else self.default
        if self.kind == INT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return self.default
            value = int(value)
            if self.minimum is not None:
                value = max(self.minimum, value)
            if self.maximum is not None:
                value = min(self.maximum, value)
            return value
        return value if value in self.choices else self.default


SETTINGS = (
    Setting("activity_log", BOOL, True),
    Setting("refresh_interval_ms", INT, 1000, minimum=100, maximum=10_000, step=100),
    Setting("wine_trace_limit", INT, 5000, minimum=0, maximum=100_000, step=500),
    Setting("large_trace", INT, 5000, minimum=100, maximum=100_000, step=500),
    Setting("trace_sequence", INT, 100_000, minimum=1000, maximum=500_000, step=10_000),
    Setting("language", CHOICE, "system", choices=("system", "de", "en"), restart=True),
)
BY_KEY = {setting.key: setting for setting in SETTINGS}

_lock = threading.Lock()
_values = {}
_callbacks = []


def get(key):
    """The value of the setting ``key``."""
    with _lock:
        if key in _values:
            return _values[key]
    return BY_KEY[key].default


def values():
    """{key: value} of every setting."""
    return {setting.key: get(setting.key) for setting in SETTINGS}


def load(stored):
    """Take the values stored in the database (unknown keys are left out)."""
    with _lock:
        _values.clear()
        for key, value in stored.items():
            if key in BY_KEY:
                _values[key] = BY_KEY[key].coerce(value)


def apply(new_values):
    """Set ``new_values``; returns {key: value} of the settings that changed."""
    changed = {}
    with _lock:
        for key, value in new_values.items():
            setting = BY_KEY[key]
            value = setting.coerce(value)
            if _values.get(key, setting.default) != value:
                changed[key] = value
            _values[key] = value
        callbacks = list(_callbacks)
    if changed:
        for callback in callbacks:
            callback(changed)
    return changed


def reset():
    """Every setting at its default (for tests)."""
    with _lock:
        _values.clear()
        _callbacks.clear()


def subscribe(callback):
    """Call ``callback(changed)`` with {key: value} whenever ``apply`` changes settings."""
    with _lock:
        _callbacks.append(callback)


def unsubscribe(callback):
    with _lock:
        if callback in _callbacks:
            _callbacks.remove(callback)


def language():
    """The language to load, or None for the system's."""
    chosen = get("language")
    return None if chosen == "system" else chosen
