// --- Native scripts: the game thread without an engine, and ctx.native ---------
// (see hermes/features/scripts.py)
//
// Scripts run on the game thread. Engine adapters know it (Unreal:
// ProcessEvent, Mono and IL2CPP: Unity's main thread). A game without one, a
// native game with an engine of its own, has no such entry; there the agent
// waits for a function the game calls every frame on its main thread. It
// hooks those of NATIVE_FRAME_FUNCTIONS the game exports and watches them for
// NATIVE_PROBE_MS: the first in the list that ran on the process's first
// thread (mainThreadId) is taken, else the first that ran on another thread.
// The order matters: SDL_PollEvent calls PeekMessageW under Wine, and jobs
// should run after SDL's whole call, not in the middle of it. The first in
// the list is taken as soon as it runs on the main thread; if nothing ran
// while watching, the first call after it. Jobs run when it returns.
//
// ctx.native helps scripts to find code by its bytes instead of by fixed
// addresses, which move with every patch of the game.

const NATIVE_FRAME_FUNCTIONS = [
  'SDL_PollEvent', 'SDL_PumpEvents', 'SDL_GL_SwapWindow',
  'glfwPollEvents', 'glfwSwapBuffers',
  'PeekMessageW', 'PeekMessageA', 'SwapBuffers', 'wglSwapBuffers',
  'glXSwapBuffers', 'eglSwapBuffers', 'vkQueuePresentKHR'
];
// How long a job may wait for the game thread (ms)
const NATIVE_MAIN_TIMEOUT = 3000;
// How long frame functions are watched for one on the main thread before one
// running on another thread is taken (ms)
const NATIVE_PROBE_MS = 500;

const nativeMain = {
  queue: [],
  candidates: null,  // [{ name, address, listener, thread, main }]
  chosen: null,      // the candidate whose returns run the jobs
  thread: null,      // and the thread they run on
  mainThread: null,
  draining: false,
  probeTimer: null,
  probing: false
};

function onGameThread(work) {
  // A promise of work()'s result, computed on the game thread: through the
  // engine adapter, or natively without one.
  return engine.refresh() ? engine.onMainThread(work) : nativeOnMainThread(work);
}

function nativeOnMainThread(work) {
  return new Promise((resolve, reject) => {
    const job = { work, resolve, reject, timer: null };
    nativeMain.queue.push(job);
    job.timer = setTimeout(() => {
      dropNativeJob(job);
      reject(nativeMain.chosen === null
        ? hermesError('error.native.no_frame_call',
                      { names: nativeMain.candidates === null ? ''
                        : nativeMain.candidates.map(candidate => candidate.name).join(', ') })
        : hermesError('error.native.game_thread_timeout',
                      { name: nativeMain.chosen.name }));
    }, NATIVE_MAIN_TIMEOUT);
    try {
      watchFrameFunctions();
    } catch (error) {
      dropNativeJob(job);
      clearTimeout(job.timer);
      reject(error);
    }
  });
}

function dropNativeJob(job) {
  const index = nativeMain.queue.indexOf(job);
  if (index >= 0)
    nativeMain.queue.splice(index, 1);
}

function watchFrameFunctions() {
  // Hook the frame functions the game exports, once.
  if (nativeMain.candidates !== null)
    return;
  // Known here, not first in a hook on a game thread (it lists the threads)
  nativeMain.mainThread = mainThreadId();
  const candidates = [];
  const seen = new Set();
  for (const name of NATIVE_FRAME_FUNCTIONS) {
    const address = Module.findGlobalExportByName(name);
    if (address === null || seen.has(address.toString()))
      continue;
    seen.add(address.toString());
    candidates.push({ name, address, listener: null, thread: null, main: false });
  }
  if (candidates.length === 0)
    throw hermesError('error.native.no_frame_function',
                      { names: NATIVE_FRAME_FUNCTIONS.join(', ') });
  nativeMain.candidates = candidates;
  for (const candidate of candidates)
    candidate.listener = Interceptor.attach(candidate.address, {
      onLeave() {
        frameFunctionReturned(candidate);
      }
    });
  nativeMain.probing = true;
  nativeMain.probeTimer = setTimeout(() => {
    nativeMain.probeTimer = null;
    nativeMain.probing = false;
    if (nativeMain.chosen !== null)
      return;
    const called = candidates.find(candidate => candidate.main)
      || candidates.find(candidate => candidate.thread !== null);
    if (called !== undefined)
      chooseFrameFunction(called, called.main ? nativeMain.mainThread : called.thread);
  }, NATIVE_PROBE_MS);
}

