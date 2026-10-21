const VALUE_IO = {
  int8: {
    size: 1,
    read: p => p.readS8(),
    write: (p, v) => p.writeS8(v),
    limits: [-128, 127]
  },
  int16: {
    size: 2,
    read: p => p.readS16(),
    write: (p, v) => p.writeS16(v),
    limits: [-32768, 32767]
  },
  int32: {
    size: 4,
    read: p => p.readS32(),
    write: (p, v) => p.writeS32(v),
    limits: [-2147483648, 2147483647]
  },
  int64: {
    size: 8,
    read: p => p.readS64().toNumber(),
    write: (p, v) => p.writeS64(int64(String(v))),
    limits: [Number.MIN_SAFE_INTEGER, Number.MAX_SAFE_INTEGER]
  },
  uint8: {
    size: 1,
    read: p => p.readU8(),
    write: (p, v) => p.writeU8(v),
    limits: [0, 255]
  },
  uint16: {
    size: 2,
    read: p => p.readU16(),
    write: (p, v) => p.writeU16(v),
    limits: [0, 65535]
  },
  uint32: {
    size: 4,
    read: p => p.readU32(),
    write: (p, v) => p.writeU32(v),
    limits: [0, 4294967295]
  },
  uint64: {
    size: 8,
    read: p => p.readU64().toNumber(),
    write: (p, v) => p.writeU64(uint64(String(v))),
    limits: [0, Number.MAX_SAFE_INTEGER]
  },
  float: {
    size: 4,
    read: p => p.readFloat(),
    write: (p, v) => p.writeFloat(v),
    limits: null
  },
  double: {
    size: 8,
    read: p => p.readDouble(),
    write: (p, v) => p.writeDouble(v),
    limits: null
  }
};

const MAX_INSTRUCTION_SIZE = 15;
const SYNC_WINDOW = 48;
const MASK64 = (1n << 64n) - 1n;
const JMP_SIZE = 5;
const NEAR_DISTANCE = 0x7ff00000;
const RED_ZONE = 128;
const FXSAVE_AREA = 512;
const FXSAVE64_RSP = [0x48, 0x0f, 0xae, 0x04, 0x24];
const FXRSTOR64_RSP = [0x48, 0x0f, 0xae, 0x0c, 0x24];
const SAVED_REGS = [
  'rax', 'rcx', 'rdx', 'rbx', 'rsi', 'rdi', 'rbp',
  'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15'
];
const MAX_CALLER_DEPTH = 4;
const CALLER_SCAN_SLOTS = 256;
const MAX_CALLERS = 64;
const CALL_SIZES = [5, 2, 3, 6, 7, 4];
const MAX_DEBUG_EVENTS = 200;
// Largest distance from a function's start to code in it (for code without symbols).
const MAX_FUNCTION_SIZE = 0x40000;
// Jumps followed from a call's target: PLT stub, thunk, a patch of another tool.
const THUNK_HOPS = 3;
const WINDOWS = Process.platform === 'windows';
const callCache = new Map();
const startCache = new Map();
const verifiedCache = new Map();
const frameCache = new Map();
// Instructions of a prologue looked at for its frame size
const PROLOGUE_INSTRUCTIONS = 24;
const debugEvents = [];
const debugDropped = new Map();
const hooks = new Map();
const sites = new Map();
const retiredSites = [];
// Code bytes kept on each side of a write instruction to recognize it in
// JIT code, which is generated anew (elsewhere) on every start.
const CONTEXT_SIZE = 32;
// Hardware watchpoint slots for training; slot 0 is used by the write watch.
const TRAINING_SLOTS = [1, 2, 3];
// Share of context bytes that must match; JIT code embeds absolute
// addresses that differ between runs.
const TRAINING_MIN_SIMILARITY = 0.6;
const trainings = new Map();
const trainingThreads = new Set();
const trainingHits = [];
let trainingTimer = null;
let trainingScheduled = false;
// While a write watch runs, trainings rest: enumerating threads while one of
// them handles the watch's trap deadlocks Frida (and the game).
let trainingPaused = false;
let trainingHandlerInstalled = false;

function toBigInt(value) {
  return BigInt(ptr(value).toString()) & MASK64;
}

function writeOperand(ins) {
  for (const op of ins.operands || []) {
    if (op.type === 'mem' && typeof op.access === 'string' && op.access.includes('w'))
      return op;
  }
  return null;
}

function staticAddress(ins, op) {
  const mem = op.value;
  if (mem.base !== 'rip' || mem.index !== undefined)
    return null;
  return (toBigInt(ins.next) + BigInt(mem.disp)) & MASK64;
}

function registerValue(regs, name) {
  if (name === undefined)
    return 0n;
  let value = regs[name];
  let truncate = false;
  if (value === undefined) {
    // 32-bit addressing on x86-64: eax -> rax, r8d -> r8
    const wide = /^e[a-z]{2}$/.test(name) ? 'r' + name.slice(1)
      : /^r\d+d$/.test(name) ? name.slice(0, -1) : null;
    value = wide === null ? undefined : regs[wide];
    truncate = true;
  }
  if (value === undefined)
    return null;
  const result = toBigInt(value);
  return truncate ? result & 0xffffffffn : result;
}

function effectiveAddress(ins, op, regs) {
  const mem = op.value;
  if (mem.segment !== undefined && mem.segment !== 'ds' && mem.segment !== 'ss')
    return null;
  const base = mem.base === 'rip' ? toBigInt(ins.next) : registerValue(regs, mem.base);
  const index = registerValue(regs, mem.index);
  if (base === null || index === null)
    return null;
  return (base + index * BigInt(mem.scale || 1) + BigInt(mem.disp || 0)) & MASK64;
}

function overlaps(start, size, targetStart, targetSize) {
  return start < targetStart + BigInt(targetSize) && targetStart < start + BigInt(size);
}

