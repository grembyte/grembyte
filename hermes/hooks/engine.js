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
// "jmp rel8": the short jump to a code cave where the jmp fits (see installSite)
const SHORT_JMP_SIZE = 2;
// Functions start 16-byte aligned; int3 padding before such a start is never executed.
const FUNCTION_ALIGNMENT = 16;
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
// Caller signatures remembered per write site for its fast path (siteSignature).
const MAX_VERIFIED_CALLERS = 1024;
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
// Flushes (100 ms each) between checks whether the game reverted a site.
const SITE_CHECK_TICKS = 10;
// Patch a site again when the game restored its original code (setRepatch).
let repatchReverted = false;
// Measure the hooked writes per site (setProfiling): calls, how long a game
// thread waited from entering the trampoline until its JavaScript ran (the
// agent runs one thread at a time), and how long that ran.
let profiling = false;
// Code bytes kept on each side of a write instruction to recognize it in
// JIT code, which is generated anew (elsewhere) on every start.
const CONTEXT_SIZE = 32;
// Debug register slots for trainings and breakpoint sites; slot 0 is used
// by the write watch.
const DEBUG_SLOTS = [1, 2, 3];
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
let exceptionHandlerInstalled = false;

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

function callerFrames(rsp, depth, pc) {
  // The return addresses of the ``depth`` calls that led to ``pc``, innermost
  // first, as ``key``, and the distances of their slots from ``rsp``.
  const frames = [];
  const offsets = [];
  let inside = pc;
  let frameRsp = rsp;
  while (frames.length < depth) {
    const found = callerFrame(frameRsp, inside);
    if (found === null)
      return null;
    frames.push(found.value.toString());
    offsets.push(found.slot.sub(rsp).toInt32());
    inside = found.value;
    frameRsp = found.slot.add(Process.pointerSize);  // the caller's after the return
  }
  return { key: frames.join(','), offsets };
}

function callerSignature(rsp, depth, pc) {
  const found = callerFrames(rsp, depth, pc);
  return found === null ? null : found.key;
}

