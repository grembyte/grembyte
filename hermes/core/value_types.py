"""Value types that can be scanned, tracked, frozen, watched and hooked.

All values are little-endian. ``VALUE_TYPES`` keeps the order shown in the UI.
"""

import struct
from dataclasses import dataclass

import numpy as np

from hermes.core.messages import LocalizedValueError


@dataclass(frozen=True)
class ValueType:
    """A numeric value type, e.g. ``int32``."""

    name: str
    struct_format: str  # little-endian struct format, e.g. "<i"

    @property
    def size(self):
        return struct.calcsize(self.struct_format)

    @property
    def dtype(self):
        return np.dtype(self.struct_format)

    @property
    def is_integer(self):
        return self.struct_format[-1] not in "fd"

    @property
    def limits(self):
        """Return ``(minimum, maximum)`` of an integer type, None for floats."""
        if not self.is_integer:
            return None
        info = np.iinfo(self.dtype)
        return int(info.min), int(info.max)

    def parse(self, text):
        """Parse user input (``0x`` prefixes allowed for integers).

        Raises ValueError for invalid or out-of-range input.
        """
        try:
            value = int(text, 0) if self.is_integer else float(text)
            return self.unpack(self.pack(value))
        except (AttributeError, OverflowError, TypeError, ValueError, struct.error) as error:
            raise LocalizedValueError("error.value_types.invalid_value", type=self.name,
                                      text=repr(text)) from error

    def pack(self, value):
        return struct.pack(self.struct_format, value)

    def unpack(self, data):
        return struct.unpack(self.struct_format, data)[0]


VALUE_TYPES = {
    value_type.name: value_type
    for value_type in (
        ValueType("int8", "<b"),
        ValueType("int16", "<h"),
        ValueType("int32", "<i"),
        ValueType("int64", "<q"),
        ValueType("uint8", "<B"),
        ValueType("uint16", "<H"),
        ValueType("uint32", "<I"),
        ValueType("uint64", "<Q"),
        ValueType("float", "<f"),
        ValueType("double", "<d"),
    )
}

DEFAULT_VALUE_TYPE = "int32"

# Types tried by the "guess automatically" first scan. Small integers would
# match almost every byte, so they have to be chosen explicitly.
AUTO_VALUE_TYPES = ("int32", "int64", "float", "double")


def value_type(name):
    """Return the ValueType called ``name``; raise ValueError if unknown."""
    try:
        return VALUE_TYPES[name]
    except KeyError as error:
        raise LocalizedValueError("error.value_types.unsupported_type", type=name) from error