function syncVotes(start) {
  // x86 decoding self-synchronizes: count how many earlier decode chains land
  // exactly on this candidate's start instead of jumping over it.
  let votes = 0;
  for (let offset = SYNC_WINDOW; offset > 0; offset--) {
    let cursor = start.sub(offset);
    try {
      while (cursor.compare(start) < 0)
        cursor = Instruction.parse(cursor).next;
    } catch (_error) {
      continue;
    }
    if (cursor.equals(start))
      votes += 1;
  }
  return votes;
}

function toPointer(value) {
  return ptr('0x' + value.toString(16));
}

function hexBytes(buffer) {
  return Array.from(new Uint8Array(buffer), b => b.toString(16).padStart(2, '0')).join('');
}

function describeInstruction(site) {
  const ins = site.writeIns;
  const module = Process.findModuleByAddress(ins.address);
  const offset = module ? ins.address.sub(module.base) : null;
  return {
    address: ins.address.toString(),
    site: module ? `${module.name}+${offset}` : ins.address.toString(),
    module: module ? module.name : null,
    moduleOffset: module ? offset.toString() : null,
    bytes: site.bytes,
    context: site.context,
    text: ins.toString(),
    size: ins.size,
    method: engine.name(ins.address)
  };
}

function readHex(address, size) {
  try {
    return hexBytes(address.readByteArray(size));
  } catch (_error) {
    return null;
  }
}

function codeContext(ins) {
  // Shorter at the start or end of mapped code.
  let before = '';
  let after = '';
  for (let size = CONTEXT_SIZE; size > 0 && before === ''; size -= 8)
    before = readHex(ins.address.sub(size), size) || '';
  for (let size = CONTEXT_SIZE; size > 0 && after === ''; size -= 8)
    after = readHex(ins.next, size) || '';
  return { before, after };
}

function describeFrame(address) {
  const module = Process.findModuleByAddress(address);
  return module ? [module.name, parseInt(address.sub(module.base).toString(), 16)] : null;
}

function remember(cache, key, value) {
  if (cache.size >= 4096)
    cache.clear();
  cache.set(key, value);
  return value;
}

function callBefore(address) {
  // The call a return address follows: { target } (null for an indirect call), or null.
  const key = address.toString();
  const known = callCache.get(key);
  if (known !== undefined)
    return known;
  let result = null;
  const range = Process.findRangeByAddress(address);
  if (range !== null && range.protection.includes('x')) {
    for (const size of CALL_SIZES) {
      try {
        const ins = Instruction.parse(address.sub(size));
        if (ins.mnemonic === 'call' && ins.next.equals(address)) {
          const operand = ins.operands[0];
          result = { target: operand.type === 'imm' ? ptr(operand.value.toString()) : null };
          break;
        }
      } catch (_error) {
      }
    }
  }
  return remember(callCache, key, result);
}

function jumpTargets(target) {
  // The target and where jumps from it lead: a PLT stub, a thunk, a patch.
  const targets = [target];
  let at = target;
  for (let hop = 0; hop < THUNK_HOPS; hop++) {
    try {
      let ins = Instruction.parse(at);
      if (ins.mnemonic === 'endbr64')
        ins = Instruction.parse(ins.next);
      if (!ins.mnemonic.endsWith('jmp'))
        break;
      const operand = ins.operands[0];
      if (operand.type === 'imm')
        at = ptr(operand.value.toString());
      else if (operand.type === 'mem' && operand.value.base === 'rip' && !operand.value.index)
        at = ins.next.add(operand.value.disp).readPointer();
      else
        break;
    } catch (_error) {
      break;
    }
    targets.push(at);
  }
  return targets;
}

function functionStart(address) {
  // Where the function holding ``address`` starts, by its symbol, or null.
  // Frida names an address inside a function "name+0x…", one without a symbol "0x…".
  if (WINDOWS)
    return null;  // dbghelp under Wine: unreliable
  const key = address.toString();
  const known = startCache.get(key);
  if (known !== undefined)
    return known;
  let start = null;
  try {
    const name = DebugSymbol.fromAddress(address).name;
    if (name !== null && !name.startsWith('0x')) {
      const inside = /\+0x([0-9a-f]+)$/.exec(name);
      start = inside === null ? address : address.sub(parseInt(inside[1], 16));
    }
  } catch (_error) {
  }
  return remember(startCache, key, start);
}

function sameRegion(a, b) {
  // Both in the same module, or both in generated code (JIT).
  const module = Process.findModuleByAddress(a);
  if (module === null)
    return Process.findModuleByAddress(b) === null;
  return b.compare(module.base) >= 0 && b.compare(module.base.add(module.size)) < 0;
}

function leadsTo(target, address) {
  // Whether a call of ``target`` can have run the code at ``address``.
  if (!sameRegion(address, target) || target.compare(address) > 0
      || address.sub(target).compare(ptr(MAX_FUNCTION_SIZE)) > 0)
    return false;
  const start = functionStart(address);
  return start === null || target.compare(start) >= 0;
}

function calledStart(returnAddress, address) {
  // The function a call before ``returnAddress`` ran to reach ``address``:
  // its start for a direct call, true for an indirect one, null if it did not.
  const call = callBefore(returnAddress);
  if (call === null)
    return null;  // most stack slots: no return address at all
  const key = `${returnAddress}>${address}`;
  const known = verifiedCache.get(key);
  if (known !== undefined)
    return known;
  let result = null;
  if (call.target === null) {
    if (sameRegion(returnAddress, address) || Process.findModuleByAddress(address) === null)
      result = true;
  } else {
    result = jumpTargets(call.target).find(target => leadsTo(target, address)) || null;
  }
  return remember(verifiedCache, key, result);
}

function frameSize(start, stop) {
  // Stack a function's prologue takes below its return address before ``stop``,
  // at least; null if it realigns the stack.
  const key = `${start}>${stop}`;
  const known = frameCache.get(key);
  if (known !== undefined)
    return known;
  let size = 0;
  let at = start;
  try {
    for (let i = 0; i < PROLOGUE_INSTRUCTIONS && at.compare(stop) < 0; i++) {
      const ins = Instruction.parse(at);
      const [first, second] = ins.operands;
      const toRsp = first !== undefined && first.type === 'reg' && first.value === 'rsp';
      if (ins.mnemonic === 'push') {
        size += Process.pointerSize;
      } else if (ins.mnemonic === 'sub' && toRsp && second.type === 'imm') {
        size += Number(second.value.toString());
        break;  // the frame is complete
      } else if (ins.mnemonic === 'and' && toRsp) {
        size = null;  // realigned: the distance is not fixed
        break;
      } else if (ins.mnemonic !== 'endbr64'
                 && !(ins.mnemonic === 'mov' && first.type === 'reg' && second.type === 'reg')) {
        break;  // the body begins
      }
      at = ins.next;
    }
  } catch (_error) {
  }
  return remember(frameCache, key, size);
}

