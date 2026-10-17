"""Frida-backed write hooks that rewrite values right after the game stores them.

The hook targets the instruction that writes the target address (found via a
write watch: the reported trap PC is the instruction *after* the write). The
agent replaces it with a jump into a trampoline (code cave)::

    before(regs)            snapshot the old value if the write hits the target
    <relocated write>
    after(regs)             hand old/new value to the ValueTransform, store result
    <rest of the relocated bytes>
    jmp back

This works independently of how the write is encoded (``add [mem], reg``,
``mov [mem], reg``, ``sub``, ...). Several hooks may share one write site;
they run in the order they were added. Only x86-64 targets are supported.
"""

import itertools
import queue
import threading
from dataclasses import dataclass, field

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS, watch_size_for_type
from hermes.hooks.transforms import ValueTransform


_AGENT_SOURCE = r"""
const VALUE_IO = {
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
const hooks = new Map();
const sites = new Map();
const retiredSites = [];

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

function describeInstruction(ins) {
  const module = Process.findModuleByAddress(ins.address);
  const site = module ? `${module.name}+${ins.address.sub(module.base)}` : ins.address.toString();
  return { address: ins.address.toString(), site, text: ins.toString(), size: ins.size };
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

function reportError(hook, error) {
  if (hook.errorReported)
    return;
  hook.errorReported = true;
  send({ kind: 'transform-error', hookId: hook.id, error: String(error) });
}

function applyTransform(hook, threadId, oldValue) {
  const current = hook.io.read(hook.target);
  const ctx = {
    old: oldValue,
    new: current,
    delta: current - oldValue,
    params: hook.transform.params,
    state: hook.transform.state,
    valueType: hook.valueType,
    address: hook.target.toString(),
    writePc: hook.site.writeIns.address.toString(),
    threadId,
    hookId: hook.id
  };
  let result;
  try {
    result = hook.transform.fn(ctx);
    if (result === undefined || result === null) {
      result = current;
    } else {
      result = normalize(result, hook.io);
      if (result !== current)
        hook.io.write(hook.target, result);
    }
    hook.errorReported = false;
  } catch (error) {
    reportError(hook, error);
    return;
  }
  hook.stats.count += 1;
  hook.stats.last = { old: oldValue, new: current, result, threadId };
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

function onBefore(site, block) {
  try {
    const threadId = Process.getCurrentThreadId();
    let regs = null;
    for (const hook of site.hooks.values()) {
      if (!hook.enabled)
        continue;
      if (hook.filterAddress) {
        if (regs === null)
          regs = readRegisters(block);
        const address = effectiveAddress(site.writeIns, site.writeOp, regs);
        if (address !== null
            && !overlaps(address, site.writeOp.size, hook.targetValue, hook.io.size))
          continue;
      }
      hook.pending.set(threadId, hook.io.read(hook.target));
    }
  } catch (error) {
    reportAgentError(`before ${site.writeIns.address}`, error);
  }
}

function onAfter(site, _block) {
  try {
    const threadId = Process.getCurrentThreadId();
    for (const hook of site.hooks.values()) {
      const oldValue = hook.pending.get(threadId);
      if (oldValue === undefined)
        continue;
      hook.pending.delete(threadId);
      applyTransform(hook, threadId, oldValue);
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

function addHook(spec) {
  if (hooks.has(spec.id))
    throw new Error(`Hook ${spec.id} already exists`);
  const io = VALUE_IO[spec.valueType];
  if (io === undefined)
    throw new Error(`Unsupported value type: ${spec.valueType}`);
  const target = ptr(spec.address);
  const transform = compileTransform(spec.transform);

  let writeIns, writeOp;
  const existingSite = spec.writePc === null ? undefined : sites.get(ptr(spec.writePc).toString());
  if (existingSite !== undefined) {
    writeIns = existingSite.writeIns;
    writeOp = existingSite.writeOp;
  } else if (spec.writePc !== null) {
    writeIns = Instruction.parse(ptr(spec.writePc));
    writeOp = writeOperand(writeIns);
    if (writeOp === null)
      throw new Error(`${writeIns} at ${writeIns.address} does not write memory`);
  } else {
    const candidate = resolveWriteInstruction(ptr(spec.trapPc), target, io.size);
    writeIns = candidate.ins;
    writeOp = candidate.op;
  }

  const site = installSite(writeIns, writeOp);
  const hook = {
    id: spec.id,
    site,
    target,
    targetValue: toBigInt(target),
    valueType: spec.valueType,
    io,
    filterAddress: spec.filterAddress,
    transform,
    enabled: true,
    pending: new Map(),
    stats: { count: 0, last: null },
    errorReported: false
  };
  site.hooks.set(spec.id, hook);
  hooks.set(spec.id, hook);

  return {
    id: spec.id,
    writeInstruction: describeInstruction(site.writeIns),
    resumePc: site.writeIns.next.toString(),
    trampoline: site.code.toString(),
    patchSize: site.patchSize,
    operandSize: site.writeOp.size,
    transform: spec.transform.kind
  };
}

function removeHook(id) {
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
}

const flushTimer = setInterval(flushStats, 100);
send({ kind: 'ready', arch: Process.arch });

rpc.exports = {
  addHook,
  removeHook,
  setTransform(id, spec) {
    const hook = requireHook(id);
    hook.transform = compileTransform(spec);
    hook.errorReported = false;
    return true;
  },
  setEnabled(id, enabled) {
    const hook = requireHook(id);
    hook.enabled = enabled;
    if (!enabled)
      hook.pending.clear();
    return true;
  },
  dispose() {
    clearInterval(flushTimer);
    for (const id of Array.from(hooks.keys()))
      removeHook(id);
    flushStats();
    return true;
  }
};
"""


