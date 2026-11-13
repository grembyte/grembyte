const T = engine.TYPE;
// The calling convention of Mono's JIT code: 'sysv', 'win64' or 'x86'.
const ABI = Process.arch === 'ia32' ? 'x86' : (Process.platform === 'windows' ? 'win64' : 'sysv');
const SYSV_INT_REGS = ['rdi', 'rsi', 'rdx', 'rcx', 'r8', 'r9'];
const SYSV_FLOAT_REGS = 8;
const SYSV_MAX_STRUCT_IN_REGS = 16;
const WIN64_REGS = ['rcx', 'rdx', 'r8', 'r9'];
const WIN64_SHADOW = 32;
const MAX_EVENTS = 200;
const MAX_STRING = 120;
// Longer strings are kept whole for a while (up to MAX_FULL_STRING
// characters each, FULL_STRING_BUDGET in all, the oldest go first), to be
// fetched with fullText(id): e.g. a save game's JSON.
const MAX_FULL_STRING = 8 * 1024 * 1024;
const FULL_STRING_BUDGET = 32 * 1024 * 1024;
const fullTexts = new Map();  // id -> { text, key }, the oldest first
// The id a string already kept has: the live inspector reads the same
// strings again and again. Strings are immutable; address, length and start
// tell one from another.
const fullTextIds = new Map();
let fullTextChars = 0;
let nextFullText = 1;
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

function is64(type) {
  return !type.byref && (type.kind === T.I8 || type.kind === T.U8 || type.kind === T.R8);
}

function hasHiddenReturn(type) {
  if (!isStruct(type))
    return false;
  if (ABI === 'x86')
    return true;
  if (ABI === 'win64')
    return ![1, 2, 4, 8].includes(type.structSize);
  return type.structSize > SYSV_MAX_STRUCT_IN_REGS;
}

function sysvSlots() {
  // Integer and float registers fill up separately, then 8-byte stack slots.
  let ints = 0;
  let floats = 0;
  let stack = 0;
  const onStack = () => ({ stack: 8 * (1 + stack++) });
  return type => {
    if (isStruct(type))
      return null;  // structs may take registers, stack or both
    if (isFloat(type))
      return floats < SYSV_FLOAT_REGS ? { xmm: floats++ } : onStack();
    return ints < SYSV_INT_REGS.length ? { reg: SYSV_INT_REGS[ints++] } : onStack();
  };
}

function win64Slots() {
  // One slot per argument, by position, register or stack alike.
  let slot = 0;
  return type => {
    const index = slot++;
    if (isStruct(type))
      return { struct: true };  // in its slot by value or by reference
    if (index >= WIN64_REGS.length)
      return { stack: 8 + WIN64_SHADOW + 8 * (index - WIN64_REGS.length) };
    return isFloat(type) ? { xmm: index } : { reg: WIN64_REGS[index] };
  };
}

function x86Slots() {
  // Everything on the stack above the return address, in 4-byte steps.
  let offset = 4;
  return type => {
    const location = { stack: offset, size: is64(type) ? 8 : 4 };
    if (isStruct(type)) {
      if (!type.structSize)
        return null;
      offset += Math.ceil(type.structSize / 4) * 4;
      return { struct: true };
    }
    offset += location.size;
    return location;
  };
}

function argumentLayout(info) {
  // { self, args: [location or null] }; a location is { reg }, { xmm } or
  // { stack, size? } (the byte offset from the stack pointer at entry).
  // Null: not known where (after a struct whose layout is unclear) or a
  // struct, which is not decoded.
  const next = ABI === 'x86' ? x86Slots() : ABI === 'win64' ? win64Slots() : sysvSlots();
  const POINTER = { kind: 0x1c, byref: false };
  const hiddenReturn = hasHiddenReturn(info.returns);
  const firstIsReference = info.params.length > 0 && isReference(info.params[0].type);
  const layout = { self: null, args: [] };
  if (hiddenReturn && info.returnPointerFirst) {
    // IL2CPP: a C function whose hidden result pointer comes before ``this``
    next(POINTER);
    if (info.instance)
      layout.self = next(POINTER);
  } else if (info.instance) {
    layout.self = next(POINTER);
    if (hiddenReturn)
      next(POINTER);
  } else if (hiddenReturn && firstIsReference) {
    layout.args.push(next(POINTER));
    next(POINTER);
  } else if (hiddenReturn) {
    next(POINTER);
  }
  let known = true;
  for (const param of info.params.slice(layout.args.length)) {
    const location = known ? next(param.type) : null;
    if (location === null)
      known = false;
    layout.args.push(location === null || location.struct ? null : location);
  }
  return layout;
}

function rawValue(context, location) {
  if (location.reg !== undefined)
    return context[location.reg];
  if (location.xmm !== undefined)
    return context['xmm' + location.xmm];
  const slot = context.sp.add(location.stack);
  return location.size === 8 && Process.pointerSize === 4 ? slot.readU64() : slot.readPointer();
}

function resultRaw(context, retval, type) {
  // The raw result as registerValue takes it.
  if (ABI === 'x86' && isFloat(type))
    throw grembyteError('error.monitor.x87_float');
  if (ABI === 'x86' && is64(type))
    return uint64(((BigInt(context.edx.toUInt32()) << 32n)
                   | BigInt(retval.toUInt32())).toString());
  return isFloat(type) ? context.xmm0 : retval;
}

// --- Hooking calls ----------------------------------------------------------
// Frida's onLeave, and in Frida 17 already an onEnter in a callbacks object,
// puts a return address of Frida's own on the stack. An exception thrown
// through such a call ends there: Mono's unwinder finds no caller behind it
// and takes the exception for unhandled, and Unity ends the game. Probes (a
// function as callback) leave the stack alone, so calls are hooked with a
// probe at the start of the code and one before each of its returns.

const RESULT_REG = Process.arch === 'ia32' ? 'eax' : 'rax';
// The jmp Frida writes over the probed instructions (rel32; more bytes are
// refused rather than written past the ret, see returnProbes).
const MIN_PATCH = 5;
// The start's probe covers at least this much; a return probe stays clear of it.
const START_PATCH = 16;

function isControl(instruction) {
  return instruction.groups.some(group => ['jump', 'call', 'ret', 'int', 'iret'].includes(group));
}

// Registers a result comes back in (rax:rdx, xmm0:xmm1 and their parts).
const RESULT_REGISTER = /^([re]?[ad]x|[ad][lh]|[xyz]mm[01])$/;

function leavesResult(instruction) {
  // Whether the instruction keeps what the method returns and leaves memory
  // alone: a return probe runs before the instructions it covers, so these
  // may only restore registers and the stack (pop, add rsp, leave, ...).
  if (instruction.mnemonic === 'vzeroupper')
    return true;  // clears only the upper halves, xmm0 stays
  let written;
  try {
    written = instruction.regsAccessed.written;
  } catch (_error) {
    return false;
  }
  return !written.some(register => RESULT_REGISTER.test(register))
    && !instruction.operands.some(operand => operand.type === 'mem' && operand.access.includes('w'));
}

function branchTarget(instruction) {
  const operand = instruction.operands[0];
  return operand !== undefined && operand.type === 'imm' ? ptr(operand.value.toString()) : null;
}

function readCode(start, end) {
  // The instructions from ``start`` up to ``end`` or code that cannot be
  // decoded (data after it): a Map from address text to instruction.
  const found = new Map();
  let address = start;
  while (address.compare(end) < 0) {
    let instruction;
    try {
      instruction = Instruction.parse(address);
    } catch (_error) {
      break;
    }
    found.set(address.toString(), instruction);
    address = instruction.next;
  }
  return found;
}

function reachedCode(start, end, linear) {
  // The instructions control reaches from ``start`` (direct branches only),
  // or null when it reaches a tail jump or an instruction the linear read
  // has not seen.
  const inside = address => address.compare(start) >= 0 && address.compare(end) < 0;
  const reached = new Map();
  const todo = [start];
  while (todo.length > 0) {
    let address = todo.pop();
    while (inside(address) && !reached.has(address.toString())) {
      let instruction = linear.get(address.toString());
      if (instruction === undefined) {
        try {
          instruction = Instruction.parse(address);
        } catch (_error) {
          return null;
        }
      }
      reached.set(address.toString(), instruction);
      const groups = instruction.groups;
      if (groups.includes('jump') || groups.includes('call')) {
        const target = branchTarget(instruction);
        if (target !== null && inside(target))
          todo.push(target);
        else if (target !== null && instruction.mnemonic === 'jmp')
          return null;  // a tail call: it returns past the code's own rets
      }
      if (groups.includes('ret') || instruction.mnemonic === 'jmp' || groups.includes('int'))
        break;
      address = instruction.next;
    }
  }
  return reached;
}

