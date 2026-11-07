"""How hermes sets hardware watchpoints: through Frida, or (Linux) through ptrace.

Write watches and hooks both offer the choice; the UI switches between
these names. The ptrace side lives in ptrace_engine, which is imported only
when it is used.
"""

import sys

# Watch backends the UI can switch between; ptrace is Linux's.
BACKEND_FRIDA = "frida"
BACKEND_PTRACE = "ptrace"
WATCH_BACKENDS = ((BACKEND_FRIDA, BACKEND_PTRACE) if sys.platform.startswith("linux")
                  else (BACKEND_FRIDA,))
