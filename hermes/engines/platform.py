"""Platform layer of the Frida agents: what differs between Linux and Windows.

A native game shows the agent Linux; a Wine/Proton game shows it Windows,
because Frida runs there as its Windows gadget (see wine_gadget.py). Engine
adapters and features use these helpers instead of calling either system's
API themselves.

``spawnThread(start, arg)``
    Run the native function ``start(arg)`` on a new thread nobody waits for
    (``pthread_create``, or ``CreateThread`` under Wine). Throws on failure.
``watchUnmaps(onUnmap)``
    Call ``onUnmap(start, end)`` before memory is unmapped or released, while
    it is still mapped (``munmap``; under Wine ``NtFreeVirtualMemory`` and
    ``NtUnmapViewOfSection``). Returns a listener with ``detach()``.
``pauseThreads()`` / ``resumeThreads(paused)``
    Stop every thread of the game while the agent changes code in bulk, and
    let them run again. Frida's own threads keep running: they are not in
    ``Process.enumerateThreads()``. Windows only (``SuspendThread``, which Wine
    delays until a thread holds none of its internal locks); elsewhere
    ``pauseThreads`` returns null and does nothing. Under Wine, game threads
    entering freshly hooked code while Frida still hooks more could deadlock
    on a lock inside Frida's Windows gadget; Linux has no such problem.
"""

PLATFORM_AGENT_SOURCE = r"""
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
          throw new Error('CreateThread failed');
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
          throw new Error(`pthread_create failed (${code})`);
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
  const INFO_SIZE = 48;  // MEMORY_BASIC_INFORMATION (64-bit)
  const currentProcess = ptr('0xffffffffffffffff');
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
           && info.add(8).readPointer().equals(base)
           && info.add(32).readU32() !== MEM_FREE)
      cursor = info.readPointer().add(info.add(24).readU64());
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

let threadPauser = null;

function pauseThreads() {
  if (Process.platform !== 'windows')
    return null;
  if (threadPauser === null) {
    const kernel32 = Process.getModuleByName('kernel32.dll');
    const fn = (name, ret, args) => new NativeFunction(kernel32.getExportByName(name), ret, args);
    threadPauser = {
      open: fn('OpenThread', 'pointer', ['uint32', 'int', 'uint32']),
      suspend: fn('SuspendThread', 'uint32', ['pointer']),
      resume: fn('ResumeThread', 'uint32', ['pointer']),
      context: fn('GetThreadContext', 'int', ['pointer', 'pointer']),
      close: fn('CloseHandle', 'int', ['pointer'])
    };
  }
  const THREAD_SUSPEND_RESUME = 0x0002, THREAD_GET_CONTEXT = 0x0008;
  const SUSPEND_FAILED = 0xffffffff;
  const CONTEXT_SIZE = 0x4d0, CONTEXT_FLAGS = 0x30, CONTEXT_CONTROL = 0x100001;
  const t = threadPauser;
  const self = Process.getCurrentThreadId();
  const handles = [];
  for (const thread of Process.enumerateThreads()) {
    if (thread.id === self)
      continue;
    const handle = t.open(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT, 0, thread.id);
    if (handle.isNull())
      continue;  // ended meanwhile
    if (t.suspend(handle) === SUSPEND_FAILED) {
      t.close(handle);
      continue;
    }
    handles.push(handle);
  }
  // SuspendThread only asks the thread to stop; reading its context returns
  // once it has. CONTEXT must be 16-byte aligned.
  const context = Memory.alloc(CONTEXT_SIZE + 16).add(15).and(ptr('0xfffffffffffffff0'));
  for (const handle of handles) {
    context.add(CONTEXT_FLAGS).writeU32(CONTEXT_CONTROL);
    t.context(handle, context);
  }
  return handles;
}

function resumeThreads(paused) {
  if (paused === null)
    return;
  for (const handle of paused) {
    threadPauser.resume(handle);
    threadPauser.close(handle);
  }
}
"""
