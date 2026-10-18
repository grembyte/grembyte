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
optional. With it, ``ref`` and ``out`` arguments are read again after the
call (``after``): an ``out`` argument only holds something then.

Arguments, ``this`` and results that are GameObjects or components show
their Unity name (``Health "Young Wolf"@0x…``): the monitor asks Unity for it
while the game thread is in the call. Names seen that way are also shown for
the same objects in snapshots and the inspector.

Values are ``{text, target?, snapshot?}``. ``target`` is an object, array or
struct the object inspector can open (``inspect``), ``snapshot`` its
contents at the time of the call (like ``inspect`` returns them, or
``{error}``) when the monitor records ``objects``.

Properties (parameterless getters, and a GameObject's components) are game
code that Unity only allows on its main thread. ``evaluate`` queues the call
until the main thread runs a script method (see ``mono.onMainThread``). The inspector
shows the current fields; a short-lived object may be collected by then.
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
// A value is { text, target?, snapshot? }: ``target`` is what the object
// inspector opens ({ address } of an object or array, { address, klass } of
// a struct inside one), ``snapshot`` its contents at the time of the call.

const MAX_ELEMENTS = 200;
const OBJECT_HEADER = 2 * Process.pointerSize;
const ARRAY_LENGTH = 3 * Process.pointerSize;
const ARRAY_DATA = 4 * Process.pointerSize;

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
  // MonoString: object header, length (int32), UTF-16 characters.
  const length = pointer.add(OBJECT_HEADER).readS32();
  const text = pointer.add(OBJECT_HEADER + 4).readUtf16String(Math.min(length, MAX_STRING));
  return JSON.stringify(text) + (length > MAX_STRING ? '…' : '');
}

function withEnum(type, text) {
  return type.enumName ? `${text} (${type.enumName})` : text;
}

// Names of Unity objects seen in calls, for values read elsewhere (fields,
// the inspector), where Unity cannot be asked.
const MAX_NAMES = 4096;
const unityNames = new Map();

function unityNameOf(pointer, ask) {
  const key = pointer.toString();
  if (ask) {
    let name = null;
    try {
      name = mono.unityName(pointer);
    } catch (_error) {
    }
    if (name !== null) {
      if (unityNames.size >= MAX_NAMES)
        unityNames.clear();
      unityNames.set(key, name);
      return name;
    }
  }
  return unityNames.get(key) || null;
}

function objectValue(pointer, ask = false) {
  // ``ask``: on the game thread of a call, Unity may be asked for names.
  if (pointer.isNull())
    return { text: 'null' };
  const klass = mono.classOf(pointer);
  if (klass === null)
    return { text: `${pointer} (no object)` };
  const layout = mono.layout(klass);
  const name = unityNameOf(pointer, ask);
  if (name !== null)
    return { text: `${layout.name} ${JSON.stringify(name)}@${pointer}`,
             target: { address: pointer.toString() } };
  if (layout.rank > 0) {
    // int[] of length 3: "System.Int32[3]"
    const length = pointer.add(ARRAY_LENGTH).readU32();
    const name = layout.name.endsWith('[]') ? layout.name.slice(0, -2) : layout.name;
    return { text: `${name}[${length}]@${pointer}`, target: { address: pointer.toString() } };
  }
  return { text: `${layout.name}@${pointer}`, target: { address: pointer.toString() } };
}

