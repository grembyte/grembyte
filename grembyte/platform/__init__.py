"""What differs between operating systems on grembyte's side of a process.

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
``frida_agents(pid)``
    The Frida runtimes (injected agents, gadget) the program has loaded, as a
    set of (device, inode): one per host process attached. None where it
    cannot be told (Windows).
``thread_stack_pointers(pid)``
    Stack pointers of the program's threads, or None where the system marks
    stacks otherwise (Windows: guard pages).

Linux (linux.py) and Windows (windows.py) are implemented. wine.py reads
the PE images of a Wine process (``wine_program``, ``wine_bits``); like the
ptrace backends and the Wine gadget loader in ``grembyte.attach`` it is
Linux-only at runtime.
"""

import sys

from grembyte.platform.mapping import Mapping

if sys.platform.startswith("linux"):
    from grembyte.platform.linux import (
        ProcessMemory,
        frida_agents,
        is_wine_process,
        main_module,
        process_start_time,
        program_bits,
        read_mappings,
        thread_stack_pointers,
    )
elif sys.platform == "win32":
    from grembyte.platform.windows import (
        ProcessMemory,
        frida_agents,
        is_wine_process,
        main_module,
        process_start_time,
        program_bits,
        read_mappings,
        thread_stack_pointers,
    )
else:
    raise ImportError(f"grembyte does not support {sys.platform} yet")

__all__ = [
    "Mapping",
    "ProcessMemory",
    "frida_agents",
    "is_wine_process",
    "main_module",
    "process_start_time",
    "program_bits",
    "read_mappings",
    "thread_stack_pointers",
]
