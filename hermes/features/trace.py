"""Trace: which C# methods a scenario in a game runs (e.g. Unity with Mono).

It works through the engine adapter of the game (see hermes/engines). While
it adds or removes its hooks, the game's threads are paused where the
platform needs it (Wine, see ``pauseThreads``).

The trace hooks many methods at once, e.g. all methods of the game's
assemblies, to find the one worth monitoring or hooking:

1. ``start`` compiles the chosen methods (like the method monitor does) and
   puts a Frida interceptor on each. The interceptors call a small C
   function (a Frida CModule) rather than JavaScript: it only counts the call
   and, while a scenario is recorded, notes method, thread, return address
   and time in a buffer. That keeps thousands of hooks affordable.
2. ``begin("baseline")`` … ``finish()`` records the noise: what the game
   calls anyway while one does nothing (Update methods, UI, AI …).
3. ``begin("scenario")`` … ``finish()`` records while one does the thing of
   interest in the game. Calls of methods seen in the baseline are only
   counted, not written into the call sequence.

``analyze`` then keeps the methods that are new in the scenario or ran
clearly more often than in the baseline, in the order they were first
called. Only the entry of a method is hooked, so Frida leaves return
addresses alone: exceptions pass through traced methods as usual.

Like the method monitor, the trace notices when Mono frees traced code
(Unity does so on exit) and removes all its interceptors first.
"""

import queue
import threading
from dataclasses import dataclass

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.core import sessions
from hermes.engines.registry import ENGINE_AGENT_SOURCE, resolve_engine
from hermes.features.catalog import CatalogMethod
from hermes.core.frida_attach import attach_session
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS


# Calls of the scenario written into the sequence; later ones are only counted.
EVENT_CAPACITY = 100_000

# A method ran "clearly more often" in the scenario than in the baseline when
# it ran this many times as often as expected, and at least MIN_EXTRA_CALLS
# times more.
MORE_FACTOR = 3.0
MIN_EXTRA_CALLS = 3

NEW, MORE, SAME = "new", "more", "same"

_AGENT_SOURCE = ENGINE_AGENT_SOURCE + r"""
const EVENT_CAPACITY = EVENT_CAPACITY_VALUE;
const EVENT_SIZE = 32;
const METHOD_ABSTRACT = 0x0400;
const METHOD_PINVOKE = 0x2000;
const IMPL_CODE_TYPE = 0x0003;  // native or runtime (delegates) instead of IL
const IMPL_INTERNAL_CALL = 0x1000;
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
// CModules of stopped traces: a game thread may still be in their code.
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

function matches(words, text) {
  const lower = text.toLowerCase();
  return words.every(word => lower.includes(word));
}

function untraceable(found) {
  // Why a method cannot be traced, or null.
  if (found.flags & METHOD_ABSTRACT)
    return 'abstract';
  if ((found.flags & METHOD_PINVOKE) || (found.implFlags & (IMPL_CODE_TYPE | IMPL_INTERNAL_CALL)))
    return 'native';
  // Open generics have no code of their own: List`1, Method<T>
  const full = found.entry[5];
  const open = full.indexOf(' (');
  const head = open < 0 ? full : full.substring(0, open);
  if (found.className.includes('`') || head.substring(head.lastIndexOf(':') + 1) !== found.entry[0])
    return 'generic';
  return null;
}

async function select(spec) {
  // The methods of spec.assemblies matching spec.words that can be traced;
  // with spec.methods ([[assembly, token], ...]) only those.
  const wanted = spec.methods ? new Set(spec.methods.map(([a, token]) => `${a}\t${token}`)) : null;
  const chosen = [];
  const skipped = { abstract: 0, native: 0, generic: 0 };
  for (const assembly of spec.assemblies) {
    for (const found of await engine.methods(assembly)) {
      if (!matches(spec.words, `${found.className}:${found.entry[0]} ${assembly}`))
        continue;
      const reason = untraceable(found);
      if (reason !== null)
        skipped[reason]++;
      else if (wanted === null || wanted.has(`${assembly}\t${found.entry[4]}`))
        chosen.push({ assembly, ...found });
    }
  }
  return { chosen, skipped };
}

async function plan(spec) {
  const { chosen, skipped } = await select(spec);
  return { methods: chosen.length, skipped };
}

async function candidates(spec) {
  // Every method a trace of spec could hook, with its signature, to choose
  // from (see trace_filters.py); nothing is compiled.
  const { chosen, skipped } = await select(spec);
  return { methods: chosen.map(found => [found.assembly, found.className, ...found.entry]),
           skipped };
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
  retired.push(t.cm);
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
  plan,
  candidates,
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
""".replace("EVENT_CAPACITY_VALUE", str(EVENT_CAPACITY))


def build_agent_source():
    return _AGENT_SOURCE


def trace_methods(rows, engine="mono"):
    """CatalogMethods of the agent's ``start`` answer, in index order."""
    return [
        CatalogMethod(assembly, class_name, name, parameters, return_type, bool(static), token,
                      signature, engine)
        for assembly, class_name, name, parameters, return_type, static, token, signature in rows
    ]