function returnProbes(info) {
  // Where to probe the returns of a method's code: per ret the first of the
  // instructions right before it, with it at least MIN_PATCH bytes, that
  // no other branch jumps into and that leave the result as it is (the probe
  // runs before them). Null if not every return can be probed so.
  if (Process.arch !== 'x64' && Process.arch !== 'ia32')
    return null;
  const start = info.start;
  const end = start.add(info.size);
  const linear = readCode(start, end);
  const reached = reachedCode(start, end, linear);
  if (reached === null)
    return null;
  const indirect = Array.from(reached.values()).some(instruction =>
    instruction.groups.includes('jump') && branchTarget(instruction) === null);
  const rets = Array.from(reached.values()).filter(instruction => instruction.groups.includes('ret'));
  // Through a jump table (switch) control reaches code not followed here:
  // only if the linear read finds no ret more is every return known.
  const linearRets = Array.from(linear.values()).filter(instruction => instruction.groups.includes('ret'));
  if (rets.length === 0 || (indirect && linearRets.length > rets.length))
    return null;
  // Branch targets of both reads: a false one only makes a probe less likely.
  const targets = new Set();
  for (const instruction of [...linear.values(), ...reached.values()]) {
    const target = branchTarget(instruction);
    if (target !== null && (instruction.groups.includes('jump') || instruction.groups.includes('call')))
      targets.add(target.toString());
  }
  const previous = new Map();  // address text of an instruction -> the one before it
  for (const instruction of reached.values())
    previous.set(instruction.next.toString(), instruction);
  const sites = [];
  for (const ret of rets) {
    let site = ret;
    while (ret.next.sub(site.address).toInt32() < MIN_PATCH) {
      const before = previous.get(site.address.toString());
      if (before === undefined || isControl(before) || !leavesResult(before)
          || targets.has(site.address.toString()))
        return null;
      site = before;
    }
    if (site.address.compare(start.add(START_PATCH)) < 0)
      return null;
    sites.push(site.address);
  }
  return sites;
}

function hookCalls(info, enter, leave = null) {
  // Call enter(invocation) when the method's code starts and, with
  // ``leave``, leave(invocation, retval) when it returns, on the same
  // invocation: { context, returnAddress, threadId, replaceResult(value) },
  // which the callbacks may add to. Returns { detach(), viaStack } with
  // viaStack true when the code's returns could not be probed and Frida's
  // onLeave serves instead.
  const sites = leave === null ? [] : returnProbes(info);
  if (sites === null)
    return hookCallsViaStack(info, enter, leave);
  const calls = new Map();  // thread id -> [{ sp, invocation }], the innermost last
  const listeners = [];
  try {
    listeners.push(Interceptor.attach(info.start, function () {
      const context = this.context;
      // A probe's returnAddress is not the caller's: it is on top of the stack.
      const invocation = { context, returnAddress: context.sp.readPointer(),
                           threadId: this.threadId };
      if (leave !== null) {
        let stack = calls.get(this.threadId);
        if (stack === undefined) {
          stack = [];
          calls.set(this.threadId, stack);
        }
        // Calls not below this one have ended (by an exception, else their probe pops them)
        while (stack.length > 0 && stack[stack.length - 1].sp.compare(context.sp) <= 0)
          stack.pop();
        stack.push({ sp: context.sp, invocation });
      }
      enter(invocation);
    }));
    for (const site of sites)
      listeners.push(Interceptor.attach(site, function () {
        const stack = calls.get(this.threadId);
        if (stack === undefined)
          return;
        const context = this.context;
        // Below the stack pointer: calls further in that ended by an exception
        while (stack.length > 0 && stack[stack.length - 1].sp.compare(context.sp) < 0)
          stack.pop();
        const entry = stack.pop();
        if (stack.length === 0)
          calls.delete(this.threadId);
        if (entry === undefined)
          return;
        const invocation = entry.invocation;
        invocation.context = context;
        invocation.replaceResult = value => { context[RESULT_REG] = value; };
        leave(invocation, ptr(context[RESULT_REG]));
      }));
  } catch (error) {
    // A return too short to probe after all (Frida found no jump target near enough)
    listeners.forEach(listener => listener.detach());
    if (leave === null)
      throw error;
    return hookCallsViaStack(info, enter, leave);
  }
  return {
    detach() {
      listeners.forEach(listener => listener.detach());
      calls.clear();
    },
    viaStack: false
  };
}

function hookCallsViaStack(info, enter, leave) {
  // hookCalls with Frida's onEnter and onLeave: an exception thrown through
  // the method ends the game (see above).
  const listener = Interceptor.attach(info.start, {
    onEnter() {
      this.invocation = { context: this.context, returnAddress: this.returnAddress,
                          threadId: this.threadId };
      enter(this.invocation);
    },
    onLeave(retval) {
      const invocation = this.invocation;
      invocation.context = this.context;
      invocation.replaceResult = value => retval.replace(value);
      leave(invocation, retval);
    }
  });
  return { detach: () => listener.detach(), viaStack: true };
}

// --- Values -----------------------------------------------------------------
// A value is { text, target?, snapshot? }: ``target`` is what the object
// inspector opens ({ address } of an object or array, { address, klass } of
// a struct inside one), ``snapshot`` its contents at the time of the call.

const MAX_ELEMENTS = 200;
const OBJECT_HEADER = 2 * Process.pointerSize;
const ARRAY_LENGTH = 3 * Process.pointerSize;
const ARRAY_DATA = 4 * Process.pointerSize;

// Grembyte's value type (grembyte/core/value_types.py) of a field that holds a
// number, for the address list; enums count as their base type.
const VALUE_TYPE_NAMES = {
  [T.BOOLEAN]: 'uint8', [T.I1]: 'int8', [T.U1]: 'uint8', [T.CHAR]: 'uint16',
  [T.I2]: 'int16', [T.U2]: 'uint16', [T.I4]: 'int32', [T.U4]: 'uint32',
  [T.I8]: 'int64', [T.U8]: 'uint64', [T.R4]: 'float', [T.R8]: 'double'
};

function valueTypeName(type) {
  return type.byref ? null : (VALUE_TYPE_NAMES[type.kind] || null);
}

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
  const text = readUtf16Text(pointer.add(OBJECT_HEADER + 4), Math.min(length, MAX_STRING));
  return JSON.stringify(text) + (length > MAX_STRING ? '…' : '');
}

function stringValue(pointer) {
  // A string as a logged value (``string`` set unless null); a long one also
  // keeps its whole text (``fullText``: the id for fullText(), ``length``:
  // its characters).
  if (pointer.isNull())
    return { text: 'null' };
  const value = { text: managedString(pointer), string: true };
  const length = pointer.add(OBJECT_HEADER).readS32();
  if (length > MAX_STRING) {
    value.fullText = keepText(pointer, length, value.text);
    value.length = length;
  }
  return value;
}

function keepText(pointer, length, start) {
  const key = `${pointer}:${length}:${start}`;
  const known = fullTextIds.get(key);
  if (known !== undefined)
    return known;
  const text = readUtf16Text(pointer.add(OBJECT_HEADER + 4), Math.min(length, MAX_FULL_STRING));
  const id = nextFullText++;
  fullTexts.set(id, { text, key });
  fullTextIds.set(key, id);
  fullTextChars += text.length;
  for (const [oldest, kept] of fullTexts) {
    if (fullTextChars <= FULL_STRING_BUDGET || oldest === id)
      break;
    fullTexts.delete(oldest);
    fullTextIds.delete(kept.key);
    fullTextChars -= kept.text.length;
  }
  return id;
}

function fullText(id) {
  // The whole text of a long string value, or null when it was dropped.
  const kept = fullTexts.get(id);
  return kept === undefined ? null : kept.text;
}

function withEnum(type, text) {
  // 2 (Dexterity) where the adapter knows the enum's names, else 2 (EPrimaryAttribute)
  if (!type.enumName)
    return text;
  const name = type.enumValues ? type.enumValues[text] : undefined;
  return `${text} (${name === undefined ? type.enumName : name})`;
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

function instanceNameOf(pointer) {
  // The name of an object for the inspector: Unreal's FName (read from
  // memory on any thread), a Unity object's name as seen in calls.
  if (engine.id === 'unreal')
    return engine.instanceName(pointer);
  return unityNameOf(pointer, false);
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
      return stringValue(address.readPointer());
    }
    default:
      if (type.engine !== undefined)
        return engineValue(type, address, ask);
      if (REFERENCE_KINDS.has(type.kind))
        return objectValue(address.readPointer(), ask);
      return { text: `? (${type.name})` };
  }
}

