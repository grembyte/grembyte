const EVENT_CAPACITY = EVENT_CAPACITY_VALUE;
const EVENT_SIZE = 32;
const PHASES = { baseline: 1, scenario: 2 };

// Runs in the game's threads on every call of a traced method.
const C_SOURCE = `
#include <gum/guminterceptor.h>

#define CAPACITY ${EVENT_CAPACITY}

typedef struct {
  guint32 seq;
  guint32 method;
  guint32 thread;
  guint32 reserved;
  guint64 caller;
  gint64 time;
} Event;

extern volatile gint state[2];  /* phase (0 off, 1 baseline, 2 scenario), events written */
extern volatile gint counts[];
extern volatile guint8 quiet[];
extern Event events[];

gint64 now (void) {
  return g_get_monotonic_time ();
}

void on_enter (GumInvocationContext * ic) {
  gint phase = state[0];
  if (phase == 0)
    return;
  guint index = GPOINTER_TO_UINT (gum_invocation_context_get_listener_function_data (ic));
  g_atomic_int_add (&counts[index], 1);
  if (phase != 2 || quiet[index])
    return;
  gint slot = g_atomic_int_add (&state[1], 1);
  if (slot >= CAPACITY)
    return;
  Event * event = &events[slot];
  event->method = index;
  event->thread = gum_invocation_context_get_thread_id (ic);
  event->caller = GPOINTER_TO_SIZE (gum_invocation_context_get_return_address (ic));
  event->time = g_get_monotonic_time ();
  /* last: the reader skips events still being written */
  ((volatile Event *) event)->seq = slot + 1;
}
`;

let trace = null;
// Stopped traces: a game thread may still be in their code, writing to their
// memory.
const retired = [];
// The agent's own thread (RPCs run there); hooks run on game threads.
const AGENT_THREAD = Process.getCurrentThreadId();

function pauseForHooking() {
  // Many hooks change at once: stop the game's threads meanwhile, so none
  // enters code Frida is still changing (see pauseThreads). Never from a game
  // thread: it may hold locks the others need.
  return Process.getCurrentThreadId() === AGENT_THREAD ? pauseThreads() : null;
}

function resumeAfterHooking(paused) {
  Interceptor.flush();  // write the pending changes before the game runs on
  resumeThreads(paused);
}

async function select(spec) {
  // The methods of spec.methods ([[assembly, token], ...]): Hermes chose
  // them by the catalog (see trace.py), counting in spec.skipped those that
  // cannot be traced.
  const tokens = new Map();
  for (const [assembly, token] of spec.methods) {
    if (!tokens.has(assembly))
      tokens.set(assembly, []);
    tokens.get(assembly).push(token);
  }
  const chosen = [];
  for (const [assembly, wanted] of tokens) {
    for (const found of await engine.methodsByToken(assembly, wanted))
      chosen.push({ assembly, ...found });
  }
  return { chosen, skipped: { ...spec.skipped } };
}

// Interceptors set at a time before other scripts of the agent get a turn
const ATTACH_CHUNK = 500;
const DISPOSE_TIMEOUT = 5000;
let starting = false;
let cancelled = false;

function reporter(step, total, current = () => '') {
  // A progress callback for a batch that reports each state once.
  let reported = -1;
  return done => {
    if (done === reported)
      return;  // the same method still: name it, should Mono hang there
    reported = done;
    send({ kind: 'progress', step, done, total, current: done < total ? current(done) : '' });
  };
}

function nextTurn() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

async function start(spec) {
  if (trace !== null || starting)
    throw new Error('a trace is running already');
  starting = true;
  cancelled = false;
  try {
    return await prepare(spec);
  } finally {
    starting = false;
  }
}

