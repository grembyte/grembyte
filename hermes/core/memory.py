"""Helpers for reading and writing Linux process memory."""

from hermes.core.value_types import value_type as get_value_type


def write_process_bytes(pid, address, data):
    """Write raw bytes to a process address."""
    with open(f"/proc/{pid}/mem", "r+b", buffering=0) as memory:
        memory.seek(address)
        bytes_written = memory.write(data)

    if bytes_written != len(data):
        raise OSError(
            f"Incomplete memory write: wrote {bytes_written} of {len(data)} bytes"
        )


def write_process_value(pid, address, value, value_type):
    """Write a typed value to a process address and return its normalized value."""
    kind = get_value_type(value_type)
    parsed_value = kind.parse(value)
    write_process_bytes(pid, address, kind.pack(parsed_value))
    return parsed_value
