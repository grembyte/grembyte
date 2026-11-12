"""Calling a method of the game, e.g. a cheat a game's scripts offer: a static
C# method or UFunction, an instance method of an inspected object, or (Unreal)
one of the catalog on a live object of its class.

The method call dialog (grembyte/ui/widgets/method_call.py) reads one text per
parameter. ``parse_argument`` turns it into what the agent passes to the
method (``callStatic`` in grembyte/features/monitor.js): a number, bool,
string, None (null), ``{"address": "0x…"}`` (an object or IntPtr) or
``{"struct": "…"}`` (the bytes of a struct, in hex: a System.Guid, or a
struct of numbers). The types are those ``describe_call`` reports: ``{kind,
byref, name, enumName?, enumValues?, engine?, fields?}`` with ECMA-335
element types as kinds; an enum has the kind of its base type, and
``enumValues`` (``{"0": "Might", ...}``, Unreal) lets its names be typed in.

Unreal's own types have ``engine`` set: FString (``string``) and FName
(``name``) are typed in as text, a bool (``bool``) like a C# one. A struct
whose fields are all numbers (FVector, UnityEngine.Vector3) comes with
``fields`` (``{name, kind, offset, enumValues?}``) and ``structSize``; its
values are typed in order (``1 2 3``) or by name (``X=1; Z=3``, the rest 0).
A parameter with ``out`` set (Unreal) is an output: nothing is typed in.
"""

import json
import math
import re
import struct
import uuid

from grembyte.core.messages import LocalizedValueError
from grembyte.engines import base as engine_base
from grembyte.engines.registry import engine_by_id

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


class ArgumentError(LocalizedValueError):
    """A text that is no value of its parameter's type."""

    @property
    def values(self):
        """What fills in the message ``key``."""
        return self.params


def call_capability(method):
    """What the engine must offer to call ``method`` (a CatalogMethod) from the
    catalog: ``CALL``, or for an instance method ``CALL_FOUND``."""
    return engine_base.CALL if method.static else engine_base.CALL_FOUND


def can_call(method):
    """Whether the engine of ``method`` can call it from the catalog."""
    engine = engine_by_id(method.engine)
    return engine is not None and engine.supports(call_capability(method))


GUID = "System.Guid"
# Unreal's FString, FName and bool
ENGINE_STRING, ENGINE_NAME, ENGINE_BOOL = "string", "name", "bool"
_ENGINE_TYPES = frozenset({ENGINE_STRING, ENGINE_NAME, ENGINE_BOOL})
# How the fields of a struct of numbers are stored (little-endian)
_FIELD_FORMATS = {I1: "<b", U1: "<B", I2: "<h", U2: "<H", I4: "<i", U4: "<I",
                  I8: "<q", U8: "<Q", R4: "<f", R8: "<d"}
# The values of a struct: separated by spaces or semicolons, "Name=value" or in order
_STRUCT_SEPARATORS = re.compile(r"[\s;]+")


def supported(param_type):
    """Whether an argument of ``param_type`` can be typed in: not structs
    other than a Guid or one of numbers, nor Unreal's containers, FText,
    delegates and weak pointers."""
    if "engine" in param_type:
        return param_type["engine"] in _ENGINE_TYPES
    return param_type["kind"] != VALUETYPE or is_guid(param_type) or is_number_struct(param_type)


def is_guid(param_type):
    """Whether ``param_type`` is System.Guid, typed in as text."""
    return param_type["kind"] == VALUETYPE and param_type["name"] == GUID


def is_number_struct(param_type):
    """Whether ``param_type`` is a struct of numbers, typed in field by field."""
    return (param_type["kind"] == VALUETYPE and bool(param_type.get("fields"))
            and bool(param_type.get("structSize")))


def is_bool(param_type):
    """Whether ``param_type`` is a bool (C#, or Unreal's bitfield bool)."""
    return param_type["kind"] == BOOLEAN or param_type.get("engine") == ENGINE_BOOL


def is_output(param):
    """Whether a parameter (``{name, type, out?}``) is an output only: nothing to type in."""
    return bool(param.get("out"))


def is_text(param_type):
    """Whether ``param_type`` is typed in as text (string, FString, FName)."""
    return (param_type["kind"] == STRING
            or param_type.get("engine") in (ENGINE_STRING, ENGINE_NAME))


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
    if is_number_struct(param_type):
        return _number_struct(param_type, text)
    if is_text(param_type):
        return text
    if is_bool(param_type):
        word = text.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        raise ArgumentError("call.error_bool", text=text)
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
    if kind == CHAR:
        if len(text) != 1:
            raise ArgumentError("call.error_char", text=text)
        return text
    if kind in (R4, R8) or kind in INTEGER_RANGES:
        return _number(param_type, text)
    raise ArgumentError("call.error_struct", type=param_type["name"])


def _number(param_type, text):
    """The value of ``text`` for a number (or enum) of ``param_type``."""
    kind = param_type["kind"]
    if kind in (R4, R8):
        try:
            value = float(text.strip().replace(",", "."))
        except ValueError:
            raise ArgumentError("call.error_number", text=text) from None
        if not math.isfinite(value):
            raise ArgumentError("call.error_number", text=text)
        return value
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


def struct_field_names(param_type):
    """The names of a struct of numbers' fields, as the hint lists them."""
    return [field["name"] for field in param_type["fields"]]


def _number_struct(param_type, text):
    """The bytes of a struct of numbers: ``1 2 3`` (every field, in order) or
    ``X=1; Z=3`` (the fields named, the rest 0); a comma after a value is
    dropped, one inside it is a decimal comma (``1,5 2 3``)."""
    fields = param_type["fields"]
    by_name = {field["name"].lower(): field for field in fields}
    words = [word.rstrip(",") for word in
             _STRUCT_SEPARATORS.split(re.sub(r"\s*=\s*", "=", text.strip())) if word]
    words = [word for word in words if word]
    named = [word for word in words if "=" in word]
    if named and len(named) != len(words):
        raise ArgumentError("call.error_struct_mixed", text=text)
    if named:
        values = {}
        for word in words:
            name, _, value = word.partition("=")
            field = by_name.get(name.lower())
            if field is None:
                raise ArgumentError("call.error_struct_field", name=name,
                                    fields=", ".join(struct_field_names(param_type)))
            values[field["name"]] = value
    elif len(words) == len(fields):
        values = {field["name"]: word for field, word in zip(fields, words)}
    else:
        raise ArgumentError("call.error_struct_count", text=text, count=len(fields),
                            fields=", ".join(struct_field_names(param_type)))
    data = bytearray(param_type["structSize"])
    parts = []
    for field in fields:
        word = values.get(field["name"])
        value = 0 if word is None else _number(
            {"kind": field["kind"], "enumValues": field.get("enumValues")}, word)
        struct.pack_into(_FIELD_FORMATS[field["kind"]], data, field["offset"], value)
        parts.append(f"{field['name']}={value}")
    return {"struct": data.hex(), "text": "{" + ", ".join(parts) + "}"}


def argument_text(value):
    """An argument as the call log shows it."""
    if value is None:
        return "null"
    if isinstance(value, dict):
        return value.get("guid") or value.get("text") or value["address"]
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