function siteSignature(site, rsp, depth) {
  // callerSignature for a hooked write, which may run thousands of times a
  // second. Functions with a fixed frame keep their return address at the
  // same distance from rsp on every run, so the slots of the last scan are
  // read first: return addresses seen there before (verified by a scan)
  // are taken as they are. Only a new combination is scanned.
  if (site.frameLayouts === undefined)
    site.frameLayouts = new Map();  // depth -> { offsets, verified: Set }
  const layout = site.frameLayouts.get(depth);
  if (layout !== undefined) {
    try {
      const key = layout.offsets.map(offset => rsp.add(offset).readPointer().toString()).join(',');
      if (layout.verified.has(key))
        return key;
    } catch (_error) {
    }
  }
  const found = callerFrames(rsp, depth, site.writeIns.address);
  if (found === null)
    return null;
  let current = layout;
  if (current === undefined || current.offsets.join() !== found.offsets.join()) {
    current = { offsets: found.offsets, verified: new Set() };
    site.frameLayouts.set(depth, current);
  }
  if (current.verified.size >= MAX_VERIFIED_CALLERS)
    current.verified.clear();
  current.verified.add(found.key);
  return found.key;
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

// Whether the process has a runtime that names code (Mono), as of the last
// flush; until then no hooked write asks for the name of its site.
let engineFound = engine.id !== null;

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

function ignoreLog() {}

function applyTransform(hook, threadId, pending) {
  // Runs on every hooked write: strings are only made when a transform asks.
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
    get address() { return target.toString(); },
    get writePc() { return hook.site.writeIns.address.toString(); },
    threadId,
    hookId: hook.id,
    log: logs === null ? ignoreLog : (...args) => { logs.push(formatLog(args)); }
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
  // The address is made a string when the stats are sent (flushStats).
  hook.stats.last = { old: oldValue, new: current, result, threadId, address: target };
}

// rdtsc; shl rdx, 32; or rax, rdx: the time stamp counter in rax.
const RDTSC_TO_RAX = [0x0f, 0x31, 0x48, 0xc1, 0xe2, 0x20, 0x48, 0x09, 0xd0];
// The time stamp counter, 0 where write hooks do not run (not x64).
const readTsc = Process.arch !== 'x64' ? () => 0 : (() => {
  const code = Memory.alloc(Process.pageSize);
  code.writeByteArray(RDTSC_TO_RAX.concat([0xc3]));
  Memory.protect(code, Process.pageSize, 'r-x');
  // Both kept: Frida frees the page with its last reference.
  // Exclusive: a call must not let go of the agent's lock, or measuring
  // would add waits of its own.
  const native = { code, fn: new NativeFunction(code, 'uint64', [], { scheduling: 'exclusive' }) };
  return () => native.fn();
})();
const tscOrigin = { ticks: Number(readTsc()), ms: Date.now() };

function ticksPerMicrosecond() {
  const ms = Date.now() - tscOrigin.ms;
  return ms <= 0 ? 0 : (Number(readTsc()) - tscOrigin.ticks) / (ms * 1000);
}

function newProfile() {
  return { calls: 0, skipped: 0, waitTicks: 0, waitMax: 0, waits: 0, runTicks: 0, runMax: 0 };
}

function profileCall(site, entered, start) {
  // One JavaScript callback of a hooked write: waiting before, running after ``start``.
  const end = Number(readTsc());
  if (site.profile === undefined)
    site.profile = newProfile();
  const profile = site.profile;
  if (entered !== undefined) {
    const wait = start - Number(entered);
    profile.waitTicks += wait;
    profile.waits += 1;
    if (wait > profile.waitMax)
      profile.waitMax = wait;
  }
  const run = end - start;
  profile.runTicks += run;
  if (run > profile.runMax)
    profile.runMax = run;
  return profile;
}

function profileEvents() {
  const perMicrosecond = ticksPerMicrosecond();
  const events = [];
  for (const site of sites.values()) {
    const profile = site.profile;
    if (profile === undefined || perMicrosecond === 0)
      continue;
    const us = ticks => Math.round(ticks / perMicrosecond * 10) / 10;
    const callbacks = Math.max(profile.waits, 1);
    events.push({
      hookIds: Array.from(site.hooks.keys()),
      calls: profile.calls,
      skipped: profile.skipped,
      waitAvg: us(profile.waitTicks / callbacks),
      waitMax: us(profile.waitMax),
      runAvg: us(profile.runTicks / callbacks),
      runMax: us(profile.runMax)
    });
    site.profile = newProfile();
  }
  return events;
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
  w.putBytes(RDTSC_TO_RAX);  // rax and rdx are saved in the block
  w.putCallAddressWithArguments(callback, ['rbx', 'rax']);
  w.putBytes(FXRSTOR64_RSP);
  w.putMovRegReg('rsp', 'rbx');
  for (const reg of SAVED_REGS.slice().reverse())
    w.putPopReg(reg);
  w.putPopfx();
  w.putLeaRegRegOffset('rsp', 'rsp', RED_ZONE);
}

// Offset of each saved register in the block emitCallback hands over.
const REGISTER_SLOTS = Object.fromEntries(
  SAVED_REGS.map((name, i) => [name, 8 * (SAVED_REGS.length - 1 - i)]));

function blockRegisters(block) {
  // The registers of a trampoline call, each read when asked for: reading
  // all of them costs more than the rest of a hooked write.
  return {
    get(name) {
      return name === 'rsp' ? block.add(8 * SAVED_REGS.length + 8 + RED_ZONE)
        : block.add(REGISTER_SLOTS[name]).readPointer();
    }
  };
}

function contextRegisters(context) {
  // The registers of an exception's context (breakpoint sites).
  return { get: name => context[name] };
}

function registerPart(name) {
  // The 64-bit register holding ``name`` and whether only its low half counts
  // (32-bit addressing on x86-64: eax -> rax, r8d -> r8).
  if (name === undefined)
    return null;
  if (name === 'rip' || REGISTER_SLOTS[name] !== undefined || name === 'rsp')
    return { name, low: false };
  const wide = /^e[a-z]{2}$/.test(name) ? 'r' + name.slice(1)
    : /^r\d+d$/.test(name) ? name.slice(0, -1) : null;
  if (wide === null || (REGISTER_SLOTS[wide] === undefined && wide !== 'rsp'))
    throw new Error(`unsupported address register ${name}`);
  return { name: wide, low: true };
}

function addressPlan(ins, op) {
  // How to compute the written address from the registers, worked out once
  // per site; null if it cannot be (fs:/gs: segments).
  const mem = op.value;
  if (mem.segment !== undefined && mem.segment !== 'ds' && mem.segment !== 'ss')
    return null;
  try {
    return {
      base: mem.base === 'rip' ? null : registerPart(mem.base),
      rip: mem.base === 'rip' ? ins.next : null,
      index: registerPart(mem.index),
      shift: Math.log2(mem.scale || 1),
      disp: mem.disp || 0
    };
  } catch (_error) {
    return null;
  }
}

function planRegister(regs, part) {
  const value = regs.get(part.name);
  return part.low ? value.and(0xffffffff) : value;
}

function planAddress(plan, regs) {
  // NativePointer arithmetic wraps at 64 bits like the CPU's.
  let address = plan.rip !== null ? plan.rip
    : plan.base !== null ? planRegister(regs, plan.base) : ptr(0);
  if (plan.index !== null)
    address = address.add(planRegister(regs, plan.index).shl(plan.shift));
  return plan.disp < 0 ? address.sub(-plan.disp) : address.add(plan.disp);
}

function overlapsPointer(start, size, target, targetSize) {
  return start.compare(target.add(targetSize)) < 0 && target.compare(start.add(size)) < 0;
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
  const hit = overlapsPointer(address, write.site.writeOp.size, hook.target, hook.io.size);
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
  return { target: address };
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
      && !overlapsPointer(address, write.site.writeOp.size, target, hook.io.size))
    return { skip: 'address' };
  return { target };
}