function engineValue(type, address, ask) {
  // A value only the engine's adapter can read (Unreal: FString, TArray ...).
  const value = engine.readValue(type, address);
  return value.object !== undefined ? objectValue(value.object, ask) : value;
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
    case T.STRING: return stringValue(raw);
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
  // { name, instanceName?, address, fields: [{ name, type, offset, static, storage?, value,
  // address?, valueType? }], length? } of an object, array or struct; throws
  // if it is no object any more. A field's ``address`` is where its value is
  // (a static field's in its class's static data; none for constants and
  // thread statics, see ``storage``), ``valueType`` its value type if it
  // holds a number.
  if (target.property)
    return containerContents(target);
  const address = ptr(target.address);
  let klass;
  if (target.klass) {
    klass = ptr(target.klass);
  } else {
    klass = engine.classOf(address);
    if (klass === null)
      throw grembyteError('error.monitor.object_gone', { address });
  }
  const layout = engine.layout(klass);
  // Mono's struct field offsets count an object header; Unreal's do not.
  const header = layout.headerSize === undefined ? OBJECT_HEADER : layout.headerSize;
  const base = target.klass ? address.sub(header) : address;
  const result = {
    name: layout.name,
    instanceName: target.klass ? null : instanceNameOf(address),
    address: address.toString(), fields: []
  };
  if (layout.rank > 0) {
    const length = address.add(ARRAY_LENGTH).readU32();
    result.length = length;
    const data = address.add(ARRAY_DATA);
    for (let i = 0; i < Math.min(length, MAX_ELEMENTS); i++)
      result.fields.push({
        name: `[${i}]`, type: layout.element.name, offset: ARRAY_DATA + i * layout.elementSize,
        static: false,
        value: safely(() => readValue(layout.element, data.add(i * layout.elementSize))),
        address: data.add(i * layout.elementSize).toString(),
        valueType: valueTypeName(layout.element)
      });
    return result;
  }
  result.properties = engine.properties(klass).map(property => ({
    name: property.name, type: property.type.name, static: property.static,
    setter: property.setter || null
  }));
  for (const field of layout.fields) {
    const entry = { name: field.name, type: field.type.name, offset: field.offset,
                    static: field.static, storage: field.storage || null };
    let at = null;
    if (!field.static) {
      at = base.add(field.offset);
    } else if (field.storage === 'data') {
      try {
        const data = engine.staticData(field.owner);
        at = data === null ? null : data.add(field.offset);
      } catch (error) {
        entry.value = { text: `? (${error.message})` };
      }
    }
    if (at === null) {
      entry.value = entry.value || { text: '' };
      entry.address = null;
      entry.valueType = null;
    } else {
      entry.value = safely(() => readValue(field.type, at));
      entry.address = at.toString();
      entry.valueType = valueTypeName(field.type);
    }
    result.fields.push(entry);
  }
  return result;
}

function containerContents(target) {
  // The entries of a TArray, TMap or TSet (Unreal, see engine.elements) as
  // contents: one field per entry, a TMap's named by its key.
  const container = engine.elements(target, MAX_ELEMENTS);
  return {
    name: container.name, address: target.address, length: container.count,
    fields: container.entries.map(entry => ({
      name: entry.key ? safely(() => ({ text: keyText(entry.key) })).text : entry.name,
      type: entry.type.name, offset: entry.offset, static: false,
      value: safely(() => readValue(entry.type, entry.address)),
      address: entry.address.toString(), valueType: valueTypeName(entry.type)
    }))
  };
}

const MAX_SUMMARY_FIELDS = 3;
const MAX_SUMMARY = 100;

