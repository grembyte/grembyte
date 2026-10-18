"""Method monitor: log the calls of C# methods in Mono games (e.g. Unity).

Mono compiles the method (see ``mono.prepare``), and a Frida interceptor on
its native code reports every call with its arguments, the calling method
and, if asked for, the return value.

Arguments are read where Mono's x86-64 calling convention puts them:
``this`` and integer-like values in rdi, rsi, rdx, rcx, r8, r9, floating
point values in xmm0-xmm7, the rest on the stack. A struct larger than 16
bytes is returned through a hidden pointer argument (after ``this``, or
after a first reference argument). Where structs are passed by value depends
on their fields, so arguments after a struct are not decoded.

When the game frees the memory of monitored code (Unity does so on exit,
when Mono releases its JIT code), the monitor is removed first: Frida would
otherwise write the original bytes back into unmapped memory when it is
unloaded, and abort the game. The agent watches ``munmap`` for that and
reports ``monitor-ended``.

The return value needs Frida to replace the return address of every call
with its own code. Mono cannot walk the stack through that code, so an
exception passing through the method can crash the game; hence it is
optional.
"""

import itertools
import queue
import sys
import threading

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core.mono import MONO_AGENT_SOURCE
from hermes.core.process import WINE_FRIDA_ERROR, is_wine_process
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS


