const FUNCTION_CAPACITY = FUNCTION_CAPACITY_VALUE;
const FIRST_CAPACITY = FIRST_CAPACITY_VALUE;
const EVENT_CAPACITY = EVENT_CAPACITY_VALUE;
const SYMBOL_BUDGET_MS = SYMBOL_BUDGET_VALUE;
const EVENT_SIZE = 32;
const THREAD_SIZE = 8;
const ENTRY_POLL_MS = 20;
const STOP_GC_TIMEOUT = 500;
const DISPOSE_GC_TIMEOUT = 3000;
const PHASES = { baseline: 1, scenario: 2 };
const WINDOWS = Process.platform === 'windows';
const AGENT_THREAD = Process.getCurrentThreadId();

// The transform puts a callout before every call of the followed code; the
// callout runs in the game's thread, works out where the call goes and
// counts it. Functions sit in a hash table by address (slot = function to
// the C side), first calls per thread in another one.
const C_SOURCE = `
#include <gum/gumstalker.h>

#define FUNCTIONS ${FUNCTION_CAPACITY}
#define FIRSTS ${FIRST_CAPACITY}
#define CAPACITY ${EVENT_CAPACITY}
#define PROBES 512

enum { TARGET_KNOWN, TARGET_REGISTER, TARGET_MEMORY };

typedef struct {
  guint32 seq;
  guint32 function;
  guint32 thread;
  guint32 reserved;
  guint64 caller;
  gint64 time;
} Event;

typedef struct {
  guint32 id;
  volatile guint32 entered;  /* runs in Stalker's code */
} Thread;

typedef struct {
  gsize site;    /* the call instruction */
  gsize next;    /* the instruction after it: rip-relative operands */
  gsize target;  /* TARGET_KNOWN */
  guint32 thread;
  gint kind;
  gint base;     /* x86_reg */
  gint index;
  gint scale;
  gint64 disp;
} Site;

/* phase (0 off, 1 baseline, 2 scenario), events, lost calls, active callouts */
extern volatile gint state[4];
extern volatile gsize keys[];     /* function address per slot, 0: free */
extern volatile gint claims[];
extern volatile gint counts[];
extern volatile guint8 quiet[];   /* called in the baseline */
extern volatile gint first_claims[];
extern volatile gint first_functions[];  /* slot + 1 */
extern volatile gint first_threads[];
extern Event events[];

gint64 now (void) {
  return g_get_monotonic_time ();
}

static gsize read_register (GumCpuContext * c, gint reg, const Site * s) {
  switch (reg) {
#if defined (__x86_64__)
    case X86_REG_RAX: return c->rax;
    case X86_REG_RBX: return c->rbx;
    case X86_REG_RCX: return c->rcx;
    case X86_REG_RDX: return c->rdx;
    case X86_REG_RSI: return c->rsi;
    case X86_REG_RDI: return c->rdi;
    case X86_REG_RBP: return c->rbp;
    case X86_REG_RSP: return c->rsp;
    case X86_REG_R8: return c->r8;
    case X86_REG_R9: return c->r9;
    case X86_REG_R10: return c->r10;
    case X86_REG_R11: return c->r11;
    case X86_REG_R12: return c->r12;
    case X86_REG_R13: return c->r13;
    case X86_REG_R14: return c->r14;
    case X86_REG_R15: return c->r15;
    case X86_REG_RIP: return s->next;
#elif defined (__i386__)
    case X86_REG_EAX: return c->eax;
    case X86_REG_EBX: return c->ebx;
    case X86_REG_ECX: return c->ecx;
    case X86_REG_EDX: return c->edx;
    case X86_REG_ESI: return c->esi;
    case X86_REG_EDI: return c->edi;
    case X86_REG_EBP: return c->ebp;
    case X86_REG_ESP: return c->esp;
    case X86_REG_EIP: return s->next;
#else
# error "the native trace needs an x86 processor"
#endif
    default: return 0;  /* X86_REG_INVALID: no base or index */
  }
}

static gsize call_target (GumCpuContext * c, const Site * s) {
  gsize address;
  if (s->kind == TARGET_KNOWN)
    return s->target;
  if (s->kind == TARGET_REGISTER)
    return read_register (c, s->base, s);
  address = read_register (c, s->base, s) + read_register (c, s->index, s) * s->scale
      + (gsize) s->disp;
  return *(const gsize *) GSIZE_TO_POINTER (address);
}

/* No compare-and-swap in Frida's TinyCC: a thread takes a free entry by
   counting up its claims, the one that counted from 0 owns it and writes the
   key; the others wait for that. */

static gint function_slot (gsize target) {
  guint i = (guint) ((target >> 4) * 0x9E3779B1u) & (FUNCTIONS - 1);
  guint probe;
  for (probe = 0; probe < PROBES; probe++) {
    gsize key = keys[i];
    if (key == 0 && claims[i] == 0 && g_atomic_int_add (&claims[i], 1) == 0) {
      keys[i] = target;
      return i;
    }
    while (key == 0)  /* taken just now: the key is written next */
      key = keys[i];
    if (key == target)
      return i;
    i = (i + 1) & (FUNCTIONS - 1);
  }
  return -1;
}

static gboolean first_call (gint function, guint32 thread) {
  /* Whether this is the first call of function in thread: notes it if so. */
  guint i = ((guint) function * 0x9E3779B1u ^ thread * 0x85EBCA6Bu) & (FIRSTS - 1);
  guint probe;
  for (probe = 0; probe < PROBES; probe++) {
    gint t;
    if (first_claims[i] == 0 && g_atomic_int_add (&first_claims[i], 1) == 0) {
      first_functions[i] = function + 1;
      first_threads[i] = thread;  /* last: the entry is complete */
      return TRUE;
    }
    /* taken just now (begin may clear it meanwhile) */
    while ((t = first_threads[i]) == 0 && first_claims[i] != 0)
      ;
    if (t == (gint) thread && first_functions[i] == function + 1)
      return FALSE;
    i = (i + 1) & (FIRSTS - 1);
  }
  return FALSE;  /* full: the event capacity is long reached */
}

static void on_call (GumCpuContext * c, gpointer data) {
  const Site * s = data;
  gint phase;
  gsize target;
  gint slot, at;
  Event * event;
  g_atomic_int_add (&state[3], 1);
  phase = state[0];
  if (phase == 0)
    goto done;
  target = call_target (c, s);
  if (target == 0)
    goto done;
  slot = function_slot (target);
  if (slot < 0) {
    g_atomic_int_add (&state[2], 1);
    goto done;
  }
  g_atomic_int_add (&counts[slot], 1);
  if (phase != 2 || quiet[slot] || !first_call (slot, s->thread))
    goto done;
  at = g_atomic_int_add (&state[1], 1);
  if (at >= CAPACITY)
    goto done;
  event = &events[at];
  event->function = slot;
  event->thread = s->thread;
  event->caller = s->site;
  event->time = g_get_monotonic_time ();
  /* last: the reader skips events still being written */
  ((volatile Event *) event)->seq = at + 1;
done:
  g_atomic_int_add (&state[3], -1);
}

static Site * describe (const cs_insn * insn, guint32 thread) {
  const cs_x86_op * op = &insn->detail->x86.operands[0];
  Site * s;
  gsize next = insn->address + insn->size;
  if (op->type == X86_OP_IMM && (gsize) op->imm == next)
    return NULL;  /* call $+5: code reading its own address (32-bit PIC) */
  if (op->type == X86_OP_MEM && op->mem.segment != X86_REG_INVALID)
    return NULL;  /* fs:/gs: the segment's base is not in the context */
  if (op->type != X86_OP_IMM && op->type != X86_OP_REG && op->type != X86_OP_MEM)
    return NULL;
  s = g_malloc0 (sizeof (Site));
  s->site = insn->address;
  s->next = next;
  s->thread = thread;
  if (op->type == X86_OP_IMM) {
    s->kind = TARGET_KNOWN;
    s->target = (gsize) op->imm;
  } else if (op->type == X86_OP_REG) {
    s->kind = TARGET_REGISTER;
    s->base = op->reg;
  } else {
    s->kind = TARGET_MEMORY;
    s->base = op->mem.base;
    s->index = op->mem.index;
    s->scale = op->mem.scale;
    s->disp = op->mem.disp;
  }
  return s;
}

void transform (GumStalkerIterator * iterator, GumStalkerOutput * output, gpointer user_data) {
  /* Runs in the followed thread whenever Stalker compiles a block of its code. */
  Thread * thread = user_data;
  const cs_insn * insn;
  thread->entered = 1;
  while (gum_stalker_iterator_next (iterator, &insn)) {
    if (insn->id == X86_INS_CALL) {
      Site * site = describe (insn, thread->id);
      if (site != NULL)
        gum_stalker_iterator_put_callout (iterator, on_call, site, g_free);
    }
    gum_stalker_iterator_keep (iterator);
  }
}

void tally (guint64 * out) {
  /* calls and functions of the phase so far */
  guint64 calls = 0;
  guint64 functions = 0;
  guint i;
  for (i = 0; i < FUNCTIONS; i++) {
    guint count = counts[i];
    if (count != 0) {
      calls += count;
      functions++;
    }
  }
  out[0] = calls;
  out[1] = functions;
}
`;

