"""Helpers for reading and writing Linux process memory."""

import struct


_VALUE_FORMATS = {
    "int32": "<i",
    "int64": "<q",
    "float": "<f",
    "double": "<d",
}


def write_process_value(pid, address, value, value_type):
    """Write a typed value to a process address and return its normalized value."""
    try:
        value_format = _VALUE_FORMATS[value_type]
    except KeyError as error:
        raise ValueError(f"Unsupported data type: {value_type}") from error

    try:
        parsed_value = (
            int(value, 0)
            if value_type in {"int32", "int64"}
            else float(value)
        )
        packed_value = struct.pack(value_format, parsed_value)
    except (OverflowError, TypeError, ValueError, struct.error) as error:
        raise ValueError(f"Invalid {value_type} value: {value!r}") from error

    with open(f"/proc/{pid}/mem", "r+b", buffering=0) as memory:
        memory.seek(address)
        bytes_written = memory.write(packed_value)

    if bytes_written != len(packed_value):
        raise OSError(
            f"Incomplete memory write: wrote {bytes_written} of {len(packed_value)} bytes"
        )

    return struct.unpack(value_format, packed_value)[0]
