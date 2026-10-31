"""Platform layer of the Frida agents: what differs between Linux and Windows.

A native game shows the agent Linux; a Wine/Proton game shows it Windows,
because Frida runs there as its Windows gadget (see wine_gadget.py). Engine
adapters and features use these helpers instead of calling either system's
API themselves.

``spawnThread(start, arg)``
    Run the native function ``start(arg)`` on a new thread nobody waits for
    (``pthread_create``, or ``CreateThread`` under Wine). Throws on failure.
``watchUnmaps(onUnmap)``
    Call ``onUnmap(start, end)`` before memory is unmapped or released, while
    it is still mapped (``munmap``; under Wine ``NtFreeVirtualMemory`` and
    ``NtUnmapViewOfSection``). Returns a listener with ``detach()``.
``mainThreadId()``
    The id of the process's first thread, where Unity runs its player loop.
    On Linux that is the process id; Windows gives threads ids of their own,
    so there it is the thread created first (``GetThreadTimes``).
``pauseThreads()`` / ``resumeThreads(paused)``
    Stop every thread of the game while the agent changes code in bulk, and
    let them run again. Frida's own threads keep running: they are not in
    ``Process.enumerateThreads()``. Windows only (``SuspendThread``, which Wine
    delays until a thread holds none of its internal locks); elsewhere
    ``pauseThreads`` returns null and does nothing. Under Wine, game threads
    entering freshly hooked code while Frida still hooks more could deadlock
    on a lock inside Frida's Windows gadget; Linux has no such problem.
``allocateCode(size)``
    Executable memory that is never freed, not even when the agent unloads
    (``VirtualAlloc``, or ``mmap``): for code game threads may still run.
"""

from hermes.core import messages
from hermes.core.agent_source import load_agent_source

PLATFORM_AGENT_SOURCE = messages.AGENT_SOURCE + load_agent_source(__file__)