let trace = null;
let modules = null;
// Traces stopped: a thread blocked in a call since may come back into code
// with callouts into their CModule.
const retired = [];

function folder(path) {
  const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  return (cut < 0 ? path : path.substring(0, cut + 1)).toLowerCase();
}

function isGameModule(module, gameFolder) {
  // The game's own code: the program and what lies in its folder tree.
  return module.path.toLowerCase().startsWith(gameFolder);
}

function inventory() {
  const main = Process.mainModule;
  const gameFolder = folder(main.path);
  modules = Process.enumerateModules();
  return {
    platform: Process.platform,
    threads: Process.enumerateThreads()
      .filter(thread => thread.id !== AGENT_THREAD)
      .map(thread => ({ id: thread.id, name: thread.name || '', state: thread.state })),
    modules: modules.map(module => ({
      name: module.name, path: module.path, base: module.base.toString(), size: module.size,
      game: module.name === main.name || isGameModule(module, gameFolder)
    }))
  };
}

function zero(pointer, length) {
  if (length > 0)
    pointer.writeByteArray(new ArrayBuffer(length));
}

function compile(threadCount) {
  // The CModule and its memory, all of it zeroed.
  if (Process.arch !== 'x64' && Process.arch !== 'ia32')
    throw new Error(`the native trace needs an x86 processor, not ${Process.arch}`);
  const memory = {
    state: Memory.alloc(16),
    keys: Memory.alloc(Process.pointerSize * FUNCTION_CAPACITY),
    claims: Memory.alloc(4 * FUNCTION_CAPACITY),
    counts: Memory.alloc(4 * FUNCTION_CAPACITY),
    quiet: Memory.alloc(FUNCTION_CAPACITY),
    first_claims: Memory.alloc(4 * FIRST_CAPACITY),
    first_functions: Memory.alloc(4 * FIRST_CAPACITY),
    first_threads: Memory.alloc(4 * FIRST_CAPACITY),
    events: Memory.alloc(EVENT_SIZE * EVENT_CAPACITY)
  };
  zero(memory.state, 16);
  zero(memory.keys, Process.pointerSize * FUNCTION_CAPACITY);
  zero(memory.claims, 4 * FUNCTION_CAPACITY);
  zero(memory.counts, 4 * FUNCTION_CAPACITY);
  zero(memory.quiet, FUNCTION_CAPACITY);
  const cm = new CModule(C_SOURCE, memory);
  const tallied = Memory.alloc(16);
  const tally = new NativeFunction(cm.tally, 'void', ['pointer']);
  return {
    ...memory, cm,
    now: new NativeFunction(cm.now, 'int64', []),
    tally() {
      tally(tallied);
      return [Number(tallied.readU64()), Number(tallied.add(8).readU64())];
    },
    threadInfo: Memory.alloc(THREAD_SIZE * Math.max(threadCount, 1))
  };
}

