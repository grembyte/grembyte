"""Helpers for reading and writing process memory."""

import os
import re

from hermes.core.pointer import module_bases
from hermes.platform import ProcessMemory
from hermes.core.value_types import value_type as get_value_type

PAGE_SIZE = 4096

_MODULE_ADDRESS = re.compile(r"^(?P<module>.+?)\s*\+\s*(?P<offset>(0x)?[0-9a-fA-F]+)$")


def read_memory(pid, address, size):
    """Read ``size`` bytes at ``address`` page by page.

    Returns ``(data, readable)``: ``data`` holds zero for bytes that cannot be
    read (unmapped or protected memory), ``readable`` is a bytearray with 1
    for every byte that was read.
    """
    data = bytearray(size)
    readable = bytearray(size)
    if size <= 0:
        return bytes(data), readable
    with ProcessMemory(pid) as memory:
        position = address
        end = address + size
        while position < end:
            chunk = min(end, (position // PAGE_SIZE + 1) * PAGE_SIZE) - position
            block = memory.read_available(position, chunk)
            if block:
                start = position - address
                data[start:start + len(block)] = block
                readable[start:start + len(block)] = b"\x01" * len(block)
            position += chunk
    return bytes(data), readable


def parse_address(text, mappings=None):
    """Parse ``0x1234``, ``1234`` (hex) or ``module+0x10`` into an address.

    Module names need the process ``mappings``. Raises ValueError.
    """
    text = (text or "").strip()
    match = _MODULE_ADDRESS.match(text)
    if match is not None and not re.fullmatch(r"(0x)?[0-9a-fA-F]+", match["module"]):
        bases = module_bases(mappings or [])
        base = bases.get(match["module"])
        if base is None:
            raise ValueError(f"Unknown module: {match['module']}")
        return base + int(match["offset"], 16)
    try:
        return int(text, 16)
    except ValueError as error:
        raise ValueError(f"Invalid address: {text!r}") from error


def describe_address(mappings, address):
    """Return where ``address`` lies, e.g. ``game+0x1f0 (rw-p)``, or None."""
    for mapping in mappings:
        if mapping.start <= address < mapping.end:
            if mapping.module is not None:
                base = module_bases(mappings)[mapping.module]
                where = f"{mapping.module}+{address - base:#x}"
            else:
                name = os.path.basename(mapping.path) or "[anon]"
                where = f"{name}+{address - mapping.start:#x}"
            return f"{where} ({mapping.permissions})"
    return None


def write_process_bytes(pid, address, data):
    """Write raw bytes to a process address."""
    with ProcessMemory(pid, writable=True) as memory:
        memory.write(address, data)


def write_process_value(pid, address, value, value_type):
    """Write a typed value to a process address and return its normalized value."""
    kind = get_value_type(value_type)
    parsed_value = kind.parse(value)
    write_process_bytes(pid, address, kind.pack(parsed_value))
    return parsed_value
