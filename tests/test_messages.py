"""Tests for error messages in the user's language."""

import json
import re
import string
from pathlib import Path

import pytest

from hermes.core import messages
from hermes.core.wine_gadget import GadgetError
from hermes.core.wine_helper import HelperError
from hermes.ui.localization import load_translations

_HERMES = Path(messages.__file__).resolve().parents[1]


def _catalog(language):
    return json.loads((_HERMES / "ui" / "localization" / f"{language}.json")
                      .read_text(encoding="utf-8"))


def _fields(template):
    return sorted(name for _, name, _, _ in string.Formatter().parse(template) if name)


def test_errors_are_english_until_a_catalog_is_chosen():
    error = GadgetError("error.gadget.load", error=HelperError("error.helper.run", error="boom"))

    assert str(error) == ("Could not load the Frida gadget into the game: "
                          "Could not run the helper: boom")
    messages.use(load_translations("de"))
    # Rendered when shown, nested errors included
    assert str(error) == ("Das Frida-Gadget konnte nicht ins Spiel geladen werden: "
                          "Hilfsprogramm konnte nicht gestartet werden: boom")


def test_error_keys_exist_in_both_catalogs_with_the_same_fields():
    english, german = _catalog("en"), _catalog("de")
    keys = {key for key in english if key.startswith("error.")}

    assert keys == {key for key in german if key.startswith("error.")}
    for key in keys:
        assert _fields(english[key]) == _fields(german[key]), key
    used = {key for path in _HERMES.rglob("*.py")
            for key in re.findall(r"""Error\(\s*"(error\.[\w.]+)\"""", path.read_text())}
    # The agents' hermesError(...) and hermesMessage(...)
    used |= {key for path in _HERMES.rglob("*.js")
             for key in re.findall(r"""hermes(?:Error|Message)\(\s*['"`](error\.[\w.]+)""",
                                   path.read_text())}
    assert used and used <= keys, used - keys


def test_unknown_keys_show_as_themselves():
    assert messages.text("error.does_not_exist") == "error.does_not_exist"


def test_agent_errors_are_rendered_in_the_users_language():
    # pylint: disable=import-outside-toplevel
    import frida

    from hermes.hooks.syntax import checker

    if not checker().available:
        pytest.skip("needs Frida's system session")
    session = frida.attach(0)
    try:
        script = session.create_script(messages.AGENT_SOURCE + """
            rpc.exports.fail = () => {
              try {
                throw hermesError('error.gadget.not_present', {path: ptr(0x1234)});
              } catch (e) {
                throw hermesError('error.gadget.load', {error: e.message});
              }
            };
            rpc.exports.plain = () => { throw new Error('plain ⟧ text'); };
        """)
        script.load()
        with pytest.raises(frida.RPCException) as raised:
            script.exports_sync.fail()
        assert messages.describe(raised.value) == (
            "Could not load the Frida gadget into the game: Gadget not present: 0x1234")
        messages.use(load_translations("de"))
        assert messages.describe(raised.value) == (
            "Das Frida-Gadget konnte nicht ins Spiel geladen werden: "
            "Gadget nicht vorhanden: 0x1234")
        with pytest.raises(frida.RPCException) as raised:
            script.exports_sync.plain()
        assert messages.describe(raised.value) == "plain ⟧ text"
        script.unload()
    finally:
        session.detach()


def test_every_trace_agent_has_its_messages():
    # pylint: disable=import-outside-toplevel
    from hermes.features.native_monitor import NativeMonitorWorker
    from hermes.features.native_trace import NativeTraceWorker
    from hermes.features.trace import AgentWorker, TraceWorker

    english = _catalog("en")
    for worker in (AgentWorker, TraceWorker, NativeTraceWorker, NativeMonitorWorker):
        for message in ("load_timeout", "agent_failed"):
            assert f"error.{worker.agent_key}.{message}" in english
