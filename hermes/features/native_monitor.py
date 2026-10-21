"""Native function monitor: the calls of a function of the game's machine code.

The native counterpart of the method monitor (monitor.py), for functions the
native trace found or that are known by address or symbol. A Frida
interceptor on the function's first instruction reports every call with

- its first arguments, as the platform's calling convention passes them
  (x86-64 Linux: rdi, rsi, rdx, rcx, r8, r9; Windows/Wine: rcx, rdx, r8, r9,
  then the stack). Floating point arguments (xmm registers) are not seen;
- the return value (rax);
- the caller (the return address);
- what changed in memory: for every argument that points to readable
  memory, ``dump`` bytes there are copied when the call begins and compared
  when it returns. For a method of a C++ object the first argument is
  ``this``, so the comparison shows which fields of the object it set.

Changes elsewhere (globals, objects reached through pointers) are not seen.
A copy taken while another thread writes there shows that thread's writes
too.

Calls to a function the game makes very often are only counted beyond
``MAX_LOGGED_PER_SECOND`` per second, to keep the game playable.

All of that holds only at the first instruction of a function: to see the
call return, Frida replaces the return address on top of the stack. Inside a
function the top of the stack is some local variable, which that would
overwrite. An address inside a function (e.g. the instruction a write watch
reported) is therefore monitored as an *instruction*: a probe that sees every
pass with all general purpose registers, and touches nothing. ``kind`` tells
which: ``"function"``, ``"instruction"``, or ``"auto"`` (a function where a
symbol starts exactly there, else an instruction). Its answer names the
function an instruction lies in, if a symbol tells, to monitor that instead.

Frida replaces the first instructions at a hooked address with a jump. A
second hook a few bytes further would patch into that jump and crash the
game: the agent refuses hooks whose patched bytes would overlap.
"""

import re

import frida
from PyQt6.QtCore import pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import sessions
from hermes.features.trace import AgentWorker
from hermes.core.agent_source import load_agent_source


# Calls logged per function and second; the others are only counted.
MAX_LOGGED_PER_SECOND = 200
# Changed ranges reported per call (a constructor changes everything).
MAX_CHANGES = 64
# Default number of arguments and bytes compared per pointer argument.
DEFAULT_ARGUMENTS = 4
DEFAULT_DUMP = 0x200
MAX_DUMP = 0x10000

FUNCTION, INSTRUCTION, AUTO = "function", "instruction", "auto"

_AGENT_SOURCE = (
    load_agent_source(__file__)
    .replace("MAX_LOGGED_VALUE", str(MAX_LOGGED_PER_SECOND))
    .replace("MAX_CHANGES_VALUE", str(MAX_CHANGES))
)


def build_agent_source():
    return _AGENT_SOURCE


# Where arguments are passed, by Process.platform (x86-64)
_REGISTERS = {
    "windows": ("rcx", "rdx", "r8", "r9"),
    "linux": ("rdi", "rsi", "rdx", "rcx", "r8", "r9"),
}


def argument_name(index, platform):
    """``rdi`` … for the register an argument is passed in, ``arg4`` on the stack."""
    registers = _REGISTERS.get(platform, ())
    return registers[index] if index < len(registers) else f"arg{index}"


_LOCATION = re.compile(r"^\s*([^\s+]+)\s*\+\s*(0x[0-9a-fA-F]+|[0-9]+)\s*$")


def parse_location(text):
    """The agent's spec of where a function is: an address, module+offset, or a name.

    ``0x55aa…`` is an address, ``BaldursGate+0x606e50`` a module and offset,
    anything else a symbol or export name. Raises ValueError for empty text.
    """
    text = text.strip()
    if not text:
        raise ValueError("no function given")
    if re.fullmatch(r"0x[0-9a-fA-F]+", text):
        return {"address": text.lower()}
    match = _LOCATION.match(text)
    if match:
        return {"module": match.group(1), "offset": int(match.group(2), 0)}
    return {"name": text}


def signed(value_text, bits=64):
    """The register value ``0x…`` as a signed number."""
    value = int(value_text, 16)
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def change_numbers(before, after):
    """(before, after) of a changed range as little-endian unsigned numbers, or None.

    Only for ranges of 1–8 bytes, the size of a number: longer ones are
    probably several fields.
    """
    size = len(before) // 2
    if not 1 <= size <= 8:
        return None
    return (int.from_bytes(bytes.fromhex(before), "little"),
            int.from_bytes(bytes.fromhex(after), "little"))


class NativeMonitorWorker(AgentWorker):
    """Keep a Frida session open for native function monitors; see the module documentation.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    # monitor id, {address, where, symbol, kind, function?, functionName?}
    added = pyqtSignal(int, dict)
    removed = pyqtSignal(int)
    # calls, {monitor id: {calls, skipped}}
    calls = pyqtSignal(list, dict)
    # monitor id, message: adding it failed
    add_failed = pyqtSignal(int, str)

    agent_name = "native monitor"

    def __init__(self, pid, parent=None):
        super().__init__(pid, parent)
        self.platform = None

    def agent_source(self):
        return build_agent_source()

    def add(self, monitor_id, location, arguments=DEFAULT_ARGUMENTS, dump=DEFAULT_DUMP,
            kind=AUTO):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        """Monitor the function or instruction at ``location`` (see parse_location).

        Expect added, with ``{address, where, symbol, kind}`` and for an
        instruction inside a known function ``function`` and ``functionName``.
        """
        spec = dict(location, id=monitor_id, kind=kind, args=arguments,
                    dump=min(dump, MAX_DUMP))
        self._commands.put(("add", spec))

    def remove(self, monitor_id):
        self._commands.put(("remove", monitor_id))

    @sessions.tracked(sessions.NATIVE_MONITOR)
    def run(self):
        self._run_agent()

    def _execute(self, api, action, argument):
        if action != "add":
            super()._execute(api, action, argument)
            return
        try:
            self.added.emit(argument["id"], api.add(argument))
        except frida.RPCException as error:
            # e.g. no such function: the other monitors go on
            self.add_failed.emit(argument["id"], str(error).split("\n", 1)[0])

    def _run_command(self, api, action, argument):
        if action == "remove":
            api.remove(argument)
            self.removed.emit(argument)
        else:
            super()._run_command(api, action, argument)

    def _on_message(self, message, data):
        payload = message.get("payload") or {}
        if payload.get("kind") == "ready":
            self.platform = payload.get("platform")
        super()._on_message(message, data)

    def _on_payload(self, kind, payload):
        if kind == "calls":
            self.calls.emit(payload.get("calls", []), {
                int(key): value for key, value in payload.get("counts", {}).items()})