function readValue(type, address, ask = false) {
  // The value of ``type`` stored at ``address`` (a field, element or ref).
  switch (type.kind) {
    case T.BOOLEAN: return { text: String(address.readU8() !== 0) };
    case T.CHAR: return { text: JSON.stringify(String.fromCharCode(address.readU16())) };
    case T.I1: return { text: withEnum(type, String(address.readS8())) };
    case T.U1: return { text: withEnum(type, String(address.readU8())) };
    case T.I2: return { text: withEnum(type, String(address.readS16())) };
    case T.U2: return { text: withEnum(type, String(address.readU16())) };
    case T.I4: return { text: withEnum(type, String(address.readS32())) };
    case T.U4: return { text: withEnum(type, String(address.readU32())) };
    case T.I8: return { text: withEnum(type, address.readS64().toString()) };
    case T.U8: return { text: withEnum(type, address.readU64().toString()) };
    case T.R4: return { text: String(address.readFloat()) };
    case T.R8: return { text: String(address.readDouble()) };
    case T.I:
    case T.U: return { text: address.readPointer().toString() };
    case T.VALUETYPE:
      return { text: `struct ${type.name}`,
               target: { address: address.toString(), klass: type.klass.toString() } };
    case T.STRING: {
      const pointer = address.readPointer();
      return { text: pointer.isNull() ? 'null' : managedString(pointer) };
    }
    default:
      if (REFERENCE_KINDS.has(type.kind))
        return objectValue(address.readPointer(), ask);
      return { text: `? (${type.name})` };
  }
}

function registerValue(type, raw, ask = false) {
  // An argument as passed in a register or stack slot.
  if (type.byref) {
    const inner = readValue({ ...type, byref: false }, raw, ask);
    return { ...inner, text: `&${raw} → ${inner.text}`, pointer: raw };
  }
  switch (type.kind) {
    case T.BOOLEAN: return { text: String((raw.toUInt32() & 0xff) !== 0) };
    case T.CHAR: return { text: JSON.stringify(String.fromCharCode(raw.toUInt32() & 0xffff)) };
    case T.I1: return { text: withEnum(type, integer(raw, 8, true)) };
    case T.U1: return { text: withEnum(type, integer(raw, 8, false)) };
    case T.I2: return { text: withEnum(type, integer(raw, 16, true)) };
    case T.U2: return { text: withEnum(type, integer(raw, 16, false)) };
    case T.I4: return { text: withEnum(type, integer(raw, 32, true)) };
    case T.U4: return { text: withEnum(type, integer(raw, 32, false)) };
    case T.I8: return { text: withEnum(type, integer(raw, 64, true)) };
    case T.U8: return { text: withEnum(type, integer(raw, 64, false)) };
    case T.R4:
    case T.R8: return { text: String(floatValue(raw, type.kind)) };
    case T.VALUETYPE: return { text: `struct ${type.name}` };
    case T.STRING: return { text: raw.isNull() ? 'null' : managedString(raw) };
    default:
      if (REFERENCE_KINDS.has(type.kind))
        return objectValue(raw, ask);
      return { text: raw.toString() };
  }
}

function safely(read) {
  try {
    return read();
  } catch (error) {
    return { text: `? (${error.message})` };
  }
}

// --- Objects --------------------------------------------------------------------

function contents(target) {
  // { name, address, fields: [{ name, type, offset, static, value }], length? }
  // of an object, array or struct; throws if it is no object any more.
  const address = ptr(target.address);
  let klass;
  let base = address;
  if (target.klass) {
    klass = ptr(target.klass);
    base = address.sub(OBJECT_HEADER);  // struct field offsets count a header
  } else {
    klass = mono.classOf(address);
    if (klass === null)
      throw new Error(`no object at ${address} (any more)`);
  }
  const layout = mono.layout(klass);
  const result = { name: layout.name, address: address.toString(), fields: [] };
  if (layout.rank > 0) {
    const length = address.add(ARRAY_LENGTH).readU32();
    result.length = length;
    const data = address.add(ARRAY_DATA);
    for (let i = 0; i < Math.min(length, MAX_ELEMENTS); i++)
      result.fields.push({
        name: `[${i}]`, type: layout.element.name, offset: ARRAY_DATA + i * layout.elementSize,
        static: false,
        value: safely(() => readValue(layout.element, data.add(i * layout.elementSize)))
      });
    return result;
  }
  result.properties = mono.properties(klass).map(property => ({
    name: property.name, type: property.type.name, static: property.static
  }));
  for (const field of layout.fields)
    result.fields.push({
      name: field.name, type: field.type.name, offset: field.offset, static: field.static,
      value: field.static ? { text: '' } : safely(() => readValue(field.type, base.add(field.offset)))
    });
  return result;
}