function pendingWrite(site, regs) {
  // The address (a NativePointer) and caller signatures are computed at
  // most once per write.
  let address;
  const signatures = new Map();
  return {
    site,
    address() {
      if (address === undefined) {
        if (site.addressPlan === undefined)
          site.addressPlan = addressPlan(site.writeIns, site.writeOp);
        address = site.addressPlan === null ? null : planAddress(site.addressPlan, regs);
      }
      return address;
    },
    signature(depth) {
      if (!signatures.has(depth))
        signatures.set(depth, siteSignature(site, regs.get('rsp'), depth));
      return signatures.get(depth);
    }
  };
}

function onBefore(site, regs, entered) {
  const start = profiling ? Number(readTsc()) : 0;
  let taken = false;
  try {
    if (site.method === undefined && engineFound)
      nameSite(site);
    const threadId = Process.getCurrentThreadId();
    const write = pendingWrite(site, regs);
    for (const hook of site.hooks.values()) {
      if (!hook.enabled)
        continue;
      const { target, skip } = hook.pointer !== null ? pointerTarget(hook, write)
        : hook.target === null ? dynamicTarget(hook, write) : fixedTarget(hook, write);
      if (target !== undefined) {
        taken = true;
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
          address: address === null ? null : address.toString(),
          caller: callerText(hook, write)
        };
        if (address === null) {
          pushDebug(hook, entry);
        } else {
          // Show the value before and after the write, as for applied calls.
          entry.old = peekTarget(hook.io, address);
          hook.pending.set(threadId, { target: address, skipped: entry });
        }
      }
    }
  } catch (error) {
    reportAgentError(`before ${site.writeIns.address}`, error);
  }
  if (profiling) {
    const profile = profileCall(site, entered, start);
    profile.calls += 1;
    if (!taken)
      profile.skipped += 1;
  }
}

function onAfter(site, entered) {
  const start = profiling ? Number(readTsc()) : 0;
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
  if (profiling)
    profileCall(site, entered, start);
}

