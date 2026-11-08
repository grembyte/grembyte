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
          throw hermesError('error.platform.create_thread');
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
          throw hermesError('error.platform.pthread_create', { code });
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
  // CONTEXT differs between x64 and x86 in size, layout and flag values.
  const wide = Process.pointerSize === 8;
  const CONTEXT_SIZE = wide ? 0x4d0 : 0x2cc, CONTEXT_FLAGS = wide ? 0x30 : 0;
  const CONTEXT_CONTROL = wide ? 0x100001 : 0x10001;
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
  const context = Memory.alloc(CONTEXT_SIZE + 16).add(15)
    .and(ptr(wide ? '0xfffffffffffffff0' : '0xfffffff0'));
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
    throw hermesError('error.platform.code_alloc', { size });
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
