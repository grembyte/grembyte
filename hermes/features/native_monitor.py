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


# Calls logged per function and second; the others are only counted.
MAX_LOGGED_PER_SECOND = 200
# Changed ranges reported per call (a constructor changes everything).
MAX_CHANGES = 64
# Default number of arguments and bytes compared per pointer argument.
DEFAULT_ARGUMENTS = 4
DEFAULT_DUMP = 0x200
MAX_DUMP = 0x10000

FUNCTION, INSTRUCTION, AUTO = "function", "instruction", "auto"

_AGENT_SOURCE = r"""
const MAX_LOGGED_PER_SECOND = MAX_LOGGED_VALUE;
const MAX_CHANGES = MAX_CHANGES_VALUE;
const MAX_STRING = 80;
const SYMBOL_BUDGET_MS = 200;
const WINDOWS = Process.platform === 'windows';
const MIN_POINTER = ptr('0x10000');
const MAX_POINTER = ptr('0x800000000000');
const PAGE = Process.pageSize;
// Frida replaces up to 14 bytes, and the rest of the instructions they cut
// into, with a jump: a hook within that of another one would patch into it.
const REDIRECT_SIZE = 14;
const REGISTERS = Process.arch === 'x64'
  ? ['rax', 'rbx', 'rcx', 'rdx', 'rsi', 'rdi', 'rbp', 'rsp',
     'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15']
  : ['eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp', 'esp'];

const monitors = new Map();   // id -> monitor
let pending = [];
let modules = new ModuleMap();
const symbols = new Map();    // address text -> symbol text ('' for none)
let reportedCalls = -1;       // calls of all monitors at the last flush

function isPointer(value) {
  return value.compare(MIN_POINTER) >= 0 && value.compare(MAX_POINTER) < 0;
}

function readRegion(address, size) {
  // Up to ``size`` bytes, or up to the end of the page if the next one is not readable.
  try {
    return address.readByteArray(size);
  } catch (_error) {
    // fall through
  }
  const toPageEnd = PAGE - address.and(PAGE - 1).toInt32();
  if (toPageEnd >= size)
    return null;
  try {
    return address.readByteArray(toPageEnd);
  } catch (_error) {
    return null;
  }
}

function asciiText(bytes) {
  // The bytes as text if they start with a printable, NUL-terminated string.
  const view = new Uint8Array(bytes);
  let text = '';
  for (let i = 0; i < Math.min(view.length, MAX_STRING); i++) {
    const byte = view[i];
    if (byte === 0)
      return text.length >= 3 ? text : null;
    if (byte < 0x20 || byte > 0x7e)
      return null;
    text += String.fromCharCode(byte);
  }
  return text.length >= 3 ? text + '…' : null;
}

function hex(view, start, end) {
  let text = '';
  for (let i = start; i < end; i++)
    text += (view[i] < 16 ? '0' : '') + view[i].toString(16);
  return text;
}

function changes(call, found) {
  // Ranges of changed bytes in the regions the arguments pointed to.
  const seen = new Set();
  call.regions.forEach((before, index) => {
    if (before === null)
      return;
    const base = call.values[index];
    let after;
    try {
      after = base.readByteArray(before.byteLength);
    } catch (_error) {
      return;
    }
    const old = new Uint8Array(before);
    const now = new Uint8Array(after);
    let i = 0;
    while (i < old.length) {
      if (old[i] === now[i]) {
        i++;
        continue;
      }
      let end = i + 1;
      while (end < old.length && old[end] !== now[end])
        end++;
      const address = base.add(i).toString();
      if (!seen.has(address)) {
        seen.add(address);
        if (found.list.length < MAX_CHANGES)
          found.list.push({ arg: index, offset: i, address,
                            before: hex(old, i, end), after: hex(now, i, end) });
        else
          found.more = true;
      }
      i = end;
    }
  });
}

function where(address) {
  // module+0x… of an address in a module, or ''.
  const module = modules.find(address);
  if (module === null)
    return '';
  return `${module.name}+0x${address.sub(module.base).toString(16)}`;
}

function symbolOf(addressText, deadline) {
  // name+0x… of the function an address lies in, or '' (not under Windows: dbghelp under Wine).
  if (symbols.has(addressText))
    return symbols.get(addressText);
  if (WINDOWS || Date.now() > deadline)
    return '';
  let text = '';
  try {
    // Frida names an address inside a function "name+0x…", one without a symbol "0x…".
    const symbol = DebugSymbol.fromAddress(ptr(addressText));
    if (symbol.name !== null && !symbol.name.startsWith('0x'))
      text = symbol.name;
  } catch (_error) {
    // unnamed
  }
  symbols.set(addressText, text);
  return text;
}

function value(pointer, region) {
  const text = region !== null && region !== undefined ? asciiText(region) : null;
  return { value: pointer.toString(), text };
}

function admit(monitor) {
  // Whether to log this call (else it is only counted).
  monitor.calls++;
  const now = Date.now();
  if (now - monitor.second >= 1000) {
    monitor.second = now;
    monitor.logged = 0;
  }
  if (monitor.logged >= MAX_LOGGED_PER_SECOND) {
    monitor.skipped++;
    return false;
  }
  monitor.logged++;
  return true;
}

function probe(monitor) {
  // A function as callback: an instruction probe, which leaves the stack alone.
  return Interceptor.attach(monitor.address, function () {
    if (!admit(monitor))
      return;
    const context = this.context;
    pending.push({
      monitorId: monitor.id,
      time: Date.now(),
      thread: this.threadId,
      registers: REGISTERS.map(name => {
        const register = ptr(context[name].toString());
        return [name, value(register, isPointer(register)
          ? readRegion(register, MAX_STRING + 1) : null)];
      })
    });
  });
}

function attach(monitor) {
  return Interceptor.attach(monitor.address, {
    onEnter(args) {
      if (!admit(monitor)) {
        this.call = null;
        return;
      }
      const now = Date.now();
      const values = [];
      const regions = [];
      for (let i = 0; i < monitor.args; i++) {
        const argument = args[i];
        values.push(argument);
        regions.push(monitor.dump > 0 && isPointer(argument)
          ? readRegion(argument, monitor.dump) : null);
      }
      this.call = { values, regions, time: now, thread: this.threadId, depth: this.depth,
                    caller: this.returnAddress };
    },
    onLeave(retval) {
      const call = this.call;
      if (call === null || call === undefined)
        return;
      const found = { list: [], more: false };
      changes(call, found);
      const result = ptr(retval.toString());
      pending.push({
        monitorId: monitor.id,
        time: call.time,
        thread: call.thread,
        depth: call.depth,
        args: call.values.map((argument, i) => value(argument, call.regions[i])),
        result: value(result, isPointer(result) ? readRegion(result, MAX_STRING + 1) : null),
        caller: call.caller.toString(),
        changes: found.list,
        moreChanges: found.more
      });
    }
  });
}

function describe(call, deadline) {
  if (call.registers !== undefined) {
    for (const [_name, register] of call.registers)
      register.where = where(ptr(register.value));
    return;
  }
  for (const argument of call.args)
    argument.where = where(ptr(argument.value));
  call.result.where = where(ptr(call.result.value));
  call.callerWhere = where(ptr(call.caller));
  call.callerSymbol = symbolOf(call.caller, deadline);
}

function flush() {
  let total = 0;
  for (const monitor of monitors.values())
    total += monitor.calls;
  if (pending.length === 0 && total === reportedCalls)
    return;
  reportedCalls = total;
  const calls = pending;
  pending = [];
  const deadline = Date.now() + SYMBOL_BUDGET_MS;
  for (const call of calls)
    describe(call, deadline);
  const counts = {};
  for (const monitor of monitors.values())
    counts[monitor.id] = { calls: monitor.calls, skipped: monitor.skipped };
  send({ kind: 'calls', calls, counts });
}

function resolve(spec) {
  // spec: {address} | {module, offset} | {name}
  if (spec.address !== undefined)
    return ptr(spec.address);
  if (spec.module !== undefined)
    return Process.getModuleByName(spec.module).base.add(spec.offset);
  if (!WINDOWS) {
    try {
      return DebugSymbol.getFunctionByName(spec.name);
    } catch (_error) {
      // not in the symbol tables: maybe an export
    }
  }
  const exported = Module.findGlobalExportByName(spec.name);
  if (exported === null)
    throw new Error(`no function named ${spec.name}`);
  return exported;
}

function patchSpan(address) {
  // The bytes Frida may replace with its jump: whole instructions, REDIRECT_SIZE at least.
  let size = 0;
  let at = address;
  try {
    while (size < REDIRECT_SIZE) {
      const instruction = Instruction.parse(at);
      size += instruction.size;
      at = instruction.next;
    }
  } catch (_error) {
    return Math.max(size, REDIRECT_SIZE);
  }
  return size;
}

function enclosing(address) {
  // {start, name} of the function an address lies in, by its symbol, or null.
  const text = symbolOf(address.toString(), Date.now() + 2000);
  if (text === '')
    return null;
  const inside = /^(.+)\+0x([0-9a-f]+)$/.exec(text);
  if (inside === null)
    return { start: address, name: text };
  return { start: address.sub(parseInt(inside[2], 16)), name: inside[1] };
}

function add(spec) {
  // spec: where (see resolve), id, kind, args, dump
  if (monitors.has(spec.id))
    throw new Error(`monitor ${spec.id} exists already`);
  const address = resolve(spec);
  const span = patchSpan(address);
  for (const other of monitors.values()) {
    const overlap = address.compare(other.address.add(other.span)) < 0
      && other.address.compare(address.add(span)) < 0;
    if (overlap && !other.address.equals(address))
      throw new Error(`${address} is too close to ${other.address}, which is monitored ` +
                      'already: Frida would patch into its jump');
  }
  if (modules.find(address) === null)
    modules.update();  // loaded since
  const inside = enclosing(address);
  let kind = spec.kind;
  if (kind === 'auto')
    kind = inside !== null && inside.start.equals(address) ? 'function' : 'instruction';
  const monitor = {
    id: spec.id, address, span, args: spec.args, dump: spec.dump,
    calls: 0, skipped: 0, second: 0, logged: 0, listener: null
  };
  monitor.listener = kind === 'function' ? attach(monitor) : probe(monitor);
  monitors.set(spec.id, monitor);
  const addressText = address.toString();
  const answer = { address: addressText, where: where(address), kind,
                   symbol: symbolOf(addressText, Date.now() + 2000) };
  if (kind === 'instruction' && inside !== null && !inside.start.equals(address)) {
    answer.function = inside.start.toString();
    answer.functionName = inside.name;
  }
  return answer;
}

function remove(id) {
  const monitor = monitors.get(id);
  if (monitor === undefined)
    return false;
  monitors.delete(id);
  monitor.listener.detach();
  return true;
}

const flushTimer = setInterval(flush, 100);
send({ kind: 'ready', platform: Process.platform, arch: Process.arch });

rpc.exports = {
  add,
  remove,
  dispose() {
    clearInterval(flushTimer);
    for (const id of Array.from(monitors.keys()))
      remove(id);
    Interceptor.flush();
    return true;
  }
};
""".replace("MAX_LOGGED_VALUE", str(MAX_LOGGED_PER_SECOND)) \
    .replace("MAX_CHANGES_VALUE", str(MAX_CHANGES))


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