function snapshot(value) {
  // The contents of an object value at the time of the call (see contents).
  if (!value.target)
    return value;
  try {
    return { ...value, snapshot: contents(value.target) };
  } catch (error) {
    return { ...value, snapshot: { error: error.message } };
  }
}

function resultValue(type, result) {
  // The value a getter returned: value types come boxed.
  if (result.isNull())
    return { text: 'null' };
  if (type.kind === T.STRING)
    return { text: managedString(result) };
  if (REFERENCE_KINDS.has(type.kind) || type.byref)
    return objectValue(result, true);
  return readValue(type, result.add(OBJECT_HEADER), true);
}

function exceptionText(exception) {
  // "System.NullReferenceException: Object reference not set …"
  const klass = mono.classOf(exception);
  if (klass === null)
    return 'exception';
  const layout = mono.layout(klass);
  const message = layout.fields.find(field => field.name === '_message' || field.name === 'message');
  let text = '';
  if (message !== undefined) {
    const pointer = exception.add(message.offset).readPointer();
    text = pointer.isNull() ? '' : `: ${managedString(pointer)}`;
  }
  return `${layout.name}${text}`;
}

function evaluate(target, names) {
  // The values of the named properties of an object, read on Unity's main
  // thread: [{ name, value }], value.error set if the getter threw.
  return mono.onMainThread(() => {
    const address = ptr(target.address);
    const klass = target.klass ? ptr(target.klass) : mono.classOf(address);
    if (klass === null)
      throw new Error(`no object at ${address} (any more)`);
    return mono.properties(klass).filter(property => names.includes(property.name))
      .map(property => {
        let value;
        try {
          const { result, exception } = mono.getProperty(address, property, !!target.klass);
          value = exception.isNull() ? resultValue(property.type, result)
            : { text: exceptionText(exception), error: true };
        } catch (error) {
          value = { text: `? (${error.message})`, error: true };
        }
        return { name: property.name, value };
      });
  });
}