function callerFrame(frameRsp, inside) {
  // The return address of the function running at ``inside`` with its stack
  // at ``frameRsp``, and the slot holding it; null if not found.
  const symbolStart = functionStart(inside);
  const known = symbolStart === null ? null : frameSize(symbolStart, inside);
  // A known frame is skipped: its locals only hold stale return addresses,
  // and a large one (kilobytes of locals) would use up the slots to scan.
  let cursor = known === null ? frameRsp : frameRsp.add(known);
  for (let slot = 0; slot < CALLER_SCAN_SLOTS; slot++, cursor = cursor.add(Process.pointerSize)) {
    let value;
    try {
      value = cursor.readPointer();
    } catch (_error) {
      return null;
    }
    const start = calledStart(value, inside);
    if (start === null)
      continue;
    if (known === null && start !== true) {
      const below = frameSize(start, inside);
      if (below !== null && cursor.sub(frameRsp).compare(ptr(below)) < 0)
        continue;  // inside the frame the call made
    }
    return { value, slot: cursor };
  }
  return null;
}

function callerSignature(rsp, depth, pc) {
  // The return addresses of the ``depth`` calls that led to ``pc``, innermost first.
  const frames = [];
  let inside = pc;
  let frameRsp = rsp;
  while (frames.length < depth) {
    const found = callerFrame(frameRsp, inside);
    if (found === null)
      return null;
    frames.push(found.value.toString());
    inside = found.value;
    frameRsp = found.slot.add(Process.pointerSize);  // the caller's after the return
  }
  return frames.join(',');
}

function plausibleCallers(addresses, pc) {
  // Whether learned frames form a chain of calls that leads to ``pc``.
  let inside = pc;
  for (const address of addresses) {
    const frame = ptr(address);
    if (calledStart(frame, inside) === null)
      return false;
    inside = frame;
  }
  return true;
}

function resolveCallers(callers, pc) {
  // Learned callers by key; those that fail the check are dropped (see the top).
  const keys = new Map();
  let dropped = 0;
  for (const frames of callers || []) {
    const addresses = [];
    for (const [name, offset] of frames) {
      const module = Process.findModuleByName(name);
      if (module === null)
        break;
      addresses.push(module.base.add(offset).toString());
    }
    if (addresses.length !== frames.length)
      continue;
    if (plausibleCallers(addresses, pc))
      keys.set(addresses.join(','), frames);
    else
      dropped++;
  }
  return { keys, dropped };
}

function reportCallers(hook) {
  send({ kind: 'callers', hookId: hook.id, callers: Array.from(hook.callers.values()) });
}

function reportAmbiguous(hook) {
  if (hook.ambiguousReported)
    return;
  hook.ambiguousReported = true;
  send({ kind: 'callers-ambiguous', hookId: hook.id });
}

function learnCaller(hook, key, hit) {
  if (key === null)
    return;
  if (!hit) {
    if (hook.callers.has(key))
      reportAmbiguous(hook);
    else if (hook.foreignCallers.size < MAX_CALLERS)
      hook.foreignCallers.add(key);
    return;
  }
  if (hook.callers.has(key) || hook.callers.size >= MAX_CALLERS)
    return;
  hook.callers.set(key, key.split(',').map(address => describeFrame(ptr(address))));
  if (hook.foreignCallers.has(key))
    reportAmbiguous(hook);
  reportCallers(hook);
}

function moduleAddress(spec) {
  const module = Process.findModuleByName(spec.module);
  if (module === null)
    throw new Error(`Module ${spec.module} is not loaded`);
  const address = module.base.add(spec.moduleOffset);
  if (spec.instructionBytes !== null && !sites.has(address.toString())) {
    const found = hexBytes(address.readByteArray(spec.instructionBytes.length / 2));
    if (found !== spec.instructionBytes)
      throw new Error(
        `Code at ${spec.module}+0x${spec.moduleOffset.toString(16)} has changed `
        + `(expected ${spec.instructionBytes}, found ${found}); the program was probably updated`);
  }
  return address;
}

function resolveWriteInstruction(trapPc, target, size) {
  // Patched sites no longer decode as the original code.
  for (const site of sites.values()) {
    if (site.writeIns.next.equals(trapPc))
      return { ins: site.writeIns, op: site.writeOp };
  }
  const targetValue = toBigInt(target);
  const candidates = [];
  for (let back = 1; back <= MAX_INSTRUCTION_SIZE; back++) {
    let ins;
    try {
      ins = Instruction.parse(trapPc.sub(back));
    } catch (_error) {
      continue;
    }
    if (!ins.next.equals(trapPc))
      continue;
    const op = writeOperand(ins);
    if (op === null)
      continue;
    const fixed = staticAddress(ins, op);
    candidates.push({
      ins,
      op,
      staticMatch: fixed !== null && overlaps(fixed, op.size, targetValue, size),
      sizeMatch: op.size === size,
      votes: syncVotes(ins.address)
    });
  }
  if (candidates.length === 0)
    throw new Error(`No memory-writing instruction ends at ${trapPc}`);
  candidates.sort((a, b) =>
    (b.staticMatch - a.staticMatch) || (b.votes - a.votes) || (b.sizeMatch - a.sizeMatch));
  return candidates[0];
}

function compileTransform(spec) {
  const fn = (0, eval)('(' + spec.source + '\n)');
  if (typeof fn !== 'function')
    throw new Error(`Transform '${spec.kind}' does not evaluate to a function`);
  return { kind: spec.kind, fn, params: spec.params || {}, state: {} };
}

