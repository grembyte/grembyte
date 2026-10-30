"""Calling a static C# method of the game, e.g. a cheat a game's scripts offer.

The method call dialog (hermes/ui/widgets/method_call.py) reads one text per
parameter. ``parse_argument`` turns it into what the agent passes to the
method (``callStatic`` in hermes/features/monitor.js): a number, bool,
string, None (null), ``{"address": "0x…"}`` (an object or IntPtr) or
``{"struct": "…"}`` (the bytes of a System.Guid, in hex). The
types are those ``describe_call`` reports: ``{kind, byref, name, enumName?,
enumValues?}`` with ECMA-335 element types as kinds; an enum has the kind of
its base type, and ``enumValues`` (``{"0": "Might", ...}``, Unreal) lets its
names be typed in.
"""

import json
import math
import uuid

BOOLEAN, CHAR = 0x02, 0x03
I1, U1, I2, U2, I4, U4, I8, U8 = range(0x04, 0x0c)
R4, R8, STRING, VALUETYPE = 0x0c, 0x0d, 0x0e, 0x11
I, U = 0x18, 0x19
# class, array, generic instance, object, one-dimensional array
REFERENCES = frozenset({0x12, 0x14, 0x15, 0x1c, 0x1d})

INTEGER_RANGES = {
    I1: (-2 ** 7, 2 ** 7 - 1), U1: (0, 2 ** 8 - 1),
    I2: (-2 ** 15, 2 ** 15 - 1), U2: (0, 2 ** 16 - 1),
    I4: (-2 ** 31, 2 ** 31 - 1), U4: (0, 2 ** 32 - 1),
    I8: (-2 ** 63, 2 ** 63 - 1), U8: (0, 2 ** 64 - 1),
}
_TRUE = {"true", "1", "ja", "yes", "wahr"}
_FALSE = {"false", "0", "nein", "no", "falsch"}


class ArgumentError(ValueError):
    """A text that is no value of its parameter's type.

    ``key`` names the translation of the message, ``values`` fill it in.
    """

    def __init__(self, key, **values):
        super().__init__(key)
        self.key = key
        self.values = values


GUID = "System.Guid"


def supported(param_type):
    """Whether an argument of ``param_type`` can be typed in (structs but a Guid cannot)."""
    return param_type["kind"] != VALUETYPE or is_guid(param_type)


def is_guid(param_type):
    """Whether ``param_type`` is System.Guid, typed in as text."""
    return param_type["kind"] == VALUETYPE and param_type["name"] == GUID


def _integer(text):
    text = text.strip().replace("_", "")
    try:
        return int(text, 10)
    except ValueError:
        return int(text, 0)  # 0x1f, 0b101


def enum_value(param_type, text):
    """The value of the enum name ``text`` (any case) if ``param_type`` names its values."""
    word = text.strip().lower()
    for value, name in (param_type.get("enumValues") or {}).items():
        if name.lower() == word:
            return int(value)
    return None


def parse_argument(param_type, text):
    """The value of ``text`` for a parameter of ``param_type``; raises ArgumentError."""
    # pylint: disable=too-many-return-statements,too-many-branches
    kind = param_type["kind"]
    if not supported(param_type):
        raise ArgumentError("call.error_struct", type=param_type["name"])
    if is_guid(param_type):
        try:
            guid = uuid.UUID(text.strip())
        except ValueError:
            raise ArgumentError("call.error_guid", text=text) from None
        # .NET keeps the first three groups little-endian, like bytes_le.
        return {"struct": guid.bytes_le.hex(), "guid": str(guid)}
    if kind == STRING:
        return text
    if kind in REFERENCES or kind in (I, U):
        if text.strip().lower() in ("", "null"):
            return None if kind in REFERENCES else {"address": "0x0"}
        try:
            address = _integer(text)
        except ValueError:
            raise ArgumentError("call.error_address", text=text) from None
        if address < 0:
            raise ArgumentError("call.error_address", text=text)
        return {"address": hex(address)}
    if kind == BOOLEAN:
        word = text.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        raise ArgumentError("call.error_bool", text=text)
    if kind == CHAR:
        if len(text) != 1:
            raise ArgumentError("call.error_char", text=text)
        return text
    if kind in (R4, R8):
        try:
            value = float(text.strip().replace(",", "."))
        except ValueError:
            raise ArgumentError("call.error_number", text=text) from None
        if not math.isfinite(value):
            raise ArgumentError("call.error_number", text=text)
        return value
    if kind in INTEGER_RANGES:
        named = enum_value(param_type, text)
        if named is not None:
            return named
        try:
            value = _integer(text)
        except ValueError:
            raise ArgumentError("call.error_integer", text=text) from None
        low, high = INTEGER_RANGES[kind]
        if not low <= value <= high:
            raise ArgumentError("call.error_range", text=text, low=low, high=high)
        return value
    raise ArgumentError("call.error_struct", type=param_type["name"])


def argument_text(value):
    """An argument as the call log shows it."""
    if value is None:
        return "null"
    if isinstance(value, dict):
        return value.get("guid") or value["address"]
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