function start(spec) {
  // spec: { threads: [ids], modules: [names to follow], ranges: [[base, size]] to run natively }
  if (trace !== null)
    throw new Error('a trace is running already');
  if (modules === null)
    inventory();
  const compiled = compile(spec.threads.length);
  const followed = new Set(spec.modules);
  const included = [];
  for (const module of modules) {
    if (followed.has(module.name))
      included.push(module);
    else
      Stalker.exclude(module);  // runs natively: only calls into it are seen
  }
  // Code Frida knows no module of (Wine's Unix side): runs natively as well.
  // A 32-bit game cannot reach the 64-bit libraries of the new WoW64.
  for (const [base, size] of spec.ranges || []) {
    if (Process.pointerSize === 4 && uint64(base).add(size).compare(uint64('0x100000000')) > 0)
      continue;
    Stalker.exclude({ base: ptr(base), size });
  }
  trace = {
    ...compiled,
    all: new ModuleMap(),
    slots: new Map(),       // slot of the C side -> function number
    index: new Map(),       // target address text -> function number
    functions: [],          // [address, module, offset]
    reported: 0,            // functions already sent to Python
    named: new Set(),       // functions whose name was looked up
    exports: new Map(),     // module name -> Map(address text -> export name)
    baseline: null,         // Uint8Array: slots called in the baseline
    phase: null,
    began: 0,
    beganMono: 0n,
    threads: [],            // [id, Thread of the C side]
    entered: new Set(),     // threads that run in Stalker's code by now
    entryTimer: null
  };
  const failed = [];
  for (const id of spec.threads) {
    if (id === AGENT_THREAD)
      continue;
    const info = trace.threadInfo.add(THREAD_SIZE * trace.threads.length);
    info.writeU32(id);
    info.add(4).writeU32(0);
    try {
      Stalker.follow(id, { transform: trace.cm.transform, data: info });
      trace.threads.push([id, info]);
    } catch (error) {
      failed.push(id);  // e.g. ended meanwhile
    }
  }
  if (WINDOWS)
    trace.entryTimer = setInterval(() => clearEntered(trace), ENTRY_POLL_MS);
  return { threads: trace.threads.length, failed: failed.length, modules: included.length };
}

