"""Getting into a game process: Frida sessions, native or under Wine/Proton.

``frida_attach``
    Opens a Frida session on a process; under Wine through the gadget.
``wine_gadget``
    Loads Frida's Windows gadget into a Wine/Proton game, with the helper
    program (``wine_helper``, source in ``inject/``) or with ptrace.
``ptrace_engine``
    Shared ptrace machinery: stopping threads, debug registers, watchpoints.
"""