@dataclass(frozen=True)
class TraceCall:
    """One call of the scenario."""

    method: int
    thread: int
    # microseconds since the scenario began
    time: int
    # where it was called from: ``Namespace.Class:Method+0x1a`` or ``module+0x…``
    caller: str


@dataclass(frozen=True)
class TraceRow:
    """A method the scenario called, compared with the baseline."""

    method: int
    calls: int
    # calls per second in the scenario, and in the baseline (None: no baseline)
    rate: float
    baseline_rate: float | None
    verdict: str
    # position of its first call in the sequence (None: not in the sequence)
    first: int | None

    @property
    def notable(self):
        return self.verdict in (NEW, MORE)

    @property
    def factor(self):
        """How many times as often as in the baseline, or None."""
        if not self.baseline_rate:
            return None
        return self.rate / self.baseline_rate


def recording_calls(result):
    """TraceCalls of a ``finish`` answer of the scenario."""
    return [TraceCall(method, thread, time, caller or "")
            for method, thread, time, caller in result.get("events", [])]


def _counts(result):
    return {int(key): value for key, value in result.get("counts", {}).items()}


def analyze(scenario, baseline=None):
    """TraceRows of the methods ``scenario`` called (``finish`` answers).

    Without a baseline every method counts as new. Rows of methods in the
    call sequence come first, in the order they were first called; the
    others (only counted, since the baseline had them) follow, the most
    increased first.
    """
    seconds = max(scenario.get("duration", 0), 1) / 1000
    base_counts = _counts(baseline) if baseline else {}
    base_seconds = max(baseline.get("duration", 0), 1) / 1000 if baseline else None
    first = {}
    for position, (method, *_rest) in enumerate(scenario.get("events", [])):
        first.setdefault(method, position)
    rows = []
    for method, calls in _counts(scenario).items():
        base_calls = base_counts.get(method, 0)
        baseline_rate = None if baseline is None else base_calls / base_seconds
        if not base_calls:
            verdict = NEW
        else:
            expected = baseline_rate * seconds
            verdict = (MORE if calls >= MORE_FACTOR * expected
                       and calls - expected >= MIN_EXTRA_CALLS else SAME)
        rows.append(TraceRow(method, calls, calls / seconds, baseline_rate, verdict,
                             first.get(method)))
    rows.sort(key=lambda row: (
        row.first is None,
        row.first if row.first is not None else 0,
        {NEW: 0, MORE: 1, SAME: 2}[row.verdict],
        -(row.factor or 0),
        -row.calls,
    ))
    return rows


class AgentWorker(QThread):
    """Keep a Frida session with one agent open and run commands on it.

    The base of the traces: the commands queue up from the GUI thread and run
    one after another as the agent's RPCs on the worker's thread (see
    ``_execute``); the agent's messages turn into signals (see ``_on_payload``).
    Subclasses decorate ``run`` with ``sessions.tracked`` and call
    ``_run_agent``.
    """

    # step, done, total, item at hand (e.g. the method Mono compiles)
    progress = pyqtSignal(str, int, int, str)
    # recording of the phase ("baseline", "scenario") has begun
    began = pyqtSignal(str)
    # {phase, calls, methods, events} while recording
    live = pyqtSignal(dict)
    # a ``finish`` answer: {phase, duration, counts, events, dropped}
    recorded = pyqtSignal(dict)
    stopped = pyqtSignal()
    # a command failed: what was asked, message
    command_failed = pyqtSignal(str, str)
    worker_failed = pyqtSignal(str)

    # What failed to load, for the error message
    agent_name = "trace"

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._agent_error = None
        self._detach_reason = None
        self._script = None
        self._session = None

    def agent_source(self):
        raise NotImplementedError

    def begin(self, phase):
        """Record ``"baseline"`` or ``"scenario"`` until ``finish``."""
        self._commands.put(("begin", phase))

    def finish(self):
        self._commands.put(("finish", None))

    def stop(self):
        """Remove the trace; the session stays open for another one."""
        self._commands.put(("stop", None))

    def cancel(self):
        """Stop preparing a trace as soon as possible.

        A message rather than a command: the worker is busy with the start
        until then.
        """
        script = self._script
        if script is not None:
            try:
                script.post({"type": "cancel"})
            except CLEANUP_ERRORS as error:
                logs.cleanup_failed(error)

    def requestInterruption(self):  # pylint: disable=invalid-name
        self.cancel()
        super().requestInterruption()

    def _run_agent(self):
        session = None
        try:
            session = attach_session(self.pid)
            self._session = session
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", self._on_detached)
            self._load_script(session)
            while (not self.isInterruptionRequested() and self._detach_reason is None
                   and self._agent_error is None):
                try:
                    action, argument = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                if self._script is None:
                    break  # a fresh agent failed to load
                self._execute(self._script.exports_sync, action, argument)
            if self._agent_error and not self.isInterruptionRequested():
                raise RuntimeError(self._agent_error)
            if self._detach_reason and not self.isInterruptionRequested():
                raise RuntimeError(f"Target process detached: {self._detach_reason}")
        except FRIDA_ERRORS + (OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(str(error))
        finally:
            self._unload_script()
            if session is not None:
                try:
                    session.detach()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)

    def _load_script(self, session):
        self._ready.clear()
        script = session.create_script(self.agent_source())
        script.on("message", self._on_message)
        script.load()
        self._script = script
        if not self._ready.wait(5):
            raise RuntimeError(f"Timed out while loading the {self.agent_name}")

    def _unload_script(self):
        script = self._script
        self._script = None
        if script is None:
            return
        try:
            script.exports_sync.dispose()
        except CLEANUP_ERRORS as error:
            logs.cleanup_failed(error)
        try:
            script.unload()
        except CLEANUP_ERRORS as error:
            logs.cleanup_failed(error)

    def _execute(self, api, action, argument):
        try:
            self._run_command(api, action, argument)
        except frida.RPCException as error:
            # Without the agent's stack trace
            self.command_failed.emit(action, str(error).split("\n", 1)[0])

    def _run_command(self, api, action, argument):
        if action == "begin":
            api.begin(argument)
            self.began.emit(argument)
        elif action == "finish":
            self.recorded.emit(api.finish())
        elif action == "stop":
            api.stop()
            self.stopped.emit()

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = message.get("stack", f"{self.agent_name} agent failed")
            self._ready.set()
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready.set()
        elif kind == "progress":
            self.progress.emit(payload.get("step", ""), payload.get("done", 0),
                               payload.get("total", 0), payload.get("current", ""))
        elif kind == "live":
            self.live.emit(payload)
        elif kind == "agent-error":
            logs.log.error("%s agent: %s", self.agent_name, payload.get("error", ""))
        else:
            self._on_payload(kind, payload)

    def _on_payload(self, kind, payload):
        """A message of the agent the base does not know."""

    def _on_detached(self, reason, _crash):
        self._detach_reason = reason


