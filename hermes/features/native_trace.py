"""Native trace: which functions of a game's machine code a scenario runs.

The native counterpart of the Mono trace (trace.py), for games without
metadata: C++, own engines, Unreal, Godot, Source. Instead of hooking
methods it follows the game's threads with Frida's Stalker, which recompiles
the code they run and so sees every call, wherever it goes. It works like
Cheat Engine's "code filter":

1. ``inventory`` lists the threads and modules. ``start`` follows the chosen
   threads; the modules not chosen (system libraries, drivers) run natively,
   only the calls into them are seen. That keeps the game playable.
2. ``begin("baseline")`` … ``finish()`` counts the calls of every function
   while nothing happens in the game, ``begin("scenario")`` … ``finish()``
   while one does the thing of interest.

The answers have the shape of the Mono trace's, so ``analyze`` compares them
the same way: functions are numbered as the agent first sees them, the
numbers stay the same for the whole trace. Stalker sums the calls up per
thread every ``DRAIN_INTERVAL_MS``; the sequence therefore holds the first
call of every function (not seen in the baseline) per thread, in the order of
these sums, and does not know the caller.

Between two sums Stalker queues at most ``QUEUE_CAPACITY`` calls per thread
and drops the rest without a word. A sum that big means calls were lost: the
answers count such sums in ``saturated``.
"""

from dataclasses import dataclass

from PyQt6.QtCore import pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import sessions
from hermes.platform import is_wine_process
from hermes.features.trace import AgentWorker


# How often Stalker hands over its sums: how exact the sequence is.
DRAIN_INTERVAL_MS = 25
# Calls Stalker queues per thread in between (32 bytes each): together with
# the interval how many calls a second a thread may make without losing any
# (Frida's default: 16384).
QUEUE_CAPACITY = 131_072
# First calls written into the sequence; later ones are only counted.
EVENT_CAPACITY = 100_000
# How long ``finish`` may look up function names.
SYMBOL_BUDGET_MS = 3000

