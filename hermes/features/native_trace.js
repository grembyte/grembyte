const FUNCTION_CAPACITY = FUNCTION_CAPACITY_VALUE;
const FIRST_CAPACITY = FIRST_CAPACITY_VALUE;
const EVENT_CAPACITY = EVENT_CAPACITY_VALUE;
const SYMBOL_BUDGET_MS = SYMBOL_BUDGET_VALUE;
const EVENT_SIZE = 32;
const THREAD_SIZE = 8;
const STOP_GC_TIMEOUT = 500;
// How long begin waits for callouts of the previous phase (ms).
const QUIESCE_TIMEOUT = 2000;
const DISPOSE_GC_TIMEOUT = 3000;
const PHASES = { baseline: 1, scenario: 2 };
const WINDOWS = Process.platform === 'windows';
const AGENT_THREAD = Process.getCurrentThreadId();
// Linux x86-64: free address space Stalker's thread contexts should land at
// the top of, see withRoomNearby.
const STEER_CONTEXTS = Process.platform === 'linux' && Process.arch === 'x64';
const LANDING_SIZE = 256n * 1024n * 1024n;
const STEER_PASSES = 8;

// The transform puts a callout before every call of the followed code; the
// callout runs in the game's thread, works out where the call goes and
// counts it. Functions sit in a hash table by address (slot = function to
// the C side), first calls per thread in another one.
const C_SOURCE = `
/* Frida 17.24's capstone.h stops TinyCC with a "#pragma message" for
   compilers it does not know; as __IBMC__ it takes the GCC attributes,
   which TinyCC understands. */
#define CAPSTONE_STATIC
#define __IBMC__ 1
#include <gum/gumstalker.h>
#undef __IBMC__

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
  volatile gint left;  /* Windows: it called leave() */
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
  Thread * info;
} Site;

/* phase (0 off, 1 baseline, 2 scenario), events, lost calls, active
   callouts, threads to leave (Windows) */
extern volatile gint state[8];
/* Windows: Stalker.unfollow() of the calling thread */
extern void leave (void);
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

/* Only the agent writes the phase. A locked add rather than a plain store:
   it is a full barrier, so the agent's following read of state[3] cannot
   pass it (the callouts' locked add on state[3] is the other half). */
void set_phase (gint phase) {
  g_atomic_int_add (&state[0], phase - state[0]);
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
  /* Windows: the thread stops following itself, once. Stalker lets it go
     at the next block; taken over from outside, threads froze under Wine. */
  if (state[4] != 0 && s->info->left == 0 && g_atomic_int_add (&s->info->left, 1) == 0)
    leave ();
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

static Site * describe (const cs_insn * insn, Thread * thread) {
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
  s->thread = thread->id;
  s->info = thread;
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
  while (gum_stalker_iterator_next (iterator, &insn)) {
    if (insn->id == X86_INS_CALL) {
      Site * site = describe (insn, thread);
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

function programModule() {
  // The game's executable. Under Wine Frida's Process.mainModule is just the
  // first module it lists (e.g. netapi32.dll); Windows itself knows better.
  if (WINDOWS) {
    const getModuleHandle = new NativeFunction(
      Process.getModuleByName('kernel32.dll').getExportByName('GetModuleHandleW'),
      'pointer', ['pointer']);
    const module = Process.findModuleByAddress(getModuleHandle(NULL));
    if (module !== null)
      return module;
  }
  return Process.mainModule;
}

function threadLoads(ids, seconds) {
  // Windows: {id: share of a CPU} over ``seconds``, from GetThreadTimes (in
  // steps of the scheduler's tick, about 10 ms); null where unknown. Under
  // Wine the agent sees Windows thread ids, which /proc does not know.
  const kernel32 = Process.getModuleByName('kernel32.dll');
  const openThread = new NativeFunction(kernel32.getExportByName('OpenThread'),
                                        'pointer', ['uint32', 'int', 'uint32']);
  const closeHandle = new NativeFunction(kernel32.getExportByName('CloseHandle'),
                                         'int', ['pointer']);
  const getThreadTimes = new NativeFunction(kernel32.getExportByName('GetThreadTimes'), 'int',
                                            ['pointer', 'pointer', 'pointer', 'pointer', 'pointer']);
  const times = Memory.alloc(32);
  const THREAD_QUERY_LIMITED_INFORMATION = 0x0800;

  function cpuTime(id) {
    // Kernel plus user time in 100 ns units, or null.
    const handle = openThread(THREAD_QUERY_LIMITED_INFORMATION, 0, id);
    if (handle.isNull())
      return null;
    try {
      if (!getThreadTimes(handle, times, times.add(8), times.add(16), times.add(24)))
        return null;
      return times.add(16).readU64().add(times.add(24).readU64());
    } finally {
      closeHandle(handle);
    }
  }

  const before = new Map(ids.map(id => [id, cpuTime(id)]));
  Thread.sleep(seconds);
  const loads = {};
  for (const id of ids) {
    const start = before.get(id);
    const end = cpuTime(id);
    loads[id] = start === null || end === null
      ? null : Number(end.sub(start)) / (seconds * 1e7);
  }
  return loads;
}

function inventory(measureSeconds = 0) {
  // ``measureSeconds``: measure the threads' load here (Windows only).
  const main = programModule();
  const gameFolder = folder(main.path);
  const modules = Process.enumerateModules();
  const threads = Process.enumerateThreads().filter(thread => thread.id !== AGENT_THREAD);
  const loads = WINDOWS && measureSeconds > 0
    ? threadLoads(threads.map(thread => thread.id), measureSeconds) : {};
  return {
    platform: Process.platform,
    threads: threads.map(thread => ({
      id: thread.id, name: thread.name || '', state: thread.state, load: loads[thread.id] ?? null
    })),
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

// Windows: what the followed threads call to stop following themselves.
let leaveFunction = null;

function leaveCallback() {
  if (leaveFunction === null)
    leaveFunction = new NativeCallback(() => { Stalker.unfollow(); }, 'void', []);
  return leaveFunction;
}

// Windows: one Stalker serves every trace of this agent (two in one process
// froze a game on the second stop). What it excluded stays excluded, so a
// trace that follows such a module needs a fresh agent.
const FRESH_AGENT = 'needs a fresh agent';
const excludedModules = new Set();

function threadsLeft(t) {
  // Followed threads of trace ``t`` that left it, and the ids still in it.
  let left = 0;
  const staying = [];
  for (const [id, info] of t.threads) {
    if (info.add(4).readS32() !== 0)
      left++;
    else
      staying.push(id);
  }
  return { left, staying };
}

function stillFollowed() {
  // Windows: threads an earlier trace of this agent still follows.
  const ids = new Set();
  for (const t of retired) {
    if (t.leaving)
      threadsLeft(t).staying.forEach(id => ids.add(id));
  }
  return ids;
}

function compile(threadCount) {
  // The CModule and its memory, all of it zeroed.
  if (Process.arch !== 'x64' && Process.arch !== 'ia32')
    throw hermesError('error.native_trace.needs_x86', { arch: Process.arch });
  const memory = {
    state: Memory.alloc(32),
    keys: Memory.alloc(Process.pointerSize * FUNCTION_CAPACITY),
    claims: Memory.alloc(4 * FUNCTION_CAPACITY),
    counts: Memory.alloc(4 * FUNCTION_CAPACITY),
    quiet: Memory.alloc(FUNCTION_CAPACITY),
    first_claims: Memory.alloc(4 * FIRST_CAPACITY),
    first_functions: Memory.alloc(4 * FIRST_CAPACITY),
    first_threads: Memory.alloc(4 * FIRST_CAPACITY),
    events: Memory.alloc(EVENT_SIZE * EVENT_CAPACITY)
  };
  zero(memory.state, 32);
  zero(memory.keys, Process.pointerSize * FUNCTION_CAPACITY);
  zero(memory.claims, 4 * FUNCTION_CAPACITY);
  zero(memory.counts, 4 * FUNCTION_CAPACITY);
  zero(memory.quiet, FUNCTION_CAPACITY);
  const cm = new CModule(C_SOURCE, { ...memory, leave: leaveCallback() });
  const tallied = Memory.alloc(16);
  const tally = new NativeFunction(cm.tally, 'void', ['pointer']);
  return {
    ...memory, cm,
    now: new NativeFunction(cm.now, 'int64', []),
    setPhase: new NativeFunction(cm.set_phase, 'void', ['int']),
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
    throw hermesError('error.native_trace.running');
  const followed = new Set(spec.modules);
  if (WINDOWS) {
    const excluded = spec.modules.filter(name => excludedModules.has(name));
    if (excluded.length > 0)
      throw new Error(`${FRESH_AGENT}: ${excluded.join(', ')} excluded by an earlier trace`);
  }
  // Threads an earlier trace still follows are left alone.
  const busy = WINDOWS ? stillFollowed() : new Set();
  const requested = spec.threads.filter(id => id !== AGENT_THREAD);
  const threadIds = requested.filter(id => !busy.has(id));
  const compiled = compile(threadIds.length);
  let included = 0;
  // The modules loaded now, also those loaded since the inventory. Only before
  // following: Stalker's exclusions must not change while threads compile
  // code (a module observer excluding modules loaded during the trace made
  // Baldur's Gate and Dark Souls III crash under Proton). Modules loaded
  // later are recompiled like the game's code.
  for (const module of Process.enumerateModules()) {
    if (followed.has(module.name)) {
      included++;
    } else {
      Stalker.exclude(module);  // runs natively: only calls into it are seen
      excludedModules.add(module.name);
    }
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
    threads: [],            // [id, Thread of the C side]: followed
    entries: [],            // Windows: hooks threads enter Stalker through
    waiting: new Map()      // Windows: id -> Thread of the C side, not entered yet
  };
  const ids = threadIds;
  const infos = new Map(ids.map((id, i) => {
    const info = trace.threadInfo.add(THREAD_SIZE * i);
    info.writeU32(id);
    info.add(4).writeU32(0);
    return [id, info];
  }));
  let steered = null;
  const failed = WINDOWS
    ? enterOnReturn(trace, infos, new ModuleMap(module => followed.has(module.name)))
    : withRoomNearby(() => followAll(trace, infos), placed => { steered = placed; });
  return { threads: ids.length - failed.length,
           failed: failed.length + requested.length - ids.length, modules: included,
           waiting: trace.waiting.size, steered };
}

// --- Room for Stalker's code ---------------------------------------------------
// On x86-64 Stalker's code reaches its thread's context with 32-bit offsets,
// so every slab of code (4 MB) must lie within ±2 GB of the context. Gum
// asks the kernel for the context without a hint; the kernel puts it into
// the highest gap below mmap_base. A game that mapped many GB (EVERSPACE:
// 9.9 GB in 5700 regions, gaps of a few MB) leaves no 4 MB within ±2 GB of
// there: gum_memory_allocate_near fails, and Stalker writes to the NULL it
// got (SIGSEGV at 0x18 a moment after following). So while Stalker creates
// the contexts, every gap above the highest large free area is filled with
// placeholders: the contexts land at the top of that area, with room below.

let libcMmap = null;

function mmapFunctions() {
  if (libcMmap === null) {
    libcMmap = {
      mmap: new NativeFunction(Module.getGlobalExportByName('mmap'), 'pointer',
                               ['pointer', 'size_t', 'int', 'int', 'int', 'long']),
      munmap: new NativeFunction(Module.getGlobalExportByName('munmap'), 'int',
                                 ['pointer', 'size_t'])
    };
  }
  return libcMmap;
}

function mappedRanges() {
  // [[start, end]] (BigInt) of the mappings, without the stack and the
  // kernel's pages: hintless mappings never go above them.
  const ranges = [];
  for (const line of File.readAllText('/proc/self/maps').split('\n')) {
    if (line === '')
      continue;
    if (/\[(stack|vvar|vvar_vclock|vdso|vsyscall)\]$/.test(line))
      break;
    const [start, end] = line.split(' ', 1)[0].split('-');
    ranges.push([BigInt('0x' + start), BigInt('0x' + end)]);
  }
  return ranges;
}

function fillGapsAboveLanding(placed) {
  // Fill every gap above the highest gap of LANDING_SIZE; how many were filled.
  const { mmap } = mmapFunctions();
  const PROT_NONE = 0;
  const MAP_PRIVATE = 0x02, MAP_ANONYMOUS = 0x20, MAP_NORESERVE = 0x4000;
  const MAP_FIXED_NOREPLACE = 0x100000;  // never over a mapping the game made meanwhile
  const ranges = mappedRanges();
  let landing = -1;
  for (let i = ranges.length - 1; i > 0 && landing < 0; i--) {
    if (ranges[i][0] - ranges[i - 1][1] >= LANDING_SIZE)
      landing = i;
  }
  if (landing < 0)
    return 0;
  let filled = 0;
  for (let i = landing + 1; i < ranges.length; i++) {
    const start = ranges[i - 1][1];
    const size = ranges[i][0] - start;
    if (size <= 0n)
      continue;
    const address = ptr(start.toString());
    const result = mmap(address, Number(size), PROT_NONE,
                        MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE | MAP_FIXED_NOREPLACE, -1, 0);
    if (result.equals(address)) {
      placed.push([address, Number(size)]);
      filled++;
    }
  }
  return filled;
}

function withRoomNearby(follow, report) {
  // Run ``follow`` (which creates Stalker's thread contexts) with the gaps
  // filled; ``report`` gets how many placeholders there were.
  if (!STEER_CONTEXTS)
    return follow();
  const placed = [];
  try {
    // Reading the map grows the agent's heap into the highest gaps: again
    // until no gap is left.
    for (let pass = 0; pass < STEER_PASSES; pass++) {
      if (fillGapsAboveLanding(placed) === 0)
        break;
    }
  } catch (_error) {
    // Without placeholders the contexts land where the kernel puts them.
  }
  try {
    return follow();
  } finally {
    const { munmap } = mmapFunctions();
    for (const [address, size] of placed)
      munmap(address, size);
    report(placed.length);
  }
}

function followAll(t, infos) {
  // Stalker takes each thread over where it stands; returns the ids it could not.
  const failed = [];
  for (const [id, info] of infos) {
    try {
      Stalker.follow(id, { transform: t.cm.transform, data: info });
      t.threads.push([id, info]);
    } catch (_error) {
      failed.push(id);  // e.g. ended meanwhile
    }
  }
  return failed;
}

function enterOnReturn(t, infos, followed) {
  // Windows: Stalker takes another thread over through a hardware breakpoint
  // at the instruction it stands at, mostly the return of a system call in
  // ntdll that Stalker's handler passes itself (it calls Sleep). Wine keeps
  // the breakpoint after the handler cleared it, so it hit again within the
  // handler, which then waited for a lock it held: the game froze, or (hit
  // later) crashed with EXCEPTION_SINGLE_STEP. Instead each thread follows
  // itself, which needs no breakpoint: a hook where it returns into the
  // followed code next (its stack's first return address there) starts
  // Stalker in the thread. Returns the ids of threads with no such address.
  const failed = [];
  const groups = new Map();  // address text -> {address, threads: Set of ids}
  const found = new Set();
  for (const thread of Process.enumerateThreads()) {
    if (!infos.has(thread.id))
      continue;
    found.add(thread.id);
    let target = null;
    try {
      target = Thread.backtrace(thread.context, Backtracer.FUZZY)
        .find(address => followed.find(address) !== null) || null;
    } catch (_error) {
      // no stack to read
    }
    if (target === null) {
      failed.push(thread.id);  // runs no followed code now: nothing to see
      continue;
    }
    const key = target.toString();
    if (!groups.has(key))
      groups.set(key, { address: target, threads: new Set() });
    groups.get(key).threads.add(thread.id);
    t.waiting.set(thread.id, infos.get(thread.id));
  }
  for (const id of infos.keys()) {
    if (!found.has(id))
      failed.push(id);  // ended meanwhile
  }
  for (const group of groups.values()) {
    try {
      group.listener = Interceptor.attach(group.address, function () {
        enter(t, group, this.threadId);
      });
      t.entries.push(group);
    } catch (_error) {
      for (const id of group.threads) {
        t.waiting.delete(id);
        failed.push(id);  // e.g. too short to hook
      }
    }
  }
  Interceptor.flush();
  return failed;
}

function enter(t, group, id) {
  // In the game's thread, at a hook of enterOnReturn: follow this thread.
  if (trace !== t || !group.threads.has(id))
    return;
  group.threads.delete(id);
  const info = t.waiting.get(id);
  t.waiting.delete(id);
  Stalker.follow(id, { transform: t.cm.transform, data: info });  // itself: no breakpoint
  t.threads.push([id, info]);
  if (group.threads.size === 0)
    setTimeout(() => detachEntry(t, group), 0);  // not from within its own callback
}

function detachEntry(t, group) {
  const index = t.entries.indexOf(group);
  if (index < 0)
    return;
  t.entries.splice(index, 1);
  group.listener.detach();
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
    throw hermesError('error.native_trace.not_running');
  return trace;
}

async function begin(phase) {
  const t = requireTrace();
  if (PHASES[phase] === undefined)
    throw hermesError('error.native_trace.unknown_phase', { phase });
  t.setPhase(0);  // calls from before belong to no phase
  t.phase = null;
  // Let callouts that observed the previous phase finish before clearing tables.
  const deadline = Date.now() + QUIESCE_TIMEOUT;
  while (t.state.add(12).readS32() !== 0) {
    if (Date.now() > deadline)
      throw hermesError('error.native_trace.thread_busy');
    await new Promise(resolve => setTimeout(resolve, 1));
  }
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
  t.setPhase(PHASES[phase]);
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
    throw hermesError('error.native_trace.not_recording');
  t.setPhase(0);
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
                   lost: t.state.add(8).readS32(), waiting: t.waiting.size };
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

function stop() {
  // Exclusions stay with the Stalker (see FRESH_AGENT).
  if (trace === null)
    return null;
  const t = trace;
  trace = null;
  t.setPhase(0);
  for (const group of t.entries.slice())
    detachEntry(t, group);  // threads not entered yet stay as they are
  t.waiting.clear();
  if (WINDOWS) {
    // Each thread leaves at its next call (on_call): taking a thread over
    // from outside froze Baldur's Gate under Proton, looping in
    // NtSetContextThread.
    t.leaving = true;
    t.state.add(16).writeS32(1);
  } else {
    for (const [id] of t.threads) {
      try {
        Stalker.unfollow(id);
      } catch (_error) {
        // ended meanwhile
      }
    }
  }
  retired.push(t);
  return t;
}

async function waitForLeaving(t, timeout) {
  // Windows: until the threads of ``t`` left, or ``timeout`` ms; returns the
  // number still followed. A thread blocked in a call leaves once it returns.
  if (t === null || !t.leaving)
    return 0;
  const deadline = Date.now() + timeout;
  let staying = threadsLeft(t).staying;
  while (staying.length > 0 && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 20));
    staying = threadsLeft(t).staying;
  }
  return staying.length;
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
    const staying = await waitForLeaving(stopped, STOP_GC_TIMEOUT);
    await collect(STOP_GC_TIMEOUT);
    return { stopped: stopped !== null, staying };
  },
  async dispose() {
    clearInterval(liveTimer);
    await waitForLeaving(stop(), DISPOSE_GC_TIMEOUT);
    await collect(DISPOSE_GC_TIMEOUT);
    return { staying: stillFollowed().size };
  }
};