function normalize(value, io) {
  if (typeof value !== 'number' || !Number.isFinite(value))
    throw new Error(`Transform returned ${value}, expected a finite number`);
  if (io.limits === null)
    return value;
  return Math.min(io.limits[1], Math.max(io.limits[0], Math.trunc(value)));
}

function errorText(error) {
  return error && error.stack ? `${error}\n${error.stack}` : String(error);
}

function formatLog(args) {
  return args.map(arg => {
    if (typeof arg === 'string')
      return arg;
    try {
      return JSON.stringify(arg);
    } catch (_error) {
      return String(arg);
    }
  }).join(' ');
}

function frameText(address) {
  // Runs on the game thread, which Mono needs to name its JIT code.
  const frame = describeFrame(address);
  if (frame)
    return `${frame[0]}+0x${frame[1].toString(16)}`;
  return engine.describe(address) || address.toString();
}

function nameSite(site) {
  // Mono only names JIT code for its own threads, i.e. the game's.
  const location = engine.locate(site.writeIns.address);
  if (location === undefined)
    return;
  site.method = location === null ? null : engine.name(site.writeIns.address);
  if (site.method !== null)
    send({
      kind: 'site-method',
      hookIds: Array.from(site.hooks.keys()),
      method: site.method,
      managed: engine.identify(site.writeIns.address)
    });
}

function callerText(hook, write) {
  if (hook.callerDepth === 0)
    return null;
  const key = write.signature(hook.callerDepth);
  return key === null ? null : key.split(',').map(a => frameText(ptr(a))).join(' ← ');
}

function pushDebug(hook, entry) {
  if (debugEvents.length >= MAX_DEBUG_EVENTS) {
    debugDropped.set(hook.id, (debugDropped.get(hook.id) || 0) + 1);
    return;
  }
  entry.hookId = hook.id;
  entry.time = Date.now();
  debugEvents.push(entry);
}

function reportError(hook, error) {
  if (hook.errorReported)
    return;
  hook.errorReported = true;
  send({ kind: 'transform-error', hookId: hook.id, error: String(error) });
}

// Holds the bytes a hook is about to write, to compare them with the target.
const scratch = Memory.alloc(8);
const readBuffer = Memory.alloc(8);

// A watchpoint on the target (write or access watch) also traps the hook's
// own accesses. Frida reports a trap inside its guarded memory access as an
// error, although the access has happened. Native copies run outside that
// guard, where the watch agent handles the trap; its hits in this code are
// dropped by the UI (see 'ready').
const ACCESS_STUB_CODE = {
  1: [0x8a, 0x06, 0x88, 0x07, 0xc3],                  // mov al, [rsi]; mov [rdi], al
  2: [0x66, 0x8b, 0x06, 0x66, 0x89, 0x07, 0xc3],
  4: [0x8b, 0x06, 0x89, 0x07, 0xc3],
  8: [0x48, 0x8b, 0x06, 0x48, 0x89, 0x07, 0xc3]
};
const accessStub = Memory.alloc(Process.pageSize);
const nativeCopy = {};
Object.entries(ACCESS_STUB_CODE).forEach(([size, code], index) => {
  const at = accessStub.add(16 * index);
  at.writeByteArray(code);
  nativeCopy[size] = new NativeFunction(at, 'void', ['pointer', 'pointer'],
    { exceptions: 'propagate' });
});
Memory.protect(accessStub, Process.pageSize, 'r-x');

function isWatchTrap(error) {
  return /breakpoint|single.?step/i.test(String(error));
}

// Debug view of a write the hook skips: null if the value cannot be read.
function peekTarget(io, target) {
  try {
    return readTarget(io, target);
  } catch (_error) {
    return null;
  }
}

function readTarget(io, target) {
  try {
    return io.read(target);
  } catch (error) {
    if (!isWatchTrap(error))
      throw error;
    // The trap proves the address is mapped, so the native read is safe.
    nativeCopy[io.size](readBuffer, target);
    return io.read(readBuffer);
  }
}

function writeValue(io, target, value) {
  try {
    io.write(target, value);
  } catch (error) {
    // A watchpoint traps right after the write: check the value is stored.
    if (!isWatchTrap(error))
      throw error;
    io.write(scratch, value);
    const expected = new Uint8Array(scratch.readByteArray(io.size));
    nativeCopy[io.size](readBuffer, target);
    const stored = new Uint8Array(readBuffer.readByteArray(io.size));
    if (stored.some((byte, i) => byte !== expected[i]))
      throw error;
  }
}

function applyTransform(hook, threadId, pending) {
  const { target, old: oldValue } = pending;
  const current = readTarget(hook.io, target);
  const logs = hook.debug ? [] : null;
  const ctx = {
    old: oldValue,
    new: current,
    delta: current - oldValue,
    params: hook.transform.params,
    state: hook.transform.state,
    valueType: hook.valueType,
    address: target.toString(),
    writePc: hook.site.writeIns.address.toString(),
    threadId,
    hookId: hook.id,
    log: logs === null ? () => {} : (...args) => { logs.push(formatLog(args)); }
  };
  const entry = logs === null ? null : {
    threadId,
    address: target.toString(),
    caller: pending.caller,
    old: oldValue,
    new: current,
    logs
  };
  let result;
  try {
    result = hook.transform.fn(ctx);
    if (entry !== null)
      entry.kept = result === undefined || result === null;
    if (result === undefined || result === null) {
      result = current;
    } else {
      result = normalize(result, hook.io);
      if (result !== current)
        writeValue(hook.io, target, result);
    }
    hook.errorReported = false;
  } catch (error) {
    if (entry !== null) {
      entry.error = errorText(error);
      pushDebug(hook, entry);
    }
    reportError(hook, error);
    return;
  }
  if (entry !== null) {
    entry.result = result;
    pushDebug(hook, entry);
  }
  hook.stats.count += 1;
  hook.stats.last = { old: oldValue, new: current, result, threadId, address: target.toString() };
}