function clearEntered(t) {
  // The entry breakpoints of the threads that run in Stalker's code by now.
  if (t === null)
    return;
  const entered = [];
  for (const [id, info] of t.threads) {
    if (!t.entered.has(id) && info.add(4).readU32() !== 0) {
      t.entered.add(id);
      entered.push(id);
    }
  }
  if (entered.length > 0)
    clearEntryBreakpoints(entered);
  if (t.entered.size === t.threads.length) {
    clearInterval(t.entryTimer);
    t.entryTimer = null;
  }
}

function functionNumber(target) {
  let number = trace.index.get(target);
  if (number === undefined) {
    number = trace.functions.length;
    trace.index.set(target, number);
    const address = ptr(target);
    const module = trace.all.find(address);
    trace.functions.push(module === null
      ? [target, '', null]
      : [target, module.name, address.sub(module.base).toInt32()]);
  }
  return number;
}

function readKeys(t) {
  const bytes = t.keys.readByteArray(Process.pointerSize * FUNCTION_CAPACITY);
  return Process.pointerSize === 8 ? new BigUint64Array(bytes) : new Uint32Array(bytes);
}

function slotNumbers(t, slots) {
  // Function numbers of C slots, numbering the ones not seen before.
  let keys = null;
  return slots.map(slot => {
    let number = t.slots.get(slot);
    if (number === undefined) {
      keys = keys || readKeys(t);
      number = functionNumber('0x' + keys[slot].toString(16));
      t.slots.set(slot, number);
    }
    return number;
  });
}