function frameFunctionReturned(candidate) {
  const thread = Process.getCurrentThreadId();
  if (nativeMain.chosen === null) {
    if (thread === nativeMain.mainThread)
      candidate.main = true;
    else if (candidate.thread === null)
      candidate.thread = thread;
    // While watching only the first in the list is taken at once; after it,
    // whatever runs first
    if ((candidate.main && candidate === nativeMain.candidates[0]) || !nativeMain.probing)
      chooseFrameFunction(candidate, thread);
    else
      return;
  }
  if (candidate !== nativeMain.chosen || thread !== nativeMain.thread)
    return;
  drainNativeJobs();
}

function chooseFrameFunction(candidate, thread) {
  nativeMain.chosen = candidate;
  nativeMain.thread = thread;
  if (nativeMain.probeTimer !== null) {
    clearTimeout(nativeMain.probeTimer);
    nativeMain.probeTimer = null;
  }
  // The others are detached on the agent's thread, not inside a game's call
  setTimeout(() => {
    for (const other of nativeMain.candidates || [])
      if (other !== candidate && other.listener !== null) {
        other.listener.detach();
        other.listener = null;
      }
  }, 0);
}

function drainNativeJobs() {
  // Jobs queued while one runs wait for the next frame; a job calling the
  // frame function itself runs nothing anew.
  if (nativeMain.draining || nativeMain.queue.length === 0)
    return;
  nativeMain.draining = true;
  try {
    for (const job of nativeMain.queue.splice(0)) {
      clearTimeout(job.timer);
      try {
        job.resolve(job.work());
      } catch (error) {
        job.reject(error);
      }
    }
  } finally {
    nativeMain.draining = false;
  }
}

let nativeThreadReported = false;

function nativeThreadNews() {
  // The frame function scripts run in, once after it was chosen (for the
  // script log): { name, thread, main }; else undefined
  if (nativeThreadReported || nativeMain.chosen === null || engine.refresh())
    return undefined;
  nativeThreadReported = true;
  return { name: nativeMain.chosen.name, thread: nativeMain.thread,
           main: nativeMain.thread === nativeMain.mainThread };
}

function disposeNative() {
  if (nativeMain.probeTimer !== null)
    clearTimeout(nativeMain.probeTimer);
  for (const candidate of nativeMain.candidates || [])
    if (candidate.listener !== null)
      candidate.listener.detach();
  for (const job of nativeMain.queue.splice(0)) {
    clearTimeout(job.timer);
    job.reject(hermesError('error.native.disposed'));
  }
  nativeMain.candidates = null;
  nativeMain.chosen = null;
  nativeMain.thread = null;
  nativeMain.probing = false;
  nativeThreadReported = false;
}

// --- ctx.native ---------------------------------------------------------------

// Matches of a pattern in a module, by base, path and pattern: code does not
// move while the module stays loaded.
const nativeScans = new Map();

function nativeAddress(value, what) {
  if (value instanceof NativePointer)
    return value;
  if (typeof value === 'number' || typeof value === 'bigint'
      || (typeof value === 'string' && value.trim() !== ''))
    return ptr(value.toString());
  if (value !== null && typeof value === 'object' && typeof value.handle === 'object'
      && value.handle instanceof NativePointer)
    return value.handle;
  throw hermesError('error.native.address', { what, value: displayText(value) });
}