function keyText(key) {
  // A TMap key as text; a struct (e.g. FGameplayAttribute) by its first fields.
  if (key.type.kind !== T.VALUETYPE)
    return readValue(key.type, key.address).text;
  const layout = engine.layout(key.type.klass);
  const parts = layout.fields.filter(field => !field.static).slice(0, MAX_SUMMARY_FIELDS)
    .map(field => `${field.name}=${safely(() => readValue(field.type,
      key.address.add(field.offset))).text}`);
  const text = `{${parts.join(', ')}}`;
  return text.length > MAX_SUMMARY ? `${text.substring(0, MAX_SUMMARY)}…}` : text;
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
    return stringValue(result);
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
      throw grembyteError('error.monitor.object_gone', { address });
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

// --- Calling static methods (the method call dialog) ------------------------------

function catalogMethod(identity, instance = false) {
  const method = engine.resolve(identity);
  if (engine.signatureOf(method).instance !== instance)
    throw grembyteError(
      instance ? 'error.monitor.no_instance_method' : 'error.monitor.no_static_method',
      { method: identity.method });
  return method;
}

const STRUCT_FIELD_KINDS = new Set([T.I1, T.U1, T.I2, T.U2, T.I4, T.U4, T.I8, T.U8, T.R4, T.R8]);

function callSignature(method) {
  // { params, returns } as the dialog reads them: a struct whose fields are
  // all numbers (FVector, UnityEngine.Vector3) comes with them, { name, kind,
  // offset, enumValues? } from the struct's start, so it can be typed in.
  const { params, returns } = engine.signatureOf(method);
  return {
    params: params.map(param => {
      const type = param.type;
      if (type.kind !== T.VALUETYPE || !type.structSize || type.klass === undefined)
        return param;
      let layout;
      try {
        layout = engine.layout(type.klass);
      } catch (_error) {
        return param;  // not typed in, as before
      }
      const fields = layout.fields.filter(field => !field.static);
      if (fields.length === 0 || !fields.every(field => STRUCT_FIELD_KINDS.has(field.type.kind)
                                               && field.type.engine === undefined))
        return param;
      const header = layout.headerSize || 0;
      return { ...param, type: { ...type, fields: fields.map(field => ({
        name: field.name, kind: field.type.kind, offset: field.offset - header,
        enumValues: field.type.enumValues })) } };
    }),
    returns
  };
}

function describeCall(identity, instance = false) {
  // The parameters and result type of a static method, or with ``instance``
  // of an instance method (see callFound): { params, returns }.
  return engine.onRuntimeThread(() => callSignature(catalogMethod(identity, instance)));
}

function callStatic(identity, args) {
  // Call a static method on Unity's main thread with the arguments the
  // dialog read (see grembyte/features/method_call.py; an object or IntPtr as
  // { address }, a struct such as a Guid as { struct: its bytes in hex }):
  // { result } with the value it returned, or { exception }.
  return engine.onMainThread(() => callResult(catalogMethod(identity), null, args));
}

function callFound(identity, className, objectText, args) {
  // Call an instance method of the catalog on a live object of its class
  // (``className``), on the game's main thread: the first one found, the one
  // named ``objectText`` or at that address (0x…). As callStatic, and with the
  // object it ran on ({ object }).
  return engine.onMainThread(() => {
    const method = catalogMethod(identity, true);
    const object = foundObject(className, (objectText || '').trim());
    return { ...callResult(engine.virtualMethod(object, method), object, args),
             object: objectValue(object, true) };
  });
}

function foundObject(className, text) {
  if (!/^0x[0-9a-f]+$/i.test(text)) {
    const object = engine.findObject(className, text === '' ? null : text);
    if (object === null)
      throw text === '' ? grembyteError('error.monitor.no_live_object', { className })
        : grembyteError('error.monitor.no_live_named', { className, name: text });
    return object;
  }
  const object = ptr(text);
  const klass = engine.classOf(object);
  const wanted = engine.findClass(className);
  if (klass === null || !engine.classChain(klass).some(current => current.equals(wanted)))
    throw grembyteError('error.monitor.wrong_object', { className, address: object });
  return object;
}

function callResult(method, self, args) {
  // Call ``method`` with the dialog's arguments (see callStatic): { result } or { exception }.
  const values = args.map(arg => {
    if (arg === null || typeof arg !== 'object')
      return arg;
    if (arg.struct === undefined)
      return ptr(arg.address);
    const bytes = arg.struct.match(/../g).map(pair => parseInt(pair, 16));
    const data = Memory.alloc(bytes.length);
    data.writeByteArray(bytes);
    return new ManagedObject(data, null, true);
  });
  const { result, exception } = invokeMethod(method, self, values);
  if (!exception.isNull())
    return { exception: exceptionText(exception) };
  const signature = engine.signatureOf(method);
  const returns = signature.returns;
  const answer = {};
  if (signature.parms) {
    // Unreal: the result and outputs are written into the parameter block,
    // which the object inspector opens later (a TArray, a struct)
    keepBlock(result);
    const outputs = signature.params.filter(param => param.out).map(param => ({
      name: param.name,
      value: safely(() => readValue({ ...param.type, byref: false },
                                    result.add(param.offset), true))
    }));
    if (outputs.length > 0)
      answer.outputs = outputs;
  }
  if (returns.kind === T.VOID && !returns.byref)
    return { ...answer, result: null };
  if (signature.parms)
    return { ...answer,
             result: safely(() => readValue(returns, result.add(signature.returnOffset), true)) };
  return { result: resultValue(returns, result) };
}

// --- Calling methods of an object (the object inspector) ---------------------------

function targetClass(target) {
  // The class of an inspected object or struct (see contents).
  const address = ptr(target.address);
  const klass = target.klass ? ptr(target.klass) : engine.classOf(address);
  if (klass === null)
    throw grembyteError('error.monitor.object_gone', { address });
  return klass;
}

function listMethods(target) {
  // The instance methods of an object, its class's own first:
  // [{ name, method, owner, params, returns }], params as text ("int amount, …").
  // A TArray, TMap or TSet has none.
  if (target.property)
    return [];
  return engine.onRuntimeThread(() => engine.methods(targetClass(target))
    .filter(entry => !entry.static)
    .map(entry => {
      let params = '?';
      let returns = '?';
      try {
        const signature = engine.signatureOf(entry.method);
        params = signature.params.map(param => `${param.type.name} ${param.name}`).join(', ');
        returns = signature.returns.name;
      } catch (_error) {
        // shown as ?; calling it reports the problem
      }
      return { name: entry.name, method: entry.method.toString(), owner: entry.owner,
               params, returns };
    }));
}

function instanceMethod(method) {
  const fn = ptr(method);
  if (!engine.signatureOf(fn).instance)
    throw grembyteError('error.monitor.no_instance_method', { method });
  return fn;
}

function describeMethod(method) {
  // The parameters and result type of an instance method: { params, returns }.
  return engine.onRuntimeThread(() => callSignature(instanceMethod(method)));
}

function callOn(target, method, args) {
  // Call an instance method on an inspected object, on the game's main
  // thread, with the dialog's arguments (see callStatic); an override of the
  // object's class runs in its place.
  return engine.onMainThread(() => {
    const address = ptr(target.address);
    targetClass(target);  // still an object?
    let fn = instanceMethod(method);
    if (!target.klass)
      fn = engine.virtualMethod(address, fn);
    return callResult(fn, address, args);
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

function frameValue(monitor, type, location) {
  // An argument the engine found in its call frame (see engine.callFrame):
  // where it is ({ address }), or its value ({ value }, a constant).
  if (location.value !== undefined) {
    const value = location.value.object !== undefined
      ? safely(() => objectValue(location.value.object, true)) : location.value;
    return monitor.objects ? snapshot(value) : value;
  }
  const value = safely(() => readValue({ ...type, byref: false }, location.address, true));
  const shown = monitor.objects ? snapshot(value) : value;
  return type.byref ? { ...shown, text: `&${location.address} → ${shown.text}` } : shown;
}

function onFrameEnter(monitor, invocation) {
  // The engine reads the arguments from its own call frame (Unreal).
  const { info } = monitor;
  const call = engine.callFrame(info, invocation.context);
  const pointers = [];
  const record = {
    monitorId: monitor.id,
    time: Date.now(),
    threadId: invocation.threadId,
    self: call.self === null ? null
      : argumentValue(monitor, { kind: 0x12, byref: false }, call.self),
    args: info.params.map((param, i) => {
      const location = call.args[i];
      if (location === null)
        return { name: param.name, text: '?' };
      if (param.type.byref && location.address !== undefined)
        pointers.push([i, location.address]);
      return { name: param.name, ...frameValue(monitor, param.type, location) };
    }),
    // The Blueprint function it came from, else the native code
    caller: call.caller || safely(() => ({ text: frameText(invocation.returnAddress) })).text
  };
  if (monitor.returns) {
    invocation.record = record;
    invocation.pointers = pointers;
    invocation.resultAddress = call.result;
  } else {
    push(monitor, record);
  }
}

function onEnter(monitor, invocation) {
  // Already on a game thread: run waiting property reads if it is the main one.
  engine.drainMain();
  if (monitor.info.frame) {
    onFrameEnter(monitor, invocation);
    return;
  }
  const context = invocation.context;
  const { info, layout } = monitor;
  const pointers = [];
  const record = {
    monitorId: monitor.id,
    time: Date.now(),
    threadId: invocation.threadId,
    self: layout.self === null ? null : selfValue(monitor, rawValue(context, layout.self)),
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

function selfValue(monitor, raw) {
  // ``this``: an object, or (IL2CPP) a pointer to the data of a struct
  const owner = monitor.info.selfStruct;
  if (owner) {
    const value = { text: `struct ${engine.layout(owner).name}@${raw}`,
                    target: { address: raw.toString(), klass: owner.toString() } };
    return monitor.objects ? snapshot(value) : value;
  }
  return argumentValue(monitor, { kind: 0x1c, byref: false }, raw);
}

function onLeave(monitor, invocation, retval) {
  const record = invocation.record;
  if (record === undefined)
    return;
  const type = monitor.info.returns;
  if (monitor.info.frame) {
    // Written where the caller asked for it (the frame's Result)
    if (type.kind !== T.VOID && invocation.resultAddress !== null) {
      const value = safely(() => readValue(type, invocation.resultAddress, true));
      record.result = monitor.objects ? snapshot(value) : value;
    }
  } else if (type.kind !== T.VOID || type.byref) {
    const value = safely(() => registerValue(
      type, resultRaw(invocation.context, retval, type), true));
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
    throw grembyteError('error.monitor.exists', { id: spec.id });
  const info = await engine.prepare(spec.identity);
  if (monitors.has(spec.id))
    throw grembyteError('error.monitor.exists', { id: spec.id });
  const hasResults = (info.returns.kind !== T.VOID || info.returns.byref)
    || info.params.some(param => param.type.byref);
  const hasObjects = info.instance || isObject(info.returns)
    || info.params.some(param => isObject(param.type));
  const monitor = {
    id: spec.id, info, layout: info.frame ? null : argumentLayout(info),
    returns: spec.returns === null ? hasResults : spec.returns,
    objects: spec.objects === null ? hasObjects : spec.objects,
    errorReported: false
  };
  monitor.listener = hookCalls(info, invocation => {
    try {
      onEnter(monitor, invocation);
    } catch (error) {
      reportError(monitor, error);
    }
  }, !monitor.returns ? null : (invocation, retval) => {
    try {
      onLeave(monitor, invocation, retval);
    } catch (error) {
      reportError(monitor, error);
    }
  });
  monitors.set(spec.id, monitor);
  updateUnmapWatch();
  return {
    id: spec.id,
    address: info.start.toString(),
    instance: info.instance,
    params: info.params.map(p => `${p.type.name} ${p.name}`),  // Mono names byref types 'T&'
    returns: info.returns.name,
    // Unreal: which arguments are unknown shows only per call ('?')
    undecoded: info.frame ? 0 : monitor.layout.args.filter(location => location === null).length,
    // what the method has to show, and the options in effect
    hasResults,
    hasObjects,
    recordsResults: monitor.returns,
    recordsObjects: monitor.objects,
    viaStack: monitor.listener.viaStack
  };
}

function removeMonitor(id) {
  const monitor = monitors.get(id);
  if (monitor === undefined)
    return false;
  monitor.listener.detach();
  monitors.delete(id);
  engine.releaseCode(monitor.info.start);
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
        send({ kind: 'agent-error', error: grembyteMessage('error.monitor.unmap',
            { error: String(error) }) });
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
// What scripts write with ctx.log, also outside debug mode (the hook panel's
// log): { hookId, time, text }, at most MAX_HOOK_LOGS per flush; more are counted.
const MAX_HOOK_LOGS = 100;
const hookLogs = [];
const hookLogsDropped = new Map();
// Per thread the hooked calls (after the call) that are still running, the
// innermost last: ctx.caller of a nested hook. A frame whose method left by an
// exception never returns; enterFrame drops it once the stack is below it.
const activeCalls = new Map();
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
      throw grembyteError('error.monitor.no_field', { className: layout.name, name });
    return readJs(field.type, this.fieldBase(layout).add(field.offset));
  }

  fieldBase(layout) {
    // Where field offsets count from: a struct's offsets include the object
    // header (Mono) unless the layout says otherwise (Unreal: none).
    if (!this.isStruct)
      return this.address;
    return this.address.sub(layout.headerSize === undefined ? OBJECT_HEADER : layout.headerSize);
  }

  setField(name, value) {
    // Change a field (in the "before the call" phase, the method sees it).
    const layout = engine.layout(this.klass);
    const field = layout.fields.find(candidate => candidate.name === name && !candidate.static);
    if (field === undefined)
      throw grembyteError('error.monitor.no_field', { className: layout.name, name });
    const address = this.fieldBase(layout).add(field.offset);
    const type = field.type;
    if (type.kind === T.VALUETYPE)
      throw grembyteError('error.monitor.struct_field_whole', { name });
    storeField(type, address, this.isStruct ? null : this.address, value,
               `${shortClass(layout.name)}.${name}`);
  }

  get(name) {
    const property = engine.properties(this.klass).find(candidate => candidate.name === name);
    if (property === undefined)
      throw grembyteError('error.monitor.no_property', { className: this.className, name });
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

function storeField(type, address, owner, value, label) {
  // Write ``value`` to a field at ``address`` of object ``owner`` (null: a
  // struct's, or static data, which the garbage collector scans as a root)
  // and note the change for the hooks' statistics.
  if (type.kind === T.VALUETYPE)
    throw grembyteError('error.monitor.struct_field_whole', { name: label });
  let old;
  try {
    old = readJs(type, address);
  } catch (_error) {
    // shown without its old value
  }
  if (type.engine !== undefined)
    engine.writeValue(type, address, value);
  else if (REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING)
    engine.writeReference(owner, address, pointerOf(type, value));
  else
    writeJs(type, address, value);
  noteChange(label, value, old);
}

function staticFieldAddress(layout, field) {
  // Where a static field of ``layout`` lies; constants have no storage,
  // thread statics none of the class's.
  const data = field.storage === 'data' && engine.staticData !== undefined
    ? engine.staticData(field.owner) : null;
  if (data === null)
    throw grembyteError('error.monitor.static_unreadable', { name: `${layout.name}.${field.name}` });
  return data.add(field.offset);
}

// Scripts may also write object.Health, object.IsPartyMember or
// object.GetComponentByName("Stats"): names the wrapper does not have are
// looked up as a field, then as a property (getter), then as a method (a
// function calling it, as .call(name, ...args)). A static field of the class
// reads through an object too, gameState.s_playerCharacter as
// ctx.static("GameState.s_playerCharacter"), like a static method calls
// through one. The wrapper's own members
// (name, className, field(), get(), call() …) come first; .field("name")
// reads a game field of the same name.
// Assigning works the same way: object.Health = 5 sets the field, else the
// property's setter (set_Health).
const shortcuts = {
  get(target, key, receiver) {
    if (typeof key !== 'string' || key in target)
      return Reflect.get(target, key, receiver);
    const layout = engine.layout(target.klass);
    if (layout.fields.some(field => field.name === key && !field.static))
      return target.field(key);
    const staticField = layout.fields.find(field => field.name === key && field.static);
    if (staticField !== undefined)
      return readJs(staticField.type, staticFieldAddress(layout, staticField));
    if (engine.properties(target.klass).some(property => property.name === key))
      return target.get(key);
    const isStatic = methodNames(target.klass).get(key);
    if (isStatic === undefined)
      return undefined;
    // An overload by the number of arguments, as .call() and ctx.call() pick it
    if (isStatic)
      return (...args) => callMethod(
        engine.findMethod(target.klass, key, args.length, true), null, args);
    return (...args) => target.call(key, ...args);
  },
  set(target, key, value, receiver) {
    if (typeof key !== 'string' || key in target)
      return Reflect.set(target, key, value, receiver);
    const layout = engine.layout(target.klass);
    if (layout.fields.some(field => field.name === key && !field.static)) {
      target.setField(key, value);
      return true;
    }
    const staticField = layout.fields.find(field => field.name === key && field.static);
    if (staticField !== undefined) {
      storeField(staticField.type, staticFieldAddress(layout, staticField), null, value,
                 `${shortClass(layout.name)}.${key}`);
      return true;
    }
    let setter;
    try {
      setter = engine.findMethod(target.klass, `set_${key}`, 1, false);
    } catch (_error) {
      throw grembyteError('error.monitor.nothing_to_set', { className: layout.name, name: key });
    }
    callMethod(target.isStruct ? setter : engine.virtualMethod(target.address, setter),
               target.address, [value]);
    noteChange(`${shortClass(layout.name)}.${key}`, value);
    return true;
  }
};

// Per class: method name -> whether it is static (an instance overload wins).
const methodNameCache = new Map();

function methodNames(klass) {
  const key = klass.toString();
  let names = methodNameCache.get(key);
  if (names === undefined) {
    names = new Map();
    for (const method of engine.methods(klass))
      names.set(method.name, (names.get(method.name) ?? true) && method.static);
    methodNameCache.set(key, names);
  }
  return names;
}

function shortClass(name) {
  return name.substring(name.lastIndexOf('.') + 1);
}

// What the scripts running before a call changed, for the hooks' statistics:
// per thread a stack of lists, one per script. A script that calls game code
// lets other threads, and hooks nested in that call, run meanwhile.
const changeStacks = new Map();

function beginChanges() {
  const threadId = Process.getCurrentThreadId();
  let stack = changeStacks.get(threadId);
  if (stack === undefined) {
    stack = [];
    changeStacks.set(threadId, stack);
  }
  const changed = [];
  stack.push(changed);
  return changed;
}

function endChanges(changed) {
  const threadId = Process.getCurrentThreadId();
  const stack = changeStacks.get(threadId);
  if (stack === undefined)
    return;
  const at = stack.lastIndexOf(changed);
  if (at !== -1)
    stack.splice(at, 1);
  if (stack.length === 0)
    changeStacks.delete(threadId);
}

function noteChange(what, value, old) {
  // old: the value before, if known (not for properties: their getter is game code)
  const stack = changeStacks.get(Process.getCurrentThreadId());
  if (stack !== undefined)
    stack[stack.length - 1].push({ what, value, old });
}

function changesText(changed, before) {
  // name=value of the changes, or of the old values (those known)
  return changed.filter(change => !before || change.old !== undefined)
    .map(change => `${change.what}=${displayText(before ? change.old : change.value)}`)
    .join(', ');
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
    throw grembyteError('error.monitor.no_object', { address: pointer });
  return wrap(new ManagedObject(pointer, klass));
}

function engineJs(type, address) {
  // A value only the engine's adapter can read (Unreal: FString, TArray,
  // weak pointers, bitfield bools ...), as a JavaScript value.
  const value = engine.valueJs(type, address);
  if (value.object !== undefined)
    return objectJs(value.object);
  if (value.elements !== undefined)
    return value.elements.map(element => readJs(value.element, element));
  return value.value;
}

function readJs(type, address) {
  // A value of ``type`` stored at ``address``, as a JavaScript value.
  if (type.engine !== undefined)
    return engineJs(type, address);
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
  return readUtf16Text(pointer.add(OBJECT_HEADER + 4), length);
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
  if (type.engine !== undefined) {
    engine.writeValue(type, address, value);
    return;
  }
  switch (type.kind) {
    case T.BOOLEAN: address.writeU8(value ? 1 : 0); return;
    case T.CHAR: address.writeU16(typeof value === 'string' ? value.charCodeAt(0) : value); return;
    case T.I1: address.writeS8(Math.trunc(value)); return;
    case T.U1: address.writeU8(Math.trunc(value)); return;
    case T.I2: address.writeS16(Math.trunc(value)); return;
    case T.U2: address.writeU16(Math.trunc(value)); return;
    case T.I4: address.writeS32(Math.trunc(value)); return;
    case T.U4: address.writeU32(Math.trunc(value)); return;
    case T.I8: address.writeS64(int64(integerText(value))); return;
    case T.U8: address.writeU64(uint64(integerText(value))); return;
    case T.R4: address.writeFloat(value); return;
    case T.R8: address.writeDouble(value); return;
    case T.VALUETYPE: throw grembyteError('error.monitor.struct_replace', { type: type.name });
    default: address.writePointer(pointerOf(type, value));
  }
}

function integerText(value) {
  // A 64-bit integer as text: integers beyond 2**53 come as { integer: text }
  // (see grembyte/features/method_call.py), others as numbers.
  if (value !== null && typeof value === 'object' && value.integer !== undefined)
    return value.integer;
  return String(Math.trunc(value));
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

function invokeMethod(method, self, args) {
  // Call game code with JavaScript arguments: { result, exception }.
  const signature = engine.signatureOf(method);
  if (signature.parms)
    return invokeWithParms(method, signature, self, args);
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
        throw grembyteError('error.monitor.argument_type',
            { name: param.name, type: param.type.name });
      slot = args[i].address;  // a struct: a pointer to its data
    } else {
      slot = Memory.alloc(8);  // a value: a pointer to it
      writeJs(param.type, slot, args[i]);
    }
    keep.push(slot);
    params.add(i * Process.pointerSize).writePointer(slot);
  });
  return engine.invoke(method, self === null ? NULL : self, params);
}

function invokeWithParms(method, signature, self, args) {
  // Unreal: the arguments in one block laid out like the parameters, where
  // the function writes its result, too ({ result: the block }).
  const parms = Memory.alloc(Math.max(signature.parmsSize, 8));
  parms.writeByteArray(new ArrayBuffer(Math.max(signature.parmsSize, 8)));
  signature.params.forEach((param, i) => {
    const type = { ...param.type, byref: false };
    const address = parms.add(param.offset);
    const value = args[i];
    if (param.out && (value === null || value === undefined))
      return;  // an output: the function writes it
    if (type.engine !== undefined)
      engine.writeParameter(type, address, value);
    else if (type.kind === T.VALUETYPE) {
      if (!(value instanceof ManagedObject))
        throw grembyteError('error.monitor.argument_type', { name: param.name, type: type.name });
      Memory.copy(address, value.address, type.structSize);
    } else if (REFERENCE_KINDS.has(type.kind))
      address.writePointer(pointerOf(type, value));
    else
      writeJs(type, address, value);
  });
  return engine.invoke(method, self, parms);
}

// Results of Unreal calls live in their parameter block (structs, and what the
// call dialog shows the object inspector): kept for a while
const keptBlocks = [];

function keepBlock(parms) {
  keptBlocks.push(parms);
  if (keptBlocks.length > 64)
    keptBlocks.shift();
}

function parmsResultJs(signature, parms) {
  const type = signature.returns;
  if (type.kind === T.VOID && !type.byref)
    return undefined;
  if (type.kind === T.VALUETYPE)
    keepBlock(parms);
  return readJs(type, parms.add(signature.returnOffset));
}

function callMethod(method, self, args) {
  // Call game code with JavaScript arguments; returns its result.
  const { result, exception } = invokeMethod(method, self, args);
  throwIfException(exception);
  const signature = engine.signatureOf(method);
  if (signature.parms)
    return parmsResultJs(signature, result);
  return resultJs(signature.returns, result);
}

function staticCall(qualifiedName, ...args) {
  // ctx.call("Namespace.Class.Method", ...), or as the catalog and the monitor
  // name it, "Namespace.Class:Method" (a parameter list after it is ignored:
  // the method is the one taking as many arguments as given)
  const name = qualifiedName.replace(/\s*\(.*\)\s*$/, '');
  const colon = name.lastIndexOf(':');
  const split = colon >= 0 ? colon : name.lastIndexOf('.');
  if (split <= 0 || split === name.length - 1)
    throw grembyteError('error.monitor.not_qualified', { name: qualifiedName });
  const klass = engine.findClass(name.substring(0, split));
  const method = engine.findMethod(klass, name.substring(split + 1), args.length, true);
  return callMethod(method, null, args);
}

function staticField(qualifiedName) {
  // ctx.static("Namespace.Class.Field"), also "Namespace.Class:Field": the
  // value of a static field, e.g. a singleton's Instance that is no Unity
  // object (Mono)
  if (engine.staticData === undefined)
    throw grembyteError('error.monitor.static_mono_only');
  const name = String(qualifiedName).trim();
  const colon = name.lastIndexOf(':');
  const split = colon >= 0 ? colon : name.lastIndexOf('.');
  if (split <= 0 || split === name.length - 1)
    throw grembyteError('error.monitor.not_qualified_field', { name: qualifiedName });
  const layout = engine.layout(engine.findClass(name.substring(0, split)));
  const fieldName = name.substring(split + 1);
  const field = layout.fields.find(candidate => candidate.name === fieldName && candidate.static);
  if (field === undefined)
    throw grembyteError('error.monitor.no_static_field', { className: layout.name, name: fieldName });
  return readJs(field.type, staticFieldAddress(layout, field));
}

function findObject(className, objectName = null) {
  // ctx.find("AlabamaCheatManager"): a live object of that class (Unreal,
  // Unity), or null; ctx.find("UnityEngine.GameObject", "Player(Clone)"): the
  // one of that name
  if (objectName !== null && typeof objectName !== 'string')
    throw grembyteError('error.monitor.find_name');
  const object = engine.findObject(className, objectName);
  return object === null ? null : objectJs(object);
}

function forEachObject(className, callback) {
  // ctx.forEach("WBP_AbilityPointsTracker_C", tracker => ...): ``callback``
  // for every live object ctx.find could give, until it returns false; how
  // many it was called for.
  if (typeof callback !== 'function')
    throw grembyteError('error.monitor.for_each_function');
  let count = 0;
  for (const object of engine.findObjects(className)) {
    let wrapped;
    try {
      wrapped = objectJs(object);
    } catch (_error) {
      continue;  // gone meanwhile
    }
    count++;
    if (callback(wrapped) === false)
      break;
  }
  return count;
}

function outerName(object) {
  // ctx.outerName(tracker): "AttributeStatsWidget.PointsAvailableTracker",
  // a name ctx.find takes (Unreal)
  if (!(object instanceof ManagedObject) || object.isStruct)
    throw grembyteError('error.monitor.outer_name_object');
  return engine.outerName(object.address);
}

function displayText(value) {
  if (value === undefined)
    return '';
  if (typeof value === 'string')
    return JSON.stringify(value);
  return String(value);
}

function replaceResult(hook, invocation, value) {
  const type = hook.info.returns;
  if (type.kind === T.VOID && !type.byref)
    throw grembyteError('error.monitor.void_result');
  if (ABI === 'x86' && (isFloat(type) || is64(type)))
    throw grembyteError('error.monitor.x86_replace');
  if (isFloat(type)) {
    if (typeof value !== 'number' || !Number.isFinite(value))
      throw grembyteError('error.monitor.result_number', { value: displayText(value) });
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
    throw grembyteError('error.monitor.struct_result', { type: type.name });
  if (type.kind === T.BOOLEAN) {
    invocation.replaceResult(ptr(value ? 1 : 0));
    return;
  }
  if (REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING || type.byref) {
    invocation.replaceResult(pointerOf(type, value));
    return;
  }
  if (typeof value !== 'number' || !Number.isFinite(value))
    throw grembyteError('error.monitor.result_number', { value: displayText(value) });
  invocation.replaceResult(ptr(BigInt.asUintN(8 * Process.pointerSize,
                                              BigInt(Math.trunc(value))).toString()));
}

function replaceFrameResult(hook, call, value) {
  // Unreal: the result is already where the caller reads it; overwrite it.
  const type = hook.info.returns;
  if (type.kind === T.VOID)
    throw grembyteError('error.monitor.void_result');
  if (call.resultAddress === null)
    throw grembyteError('error.monitor.no_caller_result');
  const number = type.kind >= T.I1 && type.kind <= T.R8;
  if (number && (typeof value !== 'number' || !Number.isFinite(value)))
    throw grembyteError('error.monitor.result_number', { value: displayText(value) });
  writeJs(type, call.resultAddress, value);
}

function writeArgument(context, location, type, value, raw) {
  // Pass ``value`` to the method instead of what the caller passed.
  if (type.byref) {
    const inner = { ...type, byref: false };
    if (REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING)
      raw.writePointer(pointerOf(inner, value));  // the caller's variable, not a field
    else if (type.kind === T.VALUETYPE)
      throw grembyteError('error.monitor.struct_replace', { type: type.name });
    else
      writeJs(inner, raw, value);
    return;
  }
  if (location === null)
    throw grembyteError('error.monitor.after_struct');
  if (type.kind === T.VALUETYPE)
    throw grembyteError('error.monitor.struct_replace', { type: type.name });
  if (isFloat(type)) {
    if (typeof value !== 'number' || !Number.isFinite(value))
      throw grembyteError('error.monitor.number_needed', { value: displayText(value) });
    if (location.xmm !== undefined) {
      const buffer = new ArrayBuffer(16);
      const view = new DataView(buffer);
      if (type.kind === T.R4)
        view.setFloat32(0, value, true);
      else
        view.setFloat64(0, value, true);
      context['xmm' + location.xmm] = buffer;
    } else {
      writeJs(type, context.sp.add(location.stack), value);
    }
    return;
  }
  let pointer;
  if (REFERENCE_KINDS.has(type.kind) || type.kind === T.STRING)
    pointer = pointerOf(type, value);
  else if (type.kind === T.BOOLEAN)
    pointer = ptr(value ? 1 : 0);
  else if (typeof value === 'number' && Number.isFinite(value) && location.size === 8
           && Process.pointerSize === 4) {
    writeJs(type, context.sp.add(location.stack), value);  // two 32-bit stack slots
    return;
  } else if (typeof value === 'number' && Number.isFinite(value))
    pointer = ptr(BigInt.asUintN(8 * Process.pointerSize, BigInt(Math.trunc(value))).toString());
  else if (type.kind === T.CHAR && typeof value === 'string')
    pointer = ptr(value.charCodeAt(0));
  else
    throw grembyteError('error.monitor.number_needed', { value: displayText(value) });
  if (location.reg !== undefined)
    context[location.reg] = pointer;
  else
    context.sp.add(location.stack).writePointer(pointer);
}

function callFrame(frames) {
  return frames === undefined || frames.length === 0 ? null : frames[frames.length - 1].frame;
}

function enterFrame(threadId, sp, frame) {
  let frames = activeCalls.get(threadId);
  if (frames === undefined) {
    frames = [];
    activeCalls.set(threadId, frames);
  }
  while (frames.length > 0 && frames[frames.length - 1].sp.compare(sp) <= 0)
    frames.pop();
  frame.caller = callFrame(frames);
  frames.push({ sp, frame });
}

function leaveFrame(threadId, frame) {
  const frames = activeCalls.get(threadId);
  if (frames === undefined)
    return;
  const index = frames.findIndex(entry => entry.frame === frame);
  if (index >= 0)
    frames.length = index;
  if (frames.length === 0)
    activeCalls.delete(threadId);
}

function currentCaller(threadId, sp) {
  // The innermost hooked call this one runs in (not stale), else null.
  const frames = activeCalls.get(threadId);
  if (frames === undefined)
    return null;
  while (frames.length > 0 && frames[frames.length - 1].sp.compare(sp) <= 0)
    frames.pop();
  return callFrame(frames);
}

function debugCaller(returnAddress, caller, scriptCaller) {
  // The debug window's caller: the Blueprint position the call came from
  // (Unreal), else the return address; and what ctx.caller is.
  const text = scriptCaller || safely(() => ({ text: frameText(returnAddress) })).text;
  return `${text} · ctx.caller: ${caller === null ? 'null' : `#${caller.hookId} ${caller.method}`}`;
}

function constantJs(type, value) {
  // A constant of the Blueprint bytecode (see engine.callFrame) as a JavaScript value.
  if (value.object !== undefined)
    return objectJs(value.object);
  if (type.engine === 'bool' || type.kind === T.BOOLEAN)
    return value.text === 'true' || value.text === '1';
  const number = Number(value.text);
  return value.text !== '' && Number.isFinite(number) ? number : value.text;
}

function hookArguments(hook, invocation) {
  // The arguments of a call as JavaScript values: { args, passed, refs,
  // self, resultAddress, scriptCaller }. passed: [{ name, original,
  // write(value) }] of those a script may change; refs: [name, type,
  // pointer] of ref/out ones; scriptCaller: the Blueprint position (Unreal).
  const context = invocation.context;
  const { info, layout } = hook;
  const found = { args: {}, passed: [], refs: [], self: null, resultAddress: null,
                  scriptCaller: null };
  if (info.frame) {
    // The engine finds them in its call frame (Unreal)
    const call = engine.callFrame(info, context);
    found.self = call.self === null ? null : objectJs(call.self);
    found.resultAddress = call.result;
    found.scriptCaller = call.caller;
    info.params.forEach((param, i) => {
      const location = call.args[i];
      if (location === null)
        return;
      const type = { ...param.type, byref: false };
      let original;
      try {
        original = location.address !== undefined ? readJs(type, location.address)
          : constantJs(type, location.value);
      } catch (_error) {
        return;  // a type scripts cannot read (FText, TMap ...): left out like unknown ones
      }
      found.args[param.name] = original;
      if (location.address !== undefined && param.type.byref)
        found.refs.push([param.name, param.type, location.address]);
      found.passed.push({ name: param.name, original, write: value => {
        if (location.address === undefined)
          throw grembyteError('error.monitor.blueprint_constant', { name: param.name });
        // From bytecode the address is the caller's own variable
        if (!call.own && !param.type.byref)
          throw grembyteError('error.monitor.blueprint_variable', { name: param.name });
        writeJs(type, location.address, value);
      } });
    });
    return found;
  }
  if (layout.self !== null) {
    const raw = rawValue(context, layout.self);
    found.self = info.selfStruct ? wrap(new ManagedObject(raw, info.selfStruct, true))
      : objectJs(raw);
  }
  info.params.forEach((param, i) => {
    const location = layout.args[i];
    if (location === null)
      return;
    const raw = rawValue(context, location);
    const original = registerJs(param.type, raw);
    found.args[param.name] = original;
    if (param.type.byref)
      found.refs.push([param.name, param.type, raw]);
    found.passed.push({ name: param.name, original,
                        write: value => writeArgument(context, location, param.type, value, raw) });
  });
  return found;
}

function hookBefore(hook, invocation) {
  // The "before the call" phase: the script may change arguments and fields.
  engine.drainMain();
  if (!hook.enabled)
    return;
  const context = invocation.context;
  const { args, passed, self, scriptCaller } = hookArguments(hook, invocation);
  const logs = [];
  const ctx = {
    phase: 'before',
    args,
    refs: {},
    this: self,
    caller: currentCaller(invocation.threadId, context.sp),
    result: undefined,
    params: hook.transform.params,
    state: hook.transform.state,
    method: hook.signature,
    hookId: hook.id,
    threadId: invocation.threadId,
    call: staticCall,
    static: staticField,
    find: findObject,
    forEach: forEachObject,
    outerName,
    log: (...items) => { logs.push(formatHookLog(items)); }
  };
  let error = null;
  const changed = beginChanges();
  try {
    hook.transform.fn(ctx);
    for (const { name, original, write } of passed) {
      const value = ctx.args[name];
      if (value !== original) {
        write(value);
        noteChange(name, value, original);
      }
    }
    hook.errorReported = false;
  } catch (thrown) {
    error = thrown;
  } finally {
    endChanges(changed);
  }
  const text = changesText(changed, false);
  const oldText = changesText(changed, true);
  const stats = hookStats.get(hook.id) || { count: 0 };
  stats.count++;
  stats.last = { old: oldText, result: text || '—', kept: changed.length === 0,
                 error: error === null ? null : String(error) };
  hookStats.set(hook.id, stats);
  if (logs.length > 0)
    pushHookLogs(hook, logs);
  if (error !== null && !hook.errorReported) {
    hook.errorReported = true;
    send({ kind: 'transform-error', hookId: hook.id, error: String(error) });
  }
  if (hook.debug)
    pushHookDebug(hook, {
      threadId: invocation.threadId,
      address: '',
      caller: debugCaller(invocation.returnAddress, ctx.caller, scriptCaller),
      old: oldText,
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
  const { args, refs, self, resultAddress, scriptCaller } = hookArguments(hook, invocation);
  const frame = {
    method: hook.signature,
    hookId: hook.id,
    args,
    this: self,
    caller: null
  };
  enterFrame(invocation.threadId, invocation.context.sp, frame);
  invocation.hookCall = { args, refs, frame, resultAddress, scriptCaller,
                          returnAddress: invocation.returnAddress };
}

function hookLeave(hook, invocation, retval) {
  const call = invocation.hookCall;
  if (call === undefined)
    return;
  leaveFrame(invocation.threadId, call.frame);
  const type = hook.info.returns;
  const isVoid = type.kind === T.VOID && !type.byref;
  let result;
  if (isVoid)
    result = undefined;
  else if (hook.info.frame)  // written where the caller asked for it (Unreal)
    result = call.resultAddress === null ? undefined : readJs(type, call.resultAddress);
  else
    result = isFloat(type) ? floatValue(resultRaw(invocation.context, retval, type), type.kind)
      : registerJs(type, resultRaw(invocation.context, retval, type));
  const refs = {};
  for (const [name, refType, pointer] of call.refs)
    refs[name] = readJs({ ...refType, byref: false }, pointer);
  const logs = [];
  const ctx = {
    phase: 'after',
    args: call.args,
    refs,
    this: call.frame.this,
    caller: call.frame.caller,
    result,
    params: hook.transform.params,
    state: hook.transform.state,
    method: hook.signature,
    hookId: hook.id,
    threadId: invocation.threadId,
    call: staticCall,
    static: staticField,
    find: findObject,
    forEach: forEachObject,
    outerName,
    log: (...items) => { logs.push(formatHookLog(items)); }
  };
  let returned;
  let error = null;
  try {
    returned = hook.transform.fn(ctx);
    if (returned !== undefined && returned !== null) {
      if (hook.info.frame)
        replaceFrameResult(hook, call, returned);
      else
        replaceResult(hook, invocation, returned);
    }
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
  if (logs.length > 0)
    pushHookLogs(hook, logs);
  if (error !== null && !hook.errorReported) {
    hook.errorReported = true;
    send({ kind: 'transform-error', hookId: hook.id, error: String(error) });
  }
  if (hook.debug)
    pushHookDebug(hook, {
      threadId: invocation.threadId,
      address: '',
      caller: debugCaller(call.returnAddress, ctx.caller, call.scriptCaller),
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

function formatHookLog(items) {
  const targets = [];
  const text = items.map(item => {
    if (!(item instanceof ManagedObject))
      return formatLog([item]);
    const target = { address: item.address.toString() };
    if (item.isStruct)
      target.klass = item.klass.toString();
    targets.push(target);
    return item.toString();
  }).join(' ');
  return targets.length === 0 ? text : { text, targets };
}

function formatScriptLog(items) {
  const targets = [];
  const text = items.map(item => {
    if (!(item instanceof ManagedObject))
      return formatLog([item]);
    const target = { address: item.address.toString() };
    if (item.isStruct)
      target.klass = item.klass.toString();
    targets.push(target);
    const name = item.isStruct ? null : item.name;
    if (name !== null)
      return `${item.className} ${JSON.stringify(name)}@${item.address}`;
    return item.toString();
  }).join(' ');
  return targets.length === 0 ? text : { text, targets };
}

function pushHookLogs(hook, lines) {
  const time = Date.now();
  for (const entry of lines) {
    if (hookLogs.length >= MAX_HOOK_LOGS)
      hookLogsDropped.set(hook.id, (hookLogsDropped.get(hook.id) || 0) + 1);
    else
      hookLogs.push({ hookId: hook.id, time,
                      ...(typeof entry === 'string' ? { text: entry } : entry) });
  }
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
    throw grembyteError('error.monitor.transform_not_function', { kind: spec.kind });
  return { kind: spec.kind, fn, params: spec.params || {}, state: {} };
}

async function addMethodHook(spec) {
  if (methodHooks.has(spec.id))
    throw grembyteError('error.monitor.hook_exists', { id: spec.id });
  const transform = compileHookTransform(spec.transform);
  const info = await engine.prepare(spec.identity);
  if (methodHooks.has(spec.id))
    throw grembyteError('error.monitor.hook_exists', { id: spec.id });
  const hook = {
    id: spec.id, info, layout: info.frame ? null : argumentLayout(info), transform,
    enabled: spec.enabled,
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
    // Unreal: which arguments are unknown shows only per call (missing in ctx.args)
    undecoded: info.frame ? 0 : hook.layout.args.filter(location => location === null).length,
    viaStack: hook.listener.viaStack
  };
}

function reportHookError(hook, error) {
  if (!hook.errorReported) {
    hook.errorReported = true;
    send({ kind: 'transform-error', hookId: hook.id, error: String(error) });
  }
}

function attachHook(hook) {
  // Before the call only the start's probe.
  if (hook.phase === 'before') {
    hook.listener = hookCalls(hook.info, invocation => {
      try {
        hookBefore(hook, invocation);
      } catch (error) {
        reportHookError(hook, error);
      }
    });
    return;
  }
  hook.listener = hookCalls(hook.info, invocation => {
    try {
      hookEnter(hook, invocation);
    } catch (error) {
      reportHookError(hook, error);
    }
  }, (invocation, retval) => {
    try {
      hookLeave(hook, invocation, retval);
    } catch (error) {
      reportHookError(hook, error);
    }
  });
}

function removeMethodHook(id) {
  const hook = methodHooks.get(id);
  if (hook === undefined)
    return false;
  hook.listener.detach();
  methodHooks.delete(id);
  engine.releaseCode(hook.info.start);
  updateUnmapWatch();
  return true;
}

function requireMethodHook(id) {
  const hook = methodHooks.get(id);
  if (hook === undefined)
    throw grembyteError('error.monitor.no_hook', { id });
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
  if (hookLogs.length > 0 || hookLogsDropped.size > 0) {
    send({ kind: 'hook-logs', lines: hookLogs.splice(0),
           dropped: Object.fromEntries(hookLogsDropped) });
    hookLogsDropped.clear();
  }
}

// --- Scripts: game code run by time (see grembyte/features/scripts.py) -------------
// A script is a function body run with ``ctx`` on the game thread (see
// onGameThread in script_native.js): once some seconds after it is added, every some
// seconds, or only when asked (``mode``: once, repeat, manual). A repeating
// script waits for its run to end before the next is timed. ``args`` are
// ctx.args of the timed runs (its parameters' defaults), ``options`` the
// function bodies that give its choice parameters' options.

const scripts = new Map();

function compileScript(source) {
  return new Function('ctx', source);
}

function compiledSpec(spec) {
  const options = new Map();
  for (const [name, source] of Object.entries(spec.options || {}))
    options.set(name, compileScript(source));
  return { fn: compileScript(spec.source), mode: spec.mode, seconds: spec.seconds,
           enabled: spec.enabled, args: spec.args || {}, options };
}

function addScript(spec) {
  if (scripts.has(spec.id))
    throw grembyteError('error.monitor.script_exists', { id: spec.id });
  const script = { id: spec.id, ...compiledSpec(spec), state: {}, runs: 0,
                   timer: null, running: false, removed: false };
  scripts.set(spec.id, script);
  timeScript(script);
  return true;
}

function updateScript(id, spec) {
  // New code or timing; the script's ctx.state stays.
  const script = requireScript(id);
  Object.assign(script, compiledSpec(spec));
  timeScript(script);
  return true;
}

function removeScript(id) {
  const script = scripts.get(id);
  if (script === undefined)
    return false;
  script.removed = true;
  clearTimeout(script.timer);
  scripts.delete(id);
  return true;
}

function requireScript(id) {
  const script = scripts.get(id);
  if (script === undefined)
    throw grembyteError('error.monitor.no_script', { id });
  return script;
}

function timeScript(script) {
  clearTimeout(script.timer);
  script.timer = null;
  if (!script.enabled || script.removed || script.mode === 'manual')
    return;
  if (script.mode === 'once' && script.runs > 0)
    return;
  script.timer = setTimeout(() => {
    script.timer = null;
    runScript(script);
  }, Math.max(0, script.seconds * 1000));
}

function scriptContext(script, logs, args) {
  return {
    find: findObject,
    forEach: forEachObject,
    outerName,
    findClass: scriptClass,
    call: staticCall,
    static: staticField,
    gas: scriptGas,
    native: nativeContext,
    lang: GREMBYTE_LANGUAGE,
    args,
    log: (...items) => { logs.push(formatScriptLog(items)); },
    state: script.state,
    runs: script.runs,
    scriptId: script.id
  };
}

function scriptClass(name) {
  // ctx.findClass("ItFo_Potion_Health_03"): an Unreal class as an object,
  // e.g. for a TSubclassOf parameter
  if (engine.id !== 'unreal')
    throw grembyteError('error.monitor.find_class_unreal_only');
  return objectJs(engine.findClass(String(name)));
}

function runScript(script, args = null) {
  // Run it on the game thread with ``args`` as ctx.args (else the
  // defaults); reports 'script-run'.
  if (script.running)
    return false;
  script.running = true;
  const logs = [];
  const ctx = scriptContext(script, logs, args === null ? { ...script.args } : args);
  const started = Date.now();
  const report = (result, error) => {
    script.running = false;
    script.runs++;
    send({ kind: 'script-run', scriptId: script.id, time: started, runs: script.runs,
           result: error === null ? result.text : '',
           target: error === null ? result.target : undefined, logs,
           gameThread: nativeThreadNews(),
           error: error === null ? null
             : (error && error.stack ? `${error}\n${error.stack}` : String(error)) });
    if (script.mode === 'repeat' || script.mode === 'once')
      timeScript(script);
  };
  onGameThread(() => scriptResult(script.fn(ctx))).then(
    result => report(result, null), error => report(undefined, error));
  return true;
}

function scriptOptions(id, name) {
  // The options of a script's choice parameter: [{ label, value }], from its
  // code on the game thread (values: strings, numbers or bools)
  const script = requireScript(id);
  const fn = script.options.get(name);
  if (fn === undefined)
    throw grembyteError('error.monitor.no_options', { name });
  return onGameThread(() => {
    const list = fn(scriptContext(script, [], { ...script.args }));
    if (!Array.isArray(list))
      throw grembyteError('error.monitor.options_array', { name });
    return list.map(option => {
      const plain = option === null || typeof option !== 'object';
      const value = plain ? option : option.value;
      if (!['string', 'number', 'boolean'].includes(typeof value))
        throw grembyteError('error.monitor.option_value', { name, option: displayText(option) });
      const label = plain || option.label === undefined || option.label === null
        ? String(value) : String(option.label);
      return { label, value };
    });
  });
}

// --- Gameplay Ability System (Unreal): the player's attributes ------------------------
// See the Unreal adapter (gasAttributes, gasSetBase). Names are "Attribute"
// or "Set.Attribute", e.g. "AttributeSetCore.DamageOutgoingMult". A set is
// also found without the number Unreal may give its object anew on every
// load: "AttributeSet_Health" finds "AttributeSet_Health_2147445233".

function gasEngine(name) {
  if (engine.id !== 'unreal')
    throw grembyteError('error.monitor.gas_unreal_only');
  return engine[name];
}

function gasAttributes() {
  // The player's attribute sets, or null without a player. Memory reads
  // only: on the agent's thread, also while the game is paused.
  return gasEngine('gasAttributes')();
}

function gasSetBase(name, value) {
  // Set an attribute's base value through the game, on the game thread
  return engine.onMainThread(() => gasEngine('gasSetBase')(name, value));
}

function gasSetMatches(setName, wanted) {
  // Whether set ``setName`` is the one named ``wanted`` (see above)
  return setName === wanted || setName.replace(/_\d+$/, '') === wanted;
}

function gasValue(name) {
  // ctx.gas.get(name): { base, current } of the player's attribute, or null
  // without a player
  name = String(name);
  const state = gasAttributes();
  if (state === null)
    return null;
  const dot = name.indexOf('.');
  const wantedSet = dot < 0 ? null : name.substring(0, dot);
  const wanted = dot < 0 ? name : name.substring(dot + 1);
  for (const set of state.sets) {
    if (wantedSet !== null && !gasSetMatches(set.name, wantedSet))
      continue;
    const attribute = set.attributes.find(entry => entry.name === wanted);
    if (attribute !== undefined)
      return { base: attribute.base, current: attribute.current };
  }
  throw grembyteError('error.monitor.no_attribute', { name });
}

function gasFindSet(pattern) {
  // ctx.gas.findSet(/^AttributeSet_Health_/): the name of the player's first
  // set the regular expression (a RegExp or its text) matches, or null
  // (also without a player)
  const regex = pattern instanceof RegExp ? pattern : new RegExp(String(pattern));
  const state = gasAttributes();
  if (state === null)
    return null;
  const set = state.sets.find(entry => {
    regex.lastIndex = 0;
    return regex.test(entry.name);
  });
  return set === undefined ? null : set.name;
}

// --- Object browser: the live objects by class (see the adapters' browseClasses) --------
// Memory reads only: on the agent's thread, also while the game is paused.

function browseClasses(options) {
  return engine.browseClasses(options || {});
}

function browseInstances(klass, options) {
  return engine.browseInstances(ptr(klass), options || {});
}

// ctx.gas in scripts (they run on the game thread)
const scriptGas = {
  attributes: gasAttributes,
  get: gasValue,
  findSet: gasFindSet,
  setBase(name, value) {
    const result = gasEngine('gasSetBase')(name, value);
    noteChange(name, value);
    return result;
  }
};

function scriptResult(value) {
  // What a script returned: { text, target? } (target: an object or struct
  // the inspector can open). Read on the game thread, where Unity may be
  // asked for an object's name.
  if (!(value instanceof ManagedObject))
    return { text: displayText(value) };
  if (!value.isStruct)
    return objectValue(value.address, true);
  return { text: `${engine.layout(value.klass).name}@${value.address}`,
           target: { address: value.address.toString(), klass: value.klass.toString() } };
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
  fullText,
  evaluate,
  describeCall,
  callStatic,
  callFound,
  listMethods,
  describeMethod,
  callOn,
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
  gasAttributes,
  gasSetBase,
  browseClasses,
  browseInstances,
  addScript,
  updateScript,
  removeScript,
  runScriptNow: (id, args) => runScript(requireScript(id), args === undefined ? null : args),
  scriptOptions,
  dispose() {
    clearInterval(flushTimer);
    for (const id of Array.from(scripts.keys()))
      removeScript(id);
    disposeNative();
    for (const id of Array.from(monitors.keys()))
      removeMonitor(id);
    for (const id of Array.from(methodHooks.keys()))
      removeMethodHook(id);
    Interceptor.flush();
    flush();
    return true;
  }
};
