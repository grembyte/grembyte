"""What differs between operating systems on hermes' side of a process.

Everything above this package reads and writes process memory, lists memory
regions and asks about processes through these names only:

``Mapping``
    A region of the address space (see mapping.py).
``read_mappings(pid)``
    The process' Mappings, sorted by address.
``ProcessMemory(pid, writable=False)``
    Context manager with ``read``, ``read_available``, ``read_pointer`` and
    ``write``.
``main_module(pid)``
    Module name of the program itself, or None.
``program_bits(pid)``
    32 or 64 for the program's executable, or None.
``process_start_time(pid)``
    A value that changes when the PID belongs to a new process; None once it
    has exited.
``is_wine_process(pid)``
    Whether the program runs under Wine or Proton.

Linux (linux.py) and Windows (windows.py) are implemented. The ptrace
backends and the Wine gadget loader in ``hermes.core`` are Linux-only on top
of that.
"""

import sys

from hermes.platform.mapping import Mapping

if sys.platform.startswith("linux"):
    from hermes.platform.linux import (
        ProcessMemory,
        is_wine_process,
        main_module,
        process_start_time,
        program_bits,
        read_mappings,
    )
elif sys.platform == "win32":
    from hermes.platform.windows import (
        ProcessMemory,
        is_wine_process,
        main_module,
        process_start_time,
        program_bits,
        read_mappings,
    )
else:
    raise ImportError(f"hermes does not support {sys.platform} yet")

__all__ = [
    "Mapping",
    "ProcessMemory",
    "is_wine_process",
    "main_module",
    "process_start_time",
    "program_bits",
    "read_mappings",
]
