let threadSpawner = null;

function spawnThread(start, arg) {
  if (threadSpawner === null) {
    if (Process.platform === 'windows') {
      const kernel32 = Process.getModuleByName('kernel32.dll');
      const create = new NativeFunction(kernel32.getExportByName('CreateThread'),
        'pointer', ['pointer', 'size_t', 'pointer', 'pointer', 'uint32', 'pointer']);
      const close = new NativeFunction(kernel32.getExportByName('CloseHandle'),
        'int', ['pointer']);
      threadSpawner = (routine, parameter) => {
        const thread = create(NULL, 0, routine, parameter, 0, NULL);
        if (thread.isNull())
          throw grembyteError('error.platform.create_thread');
        close(thread);
      };
    } else {
      const create = new NativeFunction(Module.getGlobalExportByName('pthread_create'),
        'int', ['pointer', 'pointer', 'pointer', 'pointer']);
      const detach = new NativeFunction(Module.getGlobalExportByName('pthread_detach'),
        'int', ['pointer']);
      threadSpawner = (routine, parameter) => {
        const handle = Memory.alloc(Process.pointerSize);
        const code = create(handle, NULL, routine, parameter);
        if (code !== 0)
          throw grembyteError('error.platform.pthread_create', { code });
        detach(handle.readPointer());
      };
    }
  }
  threadSpawner(start, arg);
}

function watchUnmaps(onUnmap) {
  if (Process.platform !== 'windows')
    return Interceptor.attach(Module.getGlobalExportByName('munmap'), {
      onEnter(args) {
        onUnmap(args[0], args[0].add(args[1]));
      }
    });
  // Under Wine the game's Windows code frees memory through ntdll.
  const MEM_DECOMMIT = 0x4000, MEM_RELEASE = 0x8000, MEM_FREE = 0x10000;
  // MEMORY_BASIC_INFORMATION: BaseAddress, AllocationBase, RegionSize, State
  const wide = Process.pointerSize === 8;
  const INFO_SIZE = wide ? 48 : 28;
  const ALLOCATION_BASE = wide ? 8 : 4, REGION_SIZE = wide ? 24 : 12, STATE = wide ? 32 : 16;
  const currentProcess = ptr(wide ? '0xffffffffffffffff' : '0xffffffff');
  const query = new NativeFunction(
    Process.getModuleByName('kernel32.dll').getExportByName('VirtualQuery'),
    'pointer', ['pointer', 'pointer', 'pointer']);
  const ntdll = Process.getModuleByName('ntdll.dll');
  function allocationEnd(base) {
    // Size 0 means the whole allocation at ``base``; its regions all have
    // ``base`` as AllocationBase.
    const info = Memory.alloc(INFO_SIZE);
    let cursor = base;
    while (!query(cursor, info, ptr(INFO_SIZE)).isNull()
           && info.add(ALLOCATION_BASE).readPointer().equals(base)
           && info.add(STATE).readU32() !== MEM_FREE)
      cursor = info.readPointer().add(info.add(REGION_SIZE).readPointer());
    return cursor;
  }
  const listeners = [
    Interceptor.attach(ntdll.getExportByName('NtFreeVirtualMemory'), {
      onEnter(args) {
        if (!args[0].equals(currentProcess)
            || (args[3].toUInt32() & (MEM_DECOMMIT | MEM_RELEASE)) === 0)
          return;
        const start = args[1].readPointer();
        const size = args[2].readPointer();
        onUnmap(start, size.isNull() ? allocationEnd(start) : start.add(size));
      }
    }),
    Interceptor.attach(ntdll.getExportByName('NtUnmapViewOfSection'), {
      onEnter(args) {
        if (args[0].equals(currentProcess))
          onUnmap(args[1], allocationEnd(args[1]));
      }
    })
  ];
  return { detach() { listeners.forEach(listener => listener.detach()); } };
}

let mainThread = null;