function emitCallback(w, callback) {
  // Preserve everything the game might have live: red zone, flags, all GPRs
  // and the SSE state (the JS callback is free to clobber xmm registers).
  w.putLeaRegRegOffset('rsp', 'rsp', -RED_ZONE);
  w.putPushfx();
  for (const reg of SAVED_REGS)
    w.putPushReg(reg);
  w.putMovRegReg('rbx', 'rsp');
  w.putAndRegU32('rsp', 0xfffffff0);
  w.putSubRegImm('rsp', FXSAVE_AREA);
  w.putBytes(FXSAVE64_RSP);
  w.putCallAddressWithArguments(callback, ['rbx']);
  w.putBytes(FXRSTOR64_RSP);
  w.putMovRegReg('rsp', 'rbx');
  for (const reg of SAVED_REGS.slice().reverse())
    w.putPopReg(reg);
  w.putPopfx();
  w.putLeaRegRegOffset('rsp', 'rsp', RED_ZONE);
}

function readRegisters(block) {
  const regs = {};
  SAVED_REGS.forEach((name, i) => {
    regs[name] = block.add(8 * (SAVED_REGS.length - 1 - i)).readPointer();
  });
  regs.rsp = block.add(8 * SAVED_REGS.length + 8 + RED_ZONE);
  return regs;
}

function reportAgentError(where, error) {
  send({ kind: 'agent-error', error: `${where}: ${error}` });
}

// Both return { target } for writes the hook handles, else { skip: reason }.
function fixedTarget(hook, write) {
  if (!hook.filterAddress)
    return { target: hook.target };
  const address = write.address();
  if (address === null)
    return { target: hook.target };
  const hit = overlaps(address, write.site.writeOp.size, hook.targetValue, hook.io.size);
  if (hook.callerDepth > 0)
    learnCaller(hook, write.signature(hook.callerDepth), hit);
  return hit ? { target: hook.target } : { skip: 'address' };
}

function dynamicTarget(hook, write) {
  if (hook.callerDepth > 0 && hook.callers.size > 0
      && !hook.callers.has(write.signature(hook.callerDepth)))
    return { skip: 'caller' };
  const address = write.address();
  if (address === null) {
    reportError(hook, new Error(`Cannot compute the address written by ${write.site.writeIns}`));
    return { skip: 'no-address' };
  }
  return { target: toPointer(address) };
}

function resolvePointer(pointer) {
  if (pointer.base === null) {
    const module = Process.findModuleByName(pointer.module);
    if (module === null)
      return null;
    pointer.base = module.base;
  }
  try {
    let address = pointer.base.add(pointer.baseOffset);
    for (const offset of pointer.offsets) {
      const next = address.readPointer();
      if (next.isNull())
        return null;
      address = next.add(offset);
    }
    return address;
  } catch (_error) {
    return null;
  }
}

function pointerTarget(hook, write) {
  const target = resolvePointer(hook.pointer);
  if (target === null)
    return { skip: 'pointer' };
  const address = write.address();
  if (address !== null
      && !overlaps(address, write.site.writeOp.size, toBigInt(target), hook.io.size))
    return { skip: 'address' };
  return { target };
}

function pendingWrite(site, block) {
  // Registers, address and caller signatures are computed at most once per write.
  let regs = null;
  let address;
  const signatures = new Map();
  const registers = () => (regs === null ? (regs = readRegisters(block)) : regs);
  return {
    site,
    address() {
      if (address === undefined)
        address = effectiveAddress(site.writeIns, site.writeOp, registers());
      return address;
    },
    signature(depth) {
      if (!signatures.has(depth))
        signatures.set(depth, callerSignature(registers().rsp, depth, site.writeIns.address));
      return signatures.get(depth);
    }
  };
}

function onBefore(site, block) {
  try {
    if (site.method === undefined)
      nameSite(site);
    const threadId = Process.getCurrentThreadId();
    const write = pendingWrite(site, block);
    for (const hook of site.hooks.values()) {
      if (!hook.enabled)
        continue;
      const { target, skip } = hook.pointer !== null ? pointerTarget(hook, write)
        : hook.target === null ? dynamicTarget(hook, write) : fixedTarget(hook, write);
      if (target !== undefined) {
        hook.pending.set(threadId, {
          target,
          old: readTarget(hook.io, target),
          caller: hook.debug ? callerText(hook, write) : null
        });
      } else if (hook.debug) {
        const address = write.address();
        const entry = {
          threadId,
          skipped: skip,
          address: address === null ? null : toPointer(address).toString(),
          caller: callerText(hook, write)
        };
        if (address === null) {
          pushDebug(hook, entry);
        } else {
          // Show the value before and after the write, as for applied calls.
          const target = toPointer(address);
          entry.old = peekTarget(hook.io, target);
          hook.pending.set(threadId, { target, skipped: entry });
        }
      }
    }
  } catch (error) {
    reportAgentError(`before ${site.writeIns.address}`, error);
  }
}

function onAfter(site, _block) {
  try {
    const threadId = Process.getCurrentThreadId();
    for (const hook of site.hooks.values()) {
      const pending = hook.pending.get(threadId);
      if (pending === undefined)
        continue;
      hook.pending.delete(threadId);
      if (pending.skipped !== undefined) {
        pending.skipped.new = peekTarget(hook.io, pending.target);
        pushDebug(hook, pending.skipped);
      } else {
        applyTransform(hook, threadId, pending);
      }
    }
  } catch (error) {
    reportAgentError(`after ${site.writeIns.address}`, error);
  }
}