function requireTrace() {
  if (trace === null)
    throw new Error('no trace is running');
  return trace;
}

async function begin(phase) {
  const t = requireTrace();
  if (PHASES[phase] === undefined)
    throw new Error(`unknown phase ${phase}`);
  t.state.writeS32(0);  // calls from before belong to no phase
  t.phase = null;
  // Let callouts that observed the previous phase finish before clearing tables.
  while (t.state.add(12).readS32() !== 0)
    await new Promise(resolve => setTimeout(resolve, 1));
  zero(t.counts, 4 * FUNCTION_CAPACITY);
  zero(t.first_claims, 4 * FIRST_CAPACITY);
  zero(t.first_functions, 4 * FIRST_CAPACITY);
  zero(t.first_threads, 4 * FIRST_CAPACITY);
  t.state.add(4).writeS32(0);
  t.state.add(8).writeS32(0);
  if (phase === 'scenario')
    zero(t.events, EVENT_SIZE * EVENT_CAPACITY);  // no seq of an earlier scenario
  if (phase === 'scenario' && t.baseline !== null)
    t.quiet.writeByteArray(t.baseline.buffer);
  else
    zero(t.quiet, FUNCTION_CAPACITY);
  t.phase = phase;
  t.began = Date.now();
  t.beganMono = BigInt(t.now().toString());
  t.state.writeS32(PHASES[phase]);
  return true;
}

function exportName(t, moduleName, target) {
  let names = t.exports.get(moduleName);
  if (names === undefined) {
    names = new Map();
    try {
      for (const found of Process.getModuleByName(moduleName).enumerateExports())
        names.set(found.address.toString(), found.name);
    } catch (_error) {
      // no exports to name it by
    }
    t.exports.set(moduleName, names);
  }
  return names.get(target) || null;
}

function symbolName(t, number) {
  // An export of that address, or (not under Windows: dbghelp under Wine)
  // a symbol of the module's symbol table or debug info.
  const [target, moduleName] = t.functions[number];
  if (moduleName === '')
    return null;
  const exported = exportName(t, moduleName, target);
  if (exported !== null || WINDOWS)
    return exported;
  try {
    const symbol = DebugSymbol.fromAddress(ptr(target));
    // Frida names an address without a symbol by its offset: "0x390"
    if (symbol.name !== null && !symbol.name.startsWith('0x') && symbol.address.equals(ptr(target)))
      return symbol.name;
  } catch (_error) {
    // unnamed
  }
  return null;
}

function callerText(t, address) {
  // ``module+0x…``, or the bare address for code outside modules.
  const module = t.all.find(address);
  return module === null ? address.toString() : `${module.name}+${address.sub(module.base)}`;
}

function readEvents(t) {
  // [[function number, thread, µs since the phase began, caller]] in call order.
  const written = t.state.add(4).readS32();
  const total = Math.min(written, EVENT_CAPACITY);
  const raw = [];
  if (total > 0) {
    const view = new DataView(t.events.readByteArray(total * EVENT_SIZE));
    for (let i = 0; i < total; i++) {
      const base = i * EVENT_SIZE;
      if (view.getUint32(base, true) !== i + 1)
        continue;
      raw.push([view.getUint32(base + 4, true), view.getUint32(base + 8, true),
                Number(view.getBigInt64(base + 24, true) - t.beganMono),
                view.getBigUint64(base + 16, true)]);
    }
  }
  const numbers = slotNumbers(t, raw.map(event => event[0]));
  const callers = new Map();
  return {
    events: raw.map(([_slot, thread, time, caller], i) => {
      let text = callers.get(caller);
      if (text === undefined) {
        text = callerText(t, ptr('0x' + caller.toString(16)));
        callers.set(caller, text);
      }
      return [numbers[i], thread, time, text];
    }),
    dropped: Math.max(0, written - EVENT_CAPACITY)
  };
}

