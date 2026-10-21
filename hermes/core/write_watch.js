const targetAddress = ptr('__ADDRESS__');
const targetSize = __SIZE__;
const conditions = '__CONDITIONS__';  // 'w' or 'rw'
const watchpointId = 0;
const MAX_INSTRUCTION_SIZE = 15;
const SYNC_WINDOW = 48;
// Read/write per trap PC; decoding is too slow for every frequent read.
const accessByPc = new Map();
// An access watchpoint traps reads, too, so reading the target from a
// trapping thread fails. Its value is read when the hits are sent instead,
// by the agent's own thread, which has no watchpoint.
const readInHandler = conditions === 'w';
const armedThreads = new Map();
const pendingHits = new Map();
const threadErrors = [];
let initializing = true;
let threadObserver = null;

function readTargetBytes() {
  try {
    return Array.from(new Uint8Array(targetAddress.readByteArray(targetSize)))
      .map(value => value.toString(16).padStart(2, '0'))
      .join(' ');
  } catch (_error) {
    return 'unreadable';
  }
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

function decodeAccess(pc) {
  // The trap PC is the instruction after the access.
  let best = null;
  for (let back = 1; back <= MAX_INSTRUCTION_SIZE; back++) {
    let ins;
    try {
      ins = Instruction.parse(pc.sub(back));
    } catch (_error) {
      continue;
    }
    if (!ins.next.equals(pc))
      continue;
    const op = (ins.operands || []).find(
      operand => operand.type === 'mem' && typeof operand.access === 'string');
    if (op === undefined)
      continue;
    const votes = syncVotes(ins.address);
    if (best === null || votes > best.votes)
      best = { votes, read: op.access.includes('r'), write: op.access.includes('w') };
  }
  return best;
}

function accessOf(pc) {
  const key = pc.toString();
  let access = accessByPc.get(key);
  if (access === undefined) {
    // Without a decodable memory operand (e.g. movs) it may do either.
    access = decodeAccess(pc) || { read: conditions !== 'w', write: true };
    if (conditions === 'w')
      access.write = true;  // a write watchpoint only traps on writes
    accessByPc.set(key, access);
  }
  return access;
}

function recordHit(threadId, context) {
  const pc = context.pc;
  const value = readInHandler ? readTargetBytes() : null;
  const access = accessOf(pc);
  const module = Process.findModuleByAddress(pc);
  const site = module
    ? `${module.name}+${pc.sub(module.base)}`
    : pc.toString();
  const key = pc.toString();
  const hit = pendingHits.get(key);
  if (hit) {
    hit.count += 1;
    hit.value = value;
    hit.read = hit.read || access.read;
    hit.write = hit.write || access.write;
    hit.threadId = threadId;
    hit.time = Date.now();
    return;
  }
  // The trapping thread runs the JIT code, so Mono can name it here.
  if (module === null)
    engine.locate(pc);
  pendingHits.set(key, {
    pc: key,
    site,
    value,
    read: access.read,
    write: access.write,
    threadId,
    count: 1,
    time: Date.now()
  });
}

Process.setExceptionHandler(details => {
  // Linux reports hardware data watchpoint traps as 'single-step'.
  if (details.type !== 'breakpoint' && details.type !== 'single-step')
    return false;

  const threadId = Process.getCurrentThreadId();
  if (!armedThreads.has(threadId))
    return false;

  recordHit(threadId, details.context);
  return true;
});

function armThread(thread) {
  if (armedThreads.has(thread.id))
    return;
  try {
    thread.setHardwareWatchpoint(watchpointId, targetAddress, targetSize, conditions);
    armedThreads.set(thread.id, thread);
  } catch (error) {
    const warning = {
      kind: 'thread-error',
      threadId: thread.id,
      error: String(error)
    };
    if (initializing)
      threadErrors.push(warning);
    else
      send(warning);
  }
}

for (const thread of Process.enumerateThreads())
  armThread(thread);

threadObserver = Process.attachThreadObserver({
  onAdded(thread) {
    armThread(thread);
  },
  onRemoved(thread) {
    armedThreads.delete(thread.id);
  }
});
initializing = false;

function flushHits() {
  if (pendingHits.size === 0)
    return;
  const hits = Array.from(pendingHits.values());
  pendingHits.clear();
  engine.refresh();
  for (const hit of hits) {
    const method = engine.name(ptr(hit.pc));
    if (method !== null)
      hit.method = method;
  }
  if (!readInHandler) {
    const value = readTargetBytes();
    for (const hit of hits)
      hit.value = value;
  }
  send({ kind: 'hits', hits });
}

const flushTimer = setInterval(flushHits, 100);
send({
  kind: 'ready',
  threadCount: armedThreads.size,
  warnings: threadErrors
});

rpc.exports = {
  stop() {
    clearInterval(flushTimer);
    if (threadObserver !== null)
      threadObserver.detach();
    for (const thread of armedThreads.values()) {
      try {
        thread.unsetHardwareWatchpoint(watchpointId);
      } catch (_error) {
      }
    }
    armedThreads.clear();
    // A trap raised just before the watchpoints were removed may still be
    // on its way; without the handler it would reach the program.
    Thread.sleep(0.1);
    Process.setExceptionHandler(null);
    flushHits();
    return true;
  }
};
