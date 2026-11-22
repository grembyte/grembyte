// Return addresses on the stack, for agents that ask who called the code
// that runs now: hooks learn callers with it (grembyte/hooks/engine.py tells
// how a candidate is checked), the write watch names the caller of a write.
// Without frame pointers the stack also holds stale return addresses; a
// candidate counts only if its call leads to the function it returns from.

const CALLER_SCAN_SLOTS = 256;
const CALL_SIZES = [5, 2, 3, 6, 7, 4];
// Largest distance from a function's start to code in it (for code without symbols).
const MAX_FUNCTION_SIZE = 0x40000;
// Jumps followed from a call's target: PLT stub, thunk, a patch of another tool.
const THUNK_HOPS = 3;
// Instructions of a prologue looked at for its frame size
const PROLOGUE_INSTRUCTIONS = 24;
const callCache = new Map();
const startCache = new Map();
const verifiedCache = new Map();
const frameCache = new Map();
// Whether an indirect call through a pointer beside the code (``call [rip+…]``,
// how MSVC calls a DLL's function through the import table) is followed to
// the function the pointer holds. Off for hooks: callers they learned and
// saved without it would no longer match. The write watch turns it on.
let followCallPointers = false;

function remember(cache, key, value) {
  if (cache.size >= 4096)
    cache.clear();
  cache.set(key, value);
  return value;
}

// Executable memory as sorted, merged [start, end) pairs: the stack scan
// asks for every value it meets, and Process.findRangeByAddress walks all
// ranges of the process (11 ms per call in Dark Souls III). The C scan uses
// the same snapshot (refreshFastRanges in the hook engine), so both decide alike.
let executableRanges = null;

function loadExecutableRanges() {
  const ranges = Process.enumerateRanges('--x')
    .map(r => [r.base, r.base.add(r.size)])
    .sort((a, b) => a[0].compare(b[0]));
  const merged = [];
  for (const [start, end] of ranges) {
    const last = merged[merged.length - 1];
    if (last !== undefined && start.compare(last[1]) <= 0) {
      if (end.compare(last[1]) > 0)
        last[1] = end;
    } else {
      merged.push([start, end]);
    }
  }
  executableRanges = merged;
  return merged;
}

function isExecutable(address) {
  const ranges = executableRanges ?? loadExecutableRanges();
  let low = 0;
  let high = ranges.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (address.compare(ranges[middle][0]) < 0)
      high = middle - 1;
    else if (address.compare(ranges[middle][1]) >= 0)
      low = middle + 1;
    else
      return true;
  }
  return false;
}

function callBefore(address) {
  // The call a return address follows: { target, slot } or null. ``target``
  // is null for an indirect call; ``slot`` is where ``call [rip+…]`` reads it.
  const key = address.toString();
  const known = callCache.get(key);
  if (known !== undefined)
    return known;
  // Not cached outside executable memory: the value may be code later (JIT).
  if (!isExecutable(address))
    return null;
  let result = null;
  for (const size of CALL_SIZES) {
    try {
      const ins = Instruction.parse(address.sub(size));
      if (ins.mnemonic === 'call' && ins.next.equals(address)) {
        const operand = ins.operands[0];
        const viaSlot = operand.type === 'mem' && operand.value.base === 'rip'
          && !operand.value.index;
        result = {
          target: operand.type === 'imm' ? ptr(operand.value.toString()) : null,
          slot: viaSlot ? ins.next.add(operand.value.disp) : null
        };
        break;
      }
    } catch (_error) {
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
  if (Process.platform === 'windows')
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
    if (followCallPointers && call.slot !== null) {
      try {
        result = jumpTargets(call.slot.readPointer()).find(target => leadsTo(target, address))
          || null;
      } catch (_error) {
      }
    }
    if (result === null
        && (sameRegion(returnAddress, address) || Process.findModuleByAddress(address) === null))
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

function callerFrame(frameRsp, inside, teacher = null) {
  // The return address of the function running at ``inside`` with its stack
  // at ``frameRsp``, and the slot holding it; null if not found. ``teacher``
  // ({ skip, verdict }) learns what the scan decided (the hook engine's C scan).
  const symbolStart = functionStart(inside);
  const known = symbolStart === null ? null : frameSize(symbolStart, inside);
  if (teacher !== null)
    teacher.skip(inside, known);
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
    const below = start === null || start === true ? null : frameSize(start, inside);
    if (teacher !== null && (start !== null || isExecutable(value)))
      teacher.verdict(value, inside, start, below);
    if (start === null)
      continue;
    if (known === null && start !== true) {
      if (below !== null && cursor.sub(frameRsp).compare(ptr(below)) < 0)
        continue;  // inside the frame the call made
    }
    return { value, slot: cursor };
  }
  return null;
}

function callerFrames(rsp, depth, pc, teacher = null) {
  // The return addresses of the ``depth`` calls that led to ``pc``, innermost
  // first, as ``key``, and the distances of their slots from ``rsp``.
  const frames = [];
  const offsets = [];
  let inside = pc;
  let frameRsp = rsp;
  while (frames.length < depth) {
    const found = callerFrame(frameRsp, inside, teacher);
    if (found === null)
      return null;
    frames.push(found.value.toString());
    offsets.push(found.slot.sub(rsp).toInt32());
    inside = found.value;
    frameRsp = found.slot.add(Process.pointerSize);  // the caller's after the return
  }
  return { key: frames.join(','), offsets };
}