async function prepare(spec) {
  const selected = await select(spec);
  const skipped = { ...selected.skipped, com: 0 };
  // Compiling code that uses COM interop hangs Mono on Linux: leave it out.
  const usesCom = await engine.checkAll(selected.chosen.map(found => found.method),
    reporter('check', selected.chosen.length, done => selected.chosen[done].entry[5]));
  const chosen = selected.chosen.filter((_found, i) => !usesCom[i]);
  skipped.com = selected.chosen.length - chosen.length;
  const total = chosen.length;
  const codes = await engine.compileAll(chosen.map(found => found.method),
    reporter('compile', total, done => chosen[done].entry[5]));
  const size = Math.max(total, 1);
  const memory = {
    state: Memory.alloc(8),
    counts: Memory.alloc(4 * size),
    quiet: Memory.alloc(size),
    events: Memory.alloc(EVENT_SIZE * EVENT_CAPACITY)
  };
  const cm = new CModule(C_SOURCE, memory);
  trace = {
    ...memory, cm, now: new NativeFunction(cm.now, 'int64', []),
    methods: [], listeners: [], starts: [], phase: null, baseline: null
  };
  const paused = pauseForHooking();
  let counts;
  try {
    counts = await attachAll(codes, chosen, cm);
  } finally {
    resumeAfterHooking(paused);
  }
  trace.starts.sort((a, b) => a.compare(b));
  updateUnmapWatch();
  return { methods: trace.methods, skipped, ...counts };
}

async function attachAll(codes, chosen, cm) {
  // Hook the code of every method; returns { notCompiled, failed }.
  const total = codes.length;
  const seen = new Set();
  let notCompiled = 0;
  let failed = 0;
  for (let i = 0; i < codes.length; i++) {
    if (i % ATTACH_CHUNK === 0) {
      send({ kind: 'progress', step: 'attach', done: i, total });
      await nextTurn();
      if (cancelled) {
        stop();
        throw new Error('cancelled');
      }
    }
    const code = codes[i];
    if (code === null) {
      notCompiled++;
      continue;
    }
    const key = code.start.toString();
    if (seen.has(key))
      continue;  // shared code: counted under the first method
    const index = trace.methods.length;
    try {
      trace.listeners.push(Interceptor.attach(code.start, { onEnter: cm.on_enter }, ptr(index)));
    } catch (_error) {
      failed++;  // e.g. too short to intercept
      continue;
    }
    seen.add(key);
    trace.starts.push(code.start);
    const found = chosen[i];
    trace.methods.push([found.assembly, found.className, ...found.entry]);
  }
  return { notCompiled, failed };
}

function requireTrace() {
  if (trace === null)
    throw new Error('no trace is running');
  return trace;
}

function zero(pointer, length) {
  if (length > 0)
    pointer.writeByteArray(new ArrayBuffer(length));
}

function begin(phase) {
  const t = requireTrace();
  if (PHASES[phase] === undefined)
    throw new Error(`unknown phase ${phase}`);
  t.state.writeS32(0);
  const n = t.methods.length;
  zero(t.counts, 4 * n);
  t.state.add(4).writeS32(0);
  if (phase === 'scenario')
    zero(t.events, EVENT_SIZE * EVENT_CAPACITY);  // no seq of an earlier scenario
  const quiet = new Uint8Array(Math.max(n, 1));
  if (phase === 'scenario' && t.baseline !== null)
    t.baseline.forEach((count, i) => { quiet[i] = count > 0 ? 1 : 0; });
  t.quiet.writeByteArray(quiet.buffer);
  t.phase = phase;
  t.began = Date.now();
  t.beganMono = BigInt(t.now().toString());
  t.state.writeS32(PHASES[phase]);
  return true;
}

function readCounts(t) {
  const n = t.methods.length;
  return n === 0 ? new Uint32Array(0) : new Uint32Array(t.counts.readByteArray(4 * n));
}

async function callerTexts(addresses) {
  // ``module+0x…`` for native code, ``Namespace.Class:Method+0x…`` for JIT code.
  const texts = addresses.map(address => {
    const module = Process.findModuleByAddress(address);
    return module === null ? null : `${module.name}+${address.sub(module.base)}`;
  });
  const jit = addresses.filter((_address, i) => texts[i] === null);
  let names = [];
  try {
    names = jit.length > 0 ? await engine.describeAll(jit) : [];
  } catch (_error) {
    // names stay unknown
  }
  let next = 0;
  return texts.map((text, i) => {
    if (text !== null)
      return text;
    return names[next++] || addresses[i].toString();
  });
}

