"""Helpers for reading and writing process memory."""

import os
import re

from hermes.core.pointer import module_bases
from hermes.platform import ProcessMemory
from hermes.core.value_types import value_type as get_value_type

PAGE_SIZE = 4096

_HEX = re.compile(r"(0x)?[0-9a-fA-F]+", re.IGNORECASE)
# The last offset of an address expression, e.g. "+0x244" or "- 10"
_LAST_OFFSET = re.compile(r"\s*(?P<sign>[+-])\s*(?P<offset>(0x)?[0-9a-fA-F]+)\s*$", re.IGNORECASE)


class UnknownModuleError(ValueError):
    """An address names a module the process has not loaded."""

    def __init__(self, module):
        super().__init__(f"Unknown module: {module}")
        self.module = module


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


def _splits(text):
    """``(base, offset)`` for every way to read ``text`` as a base and hex offsets.

    The longest base comes first: a module name may hold a ``-`` or ``+``
    followed by digits (``libfoo-1+0x10``).
    """
    base, offset = text, 0
    yield base, offset
    while (match := _LAST_OFFSET.search(base)) is not None and match.start() > 0:
        value = int(match["offset"], 16)
        offset += value if match["sign"] == "+" else -value
        base = base[:match.start()].rstrip()
        yield base, offset


def parse_address(text, mappings=None):
    """Parse an address: ``0x1234``, ``1234`` (hex), ``module+0x10`` or a sum.

    The base, a hex number or a module name, may be followed by any number of
    hex offsets to add or subtract, e.g. ``0x7f55e9339000+0x244`` or
    ``game+0x1f0-8``. Module names need the process ``mappings``. Raises
    ValueError, UnknownModuleError for a module the process has not loaded.
    """
    text = (text or "").strip()
    bases = None
    unknown = None
    for base, offset in _splits(text):
        if _HEX.fullmatch(base):
            address = int(base, 16) + offset
        else:
            if bases is None:
                bases = module_bases(mappings or [])
            if base not in bases:
                if base != text:
                    unknown = base  # the shortest such base names the module
                continue
            address = bases[base] + offset
        if address < 0:
            raise ValueError(f"Negative address: {text!r}")
        return address
    if unknown is not None:
        raise UnknownModuleError(unknown)
    raise ValueError(f"Invalid address: {text!r}")


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