function finish() {
  const t = requireTrace();
  const phase = t.phase;
  if (phase === null)
    throw new Error('nothing is being recorded');
  t.state.writeS32(0);
  t.phase = null;
  const duration = Date.now() - t.began;
  const slotCounts = new Uint32Array(t.counts.readByteArray(4 * FUNCTION_CAPACITY));
  const called = [];
  slotCounts.forEach((count, slot) => {
    if (count > 0)
      called.push(slot);
  });
  const counts = {};
  slotNumbers(t, called).forEach((number, i) => {
    counts[number] = slotCounts[called[i]];
  });
  const result = { phase, duration, counts, events: [], dropped: 0,
                   lost: t.state.add(8).readS32() };
  if (phase === 'scenario') {
    Object.assign(result, readEvents(t));
  } else {
    t.baseline = new Uint8Array(FUNCTION_CAPACITY);
    for (const slot of called)
      t.baseline[slot] = 1;
  }
  result.functions = t.functions.slice(t.reported);
  result.firstFunction = t.reported;
  t.reported = t.functions.length;
  // Names only for what the scenario called: the rows show nothing else.
  const names = {};
  const deadline = Date.now() + SYMBOL_BUDGET_MS;
  for (const key of phase === 'scenario' ? Object.keys(counts) : []) {
    const number = Number(key);
    if (t.named.has(number))
      continue;
    if (Date.now() > deadline)
      break;
    t.named.add(number);
    const name = symbolName(t, number);
    if (name !== null)
      names[number] = name;
  }
  result.names = names;
  return result;
}

function live() {
  const t = trace;
  if (t === null || t.phase === null)
    return;
  const [calls, methods] = t.tally();
  send({ kind: 'live', phase: t.phase, calls, methods,
         events: Math.min(t.state.add(4).readS32(), EVENT_CAPACITY) });
}

function clearEntryBreakpoints(ids) {
  // Under Windows Stalker enters a thread through a hardware breakpoint at
  // the instruction it stands at (1 and 2 serve it too in 32-bit programs),
  // and removes it by writing the old debug registers into the exception's
  // context. Wine hands over that context with debug registers of zero, so
  // nothing changes and the breakpoint stays: once Stalker forgot the
  // thread, its next pass there (a system call's return) ended Baldur's
  // Gate under Proton with EXCEPTION_SINGLE_STEP.
  const slots = Process.pointerSize === 4 ? [0, 1, 2] : [0];
  const wanted = new Set(ids);
  for (const thread of Process.enumerateThreads()) {
    if (!wanted.has(thread.id))
      continue;
    for (const slot of slots) {
      try {
        thread.unsetHardwareBreakpoint(slot);
      } catch (_error) {
        // ended meanwhile
      }
    }
  }
}

function stop() {
  // Exclusions stay with the Stalker: Python loads a fresh agent for the next trace.
  if (trace === null)
    return false;
  const t = trace;
  trace = null;
  t.state.writeS32(0);
  if (t.entryTimer !== null)
    clearInterval(t.entryTimer);
  const ids = t.threads.map(([id]) => id);
  if (WINDOWS)
    clearEntryBreakpoints(ids);
  for (const id of ids) {
    try {
      Stalker.unfollow(id);
    } catch (_error) {
      // ended meanwhile
    }
  }
  retired.push(t);
  return true;
}

async function collect(timeout) {
  // Free the recompiled code once the threads have left it (garbageCollect:
  // whether some is still in use). A thread blocked in a call since it was
  // followed holds it until the call returns; Frida then frees it on unload.
  const deadline = Date.now() + timeout;
  while (Stalker.garbageCollect() && Date.now() < deadline)
    await new Promise(resolve => setTimeout(resolve, 20));
}

const liveTimer = setInterval(live, 500);
send({ kind: 'ready' });

rpc.exports = {
  inventory,
  start,
  begin,
  finish,
  async stop() {
    const stopped = stop();
    await collect(STOP_GC_TIMEOUT);
    return stopped;
  },
  async dispose() {
    clearInterval(liveTimer);
    stop();
    await collect(DISPOSE_GC_TIMEOUT);
    return true;
  }
};
