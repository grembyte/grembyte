"""The address-space region every platform reports."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mapping:
    """A region of a process' address space.

    The platform layer decides what the fields mean for its system, so code
    above it needs no knowledge of /proc or Windows memory APIs.
    """

    start: int
    end: int
    # Shown to the user, e.g. "rw-p".
    permissions: str
    # Backing file or a name such as "[heap]"; "" for anonymous memory.
    path: str = ""
    # Name of the loaded module (program or library) the region belongs to,
    # its data and .bss included; None for other memory.
    module: str | None = None
    # Private, readable and writable memory: where values and pointers live.
    writable: bool = False