_AGENT_SOURCE = r"""
const DRAIN_INTERVAL_MS = DRAIN_INTERVAL_VALUE;
const QUEUE_CAPACITY = QUEUE_CAPACITY_VALUE;
const EVENT_CAPACITY = EVENT_CAPACITY_VALUE;
const SYMBOL_BUDGET_MS = SYMBOL_BUDGET_VALUE;
const STOP_GC_TIMEOUT = 500;
const DISPOSE_GC_TIMEOUT = 3000;
const PHASES = { baseline: 1, scenario: 2 };
const WINDOWS = Process.platform === 'windows';
const AGENT_THREAD = Process.getCurrentThreadId();

let trace = null;
let modules = null;

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

function start(spec) {
  // spec: { threads: [ids], modules: [names to follow] }
  if (trace !== null)
    throw new Error('a trace is running already');
  if (modules === null)
    inventory();
  const followed = new Set(spec.modules);
  const included = [];
  for (const module of modules) {
    if (followed.has(module.name))
      included.push(module);
    else
      Stalker.exclude(module);  // runs natively: only calls into it are seen
  }
  Stalker.queueDrainInterval = DRAIN_INTERVAL_MS;
  Stalker.queueCapacity = QUEUE_CAPACITY;
  trace = {
    included: new ModuleMap(module => followed.has(module.name)),
    all: new ModuleMap(),
    index: new Map(),       // target address text -> function number
    functions: [],          // [address, module, offset]
    reported: 0,            // functions already sent to Python
    named: new Set(),       // functions whose name was looked up
    exports: new Map(),     // module name -> Map(address text -> export name)
    counts: [],
    baseline: null,
    phase: null,
    began: 0,
    events: [],
    firsts: new Set(),
    dropped: 0,
    saturated: 0,           // sums of a full queue: calls were lost
    threads: []
  };
  const failed = [];
  for (const id of spec.threads) {
    if (id === AGENT_THREAD)
      continue;
    try {
      Stalker.follow(id, {
        events: { call: true },
        onCallSummary: summary => record(id, summary)
      });
      trace.threads.push(id);
    } catch (error) {
      failed.push(id);  // e.g. ended meanwhile
    }
  }
  return { threads: trace.threads.length, failed: failed.length, modules: included.length };
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
    trace.counts.push(0);
  }
  return number;
}

function record(thread, summary) {
  // Runs on the agent's thread with the calls one followed thread made.
  const t = trace;
  if (t === null || t.phase === null)
    return;
  const scenario = t.phase === 'scenario';
  const time = (Date.now() - t.began) * 1000;
  let calls = 0;
  for (const target in summary) {
    const number = functionNumber(target);
    calls += summary[target];
    t.counts[number] += summary[target];
    if (!scenario || (t.baseline !== null && t.baseline[number] > 0))
      continue;
    const first = `${number}:${thread}`;
    if (t.firsts.has(first))
      continue;
    t.firsts.add(first);
    if (t.events.length < EVENT_CAPACITY)
      t.events.push([number, thread, time, '']);
    else
      t.dropped++;
  }
  if (calls >= QUEUE_CAPACITY)
    t.saturated++;
}

function requireTrace() {
  if (trace === null)
    throw new Error('no trace is running');
  return trace;
}

function begin(phase) {
  const t = requireTrace();
  if (PHASES[phase] === undefined)
    throw new Error(`unknown phase ${phase}`);
  t.phase = null;
  Stalker.flush();  // calls from before belong to no phase
  t.counts.fill(0);
  t.events = [];
  t.firsts = new Set();
  t.dropped = 0;
  t.saturated = 0;
  t.began = Date.now();
  t.phase = phase;
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

function finish() {
  const t = requireTrace();
  const phase = t.phase;
  if (phase === null)
    throw new Error('nothing is being recorded');
  Stalker.flush();
  t.phase = null;
  const duration = Date.now() - t.began;
  const counts = {};
  t.counts.forEach((count, number) => {
    if (count > 0)
      counts[number] = count;
  });
  const result = { phase, duration, counts, events: [], dropped: 0, saturated: t.saturated };
  if (phase === 'scenario') {
    result.events = t.events;
    result.dropped = t.dropped;
  } else {
    t.baseline = t.counts.slice();
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
  let calls = 0;
  let methods = 0;
  for (const count of t.counts) {
    calls += count;
    if (count > 0)
      methods++;
  }
  send({ kind: 'live', phase: t.phase, calls, methods, events: t.events.length });
}

function stop() {
  // Exclusions stay with the Stalker: Python loads a fresh agent for the next trace.
  if (trace === null)
    return false;
  const t = trace;
  trace = null;
  for (const id of t.threads) {
    try {
      Stalker.unfollow(id);
    } catch (_error) {
      // ended meanwhile
    }
  }
  Stalker.flush();
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
""".replace("DRAIN_INTERVAL_VALUE", str(DRAIN_INTERVAL_MS)) \
    .replace("QUEUE_CAPACITY_VALUE", str(QUEUE_CAPACITY)) \
    .replace("EVENT_CAPACITY_VALUE", str(EVENT_CAPACITY)) \
    .replace("SYMBOL_BUDGET_VALUE", str(SYMBOL_BUDGET_MS))


def build_agent_source():
    return _AGENT_SOURCE


@dataclass
class NativeFunction:
    """A function the trace saw called: an address, by module where it has one."""

    address: int
    # "" for code outside any module (e.g. generated at run time)
    module: str
    offset: int | None
    # an export or symbol of exactly that address, or None
    symbol: str | None = None

    @property
    def location(self):
        """``module+0x…``, or the bare address."""
        if not self.module:
            return f"0x{self.address:x}"
        return f"{self.module}+0x{self.offset:x}"

    @property
    def label(self):
        """The symbol, or where it is."""
        return self.symbol or self.location

    @property
    def signature(self):
        """Everything known, for tooltips and the selection."""
        if self.symbol:
            return f"{self.symbol}  ({self.location}, 0x{self.address:x})"
        return f"{self.location}  (0x{self.address:x})"


def native_functions(rows):
    """NativeFunctions of the ``functions`` of a ``finish`` answer."""
    return [NativeFunction(int(address, 16), module, offset)
            for address, module, offset in rows]


def apply_names(functions, result):
    """Add the functions and names of a ``finish`` answer to ``functions``."""
    first = result.get("firstFunction", len(functions))
    if first != len(functions):
        raise ValueError(f"functions out of step: {first} instead of {len(functions)}")
    functions.extend(native_functions(result.get("functions", [])))
    for number, name in result.get("names", {}).items():
        functions[int(number)].symbol = name


