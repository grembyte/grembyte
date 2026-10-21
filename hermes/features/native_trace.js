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
  // spec: { threads: [ids], modules: [names to follow], ranges: [[base, size]] to run natively }
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
  // Code Frida knows no module of (Wine's Unix side): runs natively as well.
  // A 32-bit game cannot reach the 64-bit libraries of the new WoW64.
  for (const [base, size] of spec.ranges || []) {
    if (Process.pointerSize === 4 && uint64(base).add(size).compare(uint64('0x100000000')) > 0)
      continue;
    Stalker.exclude({ base: ptr(base), size });
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
    threads: [],
    entered: new Set()      // threads that run in Stalker's code by now
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
  if (t === null)
    return;
  if (WINDOWS && !t.entered.has(thread)) {
    t.entered.add(thread);
    clearEntryBreakpoints([thread]);
  }
  if (t.phase === null)
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
  if (WINDOWS)
    clearEntryBreakpoints(t.threads);
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