function nativeModule(name) {
  // The game's executable, or the module of that name
  if (name === undefined || name === null || name === '')
    return programModule();
  const module = Process.findModuleByName(String(name));
  if (module === null)
    throw hermesError('error.native.no_module', { name: String(name) });
  return module;
}

function nativePattern(pattern) {
  // Frida's pattern from "48 8B ?? 05" ("?" is "??" too); bytes may also be
  // written without spaces ("488B??05")
  if (typeof pattern !== 'string')
    throw hermesError('error.native.pattern', { pattern: displayText(pattern) });
  let tokens = pattern.trim().split(/\s+/).filter(token => token !== '');
  if (tokens.length === 1 && tokens[0].length > 2)
    tokens = tokens[0].match(/\?\?|\?|[0-9a-fA-F?]{2}/g) || [];
  tokens = tokens.map(token => (token === '?' ? '??' : token));
  if (tokens.length === 0 || tokens.some(token => !/^[0-9a-fA-F?]{2}$/.test(token))
      || tokens.every(token => token === '??'))
    throw hermesError('error.native.pattern', { pattern });
  return tokens.join(' ').toLowerCase();
}

function nativeScanAll(pattern, moduleName) {
  // Every address where the bytes are found in the module, lowest first
  const module = nativeModule(moduleName);
  const wanted = nativePattern(pattern);
  const key = `${module.base}|${module.path}|${wanted}`;
  let hits = nativeScans.get(key);
  if (hits === undefined) {
    hits = [];
    // A file page may be mapped twice (on Linux the end of .rodata and the
    // start of the RELRO data share one): bytes at one file offset count once,
    // where they are mapped first.
    const fileOffsets = new Set();
    const ranges = module.enumerateRanges('r--').sort((a, b) => a.base.compare(b.base));
    for (const range of ranges) {
      let matches;
      try {
        matches = Memory.scanSync(range.base, range.size, wanted);
      } catch (error) {
        continue;  // a range unmapped or guarded meanwhile has nothing to find
      }
      for (const match of matches) {
        if (range.file) {
          const offset = range.file.offset + match.address.sub(range.base).toUInt32();
          if (fileOffsets.has(offset))
            continue;
          fileOffsets.add(offset);
        }
        hits.push(match.address);
      }
    }
    nativeScans.set(key, hits);
  }
  return hits.slice();
}

function nativeScan(pattern, moduleName) {
  // The one address where the bytes are found; none or several is an error
  const hits = nativeScanAll(pattern, moduleName);
  const module = nativeModule(moduleName).name;
  if (hits.length === 0)
    throw hermesError('error.native.pattern_missing', { pattern, module });
  if (hits.length > 1)
    throw hermesError('error.native.pattern_ambiguous',
                      { pattern, module, count: hits.length });
  return hits[0];
}

function nativeRip(address, dispOffset, length) {
  // The target of an x64 instruction's RIP-relative operand: its 32-bit
  // displacement at ``dispOffset`` counts from the instruction's end
  // (``length`` bytes after its start).
  const at = nativeAddress(address, 'rip');
  if (!Number.isInteger(dispOffset) || !Number.isInteger(length) || dispOffset < 0
      || length < dispOffset + 4 || length > 15)
    throw hermesError('error.native.rip_operand', { dispOffset: displayText(dispOffset),
                                                    length: displayText(length) });
  return at.add(length).add(at.add(dispOffset).readS32());
}

function nativeFunction(address, returnType, argTypes, abi) {
  // A callable native function (Frida's NativeFunction); ``abi`` only when
  // it is not the platform's (win64 under Wine, sysv on Linux)
  const at = nativeAddress(address, 'fn');
  if (!Array.isArray(argTypes))
    throw hermesError('error.native.arg_types');
  return abi === undefined || abi === null
    ? new NativeFunction(at, returnType, argTypes)
    : new NativeFunction(at, returnType, argTypes, abi);
}

const nativeContext = Object.freeze({
  module: nativeModule,
  scan: nativeScan,
  scanAll: nativeScanAll,
  rip: nativeRip,
  fn: nativeFunction
});