function buildTrampoline(site) {
  const origin = site.writeIns.address;
  const code = Memory.alloc(Process.pageSize, { near: origin, maxDistance: NEAR_DISTANCE });
  Memory.protect(code, Process.pageSize, 'rwx');
  const w = new X86Writer(code, { pc: code });
  const r = new X86Relocator(origin, w);

  emitCallback(w, site.before);
  let patchSize = 0;
  let first = true;
  while (patchSize < JMP_SIZE) {
    const total = r.readOne();
    if (total === 0)
      throw new Error(`Unable to relocate code at ${origin.add(patchSize)}`);
    patchSize = total;
    r.writeOne();
    if (first) {
      emitCallback(w, site.after);
      first = false;
    }
    if (r.eoi)
      break;
  }
  if (!r.eoi)
    w.putJmpAddress(origin.add(patchSize));
  // Tiny functions such as "add [rdi], esi; ret" end before the jmp fits;
  // the alignment padding behind them is never executed and may be reused.
  while (patchSize < JMP_SIZE) {
    let padding = null;
    try {
      padding = Instruction.parse(origin.add(patchSize));
    } catch (_error) {
    }
    if (padding === null || (padding.mnemonic !== 'nop' && padding.mnemonic !== 'int3'))
      throw new Error(
        `Only ${patchSize} bytes of code at ${origin} before it ends, need ${JMP_SIZE}; `
        + 'hook the caller instead');
    patchSize += padding.size;
  }
  w.flush();
  r.dispose();
  w.dispose();
  Memory.protect(code, Process.pageSize, 'r-x');
  return { code, patchSize };
}

function installSite(writeIns, writeOp) {
  const key = writeIns.address.toString();
  const existing = sites.get(key);
  if (existing !== undefined)
    return existing;
  if (Process.arch !== 'x64')
    throw new Error(`Write hooks are only implemented for x64, not ${Process.arch}`);

  const site = { writeIns, writeOp, hooks: new Map() };
  site.bytes = hexBytes(writeIns.address.readByteArray(writeIns.size));
  site.context = codeContext(writeIns);
  site.before = new NativeCallback(block => onBefore(site, block), 'void', ['pointer']);
  site.after = new NativeCallback(block => onAfter(site, block), 'void', ['pointer']);
  const { code, patchSize } = buildTrampoline(site);

  const origin = writeIns.address;
  const distance = BigInt(code.toString()) - BigInt(origin.add(JMP_SIZE).toString());
  if (distance < -(1n << 31n) || distance >= (1n << 31n))
    throw new Error(`Trampoline at ${code} is out of jmp range from ${origin}`);
  site.code = code;
  site.patchSize = patchSize;
  site.original = origin.readByteArray(patchSize);
  Memory.patchCode(origin, patchSize, c => {
    const w = new X86Writer(c, { pc: origin });
    w.putJmpAddress(code);
    while (w.offset < patchSize)
      w.putNop();
    w.flush();
    w.dispose();
  });
  sites.set(key, site);
  return site;
}

function uninstallSite(site) {
  const origin = site.writeIns.address;
  const original = new Uint8Array(site.original);
  Memory.patchCode(origin, site.patchSize, c => {
    c.writeByteArray(Array.from(original));
  });
  sites.delete(origin.toString());
  // A thread may still be running inside the trampoline; keep it mapped.
  retiredSites.push(site);
}

// --- C# methods: hooks on JIT code found again by their method ------------
// A hook saved with the C# method of its write site (Mono games) lets Mono
// compile that method after a restart and looks for the write instruction in
// its code. The offset may shift a little: JIT code embeds constants whose
// encoding can differ between runs. So every place in the method holding the
// instruction's bytes is a candidate, the one whose surrounding code matches
// best wins, the saved offset breaks ties.

function managedCandidate(address, length) {
  const site = sites.get(address.toString());
  if (site !== undefined)
    return { bytes: site.bytes, context: site.context };
  const ins = Instruction.parse(address);
  if (ins.size !== length || writeOperand(ins) === null)
    return null;
  return { bytes: hexBytes(address.readByteArray(ins.size)), context: codeContext(ins) };
}

async function resolveManaged(spec) {
  const { start, size } = await engine.compile(spec.managed);
  const expected = spec.instructionBytes;
  const length = expected.length / 2;
  const code = new Uint8Array(start.readByteArray(size));
  const first = parseInt(expected.substr(0, 2), 16);
  let best = null;
  for (let offset = 0; offset + length <= size; offset++) {
    const address = start.add(offset);
    // Hooked sites hold a jmp; compare their original bytes instead.
    if (code[offset] !== first && !sites.has(address.toString()))
      continue;
    let candidate;
    try {
      candidate = managedCandidate(address, length);
    } catch (_error) {
      continue;
    }
    if (candidate === null || candidate.bytes !== expected)
      continue;
    const similarity = contextSimilarity(spec.signature || {}, candidate.context);
    const distance = Math.abs(offset - spec.managed.offset);
    if (best === null || similarity > best.similarity
        || (similarity === best.similarity && distance < best.distance))
      best = { address, similarity, distance };
  }
  if (best === null || best.similarity < TRAINING_MIN_SIMILARITY)
    throw new Error(`the write instruction is not in ${spec.managed.method} any more`);
  return best.address;
}

async function addHookResolving(spec) {
  // addHook for a hook known by its C# method: find the write site first.
  if (spec.managed !== null && spec.module === null && spec.writePc === null
      && spec.trapPc === null) {
    try {
      spec = { ...spec, writePc: (await resolveManaged(spec)).toString() };
    } catch (error) {
      if (spec.signature === null)
        throw error;
      // Not compiled or loaded yet, or the game changed: train instead.
      send({ kind: 'managed-failed', hookId: spec.id, error: String(error) });
    }
  }
  return addHook(spec);
}