function mainThreadId() {
  if (mainThread !== null)
    return mainThread;
  if (Process.platform !== 'windows') {
    mainThread = Process.id;
    return mainThread;
  }
  const kernel32 = Process.getModuleByName('kernel32.dll');
  const fn = (name, ret, args) => new NativeFunction(kernel32.getExportByName(name), ret, args);
  const open = fn('OpenThread', 'pointer', ['uint32', 'int', 'uint32']);
  const times = fn('GetThreadTimes', 'int', ['pointer', 'pointer', 'pointer', 'pointer', 'pointer']);
  const close = fn('CloseHandle', 'int', ['pointer']);
  const THREAD_QUERY_LIMITED_INFORMATION = 0x0800;
  const filetimes = Memory.alloc(4 * 8);  // creation, exit, kernel, user
  let first = null;
  for (const thread of Process.enumerateThreads()) {
    const handle = open(THREAD_QUERY_LIMITED_INFORMATION, 0, thread.id);
    if (handle.isNull())
      continue;
    const ok = times(handle, filetimes, filetimes.add(8), filetimes.add(16), filetimes.add(24));
    close(handle);
    if (!ok)
      continue;
    const created = BigInt(filetimes.readU64().toString());
    if (first === null || created < first.created
        || (created === first.created && thread.id < first.id))
      first = { id: thread.id, created };
  }
  if (first === null)
    return Process.id;  // not cached: try again next time
  mainThread = first.id;
  return mainThread;
}

// --- Work on the game's main thread ------------------------------------------
// The engine adapters run work on the main thread the next time it enters a
// function it calls many times a second (Unreal's ProcessEvent, Unity's
// runtime_invoke). mainThreadGate hooks it once and keeps the hook: hooking
// and unhooking it for every piece of work rewrote its first bytes thousands
// of times while the game ran them, and RoboCop crashed when its game thread
// stood between two of the rewritten instructions (2026-11-15, see the
// implementation notes). A probe in C checks a flag and calls into JavaScript
// only while work waits, so the hook costs next to nothing in between.
//
// Unity games out of focus may stop running frames: the main thread then
// waits in GetMessage for its window's next message and calls nothing the
// engine hook sees. With ``wake`` the gate also probes GetMessageA/W, and each
// request posts the main thread an empty message (WM_NULL, which programs
// ignore), so the work runs between two passes of its message loop. Windows
// (and Wine) only. A probe does not wait for the function to return, so a
// thread sleeping in GetMessage does not hold up unloading the script.

const WM_NULL = 0;
const GATE_AGENT_THREAD = Process.getCurrentThreadId();
// Bytes at a function's start Frida may rewrite (a jump and what it moves)
const GATE_PATCH_SIZE = 16;
const GATE_ATTEMPTS = 50;

const MAIN_GATE_SOURCE = `
#include <gum/guminterceptor.h>

extern volatile int pending;
extern volatile unsigned long long main_thread;
extern void run_main (void);

void
on_enter (GumInvocationContext * ic)
{
  if (pending == 0)
    return;
  if ((unsigned long long) gum_invocation_context_get_thread_id (ic) != main_thread)
    return;
  run_main ();
}
`;

function mainThreadGate(addresses, onMain, wake = false) {
  // { request(), clear() }: after request(), ``onMain`` runs each time the
  // main thread enters one of ``addresses`` (with ``wake`` also GetMessage),
  // until clear(). Errors of onMain are logged, never thrown into the game.
  // The steps before the hooks go to the activity log as they begin
  // (main_gate_preparing): finding the main thread calls into Windows (Wine)
  // for each thread, compiling the C code allocates code memory.
  noteActivity('main_gate_preparing', { step: 'main_thread' });
  const main = mainThreadId();
  const pending = Memory.alloc(4);
  const mainThread = Memory.alloc(8);
  mainThread.writeU64(main);
  const runMain = new NativeCallback(() => {
    try {
      onMain();
    } catch (error) {
      console.error(`main thread work failed: ${error.stack || error}`);
    }
  }, 'void', []);
  noteActivity('main_gate_preparing', { step: 'cmodule' });
  const cm = new CModule(MAIN_GATE_SOURCE,
                         { pending, main_thread: mainThread, run_main: runMain });
  const targets = addresses.slice();
  let postMessage = null;
  if (wake && Process.platform === 'windows') {
    const user32 = Process.findModuleByName('user32.dll');
    const post = user32 === null ? null : user32.findExportByName('PostThreadMessageW');
    if (post !== null) {
      for (const name of ['GetMessageA', 'GetMessageW']) {
        const address = user32.findExportByName(name);
        if (address !== null)
          targets.push(address);
      }
      postMessage = new NativeFunction(post, 'int',
                                       ['uint32', 'uint32', 'pointer', 'pointer']);
    }
  }
  // The callback, the C code and the listeners live as long as the gate
  const listeners = attachOutsideThreads(targets, cm.on_enter);
  return {
    cm, runMain, listeners,
    request() {
      pending.writeS32(1);
      if (postMessage !== null)
        postMessage(main, WM_NULL, NULL, NULL);
    },
    clear() {
      pending.writeS32(0);
    }
  };
}