_AGENT_SOURCE = MONO_AGENT_SOURCE + r"""
const T = mono.TYPE;
const INT_REGS = ['rdi', 'rsi', 'rdx', 'rcx', 'r8', 'r9'];
const FLOAT_REGS = 8;
const MAX_STRUCT_IN_REGS = 16;
const MAX_EVENTS = 200;
const MAX_STRING = 120;
const REFERENCE_KINDS = new Set([0x12, 0x14, 0x15, 0x1c, 0x1d]);  // class, array, generic, object, szarray
const monitors = new Map();
const events = [];
const dropped = new Map();
const counts = new Map();

// --- Where the arguments are ----------------------------------------------

function isFloat(type) {
  return !type.byref && (type.kind === T.R4 || type.kind === T.R8);
}

function isStruct(type) {
  return !type.byref && type.kind === T.VALUETYPE;
}

function isReference(type) {
  return type.byref || REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING;
}

function argumentLayout(info) {
  // { self, args: [location or null] }; a location is { reg }, { xmm } or
  // { stack } (8-byte slot above the return address).
  let ints = 0;
  let floats = 0;
  let stack = 0;
  const nextInt = () => (ints < INT_REGS.length ? { reg: INT_REGS[ints++] } : { stack: stack++ });
  const nextFloat = () => (floats < FLOAT_REGS ? { xmm: floats++ } : { stack: stack++ });
  const hiddenReturn = isStruct(info.returns) && info.returns.structSize > MAX_STRUCT_IN_REGS;
  const firstIsReference = info.params.length > 0 && isReference(info.params[0].type);
  const layout = { self: null, args: [] };
  if (info.instance) {
    layout.self = nextInt();
    if (hiddenReturn)
      nextInt();
  } else if (hiddenReturn && firstIsReference) {
    layout.args.push(nextInt());
    nextInt();
  } else if (hiddenReturn) {
    nextInt();
  }
  let known = true;
  for (const param of info.params.slice(layout.args.length)) {
    if (!known || isStruct(param.type)) {
      known = false;  // structs may take registers, stack or both
      layout.args.push(null);
    } else {
      layout.args.push(isFloat(param.type) ? nextFloat() : nextInt());
    }
  }
  return layout;
}

function rawValue(context, location) {
  if (location.reg !== undefined)
    return context[location.reg];
  if (location.xmm !== undefined)
    return context['xmm' + location.xmm];
  return context.sp.add(8 * (location.stack + 1)).readPointer();
}

// --- Values -----------------------------------------------------------------

function integer(pointer, bits, signed) {
  const value = BigInt(pointer.toString());
  return (signed ? BigInt.asIntN(bits, value) : BigInt.asUintN(bits, value)).toString();
}

function floatValue(raw, kind) {
  if (raw instanceof ArrayBuffer) {
    const view = new DataView(raw);
    return kind === T.R4 ? view.getFloat32(0, true) : view.getFloat64(0, true);
  }
  // On the stack: the value's bytes in the slot.
  const buffer = new ArrayBuffer(8);
  new DataView(buffer).setBigUint64(0, BigInt(raw.toString()), true);
  return floatValue(buffer, kind);
}

function managedString(pointer) {
  // MonoString: object header (16 bytes), length (int32), UTF-16 characters.
  const length = pointer.add(0x10).readS32();
  const text = pointer.add(0x14).readUtf16String(Math.min(length, MAX_STRING));
  return JSON.stringify(text) + (length > MAX_STRING ? '…' : '');
}

function readAt(pointer, kind) {
  switch (kind) {
    case T.BOOLEAN: return String(pointer.readU8() !== 0);
    case T.CHAR: return JSON.stringify(String.fromCharCode(pointer.readU16()));
    case T.I1: return String(pointer.readS8());
    case T.U1: return String(pointer.readU8());
    case T.I2: return String(pointer.readS16());
    case T.U2: return String(pointer.readU16());
    case T.I4: return String(pointer.readS32());
    case T.U4: return String(pointer.readU32());
    case T.I8: return pointer.readS64().toString();
    case T.U8: return pointer.readU64().toString();
    case T.R4: return String(pointer.readFloat());
    case T.R8: return String(pointer.readDouble());
    default: return describeValue({ kind, byref: false }, pointer.readPointer());
  }
}

function describeValue(type, raw) {
  if (type.byref)
    return `&${raw} → ${readAt(raw, type.kind)}`;
  let text;
  switch (type.kind) {
    case T.BOOLEAN: text = String((raw.toUInt32() & 0xff) !== 0); break;
    case T.CHAR: text = JSON.stringify(String.fromCharCode(raw.toUInt32() & 0xffff)); break;
    case T.I1: text = integer(raw, 8, true); break;
    case T.U1: text = integer(raw, 8, false); break;
    case T.I2: text = integer(raw, 16, true); break;
    case T.U2: text = integer(raw, 16, false); break;
    case T.I4: text = integer(raw, 32, true); break;
    case T.U4: text = integer(raw, 32, false); break;
    case T.I8: text = integer(raw, 64, true); break;
    case T.U8: text = integer(raw, 64, false); break;
    case T.R4:
    case T.R8: text = String(floatValue(raw, type.kind)); break;
    case T.VALUETYPE: return `struct ${type.name}`;
    case T.STRING: return raw.isNull() ? 'null' : managedString(raw);
    default:
      if (REFERENCE_KINDS.has(type.kind))
        return raw.isNull() ? 'null' : `${mono.objectClassName(raw)}@${raw}`;
      return raw.toString();
  }
  return type.enumName ? `${text} (${type.enumName})` : text;
}

function safely(describe) {
  try {
    return describe();
  } catch (error) {
    return `? (${error.message})`;
  }
}

function frameText(address) {
  const module = Process.findModuleByAddress(address);
  if (module !== null)
    return `${module.name}+${address.sub(module.base)}`;
  return mono.describe(address) || address.toString();
}

// --- Calls --------------------------------------------------------------------

function push(monitor, record) {
  counts.set(monitor.id, (counts.get(monitor.id) || 0) + 1);
  if (events.length >= MAX_EVENTS) {
    dropped.set(monitor.id, (dropped.get(monitor.id) || 0) + 1);
    return;
  }
  events.push(record);
}

function onEnter(monitor, invocation) {
  const context = invocation.context;
  const { info, layout } = monitor;
  const record = {
    monitorId: monitor.id,
    time: Date.now(),
    threadId: invocation.threadId,
    self: layout.self === null ? null
      : safely(() => describeValue({ kind: 0x1c, byref: false }, rawValue(context, layout.self))),
    args: info.params.map((param, i) => [
      param.name,
      layout.args[i] === null ? '?'
        : safely(() => describeValue(param.type, rawValue(context, layout.args[i])))
    ]),
    caller: safely(() => frameText(invocation.returnAddress))
  };
  if (monitor.returns)
    invocation.record = record;
  else
    push(monitor, record);
}

function onLeave(monitor, invocation, retval) {
  const record = invocation.record;
  if (record === undefined)
    return;
  const type = monitor.info.returns;
  if (type.kind !== T.VOID || type.byref)
    record.result = safely(() => describeValue(
      type, isFloat(type) ? invocation.context.xmm0 : retval));
  push(monitor, record);
}

function reportError(monitor, error) {
  // Once per monitor: a failing call usually fails the same way every time.
  if (monitor.errorReported)
    return;
  monitor.errorReported = true;
  send({ kind: 'monitor-error', monitorId: monitor.id, error: String(error) });
}

function addMonitor(spec) {
  if (monitors.has(spec.id))
    throw new Error(`Monitor ${spec.id} exists already`);
  const info = mono.prepare(spec.identity);
  const monitor = {
    id: spec.id, info, layout: argumentLayout(info), returns: spec.returns, errorReported: false
  };
  const callbacks = {
    onEnter() {
      try {
        onEnter(monitor, this);
      } catch (error) {
        reportError(monitor, error);
      }
    }
  };
  // Without onLeave Frida leaves the return address alone.
  if (spec.returns)
    callbacks.onLeave = function (retval) {
      try {
        onLeave(monitor, this, retval);
      } catch (error) {
        reportError(monitor, error);
      }
    };
  monitor.listener = Interceptor.attach(info.start, callbacks);
  monitors.set(spec.id, monitor);
  updateUnmapWatch();
  return {
    id: spec.id,
    address: info.start.toString(),
    instance: info.instance,
    params: info.params.map(p => `${p.type.name} ${p.name}`),  // Mono names byref types 'T&'
    returns: info.returns.name,
    undecoded: monitor.layout.args.filter(location => location === null).length
  };
}

function removeMonitor(id) {
  const monitor = monitors.get(id);
  if (monitor === undefined)
    return false;
  monitor.listener.detach();
  monitors.delete(id);
  updateUnmapWatch();
  return true;
}

// --- Freed code ---------------------------------------------------------------

let unmapWatch = null;

function onUnmap(start, length) {
  const end = start.add(length);
  for (const monitor of Array.from(monitors.values())) {
    const code = monitor.info.start;
    if (code.compare(start) >= 0 && code.compare(end) < 0) {
      // Still mapped here: Frida can restore the code before it is gone.
      removeMonitor(monitor.id);
      send({ kind: 'monitor-ended', monitorId: monitor.id });
    }
  }
}

function updateUnmapWatch() {
  // Only while there is something to protect: munmap is called often.
  if (monitors.size > 0 && unmapWatch === null) {
    unmapWatch = Interceptor.attach(Module.getGlobalExportByName('munmap'), {
      onEnter(args) {
        try {
          onUnmap(args[0], args[1]);
        } catch (error) {
          send({ kind: 'agent-error', error: `munmap: ${error}` });
        }
      }
    });
  } else if (monitors.size === 0 && unmapWatch !== null) {
    unmapWatch.detach();
    unmapWatch = null;
  }
}

function flush() {
  if (events.length === 0 && dropped.size === 0)
    return;
  send({
    kind: 'calls',
    events: events.splice(0),
    dropped: Object.fromEntries(dropped),
    counts: Object.fromEntries(counts)
  });
  dropped.clear();
  counts.clear();
}

const flushTimer = setInterval(flush, 100);
send({ kind: 'ready', mono: mono.refresh() });

rpc.exports = {
  addMonitor,
  removeMonitor,
  dispose() {
    clearInterval(flushTimer);
    for (const id of Array.from(monitors.keys()))
      removeMonitor(id);
    Interceptor.flush();
    flush();
    return true;
  }
};
"""