function addHook(spec) {
  if (hooks.has(spec.id) || trainings.has(spec.id))
    throw new Error(`Hook ${spec.id} already exists`);
  if (spec.signature !== null && spec.module === null && spec.writePc === null
      && spec.trapPc === null)
    return startTraining(spec);
  const io = VALUE_IO[spec.valueType];
  if (io === undefined)
    throw new Error(`Unsupported value type: ${spec.valueType}`);
  if (spec.callerDepth < 0 || spec.callerDepth > MAX_CALLER_DEPTH)
    throw new Error(`Caller depth must be between 0 and ${MAX_CALLER_DEPTH}`);
  const target = spec.address === null ? null : ptr(spec.address);
  const transform = compileTransform(spec.transform);

  let writeIns, writeOp;
  const writePc = spec.module !== null ? moduleAddress(spec)
    : spec.writePc !== null ? ptr(spec.writePc) : null;
  const existingSite = writePc === null ? undefined : sites.get(writePc.toString());
  if (existingSite !== undefined) {
    writeIns = existingSite.writeIns;
    writeOp = existingSite.writeOp;
  } else if (writePc !== null) {
    writeIns = Instruction.parse(writePc);
    writeOp = writeOperand(writeIns);
    if (writeOp === null)
      throw new Error(`${writeIns} at ${writeIns.address} does not write memory`);
  } else {
    const candidate = resolveWriteInstruction(ptr(spec.trapPc), target, io.size);
    writeIns = candidate.ins;
    writeOp = candidate.op;
  }

  const site = installSite(writeIns, writeOp);
  const learned = resolveCallers(spec.callers, writeIns.address);
  const hook = {
    id: spec.id,
    site,
    target,
    targetValue: target === null ? null : toBigInt(target),
    valueType: spec.valueType,
    io,
    filterAddress: spec.filterAddress,
    pointer: spec.pointer === null ? null : {
      module: spec.pointer.module,
      baseOffset: spec.pointer.baseOffset,
      offsets: spec.pointer.offsets,
      base: null
    },
    callerDepth: spec.callerDepth,
    callers: learned.keys,
    foreignCallers: new Set(),
    ambiguousReported: false,
    transform,
    enabled: spec.enabled,
    debug: false,
    pending: new Map(),
    stats: { count: 0, last: null },
    errorReported: false
  };
  site.hooks.set(spec.id, hook);
  hooks.set(spec.id, hook);
  if (learned.dropped > 0) {
    reportCallers(hook);  // saved without them
    send({ kind: 'callers-dropped', hookId: spec.id, count: learned.dropped });
  }

  return {
    id: spec.id,
    writeInstruction: describeInstruction(site),
    resumePc: site.writeIns.next.toString(),
    trampoline: site.code.toString(),
    patchSize: site.patchSize,
    operandSize: site.writeOp.size,
    callers: hook.callers.size,
    transform: spec.transform.kind
  };
}

// --- Training -------------------------------------------------------------
// A hook on JIT code with a pointer target is saved with the bytes of its
// write instruction and of the code around it. After a restart, a hardware
// watchpoint on the target finds the instruction again: the hook is
// installed at the first write whose code matches.

function startTraining(spec) {
  const io = VALUE_IO[spec.valueType];
  if (io === undefined)
    throw new Error(`Unsupported value type: ${spec.valueType}`);
  if (spec.pointer === null)
    throw new Error('Training needs a pointer chain to the target');
  compileTransform(spec.transform);
  const training = {
    id: spec.id,
    spec,
    io,
    pointer: { ...spec.pointer, base: null },
    target: null,
    slot: null,
    debug: false,
    seen: 0,
    rejected: 0,
    reported: null
  };
  trainings.set(spec.id, training);
  if (!trainingPaused) {
    startTrainingWatch();
    updateTrainings();
  }
  return { id: spec.id, training: true };
}

function startTrainingWatch() {
  if (trainingTimer !== null)
    return;
  // Installed once and kept: removing it could let a late trap through.
  if (!trainingHandlerInstalled) {
    Process.setExceptionHandler(onTrainingException);
    trainingHandlerInstalled = true;
  }
  // No thread observer: setting a watchpoint starts a helper thread, and
  // reporting it to the observer while the agent sets one deadlocks.
  // New threads are armed by this timer instead.
  trainingTimer = setInterval(updateTrainings, 500);
}

function stopTrainingWatch() {
  if (trainingTimer === null)
    return;
  clearInterval(trainingTimer);
  trainingTimer = null;
  for (const training of trainings.values()) {
    if (training.slot !== null && training.target !== null) {
      training.target = null;
      for (const thread of Process.enumerateThreads())
        armTraining(thread, training);
    }
  }
  // A trap raised just before the watchpoints were removed may still be on
  // its way; let it reach the handler before its threads are forgotten.
  // Keeping them longer would swallow the traps of a write watch.
  Thread.sleep(0.1);
  trainingThreads.clear();
}

function setTrainingPaused(paused) {
  trainingPaused = paused;
  if (paused) {
    stopTrainingWatch();
  } else if (trainings.size > 0) {
    startTrainingWatch();
    updateTrainings();
  }
  return trainings.size;
}

function armThreadForTrainings(thread) {
  for (const training of trainings.values()) {
    if (training.target !== null)
      armTraining(thread, training);
  }
}

function armTraining(thread, training) {
  if (training.slot === null)
    return;
  try {
    if (training.target === null) {
      thread.unsetHardwareWatchpoint(training.slot);
    } else {
      thread.setHardwareWatchpoint(training.slot, training.target, training.io.size, 'w');
      trainingThreads.add(thread.id);
    }
  } catch (_error) {
    // The thread exited, or it is the agent's own.
  }
}

function freeTrainingSlot() {
  const used = new Set(Array.from(trainings.values(), t => t.slot));
  return TRAINING_SLOTS.find(slot => !used.has(slot)) ?? null;
}

function updateTrainings() {
  for (const training of trainings.values()) {
    if (training.slot === null)
      training.slot = freeTrainingSlot();
    if (training.slot === null)
      continue;
    // The chain leads elsewhere e.g. after loading a game, or nowhere yet.
    const target = resolvePointer(training.pointer);
    const unchanged = target === null ? training.target === null
      : training.target !== null && target.equals(training.target);
    if (unchanged)
      continue;
    training.target = target;
    for (const thread of Process.enumerateThreads())
      armTraining(thread, training);
  }
  // Threads started since the last update.
  for (const thread of Process.enumerateThreads()) {
    if (!trainingThreads.has(thread.id))
      armThreadForTrainings(thread);
  }
}

function stopTraining(training) {
  trainings.delete(training.id);
  if (training.slot !== null && training.target !== null) {
    training.target = null;
    for (const thread of Process.enumerateThreads())
      armTraining(thread, training);
  }
  if (trainings.size === 0)
    stopTrainingWatch();
}