function attachOutsideThreads(addresses, probe) {
  // Attach ``probe`` to each of ``addresses`` while no other thread stands
  // in the bytes Frida rewrites there: a thread resumed in the middle of the
  // new jump runs garbage. Threads are paused only from the agent's own
  // thread (a game thread may hold locks the others need) and on Windows;
  // elsewhere, or after GATE_ATTEMPTS tries, the hooks are set as they come.
  // Each step goes to the activity log, so a game that hangs here shows
  // where (main_gate_pausing, main_gate_attaching, then main_gate_attached
  // or main_gate_failed).
  const inPatch = ip => ip !== null && addresses.some(address =>
    ip.compare(address) >= 0 && ip.compare(address.add(GATE_PATCH_SIZE)) < 0);
  const canPause = Process.getCurrentThreadId() === GATE_AGENT_THREAD;
  const began = Date.now();
  noteActivity('main_gate_pausing', { targets: addresses.length, pause: canPause });
  for (let attempt = 1; ; attempt++) {
    const paused = canPause ? pauseThreads() : null;
    if (paused !== null && paused.ips.some(inPatch) && attempt < GATE_ATTEMPTS) {
      resumeThreads(paused);
      Thread.sleep(0.001);
      continue;
    }
    // Sent while the threads are paused: Frida's own threads, which deliver
    // it, are not among them (Process.enumerateThreads leaves them out).
    noteActivity('main_gate_attaching', {
      attempt, waited_ms: Date.now() - began,
      ...(paused === null ? {} : paused.stats)
    });
    const attaching = Date.now();
    let attached = false;
    try {
      const listeners = addresses.map(address => Interceptor.attach(address, probe));
      Interceptor.flush();  // write the jumps before the threads run on
      attached = true;
      return listeners;
    } finally {
      resumeThreads(paused);
      noteActivity(attached ? 'main_gate_attached' : 'main_gate_failed', {
        attempts: attempt, attach_ms: Date.now() - attaching, total_ms: Date.now() - began
      });
    }
  }
}

function noteActivity(event, fields = {}) {
  // An entry in Grembyte's activity log (activity.jsonl), with the agent's
  // name, pid and session added there (see grembyte/attach/frida_attach.py).
  // Only the course of things, never values of the game's memory.
  send({ 'grembyte:activity': event, fields });
}

let threadPauser = null;

// A paused section may take this long before the watchdog lets the threads
// run on (milliseconds).
const PAUSE_LIMIT_MS = 3000;
// Threads the watchdog can resume; more are resumed by the agent only.
const WATCH_CAPACITY = 4096;
// Watch fields: eight 32-bit values, the wake-up event, then the handles.
const WATCH_STARTED = 0, WATCH_ACTIVE = 4, WATCH_CLAIM = 8, WATCH_BEAT = 12,
  WATCH_COUNT = 16, WATCH_LIMIT = 20, WATCH_FIRED = 24, WATCH_QUIT = 28,
  WATCH_EVENT = 32;
const WATCH_HANDLES = WATCH_EVENT + Process.pointerSize;
const WAIT_OBJECT_0 = 0;

const PAUSE_WATCH_SOURCE = `
#include <glib.h>

#ifdef __i386__
# define WINAPI __attribute__ ((stdcall))
#else
# define WINAPI
#endif

/* kernel32's ResumeThread, Sleep and WaitForSingleObject */
extern unsigned int WINAPI resume_thread (gpointer thread);
extern void WINAPI sleep_ms (unsigned int milliseconds);
extern unsigned int WINAPI wait_for (gpointer handle, unsigned int milliseconds);

#define STEP_MS 10
#define INFINITE 0xffffffff

typedef struct
{
  volatile gint started;
  volatile gint active;  /* a pause is on */
  volatile gint claim;   /* who adds 1 to 0 resumes the threads */
  volatile gint beat;
  volatile gint count;
  gint limit;
  volatile gint fired;
  volatile gint quit;
  gpointer event;        /* wakes the watchdog for a pause, or to quit */
  gpointer handles[1];
} Watch;

unsigned int WINAPI
watch_pauses (gpointer data)
{
  Watch * w = data;

  w->started = 1;
  for (;;)
  {
    gint last, quiet = 0;

    wait_for (w->event, INFINITE);
    if (w->quit)
      return 0;
    last = w->beat;
    while (w->active && !w->quit)
    {
      sleep_ms (STEP_MS);
      if (w->beat != last)
      {
        last = w->beat;
        quiet = 0;
        continue;
      }
      quiet += STEP_MS;
      if (quiet < w->limit)
        continue;
      if (g_atomic_int_add (&w->claim, 1) == 0)
      {
        gint i, n = w->count;

        for (i = 0; i != n; i++)
          resume_thread (w->handles[i]);
        w->fired = 1;
      }
      break;
    }
  }
}

int
claim_resume (Watch * w)
{
  return g_atomic_int_add (&w->claim, 1) == 0;
}
`;