@dataclass
class HookSpec:
    """Where to hook and how to transform the written value.

    Pass either ``trap_pc`` (the PC reported by a write watch, i.e. the
    instruction after the write) or ``write_pc`` (the write instruction itself).
    With ``filter_address`` the transform only runs when the hooked instruction
    actually writes ``address``; disable it only when the effective address
    cannot be computed reliably.
    """

    address: int
    value_type: str
    transform: ValueTransform
    trap_pc: int | None = None
    write_pc: int | None = None
    filter_address: bool = True
    label: str = field(default="", compare=False)

    def __post_init__(self):
        watch_size_for_type(self.value_type)
        if not isinstance(self.transform, ValueTransform):
            raise ValueError("transform must be a ValueTransform instance")
        if self.trap_pc is None and self.write_pc is None:
            raise ValueError("HookSpec needs a trap_pc or a write_pc")

    def to_agent(self, hook_id):
        return {
            "id": hook_id,
            "address": hex(self.address),
            "valueType": self.value_type,
            "trapPc": None if self.trap_pc is None else hex(self.trap_pc),
            "writePc": None if self.write_pc is None else hex(self.write_pc),
            "filterAddress": self.filter_address,
            "transform": self.transform.to_agent(),
        }


def build_agent_source():
    return _AGENT_SOURCE


class HookWorker(QThread):
    """Keep a Frida session open and manage value hooks inside one process.

    The public methods are safe to call from the GUI thread: they enqueue a
    command that the worker thread executes against the agent. Results come
    back through the signals.
    """

    session_ready = pyqtSignal(str)
    hook_installed = pyqtSignal(int, dict)
    hook_failed = pyqtSignal(int, str)
    hook_removed = pyqtSignal(int)
    hook_updated = pyqtSignal(int)
    hook_events = pyqtSignal(list)
    transform_error = pyqtSignal(int, str)
    agent_warning = pyqtSignal(str)
    worker_failed = pyqtSignal(str)
    worker_stopped = pyqtSignal()

    # Shared by all workers so ids stay unique when a worker is replaced.
    _ids = itertools.count(1)

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready_event = threading.Event()
        self._ready_payload = None
        self._agent_error = None
        self._detach_reason = None

    def add_hook(self, spec):
        """Queue a hook and return its id; expect hook_installed or hook_failed."""
        hook_id = next(self._ids)
        self._commands.put(("add", hook_id, spec))
        return hook_id

    def remove_hook(self, hook_id):
        self._commands.put(("remove", hook_id, None))

    def set_transform(self, hook_id, transform):
        if not isinstance(transform, ValueTransform):
            raise ValueError("transform must be a ValueTransform instance")
        self._commands.put(("transform", hook_id, transform))

    def set_enabled(self, hook_id, enabled):
        self._commands.put(("enabled", hook_id, bool(enabled)))

    def run(self):
        session = None
        script = None
        try:
            session = frida.attach(self.pid)
            session.on("detached", self._on_session_detached)
            script = session.create_script(build_agent_source())
            script.on("message", self._on_message)
            script.load()

            if not self._ready_event.wait(5):
                raise RuntimeError("Timed out while loading the hook agent")
            if self._agent_error:
                raise RuntimeError(self._agent_error)
            self.session_ready.emit((self._ready_payload or {}).get("arch", ""))

            while (not self.isInterruptionRequested()
                   and self._detach_reason is None
                   and self._agent_error is None):
                try:
                    command = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._execute(script, *command)

            if self._agent_error and not self.isInterruptionRequested():
                raise RuntimeError(self._agent_error)
            if self._detach_reason and not self.isInterruptionRequested():
                raise RuntimeError(
                    f"Target process detached: {self._detach_reason}"
                )
        except FRIDA_ERRORS + (ImportError, OSError, RuntimeError, ValueError) as error:
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
            self.worker_stopped.emit()

    def _execute(self, script, action, hook_id, argument):
        api = script.exports_sync
        try:
            if action == "add":
                info = api.add_hook(argument.to_agent(hook_id))
                info["label"] = argument.label
                info["transformDescription"] = argument.transform.describe()
                self.hook_installed.emit(hook_id, info)
            elif action == "remove":
                if api.remove_hook(hook_id):
                    self.hook_removed.emit(hook_id)
            elif action == "transform":
                api.set_transform(hook_id, argument.to_agent())
                self.hook_updated.emit(hook_id)
            elif action == "enabled":
                api.set_enabled(hook_id, argument)
                self.hook_updated.emit(hook_id)
        except frida.RPCException as error:
            self.hook_failed.emit(hook_id, str(error))

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = message.get("stack", "Frida hook agent failed")
            self._ready_event.set()
            return

        if message.get("type") != "send":
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready_payload = payload
            self._ready_event.set()
        elif kind == "events":
            self.hook_events.emit(payload.get("events", []))
        elif kind == "agent-error":
            self.agent_warning.emit(payload.get("error", ""))
        elif kind == "transform-error":
            self.transform_error.emit(
                payload.get("hookId", 0), payload.get("error", "")
            )

    def _on_session_detached(self, reason, _crash):
        self._detach_reason = reason