class TraceWorker(AgentWorker):
    """Keep a Frida session open for a trace; see the module documentation.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    # {methods, skipped: {abstract, native, generic}, assemblies}
    planned = pyqtSignal(dict)
    # every method a trace could hook (CatalogMethods), {abstract, native, generic}
    candidates_found = pyqtSignal(list, dict)
    # CatalogMethods in index order, {skipped: {abstract, native, generic, com},
    # notCompiled, failed}
    started = pyqtSignal(list, dict)
    # the game freed traced code (e.g. while exiting); the trace was removed
    ended = pyqtSignal()

    agent_name = "Mono trace"

    def __init__(self, pid, parent=None, engine=None):
        super().__init__(pid, parent)
        self.engine = resolve_engine(pid, engine)

    def agent_source(self):
        return build_agent_source()

    def plan(self, words):
        """Count the methods a trace of ``words`` would hook; expect planned."""
        self._commands.put(("plan", list(words)))

    def trace(self, words):
        """Hook the methods of the game's assemblies matching ``words``."""
        self._commands.put(("start", list(words)))

    def candidates(self):
        """List every method a trace could hook; expect candidates_found."""
        self._commands.put(("candidates", []))

    def trace_selection(self, methods, words=()):
        """Hook exactly ``methods`` (CatalogMethods, e.g. from candidates_found).

        ``words`` (the free-text filter they were chosen with) only make the
        skipped methods count like ``trace`` counts them.
        """
        self._commands.put(("start", {"methods": [[m.assembly, m.token] for m in methods],
                                      "words": list(words)}))

    @sessions.tracked(sessions.TRACE)
    def run(self):
        self._run_agent()

    def _spec(self, api, argument):
        """The agent's spec: words, or {"methods": [[assembly, token], ...]}."""
        if isinstance(argument, dict):
            methods = argument["methods"]
            return {"assemblies": sorted({assembly for assembly, _token in methods}),
                    "words": [word.lower() for word in argument.get("words", [])],
                    "methods": methods}
        names = [a["name"] for a in api.assemblies()
                 if not self.engine.is_framework_assembly(a["name"])]
        return {"assemblies": names, "words": [word.lower() for word in argument]}

    def _run_command(self, api, action, argument):
        if action == "plan":
            spec = self._spec(api, argument)
            self.planned.emit({**api.plan(spec), "assemblies": spec["assemblies"]})
        elif action == "candidates":
            answer = api.candidates(self._spec(api, argument))
            self.candidates_found.emit(trace_methods(answer["methods"], self.engine.id),
                                       answer["skipped"])
        elif action == "start":
            answer = api.start(self._spec(api, argument))
            self.started.emit(trace_methods(answer.pop("methods"), self.engine.id), answer)
        else:
            super()._run_command(api, action, argument)

    def _on_payload(self, kind, payload):
        if kind == "trace-ended":
            self.ended.emit()