function pauseThreads() {
  // Suspend all threads but this one; the handles, with where each stands
  // (ips) and how long it took (stats). Nothing here may wait for a lock a
  // paused thread could hold:
  // - The calls keep Frida's JavaScript lock (scheduling 'exclusive'). By
  //   default a NativeFunction gives it up while it runs, a game thread in a
  //   JavaScript hook (another agent's, too: they share the lock) took it and
  //   was suspended holding it, and the agent waited for it for good when
  //   SuspendThread returned (RoboCop under Proton, 2026-11-17).
  // - The CONTEXT buffer is allocated once, before any thread is suspended.
  // What the caller does while the threads are paused may still need a lock
  // of Frida's that a game thread was suspended with: each game thread
  // entering or leaving a JavaScript hook takes the Interceptor's lock for a
  // moment, and attaching or detaching takes it too (RoboCop hung for good
  // removing a trace's hooks, 2026-11-19). A watchdog thread lets the
  // threads run on when the caller is not done after PAUSE_LIMIT_MS.
  if (Process.platform !== 'windows')
    return null;
  // CONTEXT differs between x64 and x86 in size, layout and flag values.
  const wide = Process.pointerSize === 8;
  const CONTEXT_SIZE = wide ? 0x4d0 : 0x2cc, CONTEXT_FLAGS = wide ? 0x30 : 0;
  const CONTEXT_CONTROL = wide ? 0x100001 : 0x10001;
  if (threadPauser === null) {
    const kernel32 = Process.getModuleByName('kernel32.dll');
    // Kept referenced: Frida frees what Memory.alloc returned once that
    // pointer object is collected, and a pointer derived from it (the
    // aligned one) does not keep it alive. GetThreadContext then wrote into
    // freed memory of Frida's heap, which broke it (RoboCop, 2026-11-19).
    const contextMemory = Memory.alloc(CONTEXT_SIZE + 16);
    const exclusive = { scheduling: 'exclusive' };
    const fn = (name, ret, args) =>
      new NativeFunction(kernel32.getExportByName(name), ret, args, exclusive);
    threadPauser = {
      open: fn('OpenThread', 'pointer', ['uint32', 'int', 'uint32']),
      suspend: fn('SuspendThread', 'uint32', ['pointer']),
      resume: fn('ResumeThread', 'uint32', ['pointer']),
      context: fn('GetThreadContext', 'int', ['pointer', 'pointer']),
      close: fn('CloseHandle', 'int', ['pointer']),
      // Thread.sleep would give up the JavaScript lock, which game threads in
      // a busy hook may then keep from this one for long.
      sleep: fn('Sleep', 'void', ['uint32']),
      contextMemory,
      // CONTEXT must be 16-byte aligned.
      contextBuffer: contextMemory.add(15)
        .and(ptr(wide ? '0xfffffffffffffff0' : '0xfffffff0')),
      watch: startPauseWatch(kernel32, fn)
    };
  }
  const THREAD_SUSPEND_RESUME = 0x0002, THREAD_GET_CONTEXT = 0x0008;
  const SUSPEND_FAILED = 0xffffffff;
  const t = threadPauser;
  const context = t.contextBuffer;
  const self = Process.getCurrentThreadId();
  const watch = beginWatch(t.watch);
  const threads = Process.enumerateThreads();
  const handles = [];
  handles.watch = watch;
  const began = Date.now();
  for (const thread of threads) {
    if (thread.id === self || (watch !== null && thread.id === watch.id))
      continue;
    const handle = t.open(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT, 0, thread.id);
    if (handle.isNull())
      continue;  // ended meanwhile
    if (t.suspend(handle) === SUSPEND_FAILED) {
      t.close(handle);
      continue;
    }
    handles.push(handle);
    if (watch !== null && handles.length <= WATCH_CAPACITY) {
      // The handle before the count: the watchdog reads only what is counted.
      const memory = watch.memory;
      memory.add(WATCH_HANDLES + (handles.length - 1) * Process.pointerSize)
        .writePointer(handle);
      memory.add(WATCH_COUNT).writeS32(handles.length);
    }
  }
  const suspended = Date.now();
  // SuspendThread only asks the thread to stop; reading its context returns
  // once it has. Where each thread stands (CONTEXT.Rip, CONTEXT.Eip):
  // handles.ips, null for one whose context could not be read
  const IP = wide ? 0xf8 : 0xb8;
  handles.ips = [];
  for (const handle of handles) {
    context.add(CONTEXT_FLAGS).writeU32(CONTEXT_CONTROL);
    handles.ips.push(t.context(handle, context) ? context.add(IP).readPointer() : null);
  }
  handles.stats = {
    threads: handles.length,
    suspend_ms: suspended - began,
    context_ms: Date.now() - suspended,
    unreadable: handles.ips.filter(ip => ip === null).length
  };
  return handles;
}

