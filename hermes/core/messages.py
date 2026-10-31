"""Error messages in the user's language.

The texts are keys of the UI catalogs (hermes/ui/localization). The main
window passes the catalog it loaded to :func:`use`; until then, as in tests
and command-line tools, messages are English.

The Frida agents cannot render them: their errors (``hermesError(key,
params)``, see messages.js) carry the key and values marked in the message,
and :func:`describe` renders them wherever an agent's error becomes text.
"""

import json
import re
from pathlib import Path

import frida

from hermes.core.agent_source import load_agent_source

# Prepended to every agent source that reports errors.
AGENT_SOURCE = load_agent_source(__file__)

_MARKER = re.compile(r"\u27e6hermes:([\w.]+):(.*?)\u27e7", re.DOTALL)

_CATALOG_DIR = Path(__file__).resolve().parents[1] / "ui" / "localization"
_catalog = None
_english = None


def use(translations):
    """Render messages from ``translations`` from now on."""
    global _catalog  # pylint: disable=global-statement
    _catalog = translations


def _english_catalog():
    global _english  # pylint: disable=global-statement
    if _english is None:
        _english = json.loads((_CATALOG_DIR / "en.json").read_text(encoding="utf-8"))
    return _english


def text(key, **params):
    """Return the message ``key`` with ``params`` filled in."""
    template = (_catalog or {}).get(key) or _english_catalog().get(key, key)
    # Values may be errors of an agent.
    return _MARKER.sub(_render_marker, template.format(**params))


def describe(error):
    """Return the text of ``error`` (an exception or text), agent messages rendered.

    Of an agent's RPC error only the message, without its JavaScript stack.
    """
    if isinstance(error, frida.RPCException) and error.args:
        error = error.args[0]
    return _MARKER.sub(_render_marker, str(error))


def _render_marker(match):
    try:
        params = json.loads(match.group(2))
    except ValueError:
        return match.group(0)
    # Values may hold the agent's messages of errors it caught.
    params = {name: describe(value) if isinstance(value, str) else value
              for name, value in params.items()}
    try:
        return text(match.group(1), **params)
    except (KeyError, IndexError, ValueError):
        return match.group(0)


class LocalizedError(Exception):
    """An error whose message is a catalog key, rendered when it is shown.

    ``str(error)`` gives the text in the current language, so it can be shown
    wherever a plain error's message is.
    """

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params

    def __str__(self):
        return text(self.key, **self.params)


class LocalizedValueError(LocalizedError, ValueError):
    """A ValueError with a catalog message, e.g. for input the user gave."""


class LocalizedRuntimeError(LocalizedError, RuntimeError):
    """A RuntimeError with a catalog message."""


class LocalizedOSError(LocalizedError, OSError):
    """An OSError with a catalog message; ``errno`` as with OSError(errno, text)."""

    def __init__(self, key, errno=None, **params):
        super().__init__(key, **params)
        self.errno = errno