@dataclass(frozen=True)
class TraceThread:
    """A thread of the game, to follow or not."""

    id: int
    name: str
    # share of a CPU it used just now (0–1), None if unknown
    load: float | None = None
    # whether it ran just now at all, None if unknown
    active: bool | None = None


@dataclass(frozen=True)
class TraceModule:
    """A module of the game; the game's own ones are followed by default."""

    name: str
    path: str
    base: int
    size: int
    game: bool


def _thread_times(pid, tid, proc_root="/proc"):
    """(CPU time in clock ticks, context switches) of a thread, or None."""
    task = f"{proc_root}/{pid}/task/{tid}"
    try:
        with open(f"{task}/stat", encoding="utf-8") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
        with open(f"{task}/status", encoding="utf-8") as status:
            # voluntary_ctxt_switches and nonvoluntary_ctxt_switches
            switches = sum(int(line.split(":", 1)[1]) for line in status
                           if "ctxt_switches:" in line)
    except (OSError, IndexError, ValueError):
        return None
    # fields[0] is the state (field 3): utime and stime are fields 14 and 15
    return int(fields[11]) + int(fields[12]), switches


def thread_activity(pid, tids, seconds=0.5, proc_root="/proc", sleep=None):
    """{tid: (share of a CPU, whether it ran)} over ``seconds``, for the tids /proc knows.

    A thread that only wakes up briefly uses less than a clock tick: whether
    it ran shows in its context switches.
    """
    # pylint: disable=import-outside-toplevel
    import os
    import time

    sleep = sleep or time.sleep
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    before = {tid: _thread_times(pid, tid, proc_root) for tid in tids}
    sleep(seconds)
    activity = {}
    for tid, start in before.items():
        end = _thread_times(pid, tid, proc_root)
        if start is not None and end is not None:
            ticks, switches = end[0] - start[0], end[1] - start[1]
            activity[tid] = (ticks / ticks_per_second / seconds, ticks > 0 or switches > 0)
    return activity


class NativeTraceWorker(AgentWorker):
    """Keep a Frida session open for a native trace; see the module documentation.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    # TraceThreads (the busiest first), TraceModules, platform
    inventory_found = pyqtSignal(list, list, str)
    # {threads, failed, modules}
    started = pyqtSignal(dict)

    agent_name = "native trace"

    def __init__(self, pid, parent=None):
        super().__init__(pid, parent)
        # Under Wine the agent sees Windows thread ids, which /proc does not know.
        self._measure_load = not is_wine_process(pid)
        # Agents of earlier traces: their Stalker keeps its exclusions, and
        # unloading one waits for threads still blocked in its code.
        self._retired = []
        self._traced = False

    def agent_source(self):
        return build_agent_source()

    def inventory(self):
        """List the threads and modules; expect inventory_found."""
        self._commands.put(("inventory", None))

    def trace(self, threads, modules):
        """Follow the thread ids ``threads`` in the modules named ``modules``."""
        self._commands.put(("start", {"threads": list(threads), "modules": list(modules)}))

    @sessions.tracked(sessions.NATIVE_TRACE)
    def run(self):
        self._run_agent()

    def _run_command(self, api, action, argument):
        if action == "inventory":
            answer = api.inventory()
            ids = [thread["id"] for thread in answer["threads"]]
            activity = thread_activity(self.pid, ids) if self._measure_load else {}
            threads = [TraceThread(thread["id"], thread["name"],
                                   *activity.get(thread["id"], (None, None)))
                       for thread in answer["threads"]]
            threads.sort(key=lambda thread: -(thread.load or 0))
            modules = [TraceModule(module["name"], module["path"], int(module["base"], 16),
                                   module["size"], module["game"])
                       for module in answer["modules"]]
            self.inventory_found.emit(threads, modules, answer["platform"])
        elif action == "start":
            if self._traced:
                # Stalker keeps what it excluded: a new trace needs a fresh agent.
                self._retired.append(self._script)
                self._script = None
                self._load_script(self._session)
                api = self._script.exports_sync
            self._traced = True
            self.started.emit(api.start(argument))
        else:
            super()._run_command(api, action, argument)

    def _unload_script(self):
        super()._unload_script()
        while self._retired:
            self._script = self._retired.pop()
            super()._unload_script()