def build_agent_source():
    return _AGENT_SOURCE


class MethodMonitorWorker(QThread):
    """Keep a Frida session open and log the calls of monitored C# methods.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    # monitor id, {address, instance, params, returns, undecoded}
    monitor_added = pyqtSignal(int, dict)
    # monitor id: the game freed the method's code (e.g. while exiting)
    monitor_ended = pyqtSignal(int)
    monitor_failed = pyqtSignal(int, str)
    # calls, dropped calls per monitor id, all calls per monitor id
    calls = pyqtSignal(list, dict, dict)
    worker_failed = pyqtSignal(str)

    _ids = itertools.count(1)

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._agent_error = None
        self._detach_reason = None

    def add_monitor(self, identity, returns=False):
        """Queue a method (``CatalogMethod.identity()``) and return its monitor id."""
        monitor_id = next(self._ids)
        self._commands.put(("add", monitor_id, {"identity": identity, "returns": returns}))
        return monitor_id

    def remove_monitor(self, monitor_id):
        self._commands.put(("remove", monitor_id, None))

    def run(self):
        session = None
        script = None
        try:
            if is_wine_process(self.pid):
                raise RuntimeError(WINE_FRIDA_ERROR)
            session = frida.attach(self.pid)
            session.on("detached", self._on_detached)
            script = session.create_script(build_agent_source())
            script.on("message", self._on_message)
            script.load()
            if not self._ready.wait(5):
                raise RuntimeError("Timed out while loading the method monitor")
            while (not self.isInterruptionRequested() and self._detach_reason is None
                   and self._agent_error is None):
                try:
                    action, monitor_id, argument = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._execute(script.exports_sync, action, monitor_id, argument)
            if self._agent_error and not self.isInterruptionRequested():
                raise RuntimeError(self._agent_error)
            if self._detach_reason and not self.isInterruptionRequested():
                raise RuntimeError(f"Target process detached: {self._detach_reason}")
        except FRIDA_ERRORS + (OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(str(error))
        finally:
            if script is not None:
                try:
                    script.exports_sync.dispose()
                except CLEANUP_ERRORS:
                    pass
                try:
                    script.unload()
                except CLEANUP_ERRORS:
                    pass
            if session is not None:
                try:
                    session.detach()
                except CLEANUP_ERRORS:
                    pass

    def _execute(self, api, action, monitor_id, argument):
        try:
            if action == "add":
                self.monitor_added.emit(
                    monitor_id, api.add_monitor({"id": monitor_id, **argument})
                )
            elif action == "remove":
                api.remove_monitor(monitor_id)
        except frida.RPCException as error:
            self.monitor_failed.emit(monitor_id, str(error))

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = message.get("stack", "Method monitor agent failed")
            self._ready.set()
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready.set()
        elif kind == "calls":
            self.calls.emit(
                payload.get("events", []),
                {int(key): value for key, value in payload.get("dropped", {}).items()},
                {int(key): value for key, value in payload.get("counts", {}).items()},
            )
        elif kind == "monitor-ended":
            self.monitor_ended.emit(payload.get("monitorId", 0))
        elif kind == "agent-error":
            print(f"[method monitor] {payload.get('error', '')}", file=sys.stderr)
        elif kind == "monitor-error":
            self.monitor_failed.emit(payload.get("monitorId", 0), payload.get("error", ""))

    def _on_detached(self, reason, _crash):
        self._detach_reason = reason