function onTrainingException(details) {
  // Linux reports hardware data watchpoint traps as 'single-step'.
  if (details.type !== 'breakpoint' && details.type !== 'single-step')
    return false;
  if (!trainingThreads.has(Process.getCurrentThreadId()))
    return false;
  if (trainingTimer === null)
    return true;  // a late trap after the training watch stopped
  trainingHits.push(details.context.pc);
  if (!trainingScheduled) {
    // Hooks are installed outside the exception handler.
    trainingScheduled = true;
    setTimeout(processTrainingHits, 0);
  }
  return true;
}

function processTrainingHits() {
  trainingScheduled = false;
  for (const pc of trainingHits.splice(0)) {
    for (const training of Array.from(trainings.values())) {
      if (training.target !== null)
        tryTraining(training, pc);
    }
  }
}

function trainingCandidate(training, pc) {
  // A write that is hooked already runs relocated in its trampoline.
  for (const site of sites.values()) {
    if (pc.compare(site.code) >= 0 && pc.compare(site.code.add(Process.pageSize)) < 0)
      return { ins: site.writeIns, bytes: site.bytes, context: site.context };
  }
  const { ins } = resolveWriteInstruction(pc, training.target, training.io.size);
  const site = sites.get(ins.address.toString());
  if (site !== undefined)
    return { ins, bytes: site.bytes, context: site.context };
  return { ins, bytes: hexBytes(ins.address.readByteArray(ins.size)), context: codeContext(ins) };
}

function matchingBytes(expected, actual, fromEnd) {
  const count = Math.min(expected.length, actual.length) / 2;
  let same = 0;
  for (let i = 0; i < count; i++) {
    const a = fromEnd ? expected.length - 2 * (i + 1) : 2 * i;
    const b = fromEnd ? actual.length - 2 * (i + 1) : 2 * i;
    if (expected.substr(a, 2) === actual.substr(b, 2))
      same++;
  }
  return [same, count];
}

function contextSimilarity(expected, actual) {
  const [sameBefore, before] = matchingBytes(expected.before || '', actual.before, true);
  const [sameAfter, after] = matchingBytes(expected.after || '', actual.after, false);
  return before + after === 0 ? 1 : (sameBefore + sameAfter) / (before + after);
}

function tryTraining(training, pc) {
  let candidate;
  try {
    candidate = trainingCandidate(training, pc);
  } catch (_error) {
    training.rejected++;
    return;
  }
  training.seen++;
  const similarity = contextSimilarity(training.spec.signature, candidate.context);
  if (candidate.bytes !== training.spec.instructionBytes
      || similarity < TRAINING_MIN_SIMILARITY) {
    training.rejected++;
    return;
  }
  stopTraining(training);
  try {
    const info = addHook({
      ...training.spec, writePc: candidate.ins.address.toString(), signature: null,
      managed: null
    });
    hooks.get(training.id).debug = training.debug;
    info.similarity = similarity;
    send({ kind: 'trained', hookId: training.id, info });
  } catch (error) {
    send({ kind: 'training-failed', hookId: training.id, error: String(error) });
  }
}

function trainingEvents() {
  const events = [];
  for (const training of trainings.values()) {
    const state = {
      hookId: training.id,
      paused: trainingPaused,
      resolved: training.target !== null,
      waiting: training.slot === null,
      seen: training.seen,
      rejected: training.rejected
    };
    const key = JSON.stringify(state);
    if (key !== training.reported) {
      training.reported = key;
      events.push(state);
    }
  }
  return events;
}

function removeHook(id) {
  const training = trainings.get(id);
  if (training !== undefined) {
    stopTraining(training);
    return true;
  }
  const hook = hooks.get(id);
  if (hook === undefined)
    return false;
  hooks.delete(id);
  hook.site.hooks.delete(id);
  if (hook.site.hooks.size === 0)
    uninstallSite(hook.site);
  return true;
}

function requireHook(id) {
  const hook = hooks.get(id);
  if (hook === undefined)
    throw new Error(`Unknown hook ${id}`);
  return hook;
}

function flushStats() {
  const events = [];
  for (const hook of hooks.values()) {
    if (hook.stats.count === 0)
      continue;
    events.push({ hookId: hook.id, count: hook.stats.count, last: hook.stats.last });
    hook.stats.count = 0;
  }
  if (events.length > 0)
    send({ kind: 'events', events });
  const training = trainingEvents();
  if (training.length > 0)
    send({ kind: 'training', events: training });
  if (debugEvents.length > 0 || debugDropped.size > 0) {
    send({
      kind: 'debug',
      events: debugEvents.splice(0),
      dropped: Object.fromEntries(debugDropped)
    });
    debugDropped.clear();
  }
}

const flushTimer = setInterval(() => {
  engine.refresh();
  flushStats();
}, 100);
send({
  kind: 'ready',
  arch: Process.arch,
  accessStub: accessStub.toString(),
  accessStubSize: Process.pageSize
});

rpc.exports = {
  addHook: addHookResolving,
  removeHook,
  setTrainingPaused,
  setTransform(id, spec) {
    const training = trainings.get(id);
    if (training !== undefined) {
      compileTransform(spec);
      training.spec.transform = spec;
      return true;
    }
    const hook = requireHook(id);
    hook.transform = compileTransform(spec);
    hook.errorReported = false;
    return true;
  },
  setDebug(id, enabled) {
    const training = trainings.get(id);
    if (training !== undefined) {
      training.debug = enabled;
      return true;
    }
    requireHook(id).debug = enabled;
    return true;
  },
  setEnabled(id, enabled) {
    const training = trainings.get(id);
    if (training !== undefined) {
      training.spec.enabled = enabled;
      return true;
    }
    const hook = requireHook(id);
    hook.enabled = enabled;
    if (!enabled)
      hook.pending.clear();
    return true;
  },
  dispose() {
    clearInterval(flushTimer);
    for (const training of Array.from(trainings.values()))
      stopTraining(training);
    if (trainingHandlerInstalled)
      Thread.sleep(0.1);  // let late traps reach the handler before unloading
    for (const id of Array.from(hooks.keys()))
      removeHook(id);
    flushStats();
    return true;
  }
};