function startPauseWatch(kernel32, fn) {
  // The watchdog of this agent's pauses: one thread of plain C (no Frida
  // locks, no JavaScript) for the agent's lifetime. It waits for an event,
  // and during a pause resumes the paused threads once the agent has not
  // resumed them within PAUSE_LIMIT_MS. Started before any thread is paused, and
  // kept: a thread starting or ending runs code of every DLL (Frida's, too)
  // under the loader's lock, which a paused thread may hold. Null if it
  // cannot start: pauses then go on without.
  const create = fn('CreateThread', 'pointer',
                    ['pointer', 'size_t', 'pointer', 'pointer', 'uint32', 'pointer']);
  const createEvent = fn('CreateEventW', 'pointer', ['pointer', 'int', 'int', 'pointer']);
  const setEvent = fn('SetEvent', 'int', ['pointer']);
  const wait = fn('WaitForSingleObject', 'uint32', ['pointer', 'uint32']);
  const close = fn('CloseHandle', 'int', ['pointer']);
  const sleep = fn('Sleep', 'void', ['uint32']);
  const cm = new CModule(PAUSE_WATCH_SOURCE, {
    resume_thread: kernel32.getExportByName('ResumeThread'),
    sleep_ms: kernel32.getExportByName('Sleep'),
    wait_for: kernel32.getExportByName('WaitForSingleObject')
  });
  const memory = Memory.alloc(WATCH_HANDLES + Process.pointerSize * WATCH_CAPACITY);
  memory.add(WATCH_LIMIT).writeS32(PAUSE_LIMIT_MS);
  const event = createEvent(NULL, 0, 0, NULL);  // auto-reset
  if (event.isNull())
    return null;
  memory.add(WATCH_EVENT).writePointer(event);
  // TinyCC decorates a stdcall function of 32-bit Windows code: "_watch_pauses@4"
  const watchPauses = cm.watch_pauses || cm[`_watch_pauses@${Process.pointerSize}`];
  if (watchPauses === undefined) {
    close(event);
    return null;
  }
  const threadId = Memory.alloc(4);
  const thread = create(NULL, 0, watchPauses, memory, 0, threadId);
  if (thread.isNull()) {
    close(event);
    return null;
  }
  const START_MS = 1000;
  const deadline = Date.now() + START_MS;
  while (memory.add(WATCH_STARTED).readS32() === 0 && Date.now() < deadline)
    sleep(1);
  const watch = {
    cm, memory, event, thread, id: threadId.readU32(), setEvent, sleep,
    claim: new NativeFunction(cm.claim_resume, 'int', ['pointer'], { scheduling: 'exclusive' }),
    began: 0
  };
  // Out of other agents' Process.enumerateThreads(): their pauses leave it be.
  try {
    Cloak.addThread(watch.id);
  } catch (_error) {
    // Older Frida: the others may suspend it, which only matters to this one.
  }
  // End it before the agent unloads and its C code goes. The callback holds
  // the C code itself: collected with ``watch`` it could be freed before the
  // thread returned from it (that ended a 32-bit game in a test, 2026-11-21).
  Script.bindWeak(watch, () => {
    memory.add(WATCH_QUIT).writeS32(1);
    setEvent(event);
    const ended = wait(thread, START_MS) === WAIT_OBJECT_0;
    close(thread);
    close(event);
    if (ended)
      cm.dispose();  // else it stays: the thread may still run in it
  });
  return watch;
}