function buildTrampoline(site, needed) {
  const origin = site.writeIns.address;
  const code = Memory.alloc(Process.pageSize, { near: origin, maxDistance: NEAR_DISTANCE });
  Memory.protect(code, Process.pageSize, 'rwx');
  const w = new X86Writer(code, { pc: code });
  const r = new X86Relocator(origin, w);

  emitCallback(w, site.before);
  let patchSize = 0;
  let first = true;
  while (patchSize < needed) {
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
  while (patchSize < needed) {
    let padding = null;
    try {
      padding = Instruction.parse(origin.add(patchSize));
    } catch (_error) {
    }
    if (padding === null || (padding.mnemonic !== 'nop' && padding.mnemonic !== 'int3')) {
      const error = new Error(
        `Only ${patchSize} bytes of code at ${origin} before it ends, need ${needed}; `
        + 'hook the caller instead');
      error.tooShort = true;
      throw error;
    }
    patchSize += padding.size;
  }
  w.flush();
  r.dispose();
  w.dispose();
  Memory.protect(code, Process.pageSize, 'r-x');
  return { code, patchSize };
}

// A run of int3 padding within reach of a "jmp rel8" from the end of the
// write's short jump, for the jmp to the trampoline. Only padding that ends
// at an aligned function start counts: int3 bytes elsewhere may be part of
// an instruction. A cave in use holds a jmp and no longer looks like padding.
function findCave(origin, patchSize, code) {
  const from = origin.add(SHORT_JMP_SIZE);
  const range = Process.findRangeByAddress(origin);
  if (range === null)
    return null;
  const rangeEnd = range.base.add(range.size);
  let start = from.sub(128);
  if (start.compare(range.base) < 0)
    start = range.base;
  let end = from.add(127 + JMP_SIZE);
  if (end.compare(rangeEnd) > 0)
    end = rangeEnd;
  const size = end.sub(start).toInt32();
  const bytes = new Uint8Array(start.readByteArray(size));
  const patched = [origin.sub(start).toInt32(), origin.sub(start).toInt32() + patchSize];

  let best = null;
  let i = 0;
  while (i < size) {
    if (bytes[i] !== 0xcc) {
      i++;
      continue;
    }
    let runEnd = i;
    while (runEnd < size && bytes[runEnd] === 0xcc)
      runEnd++;
    const runStart = i;
    i = runEnd;
    // A run cut off by the window may go on; its end is not known to be aligned.
    if (runEnd === size && end.compare(rangeEnd) < 0)
      continue;
    if (start.add(runEnd).and(FUNCTION_ALIGNMENT - 1).toInt32() !== 0)
      continue;
    // The last bytes of the run, next to the function start, are surely padding.
    for (let at = runEnd - JMP_SIZE; at > runStart; at--) {
      if (at < patched[1] && at + JMP_SIZE > patched[0])
        continue;
      const cave = start.add(at);
      const offset = cave.sub(from).toInt32();
      if (offset < -128 || offset > 127)
        continue;
      if (!jmpReaches(cave, code))
        continue;
      if (best === null || Math.abs(offset) < Math.abs(best.offset))
        best = { cave, offset };
      break;
    }
  }
  return best;
}

function jmpReaches(from, to) {
  const distance = BigInt(to.toString()) - BigInt(from.add(JMP_SIZE).toString());
  return distance >= -(1n << 31n) && distance < (1n << 31n);
}

function installSite(writeIns, writeOp, shortJump) {
  const key = writeIns.address.toString();
  const existing = sites.get(key);
  if (existing !== undefined)
    return existing;
  if (Process.arch !== 'x64')
    throw new Error(`Write hooks are only implemented for x64, not ${Process.arch}`);

  const site = { writeIns, writeOp, hooks: new Map(), cave: null, breakpoint: false };
  site.bytes = hexBytes(writeIns.address.readByteArray(writeIns.size));
  site.context = codeContext(writeIns);
  site.before = new NativeCallback(
    (block, entered) => onBefore(site, blockRegisters(block), entered),
    'void', ['pointer', 'uint64']);
  site.after = new NativeCallback((_block, entered) => onAfter(site, entered),
    'void', ['pointer', 'uint64']);
  const origin = writeIns.address;
  let built;
  try {
    built = buildTrampoline(site, JMP_SIZE);
  } catch (error) {
    if (!shortJump || !error.tooShort)
      throw error;
    // Too little code for the jmp (a tiny function followed directly by the
    // next one): a short jump to int3 padding nearby, which jumps on to the
    // trampoline. Only the write is relocated; the rest stays in place.
    built = buildTrampoline(site, SHORT_JMP_SIZE);
    const found = findCave(origin, built.patchSize, built.code);
    if (found === null)
      throw new Error(`${error.message}; no int3 padding for a short jump within 128 bytes`);
    site.cave = found.cave;
  }
  const { code, patchSize } = built;

  if (!jmpReaches(site.cave === null ? origin : site.cave, code))
    throw new Error(`Trampoline at ${code} is out of jmp range from ${origin}`);
  site.code = code;
  site.patchSize = patchSize;
  site.original = origin.readByteArray(patchSize);
  site.originalHex = hexBytes(site.original);
  site.caveOriginalHex = site.cave === null ? null : readHex(site.cave, JMP_SIZE);
  patchSite(site);
  site.patchedHex = readHex(origin, patchSize);
  site.cavePatchedHex = site.cave === null ? null : readHex(site.cave, JMP_SIZE);
  site.reverted = false;
  site.reverts = 0;
  sites.set(key, site);
  return site;
}

function patchSite(site) {
  // The jmp (or short jump and cave) into the site's trampoline.
  const origin = site.writeIns.address;
  if (site.cave !== null) {
    // The cave first: the write jumps there as soon as it is patched.
    Memory.patchCode(site.cave, JMP_SIZE, c => {
      const w = new X86Writer(c, { pc: site.cave });
      w.putJmpAddress(site.code);
      w.flush();
      w.dispose();
    });
  }
  Memory.patchCode(origin, site.patchSize, c => {
    const w = new X86Writer(c, { pc: origin });
    if (site.cave === null) {
      w.putJmpAddress(site.code);
    } else {
      const offset = site.cave.sub(origin.add(SHORT_JMP_SIZE)).toInt32();
      w.putBytes([0xeb, offset & 0xff]);
    }
    while (w.offset < site.patchSize)
      w.putNop();
    w.flush();
    w.dispose();
  });
}

// --- Reverted sites ---------------------------------------------------------
// Anti-tamper code in some games (e.g. Arxan in Dark Souls III) checks its
// code and repairs changes: the original write is back, the hook silently
// stops. Every SITE_CHECK_TICKS flushes the agent compares each site with
// the bytes it wrote and reports a change ('site-state'). With repatching
// on, a site whose original code is back is patched again.

function siteState(site) {
  // 'intact', 'reverted' (may be patched again), 'changed' (other code: left
  // alone) or null if the code is unreadable (e.g. JIT code the runtime freed).
  // Only the write's own bytes decide: with the original write back, nothing
  // jumps to the cave any more, so other code there just needs a new cave.
  const origin = readHex(site.writeIns.address, site.patchSize);
  const cave = site.cave === null ? null : readHex(site.cave, JMP_SIZE);
  if (origin === null || (site.cave !== null && cave === null))
    return null;
  if (origin === site.patchedHex && cave === site.cavePatchedHex)
    return 'intact';
  if (origin === site.originalHex)
    return 'reverted';
  // Still jumping to a cave the game restored to padding: patch it again.
  return origin === site.patchedHex && cave === site.caveOriginalHex ? 'reverted' : 'changed';
}

function repatchSite(site) {
  // A cave holding other code now (e.g. anti-tamper code that wrote over the
  // padding) is given up for new padding nearby.
  if (site.cave !== null) {
    const cave = readHex(site.cave, JMP_SIZE);
    if (cave !== site.cavePatchedHex && cave !== site.caveOriginalHex) {
      const found = findCave(site.writeIns.address, site.patchSize, site.code);
      if (found === null)
        throw new Error('no int3 padding left for the short jump');
      site.cave = found.cave;
      site.caveOriginalHex = readHex(site.cave, JMP_SIZE);
    }
  }
  patchSite(site);
  site.cavePatchedHex = site.cave === null ? null : readHex(site.cave, JMP_SIZE);
}

function reportSite(site, state, error = null) {
  site.reportedState = state;
  site.reportedError = error;
  send({
    kind: 'site-state',
    hookIds: Array.from(site.hooks.keys()),
    state,
    reverts: site.reverts,
    error
  });
}

function checkSites() {
  for (const site of sites.values()) {
    if (site.breakpoint)
      continue;  // the game's code is untouched
    const state = siteState(site);
    if (state === null)
      continue;
    if (state === 'intact') {
      if (site.reverted) {
        site.reverted = false;
        reportSite(site, 'restored');
      }
      continue;
    }
    if (!site.reverted)
      site.reverts += 1;
    site.reverted = true;
    let error = null;
    if (state === 'reverted' && repatchReverted) {
      try {
        repatchSite(site);
        site.reverted = false;
        reportSite(site, 'repatched');
        continue;
      } catch (e) {
        error = String(e.message || e);
      }
    }
    // Anti-tamper code may decrypt and encrypt the code around the site:
    // 'changed' turns into 'reverted' and back. Each change is reported.
    if (state !== site.reportedState || error !== site.reportedError)
      reportSite(site, state, error);
  }
}

function uninstallSite(site) {
  if (site.breakpoint) {
    uninstallBreakpointSite(site);
    return;
  }
  const origin = site.writeIns.address;
  const original = new Uint8Array(site.original);
  // Code the game restored or changed is left as it is.
  if (readHex(origin, site.patchSize) === site.patchedHex) {
    Memory.patchCode(origin, site.patchSize, c => {
      c.writeByteArray(Array.from(original));
    });
  }
  sites.delete(origin.toString());
  // A thread may still be running inside the trampoline; keep it mapped.
  // The same holds for the cave, which keeps its jmp to the trampoline.
  retiredSites.push(site);
}

// --- Breakpoint sites -------------------------------------------------------
// A site hooked by a hardware breakpoint leaves the game's code untouched,
// for games whose anti-tamper code repairs patches. Frida resumes from a
// breakpoint without the resume flag, so the trap would repeat at once;
// instead the breakpoint moves on every trap. On the write instruction the
// hooks take the old value (onBefore) and the breakpoint moves to the next
// instruction; the write runs natively, and on the next instruction the
// transforms run (onAfter) and the breakpoint moves back. Each trap costs a
// few microseconds: fine for a value written every frame, not for a hot loop.
// Debug registers belong to a thread: threads started later are armed by
// the flush timer, at most every BREAKPOINT_ARM_TICKS.
const BREAKPOINT_ARM_TICKS = 5;
// thread id -> Thread whose debug registers hold the breakpoint sites
const breakpointThreads = new Map();
// address -> { site, after }: the write instruction and the one after it
const breakpointAddresses = new Map();
// Addresses of removed breakpoint sites, for traps raised just before.
const retiredBreakpoints = new Set();

function installBreakpointSite(writeIns, writeOp) {
  const key = writeIns.address.toString();
  const existing = sites.get(key);
  if (existing !== undefined)
    return existing;
  if (Process.arch !== 'x64')
    throw new Error(`Write hooks are only implemented for x64, not ${Process.arch}`);
  const next = writeIns.next.toString();
  if (breakpointAddresses.has(key) || breakpointAddresses.has(next))
    throw new Error(`Another breakpoint hook uses the code at ${writeIns.address}`);
  const slot = freeDebugSlot();
  if (slot === null)
    throw new Error(`No free hardware breakpoint: ${DEBUG_SLOTS.length} at most, `
      + 'shared with hooks in training');
  const site = {
    writeIns, writeOp, hooks: new Map(), cave: null, code: null, patchSize: 0,
    breakpoint: true, slot
  };
  site.bytes = hexBytes(writeIns.address.readByteArray(writeIns.size));
  site.context = codeContext(writeIns);
  ensureExceptionHandler();
  breakpointAddresses.set(key, { site, after: false });
  breakpointAddresses.set(next, { site, after: true });
  retiredBreakpoints.delete(key);
  retiredBreakpoints.delete(next);
  sites.set(key, site);
  for (const thread of breakpointThreads.values())
    armBreakpoint(thread, site, writeIns.address);
  armNewThreads();
  return site;
}

function armBreakpoint(thread, site, address) {
  try {
    thread.setHardwareBreakpoint(site.slot, address);
    return true;
  } catch (_error) {
    return false;  // the thread exited, or it is the agent's own
  }
}

function armNewThreads() {
  // Arms the threads started since the last call; forgets those that ended.
  const current = new Set();
  for (const thread of Process.enumerateThreads()) {
    current.add(thread.id);
    if (breakpointThreads.has(thread.id))
      continue;
    let armed = false;
    for (const site of sites.values()) {
      if (site.breakpoint)
        armed = armBreakpoint(thread, site, site.writeIns.address) || armed;
    }
    if (armed)
      breakpointThreads.set(thread.id, thread);
  }
  for (const id of Array.from(breakpointThreads.keys())) {
    if (!current.has(id))
      breakpointThreads.delete(id);
  }
}

function hasBreakpointSites() {
  for (const site of sites.values()) {
    if (site.breakpoint)
      return true;
  }
  return false;
}

function uninstallBreakpointSite(site) {
  for (const thread of Process.enumerateThreads()) {
    try {
      thread.unsetHardwareBreakpoint(site.slot);
    } catch (_error) {
    }
  }
  const key = site.writeIns.address.toString();
  const next = site.writeIns.next.toString();
  breakpointAddresses.delete(key);
  breakpointAddresses.delete(next);
  retiredBreakpoints.add(key);
  retiredBreakpoints.add(next);
  sites.delete(key);
  if (!hasBreakpointSites())
    breakpointThreads.clear();
}

function onBreakpointException(details) {
  // true if the trap was a breakpoint site's, else undefined.
  const pc = details.context.pc.toString();
  const entry = breakpointAddresses.get(pc);
  if (entry === undefined)
    return retiredBreakpoints.has(pc) ? true : undefined;
  const { site, after } = entry;
  const id = Process.getCurrentThreadId();
  let thread = breakpointThreads.get(id);
  if (thread === undefined) {
    thread = Process.enumerateThreads().find(t => t.id === id);
    if (thread === undefined)
      return undefined;
    breakpointThreads.set(id, thread);
  }
  if (after)
    onAfter(site);
  else
    onBefore(site, contextRegisters(details.context));
  const moveTo = after ? site.writeIns.address : site.writeIns.next;
  if (!armBreakpoint(thread, site, moveTo)) {
    // The trap would repeat forever: this thread goes without the hook.
    try {
      thread.unsetHardwareBreakpoint(site.slot);
    } catch (error) {
      reportAgentError(`breakpoint ${pc}`, error);
    }
  }
  return true;
}

function ensureExceptionHandler() {
  // Installed once and kept: removing it could let a late trap through.
  if (exceptionHandlerInstalled)
    return;
  Process.setExceptionHandler(onException);
  exceptionHandlerInstalled = true;
}

function onException(details) {
  // Linux reports hardware data watchpoint traps as 'single-step'.
  if (details.type !== 'breakpoint' && details.type !== 'single-step')
    return false;
  const handled = onBreakpointException(details);
  return handled === undefined ? onTrainingException(details) : handled;
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

  const site = existingSite !== undefined ? existingSite
    : spec.breakpoint === true ? installBreakpointSite(writeIns, writeOp)
      : installSite(writeIns, writeOp, spec.shortJump === true);
  const learned = resolveCallers(spec.callers, writeIns.address);
  const hook = {
    id: spec.id,
    site,
    target,
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
    trampoline: site.code === null ? null : site.code.toString(),
    patchSize: site.patchSize,
    breakpoint: site.breakpoint ? site.slot : null,
    cave: site.cave === null ? null : site.cave.toString(),
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
  ensureExceptionHandler();
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

function freeDebugSlot() {
  const used = new Set(Array.from(trainings.values(), t => t.slot));
  for (const site of sites.values()) {
    if (site.breakpoint)
      used.add(site.slot);
  }
  return DEBUG_SLOTS.find(slot => !used.has(slot)) ?? null;
}

function updateTrainings() {
  for (const training of trainings.values()) {
    if (training.slot === null)
      training.slot = freeDebugSlot();
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
    if (site.code !== null && pc.compare(site.code) >= 0
        && pc.compare(site.code.add(Process.pageSize)) < 0)
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
    const last = hook.stats.last;
    events.push({
      hookId: hook.id,
      count: hook.stats.count,
      last: last === null ? null : { ...last, address: last.address.toString() }
    });
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

let flushTicks = 0;
const flushTimer = setInterval(() => {
  engineFound = engine.refresh();
  if (++flushTicks % SITE_CHECK_TICKS === 0) {
    checkSites();
    if (profiling) {
      const profile = profileEvents();
      if (profile.length > 0)
        send({ kind: 'profile', sites: profile });
    }
  }
  // Enumerating threads while one handles a write watch's trap deadlocks.
  if (flushTicks % BREAKPOINT_ARM_TICKS === 0 && !trainingPaused && hasBreakpointSites())
    armNewThreads();
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
  setRepatch(enabled) {
    repatchReverted = enabled;
    return true;
  },
  setProfiling(enabled) {
    profiling = enabled;
    for (const site of sites.values())
      site.profile = undefined;
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
    if (exceptionHandlerInstalled)
      Thread.sleep(0.1);  // let late traps reach the handler before unloading
    for (const id of Array.from(hooks.keys()))
      removeHook(id);
    flushStats();
    return true;
  }
};
