"""The user's general settings: what they are, their values in this process.

``SETTINGS`` defines each setting with its default and bounds; the values
live in the table ``settings`` of grembyte.db (grembyte.db.repository.settings).
At start ``load`` takes the stored values, ``get`` returns the value of a
setting anywhere in Grembyte, and ``apply`` takes new ones from the settings
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
    # takes effect only after Grembyte was started again
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
    Setting("activity_log", BOOL, False),
    Setting("refresh_interval_ms", INT, 1000, minimum=100, maximum=10_000, step=100),
    Setting("wine_trace_limit", INT, 5000, minimum=0, maximum=100_000, step=500),
    Setting("large_trace", INT, 5000, minimum=100, maximum=100_000, step=500),
    Setting("hook_short_jump", BOOL, False),
    Setting("hook_repatch", BOOL, False),
    Setting("hook_profiling", BOOL, False),
    Setting("trace_sequence", INT, 100_000, minimum=1000, maximum=500_000, step=10_000),
    Setting("object_browser_limit", INT, 2_000_000, minimum=100_000, maximum=10_000_000,
            step=500_000),
    # Whether the reference graph shows holders in memory no object covers
    Setting("reference_other_memory", BOOL, False),
    # Graphs (references, calls): what unfolding a node shows at most
    Setting("graph_max_nodes", INT, 100, minimum=10, maximum=1000, step=10),
    Setting("wine_gadget_download", BOOL, False),
    Setting("language", CHOICE, "system", choices=("system", "de", "en"), restart=True),
    # Size of the whole interface in percent (Qt's scale factor, read at start)
    Setting("ui_scale", INT, 100, minimum=100, maximum=150, step=10, restart=True),
    Setting("scan_aligned", BOOL, True),
    # Pointer scan: "auto" is the pointer size of the scanned process
    Setting("pointer_location_step", CHOICE, "auto", choices=("auto", "4")),
    Setting("pointer_value_alignment", CHOICE, "auto", choices=("auto", "16", "8", "4", "1")),
    Setting("pointer_stacks", BOOL, False),
    Setting("pointer_max_results", INT, 10_000, minimum=100, maximum=1_000_000, step=1000),
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


def pointer_alignment(key, size):
    """Bytes of the pointer-scan alignment ``key`` for a process with ``size``-byte pointers.

    "auto" is ``size``; a step larger than a pointer would skip pointers, so it
    is never more than ``size`` for ``pointer_location_step``.
    """
    chosen = get(key)
    value = size if chosen == "auto" else int(chosen)
    return min(value, size) if key == "pointer_location_step" else value


def language():
    """The language to load, or None for the system's."""
    chosen = get("language")
    return None if chosen == "system" else chosen