function beginWatch(watch) {
  // Arm the watchdog for a pause; null if there is none or it is busy.
  if (watch === null || watch.memory.add(WATCH_ACTIVE).readS32() !== 0)
    return null;
  const memory = watch.memory;
  memory.add(WATCH_COUNT).writeS32(0);
  memory.add(WATCH_FIRED).writeS32(0);
  memory.add(WATCH_CLAIM).writeS32(0);
  pauseHeartbeatOf(watch);
  memory.add(WATCH_ACTIVE).writeS32(1);
  watch.setEvent(watch.event);
  watch.began = Date.now();
  return watch;
}

function pauseHeartbeatOf(watch) {
  const beat = watch.memory.add(WATCH_BEAT);
  beat.writeS32((beat.readS32() + 1) | 0);
}

function resumeThreads(paused) {
  if (paused === null)
    return;
  const t = threadPauser;
  const watch = paused.watch;
  // Whoever claims first resumes the threads: this agent or the watchdog.
  const mine = watch === null || watch.claim(watch.memory) !== 0;
  if (mine) {
    for (const handle of paused)
      t.resume(handle);
  } else {
    // The watchdog is at it: wait until it is through before the handles go.
    const deadline = Date.now() + PAUSE_LIMIT_MS;
    while (watch.memory.add(WATCH_FIRED).readS32() === 0 && Date.now() < deadline)
      t.sleep(1);
  }
  if (watch !== null)
    watch.memory.add(WATCH_ACTIVE).writeS32(0);
  for (const handle of paused)
    t.close(handle);
  if (!mine) {
    noteActivity('threads_pause_timeout', {
      threads: paused.length, paused_ms: Date.now() - watch.began,
      limit_ms: PAUSE_LIMIT_MS
    });
  }
}

let codeAllocator = null;

function allocateCode(size) {
  // Executable memory that stays when the agent unloads: game threads may
  // still run code there (VirtualAlloc, or mmap).
  if (codeAllocator === null) {
    if (Process.platform === 'windows') {
      const virtualAlloc = new NativeFunction(
        Process.getModuleByName('kernel32.dll').getExportByName('VirtualAlloc'),
        'pointer', ['pointer', 'size_t', 'uint32', 'uint32']);
      const MEM_COMMIT_RESERVE = 0x3000, PAGE_EXECUTE_READWRITE = 0x40;
      codeAllocator = length => virtualAlloc(NULL, length, MEM_COMMIT_RESERVE,
                                             PAGE_EXECUTE_READWRITE);
    } else {
      const mmap = new NativeFunction(Module.getGlobalExportByName('mmap'), 'pointer',
                                      ['pointer', 'size_t', 'int', 'int', 'int', 'long']);
      const PROT_RWX = 7, MAP_PRIVATE_ANONYMOUS = 0x22;
      codeAllocator = length => {
        const memory = mmap(NULL, length, PROT_RWX, MAP_PRIVATE_ANONYMOUS, -1, 0);
        return memory.equals(ptr(-1)) ? NULL : memory;
      };
    }
  }
  const memory = codeAllocator(size);
  if (memory.isNull())
    throw grembyteError('error.platform.code_alloc', { size });
  return memory;
}

let programModuleFound = null;

function programModule() {
  // The game's executable. Under Wine Frida's Process.mainModule is just the
  // first module it lists (e.g. netapi32.dll or uxtheme.dll); Windows itself
  // knows better.
  if (programModuleFound !== null)
    return programModuleFound;
  if (Process.platform === 'windows') {
    const getModuleHandle = new NativeFunction(
      Process.getModuleByName('kernel32.dll').getExportByName('GetModuleHandleW'),
      'pointer', ['pointer']);
    const module = Process.findModuleByAddress(getModuleHandle(NULL));
    if (module !== null) {
      programModuleFound = module;
      return module;
    }
  }
  programModuleFound = Process.mainModule;
  return programModuleFound;
}
