"""Method monitor: log the calls of C# methods (e.g. Unity games with Mono).

It works through the engine adapter of the game (see hermes/engines).

Mono compiles the method (see ``engine.prepare``), and a Frida interceptor on
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
unloaded, and abort the game. The agent watches unmapping for that
(``watchUnmaps``: ``munmap``, or ntdll's calls under Wine) and reports
``monitor-ended``.

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
until the main thread runs a script method (see ``engine.onMainThread``). The inspector
shows the current fields; a short-lived object may be collected by then.
"""

import itertools
import queue
import threading

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.core import sessions
from hermes.engines.registry import ENGINE_AGENT_SOURCE
from hermes.core.frida_attach import attach_session
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS


_AGENT_SOURCE = ENGINE_AGENT_SOURCE + r"""
const T = engine.TYPE;
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
      name = engine.unityName(pointer);
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
  const klass = engine.classOf(pointer);
  if (klass === null)
    return { text: `${pointer} (no object)` };
  const layout = engine.layout(klass);
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
    klass = engine.classOf(address);
    if (klass === null)
      throw new Error(`no object at ${address} (any more)`);
  }
  const layout = engine.layout(klass);
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
  result.properties = engine.properties(klass).map(property => ({
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
  const klass = engine.classOf(exception);
  if (klass === null)
    return 'exception';
  const layout = engine.layout(klass);
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
  return engine.onMainThread(() => {
    const address = ptr(target.address);
    const klass = target.klass ? ptr(target.klass) : engine.classOf(address);
    if (klass === null)
      throw new Error(`no object at ${address} (any more)`);
    return engine.properties(klass).filter(property => names.includes(property.name))
      .map(property => {
        let value;
        try {
          const { result, exception } = engine.getProperty(address, property, !!target.klass);
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
  // knows (see engine.onRuntimeThread).
  return engine.onRuntimeThread(() => contents(target));
}

function frameText(address) {
  const module = Process.findModuleByAddress(address);
  if (module !== null)
    return `${module.name}+${address.sub(module.base)}`;
  return engine.describe(address) || address.toString();
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
  engine.drainMain();
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

async function addMonitor(spec) {
  // spec.returns/objects: true, false, or null for "if the method has any".
  if (monitors.has(spec.id))
    throw new Error(`Monitor ${spec.id} exists already`);
  const info = await engine.prepare(spec.identity);
  if (monitors.has(spec.id))
    throw new Error(`Monitor ${spec.id} exists already`);
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

function onUnmap(start, end) {
  const freed = code => code.compare(start) >= 0 && code.compare(end) < 0;
  for (const monitor of Array.from(monitors.values())) {
    if (freed(monitor.info.start)) {
      // Still mapped here: Frida can restore the code before it is gone.
      removeMonitor(monitor.id);
      send({ kind: 'monitor-ended', monitorId: monitor.id });
    }
  }
  for (const hook of Array.from(methodHooks.values())) {
    if (freed(hook.info.start)) {
      removeMethodHook(hook.id);
      send({ kind: 'hook-ended', hookId: hook.id });
    }
  }
}

function updateUnmapWatch() {
  // Only while there is something to protect: unmapping is frequent.
  const watched = monitors.size + methodHooks.size;
  if (watched > 0 && unmapWatch === null) {
    unmapWatch = watchUnmaps((start, end) => {
      try {
        onUnmap(start, end);
      } catch (error) {
        send({ kind: 'agent-error', error: `unmap: ${error}` });
      }
    });
  } else if (watched === 0 && unmapWatch !== null) {
    unmapWatch.detach();
    unmapWatch = null;
  }
}

// --- Method hooks ---------------------------------------------------------------
// A method hook runs a script when the hooked method returns; a value the
// script returns replaces the method's result (see method_hooks.py).

const methodHooks = new Map();
const hookStats = new Map();
const hookDebug = [];
const hookDebugDropped = new Map();
const MAX_HOOK_DEBUG = 200;

// Objects the scripts see: a managed object (or a struct inside one).
class ManagedObject {
  constructor(address, klass, isStruct = false) {
    this.address = address;
    this.klass = klass;
    this.isStruct = isStruct;
  }

  get className() {
    return engine.layout(this.klass).name;
  }

  get name() {
    // The Unity name of a GameObject or component, else null.
    return unityNameOf(this.address, true);
  }

  field(name) {
    const layout = engine.layout(this.klass);
    const field = layout.fields.find(candidate => candidate.name === name && !candidate.static);
    if (field === undefined)
      throw new Error(`${layout.name} has no field ${name}`);
    const base = this.isStruct ? this.address.sub(OBJECT_HEADER) : this.address;
    return readJs(field.type, base.add(field.offset));
  }

  setField(name, value) {
    // Change a field (in the "before the call" phase, the method sees it).
    const layout = engine.layout(this.klass);
    const field = layout.fields.find(candidate => candidate.name === name && !candidate.static);
    if (field === undefined)
      throw new Error(`${layout.name} has no field ${name}`);
    const base = this.isStruct ? this.address.sub(OBJECT_HEADER) : this.address;
    const address = base.add(field.offset);
    const type = field.type;
    if (type.kind === T.VALUETYPE)
      throw new Error(`the struct field ${name} cannot be set as a whole`);
    if (REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING)
      engine.writeReference(this.isStruct ? null : this.address, address, pointerOf(type, value));
    else
      writeJs(type, address, value);
    noteChange(`${shortClass(layout.name)}.${name}`, value);
  }

  get(name) {
    const property = engine.properties(this.klass).find(candidate => candidate.name === name);
    if (property === undefined)
      throw new Error(`${this.className} has no property ${name}`);
    const { result, exception } = engine.getProperty(this.address, property, this.isStruct);
    throwIfException(exception);
    return resultJs(property.type, result);
  }

  call(name, ...args) {
    let method = engine.findMethod(this.klass, name, args.length, false);
    if (!this.isStruct)
      method = engine.virtualMethod(this.address, method);
    return callMethod(method, this.address, args);
  }

  component(typeName) {
    // A component of this GameObject (or of the GameObject of this component).
    const gameObject = engine.classChain(this.klass).some(
      klass => engine.layout(klass).name === 'UnityEngine.GameObject') ? this : this.get('gameObject');
    if (gameObject === null)
      return null;
    const { result, exception } = engine.getProperty(
      gameObject.address, { special: 'components' }, false);
    throwIfException(exception);
    const length = result.add(ARRAY_LENGTH).readU32();
    for (let i = 0; i < length; i++) {
      const element = result.add(ARRAY_DATA + i * Process.pointerSize).readPointer();
      const klass = engine.classOf(element);
      if (klass === null)
        continue;
      const name = engine.layout(klass).name;
      if (name === typeName || name.endsWith('.' + typeName))
        return wrap(new ManagedObject(element, klass));
    }
    return null;
  }

  toString() {
    const name = unityNameOf(this.address, false);
    return `${this.className}${name === null ? '' : ` "${name}"`}@${this.address}`;
  }

  toJSON() {
    return this.toString();
  }
}

// Scripts may also write object.Health or object.IsPartyMember: names the
// wrapper does not have are looked up as a field, then as a property
// (getter). The wrapper's own members (name, className, field(), get() …)
// come first; .field("name") reads a game field of the same name.
// Assigning works the same way: object.Health = 5 sets the field, else the
// property's setter (set_Health).
const shortcuts = {
  get(target, key, receiver) {
    if (typeof key !== 'string' || key in target)
      return Reflect.get(target, key, receiver);
    const layout = engine.layout(target.klass);
    if (layout.fields.some(field => field.name === key && !field.static))
      return target.field(key);
    if (engine.properties(target.klass).some(property => property.name === key))
      return target.get(key);
    return undefined;
  },
  set(target, key, value, receiver) {
    if (typeof key !== 'string' || key in target)
      return Reflect.set(target, key, value, receiver);
    const layout = engine.layout(target.klass);
    if (layout.fields.some(field => field.name === key && !field.static)) {
      target.setField(key, value);
      return true;
    }
    let setter;
    try {
      setter = engine.findMethod(target.klass, `set_${key}`, 1, false);
    } catch (_error) {
      throw new Error(`${layout.name} has no field or property ${key} to set`);
    }
    callMethod(target.isStruct ? setter : engine.virtualMethod(target.address, setter),
               target.address, [value]);
    noteChange(`${shortClass(layout.name)}.${key}`, value);
    return true;
  }
};

function shortClass(name) {
  return name.substring(name.lastIndexOf('.') + 1);
}

// What a script running before a call changed, for the hook's statistics.
let changes = null;

function noteChange(what, value) {
  if (changes !== null)
    changes.push(`${what}=${displayText(value)}`);
}

function wrap(object) {
  return new Proxy(object, shortcuts);
}

function throwIfException(exception) {
  if (!exception.isNull())
    throw new Error(exceptionText(exception));
}

function objectJs(pointer) {
  if (pointer.isNull())
    return null;
  const klass = engine.classOf(pointer);
  if (klass === null)
    throw new Error(`no object at ${pointer}`);
  return wrap(new ManagedObject(pointer, klass));
}

function readJs(type, address) {
  // A value of ``type`` stored at ``address``, as a JavaScript value.
  switch (type.kind) {
    case T.BOOLEAN: return address.readU8() !== 0;
    case T.CHAR: return String.fromCharCode(address.readU16());
    case T.I1: return address.readS8();
    case T.U1: return address.readU8();
    case T.I2: return address.readS16();
    case T.U2: return address.readU16();
    case T.I4: return address.readS32();
    case T.U4: return address.readU32();
    case T.I8: return address.readS64().toNumber();
    case T.U8: return address.readU64().toNumber();
    case T.R4: return address.readFloat();
    case T.R8: return address.readDouble();
    case T.I:
    case T.U: return address.readPointer();
    case T.VALUETYPE: return wrap(new ManagedObject(address, type.klass, true));
    case T.STRING: {
      const pointer = address.readPointer();
      return pointer.isNull() ? null : stringJs(pointer);
    }
    default:
      if (REFERENCE_KINDS.has(type.kind))
        return objectJs(address.readPointer());
      return address.readPointer();
  }
}

function stringJs(pointer) {
  const length = pointer.add(OBJECT_HEADER).readS32();
  return pointer.add(OBJECT_HEADER + 4).readUtf16String(length);
}

function registerJs(type, raw) {
  // An argument as passed in a register or stack slot, as a JavaScript value.
  if (type.byref)
    return readJs({ ...type, byref: false }, raw);
  switch (type.kind) {
    case T.BOOLEAN: return (raw.toUInt32() & 0xff) !== 0;
    case T.CHAR: return String.fromCharCode(raw.toUInt32() & 0xffff);
    case T.I1: return Number(integer(raw, 8, true));
    case T.U1: return Number(integer(raw, 8, false));
    case T.I2: return Number(integer(raw, 16, true));
    case T.U2: return Number(integer(raw, 16, false));
    case T.I4: return Number(integer(raw, 32, true));
    case T.U4: return Number(integer(raw, 32, false));
    case T.I8: return Number(integer(raw, 64, true));
    case T.U8: return Number(integer(raw, 64, false));
    case T.R4:
    case T.R8: return floatValue(raw, type.kind);
    case T.STRING: return raw.isNull() ? null : stringJs(raw);
    case T.VALUETYPE: return undefined;  // passed in parts; not decoded
    default:
      if (REFERENCE_KINDS.has(type.kind))
        return objectJs(raw);
      return raw;
  }
}

function resultJs(type, result) {
  // The value mono_runtime_invoke returned (value types come boxed).
  if (type.kind === T.VOID && !type.byref)
    return undefined;
  if (result.isNull())
    return null;
  if (type.kind === T.STRING)
    return stringJs(result);
  if (REFERENCE_KINDS.has(type.kind) || type.byref)
    return objectJs(result);
  if (type.kind === T.VALUETYPE)
    return wrap(new ManagedObject(result.add(OBJECT_HEADER), type.klass, true));
  return readJs(type, result.add(OBJECT_HEADER));
}

function writeJs(type, address, value) {
  // Store a JavaScript value as ``type`` at ``address``.
  switch (type.kind) {
    case T.BOOLEAN: address.writeU8(value ? 1 : 0); return;
    case T.CHAR: address.writeU16(typeof value === 'string' ? value.charCodeAt(0) : value); return;
    case T.I1: address.writeS8(Math.trunc(value)); return;
    case T.U1: address.writeU8(Math.trunc(value)); return;
    case T.I2: address.writeS16(Math.trunc(value)); return;
    case T.U2: address.writeU16(Math.trunc(value)); return;
    case T.I4: address.writeS32(Math.trunc(value)); return;
    case T.U4: address.writeU32(Math.trunc(value)); return;
    case T.I8: address.writeS64(int64(String(Math.trunc(value)))); return;
    case T.U8: address.writeU64(uint64(String(Math.trunc(value)))); return;
    case T.R4: address.writeFloat(value); return;
    case T.R8: address.writeDouble(value); return;
    default: address.writePointer(pointerOf(type, value));
  }
}

function pointerOf(type, value) {
  // A reference (object, string) or pointer-sized value as a pointer.
  if (value === null || value === undefined)
    return NULL;
  if (value instanceof ManagedObject)
    return value.address;
  if (type.kind === T.STRING || typeof value === 'string')
    return engine.newString(String(value));
  if (value instanceof NativePointer)
    return value;
  return ptr(value);
}

function callMethod(method, self, args) {
  // Call game code with JavaScript arguments; returns its result.
  const signature = engine.signatureOf(method);
  const params = Memory.alloc(Process.pointerSize * Math.max(args.length, 1));
  const keep = [];
  signature.params.forEach((param, i) => {
    let slot;
    if (REFERENCE_KINDS.has(param.type.kind) || param.type.kind === T.STRING) {
      slot = pointerOf(param.type, args[i]);
      if (param.type.byref) {
        const cell = Memory.alloc(Process.pointerSize);
        cell.writePointer(slot);
        slot = cell;
      }
    } else if (param.type.kind === T.VALUETYPE) {
      if (!(args[i] instanceof ManagedObject))
        throw new Error(`argument ${param.name} must be a ${param.type.name}`);
      slot = args[i].address;  // a struct: a pointer to its data
    } else {
      slot = Memory.alloc(8);  // a value: a pointer to it
      writeJs(param.type, slot, args[i]);
    }
    keep.push(slot);
    params.add(i * Process.pointerSize).writePointer(slot);
  });
  const { result, exception } = engine.invoke(method, self === null ? NULL : self, params);
  throwIfException(exception);
  return resultJs(signature.returns, result);
}

function staticCall(qualifiedName, ...args) {
  // ctx.call("Namespace.Class.Method", ...)
  const dot = qualifiedName.lastIndexOf('.');
  if (dot < 0)
    throw new Error(`"${qualifiedName}" is no Class.Method`);
  const klass = engine.findClass(qualifiedName.substring(0, dot));
  const method = engine.findMethod(klass, qualifiedName.substring(dot + 1), args.length, true);
  return callMethod(method, null, args);
}

function displayText(value) {
  if (value === undefined)
    return '';
  if (typeof value === 'string')
    return JSON.stringify(value);
  return String(value);
}

function replaceResult(hook, invocation, retval, value) {
  const type = hook.info.returns;
  if (type.kind === T.VOID && !type.byref)
    throw new Error('the method returns nothing (void)');
  if (isFloat(type)) {
    if (typeof value !== 'number' || !Number.isFinite(value))
      throw new Error(`the result must be a number, not ${displayText(value)}`);
    const buffer = new ArrayBuffer(16);
    const view = new DataView(buffer);
    if (type.kind === T.R4)
      view.setFloat32(0, value, true);
    else
      view.setFloat64(0, value, true);
    invocation.context.xmm0 = buffer;
    return;
  }
  if (type.kind === T.VALUETYPE && !type.byref)
    throw new Error(`a struct result (${type.name}) cannot be replaced`);
  if (type.kind === T.BOOLEAN) {
    retval.replace(ptr(value ? 1 : 0));
    return;
  }
  if (REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING || type.byref) {
    retval.replace(pointerOf(type, value));
    return;
  }
  if (typeof value !== 'number' || !Number.isFinite(value))
    throw new Error(`the result must be a number, not ${displayText(value)}`);
  retval.replace(ptr(BigInt.asUintN(64, BigInt(Math.trunc(value))).toString()));
}

function writeArgument(context, location, type, value, raw) {
  // Pass ``value`` to the method instead of what the caller passed.
  if (type.byref) {
    const inner = { ...type, byref: false };
    if (REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING)
      raw.writePointer(pointerOf(inner, value));  // the caller's variable, not a field
    else if (type.kind === T.VALUETYPE)
      throw new Error(`a struct (${type.name}) cannot be replaced`);
    else
      writeJs(inner, raw, value);
    return;
  }
  if (location === null)
    throw new Error('arguments after a struct cannot be changed');
  if (type.kind === T.VALUETYPE)
    throw new Error(`a struct (${type.name}) cannot be replaced`);
  if (isFloat(type)) {
    if (typeof value !== 'number' || !Number.isFinite(value))
      throw new Error(`a number is needed, not ${displayText(value)}`);
    if (location.xmm !== undefined) {
      const buffer = new ArrayBuffer(16);
      const view = new DataView(buffer);
      if (type.kind === T.R4)
        view.setFloat32(0, value, true);
      else
        view.setFloat64(0, value, true);
      context['xmm' + location.xmm] = buffer;
    } else {
      writeJs(type, context.sp.add(8 * (location.stack + 1)), value);
    }
    return;
  }
  let pointer;
  if (REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING)
    pointer = pointerOf(type, value);
  else if (type.kind === T.BOOLEAN)
    pointer = ptr(value ? 1 : 0);
  else if (typeof value === 'number' && Number.isFinite(value))
    pointer = ptr(BigInt.asUintN(64, BigInt(Math.trunc(value))).toString());
  else if (type.kind === T.CHAR && typeof value === 'string')
    pointer = ptr(value.charCodeAt(0));
  else
    throw new Error(`a number is needed, not ${displayText(value)}`);
  if (location.reg !== undefined)
    context[location.reg] = pointer;
  else
    context.sp.add(8 * (location.stack + 1)).writePointer(pointer);
}

function hookBefore(hook, invocation) {
  // The "before the call" phase: the script may change arguments and fields.
  engine.drainMain();
  if (!hook.enabled)
    return;
  const context = invocation.context;
  const { info, layout } = hook;
  const args = {};
  const passed = [];
  info.params.forEach((param, i) => {
    if (layout.args[i] === null)
      return;
    const raw = rawValue(context, layout.args[i]);
    args[param.name] = registerJs(param.type, raw);
    passed.push([param, layout.args[i], raw, args[param.name]]);
  });
  const logs = hook.debug ? [] : null;
  const ctx = {
    phase: 'before',
    args,
    refs: {},
    this: layout.self === null ? null : objectJs(rawValue(context, layout.self)),
    result: undefined,
    params: hook.transform.params,
    state: hook.transform.state,
    method: hook.signature,
    hookId: hook.id,
    threadId: invocation.threadId,
    call: staticCall,
    log: logs === null ? () => {} : (...items) => { logs.push(formatLog(items)); }
  };
  let error = null;
  changes = [];
  try {
    hook.transform.fn(ctx);
    for (const [param, location, raw, original] of passed) {
      const value = ctx.args[param.name];
      if (value !== original) {
        writeArgument(context, location, param.type, value, raw);
        noteChange(param.name, value);
      }
    }
    hook.errorReported = false;
  } catch (thrown) {
    error = thrown;
  }
  const changed = changes;
  changes = null;
  const text = changed.join(', ');
  const stats = hookStats.get(hook.id) || { count: 0 };
  stats.count++;
  stats.last = { old: '', result: text || '—', kept: changed.length === 0,
                 error: error === null ? null : String(error) };
  hookStats.set(hook.id, stats);
  if (error !== null && !hook.errorReported) {
    hook.errorReported = true;
    send({ kind: 'transform-error', hookId: hook.id, error: String(error) });
  }
  if (hook.debug)
    pushHookDebug(hook, {
      threadId: invocation.threadId,
      address: '',
      caller: safely(() => ({ text: frameText(invocation.returnAddress) })).text,
      old: '',
      result: text,
      kept: changed.length === 0,
      logs,
      error: error === null ? undefined
        : (error && error.stack ? `${error}\n${error.stack}` : String(error))
    });
}

function hookEnter(hook, invocation) {
  engine.drainMain();
  if (!hook.enabled)
    return;
  const context = invocation.context;
  const { info, layout } = hook;
  const args = {};
  const refs = [];
  info.params.forEach((param, i) => {
    if (layout.args[i] === null)
      return;
    const raw = rawValue(context, layout.args[i]);
    args[param.name] = registerJs(param.type, raw);
    if (param.type.byref)
      refs.push([param.name, param.type, raw]);
  });
  invocation.hookCall = {
    args,
    refs,
    self: layout.self === null ? null : objectJs(rawValue(context, layout.self)),
    caller: invocation.returnAddress
  };
}

function hookLeave(hook, invocation, retval) {
  const call = invocation.hookCall;
  if (call === undefined)
    return;
  const type = hook.info.returns;
  const isVoid = type.kind === T.VOID && !type.byref;
  const result = isVoid ? undefined
    : isFloat(type) ? floatValue(invocation.context.xmm0, type.kind) : registerJs(type, retval);
  const refs = {};
  for (const [name, refType, pointer] of call.refs)
    refs[name] = readJs({ ...refType, byref: false }, pointer);
  const logs = hook.debug ? [] : null;
  const ctx = {
    phase: 'after',
    args: call.args,
    refs,
    this: call.self,
    result,
    params: hook.transform.params,
    state: hook.transform.state,
    method: hook.signature,
    hookId: hook.id,
    threadId: invocation.threadId,
    call: staticCall,
    log: logs === null ? () => {} : (...items) => { logs.push(formatLog(items)); }
  };
  let returned;
  let error = null;
  try {
    returned = hook.transform.fn(ctx);
    if (returned !== undefined && returned !== null)
      replaceResult(hook, invocation, retval, returned);
    hook.errorReported = false;
  } catch (thrown) {
    error = thrown;
  }
  const kept = error !== null || returned === undefined || returned === null;
  const stats = hookStats.get(hook.id) || { count: 0 };
  stats.count++;
  stats.last = { old: displayText(result), result: displayText(kept ? result : returned),
                 kept, error: error === null ? null : String(error) };
  hookStats.set(hook.id, stats);
  if (error !== null && !hook.errorReported) {
    hook.errorReported = true;
    send({ kind: 'transform-error', hookId: hook.id, error: String(error) });
  }
  if (hook.debug)
    pushHookDebug(hook, {
      threadId: invocation.threadId,
      address: '',
      caller: safely(() => ({ text: frameText(call.caller) })).text,
      old: displayText(result),
      result: displayText(kept ? result : returned),
      kept,
      logs,
      error: error === null ? undefined
        : (error && error.stack ? `${error}\n${error.stack}` : String(error))
    });
}

function formatLog(items) {
  return items.map(item => {
    if (typeof item === 'string')
      return item;
    if (item === undefined)
      return 'undefined';
    try {
      return JSON.stringify(item);
    } catch (_error) {
      return String(item);
    }
  }).join(' ');
}

function pushHookDebug(hook, entry) {
  if (hookDebug.length >= MAX_HOOK_DEBUG) {
    hookDebugDropped.set(hook.id, (hookDebugDropped.get(hook.id) || 0) + 1);
    return;
  }
  entry.hookId = hook.id;
  entry.time = Date.now();
  hookDebug.push(entry);
}

function compileHookTransform(spec) {
  const fn = (0, eval)('(' + spec.source + '\n)');
  if (typeof fn !== 'function')
    throw new Error(`Transform '${spec.kind}' does not evaluate to a function`);
  return { kind: spec.kind, fn, params: spec.params || {}, state: {} };
}

async function addMethodHook(spec) {
  if (methodHooks.has(spec.id))
    throw new Error(`Method hook ${spec.id} exists already`);
  const transform = compileHookTransform(spec.transform);
  const info = await engine.prepare(spec.identity);
  if (methodHooks.has(spec.id))
    throw new Error(`Method hook ${spec.id} exists already`);
  const hook = {
    id: spec.id, info, layout: argumentLayout(info), transform, enabled: spec.enabled,
    debug: false, errorReported: false, signature: spec.identity.method,
    phase: spec.phase || 'after'
  };
  attachHook(hook);
  methodHooks.set(spec.id, hook);
  updateUnmapWatch();
  return {
    id: spec.id,
    address: info.start.toString(),
    params: info.params.map(p => `${p.type.name} ${p.name}`),
    returns: info.returns.name,
    undecoded: hook.layout.args.filter(location => location === null).length
  };
}

function reportHookError(hook, error) {
  if (!hook.errorReported) {
    hook.errorReported = true;
    send({ kind: 'transform-error', hookId: hook.id, error: String(error) });
  }
}

function attachHook(hook) {
  // Before the call only onEnter: Frida then leaves the return address alone.
  if (hook.phase === 'before') {
    hook.listener = Interceptor.attach(hook.info.start, {
      onEnter() {
        try {
          hookBefore(hook, this);
        } catch (error) {
          changes = null;
          reportHookError(hook, error);
        }
      }
    });
    return;
  }
  hook.listener = Interceptor.attach(hook.info.start, {
    onEnter() {
      try {
        hookEnter(hook, this);
      } catch (error) {
        reportHookError(hook, error);
      }
    },
    onLeave(retval) {
      try {
        hookLeave(hook, this, retval);
      } catch (error) {
        reportHookError(hook, error);
      }
    }
  });
}

function removeMethodHook(id) {
  const hook = methodHooks.get(id);
  if (hook === undefined)
    return false;
  hook.listener.detach();
  methodHooks.delete(id);
  updateUnmapWatch();
  return true;
}

function requireMethodHook(id) {
  const hook = methodHooks.get(id);
  if (hook === undefined)
    throw new Error(`No method hook ${id}`);
  return hook;
}

function flushHooks() {
  if (hookStats.size > 0) {
    send({ kind: 'hook-events', events: Array.from(hookStats, ([hookId, stats]) => ({
      hookId, count: stats.count, last: stats.last
    })) });
    hookStats.clear();
  }
  if (hookDebug.length > 0 || hookDebugDropped.size > 0) {
    send({ kind: 'hook-debug', events: hookDebug.splice(0),
           dropped: Object.fromEntries(hookDebugDropped) });
    hookDebugDropped.clear();
  }
}

function flush() {
  flushHooks();
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
send({ kind: 'ready', engine: engine.refresh() ? engine.id : null });

rpc.exports = {
  addMonitor,
  removeMonitor,
  inspect,
  evaluate,
  addMethodHook,
  removeMethodHook,
  setMethodHookEnabled(id, enabled) {
    requireMethodHook(id).enabled = enabled;
    return true;
  },
  setMethodHookTransform(id, spec) {
    const hook = requireMethodHook(id);
    hook.transform = compileHookTransform(spec);
    hook.errorReported = false;
    return true;
  },
  setMethodHookPhase(id, phase) {
    const hook = requireMethodHook(id);
    if (hook.phase !== phase) {
      hook.listener.detach();
      hook.phase = phase;
      attachHook(hook);
    }
    return true;
  },
  setMethodHookDebug(id, enabled) {
    requireMethodHook(id).debug = enabled;
    return true;
  },
  dispose() {
    clearInterval(flushTimer);
    for (const id of Array.from(monitors.keys()))
      removeMonitor(id);
    for (const id of Array.from(methodHooks.keys()))
      removeMethodHook(id);
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

    @sessions.tracked(sessions.MONITOR)
    def run(self):
        session = None
        script = None
        try:
            session = attach_session(self.pid)
            sessions.SESSIONS.set_frida_session(self, session)
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
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)
                try:
                    script.unload()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)
            if session is not None:
                try:
                    session.detach()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)

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
            logs.log.error("method monitor agent: %s", payload.get("error", ""))
        elif kind == "monitor-error":
            self.monitor_failed.emit(payload.get("monitorId", 0), payload.get("error", ""))

    def _on_detached(self, reason, _crash):
        self._detach_reason = reason