function inspect(target) {
  // Called on the agent's thread; class layouts are read by a thread Mono
  // knows (see mono.onMonoThread).
  return mono.onMonoThread(() => contents(target));
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

function argumentValue(monitor, type, raw) {
  const value = safely(() => registerValue(type, raw, true));
  return monitor.objects ? snapshot(value) : value;
}

function onEnter(monitor, invocation) {
  // Already on a game thread: run waiting property reads if it is the main one.
  mono.drainMain();
  const context = invocation.context;
  const { info, layout } = monitor;
  const pointers = [];
  const record = {
    monitorId: monitor.id,
    time: Date.now(),
    threadId: invocation.threadId,
    self: layout.self === null ? null
      : argumentValue(monitor, { kind: 0x1c, byref: false }, rawValue(context, layout.self)),
    args: info.params.map((param, i) => {
      if (layout.args[i] === null)
        return { name: param.name, text: '?' };
      const value = argumentValue(monitor, param.type, rawValue(context, layout.args[i]));
      if (value.pointer !== undefined) {
        pointers.push([i, value.pointer]);
        delete value.pointer;
      }
      return { name: param.name, ...value };
    }),
    caller: safely(() => ({ text: frameText(invocation.returnAddress) })).text
  };
  if (monitor.returns) {
    invocation.record = record;
    invocation.pointers = pointers;
  } else {
    push(monitor, record);
  }
}

function onLeave(monitor, invocation, retval) {
  const record = invocation.record;
  if (record === undefined)
    return;
  const type = monitor.info.returns;
  if (type.kind !== T.VOID || type.byref) {
    const value = safely(() => registerValue(
      type, isFloat(type) ? invocation.context.xmm0 : retval, true));
    delete value.pointer;
    record.result = monitor.objects ? snapshot(value) : value;
  }
  // ref and out arguments: what the method left in them
  for (const [i, pointer] of invocation.pointers) {
    const type = monitor.info.params[i].type;
    const value = safely(() => readValue({ ...type, byref: false }, pointer, true));
    record.args[i].after = monitor.objects ? snapshot(value) : value;
  }
  push(monitor, record);
}

function reportError(monitor, error) {
  // Once per monitor: a failing call usually fails the same way every time.
  if (monitor.errorReported)
    return;
  monitor.errorReported = true;
  send({ kind: 'monitor-error', monitorId: monitor.id, error: String(error) });
}

function isObject(type) {
  return REFERENCE_KINDS.has(type.kind);  // also a ref/out argument holding one
}

function addMonitor(spec) {
  // spec.returns/objects: true, false, or null for "if the method has any".
  if (monitors.has(spec.id))
    throw new Error(`Monitor ${spec.id} exists already`);
  const info = mono.prepare(spec.identity);
  const hasResults = (info.returns.kind !== T.VOID || info.returns.byref)
    || info.params.some(param => param.type.byref);
  const hasObjects = info.instance || isObject(info.returns)
    || info.params.some(param => isObject(param.type));
  const monitor = {
    id: spec.id, info, layout: argumentLayout(info),
    returns: spec.returns === null ? hasResults : spec.returns,
    objects: spec.objects === null ? hasObjects : spec.objects,
    errorReported: false
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
  if (monitor.returns)
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
    undecoded: monitor.layout.args.filter(location => location === null).length,
    // what the method has to show, and the options in effect
    hasResults,
    hasObjects,
    recordsResults: monitor.returns,
    recordsObjects: monitor.objects
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
  inspect,
  evaluate,
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
    # request id, {name, address, fields: [{name, type, offset, static, value}],
    # properties: [{name, type, static}], length?}
    inspected = pyqtSignal(int, dict)
    inspect_failed = pyqtSignal(int, str)
    # request id, [{name, value}] of evaluated properties
    evaluated = pyqtSignal(int, list)
    evaluate_failed = pyqtSignal(int, str)
    monitor_failed = pyqtSignal(int, str)
    # calls, dropped calls per monitor id, all calls per monitor id
    calls = pyqtSignal(list, dict, dict)
    worker_failed = pyqtSignal(str)

    _ids = itertools.count(1)
    _requests = itertools.count(1)

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._agent_error = None
        self._detach_reason = None

    def add_monitor(self, identity, returns=False, objects=False):
        """Queue a method (``CatalogMethod.identity()``) and return its monitor id.

        With ``returns`` the return value and what ``ref``/``out`` arguments
        hold afterwards are logged too; with ``objects`` the fields of object
        arguments (``snapshot``). ``None`` enables an option if the method
        has a result or objects; monitor_added tells (``recordsResults``,
        ``recordsObjects``, ``hasResults``, ``hasObjects``).
        """
        monitor_id = next(self._ids)
        self._commands.put(("add", monitor_id,
                            {"identity": identity, "returns": returns, "objects": objects}))
        return monitor_id

    def inspect(self, target):
        """Read an object (a value's ``target``); expect inspected or inspect_failed."""
        request_id = next(self._requests)
        self._commands.put(("inspect", request_id, target))
        return request_id

    def evaluate(self, target, names):
        """Call the named getters of an object on the game's main thread.

        Expect evaluated or evaluate_failed. Getters are game code: they may
        have side effects.
        """
        request_id = next(self._requests)
        self._commands.put(("evaluate", request_id, (target, list(names))))
        return request_id

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
            elif action == "inspect":
                try:
                    self.inspected.emit(monitor_id, api.inspect(argument))
                except frida.RPCException as error:
                    self.inspect_failed.emit(monitor_id, str(error))
            elif action == "evaluate":
                try:
                    self.evaluated.emit(monitor_id, api.evaluate(*argument))
                except frida.RPCException as error:
                    self.evaluate_failed.emit(monitor_id, str(error))
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