async function readEvents(t) {
  // [[method index, thread, µs since the phase began, caller]] in call order.
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
                view.getBigUint64(base + 16, true).toString(16)]);
    }
  }
  const callers = Array.from(new Set(raw.map(event => event[3])));
  const texts = await callerTexts(callers.map(caller => ptr('0x' + caller)));
  const names = new Map(callers.map((caller, i) => [caller, texts[i]]));
  return {
    events: raw.map(([method, thread, time, caller]) => [method, thread, time, names.get(caller)]),
    dropped: Math.max(0, written - EVENT_CAPACITY)
  };
}

async function finish() {
  const t = requireTrace();
  const phase = t.phase;
  if (phase === null)
    throw new Error('nothing is being recorded');
  t.state.writeS32(0);
  const duration = Date.now() - t.began;
  const counts = readCounts(t);
  const sparse = {};
  counts.forEach((count, i) => {
    if (count > 0)
      sparse[i] = count;
  });
  const result = { phase, duration, counts: sparse, events: [], dropped: 0 };
  if (phase === 'scenario')
    Object.assign(result, await readEvents(t));
  else
    t.baseline = counts;
  t.phase = null;
  return result;
}

function live() {
  // While recording: calls so far, how many methods, events written.
  if (trace === null || trace.phase === null)
    return;
  let calls = 0;
  let methods = 0;
  for (const count of readCounts(trace)) {
    calls += count;
    if (count > 0)
      methods++;
  }
  send({ kind: 'live', phase: trace.phase, calls, methods,
         events: Math.min(trace.state.add(4).readS32(), EVENT_CAPACITY) });
}

function stop() {
  if (trace === null)
    return false;
  const t = trace;
  trace = null;
  t.state.writeS32(0);
  const paused = pauseForHooking();
  try {
    for (const listener of t.listeners)
      listener.detach();
  } finally {
    resumeAfterHooking(paused);
  }
  retired.push(t);
  updateUnmapWatch();
  return true;
}

// --- Freed code ---------------------------------------------------------------

let unmapWatch = null;

function tracesCodeIn(start, end) {
  // Whether a traced method starts in [start, end): binary search.
  const starts = trace.starts;
  let low = 0;
  let high = starts.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (starts[middle].compare(start) < 0)
      low = middle + 1;
    else
      high = middle;
  }
  return low < starts.length && starts[low].compare(end) < 0;
}

function updateUnmapWatch() {
  if (trace !== null && unmapWatch === null) {
    unmapWatch = watchUnmaps((start, end) => {
      try {
        // Still mapped here: Frida can restore the code before it is gone.
        if (trace !== null && tracesCodeIn(start, end)) {
          stop();
          send({ kind: 'trace-ended' });
        }
      } catch (error) {
        send({ kind: 'agent-error', error: `unmap: ${error}` });
      }
    });
  } else if (trace === null && unmapWatch !== null) {
    const watch = unmapWatch;
    unmapWatch = null;
    setTimeout(() => watch.detach(), 0);  // not from within its own callback
  }
}

function onCancel() {
  // A message rather than an RPC: it arrives while start() is still busy.
  cancelled = true;
  engine.cancelBatches();
  recv('cancel', onCancel);
}
recv('cancel', onCancel);

const liveTimer = setInterval(live, 500);
send({ kind: 'ready', engine: engine.refresh() ? engine.id : null });

rpc.exports = {
  assemblies() {
    return engine.assemblies();
  },
  classes(assembly) {
    return engine.classes(assembly);
  },
  start,
  begin,
  finish,
  stop,
  async dispose() {
    clearInterval(liveTimer);
    cancelled = true;
    engine.cancelBatches();
    // The batch thread runs our C code: wait until it is out of it (it
    // checks after each method), but not forever should Mono hang.
    const deadline = Date.now() + DISPOSE_TIMEOUT;
    while ((engine.batchesRunning() || starting) && Date.now() < deadline)
      await new Promise(resolve => setTimeout(resolve, 20));
    stop();
    if (unmapWatch !== null) {
      unmapWatch.detach();
      unmapWatch = null;
    }
    Interceptor.flush();
    return true;
  }
};
