"""C# method names for JIT code of Mono games (e.g. Unity games using Mono).

Mono compiles C# methods into anonymous memory, so a code address there has
no module + offset. The Mono runtime knows which method it belongs to. The
agent snippet below asks it through Mono's embedding API, which the runtime
library (``libmonobdwgc-2.0.so``, ``libmonosgen-2.0.so``, ``libmono.so``)
exports.

Frida agents include ``MONO_AGENT_SOURCE``; it defines ``mono`` with:

``mono.refresh()``
    Look for the Mono runtime (again, at most every two seconds) and return
    whether it is loaded. Call it from the agent's own thread.
``mono.locate(address)``
    Find the JIT method containing ``address`` and remember it. Returns the
    location, ``null`` (no JIT code) or ``undefined`` (unknown: no Mono
    runtime, or this thread is no Mono thread; ask again later). Only call it
    on a thread that runs managed code: Mono's lock-free JIT table expects a
    thread it knows, and the thread's domain is the one its code lives in.
    That makes it usable in an exception handler for a trap in JIT code.
``mono.name(address)``
    ``Namespace.Class:Method+0x1a`` for an address ``locate`` found, from
    any thread, else ``null``. It never searches the JIT table itself.
``mono.describe(address)``
    ``locate`` and ``name`` in one, for threads that run managed code.
``mono.identify(address)``
    What finds the code again after a restart, for an address ``locate``
    found: ``{image, file, token, method, offset}`` (assembly name and path,
    metadata token, ``Namespace.Class:Method (params)``, offset in its code),
    else ``null``. Token and offset stay valid while the game is unchanged.
``mono.compile(identity)``
    Look up the method of an ``identify`` result, let Mono compile it (or
    return the code it has) and return ``{start, size}`` of its code.
    Throws if the assembly is not loaded or the method has changed.

The agent's own thread is deliberately not attached to Mono: an attached
thread is stopped by every garbage collection, also while it holds the
agent's lock, which a game thread trapped in an exception handler may wait
for. ``compile`` runs on a short-lived thread of its own instead, which
Mono forgets again when it ends.
"""

MONO_AGENT_SOURCE = r"""
const mono = (() => {
  const SEARCH_INTERVAL = 2000;
  const MAX_CACHE = 4096;
  let api = null;
  let lastSearch = -Infinity;
  // address -> { method, offset } or null (no JIT code)
  const locations = new Map();
  // method -> 'Namespace.Class:Method' or null
  const names = new Map();

  function bind(module) {
    const address = name => {
      const found = module.findExportByName(name);
      if (found === null)
        throw new Error(`${module.name} does not export ${name}`);
      return found;
    };
    // Exclusive: the calls are short, and some run in exception handlers.
    const exported = (name, ret, args) =>
      new NativeFunction(address(name), ret, args, { scheduling: 'exclusive' });
    // Cooperative: these may take Mono's locks or run a garbage collection,
    // so they must not hold the agent's lock that game threads wait for.
    const blocking = (name, ret, args) => new NativeFunction(address(name), ret, args);
    const free = module.findExportByName('mono_free');
    return {
      domainForeach: blocking('mono_domain_foreach', 'void', ['pointer', 'pointer']),
      threadAttach: blocking('mono_thread_attach', 'pointer', ['pointer']),
      threadDetach: blocking('mono_thread_detach', 'void', ['pointer']),
      imageLoaded: blocking('mono_image_loaded', 'pointer', ['pointer']),
      getMethod: blocking('mono_get_method', 'pointer', ['pointer', 'uint32', 'pointer']),
      compileMethod: blocking('mono_compile_method', 'pointer', ['pointer']),
      codeSize: exported('mono_jit_info_get_code_size', 'int', ['pointer']),
      methodToken: exported('mono_method_get_token', 'uint32', ['pointer']),
      methodClass: exported('mono_method_get_class', 'pointer', ['pointer']),
      classImage: exported('mono_class_get_image', 'pointer', ['pointer']),
      imageName: exported('mono_image_get_name', 'pointer', ['pointer']),
      imageFile: exported('mono_image_get_filename', 'pointer', ['pointer']),
      rootDomain: exported('mono_get_root_domain', 'pointer', []),
      currentDomain: exported('mono_domain_get', 'pointer', []),
      findJitInfo: exported('mono_jit_info_table_find', 'pointer', ['pointer', 'pointer']),
      jitMethod: exported('mono_jit_info_get_method', 'pointer', ['pointer']),
      codeStart: exported('mono_jit_info_get_code_start', 'pointer', ['pointer']),
      fullName: exported('mono_method_full_name', 'pointer', ['pointer', 'int']),
      free: free === null ? null
        : new NativeFunction(free, 'void', ['pointer'], { scheduling: 'exclusive' })
    };
  }

  function refresh() {
    if (api !== null)
      return true;
    const now = Date.now();
    if (now - lastSearch < SEARCH_INTERVAL)
      return false;
    lastSearch = now;
    for (const module of Process.enumerateModules()) {
      if (!module.name.startsWith('libmono')
          || module.findExportByName('mono_jit_info_table_find') === null)
        continue;
      try {
        api = bind(module);
        return true;
      } catch (_error) {
      }
    }
    return false;
  }

  function remember(key, location) {
    if (locations.size >= MAX_CACHE) {
      locations.clear();
      names.clear();
    }
    locations.set(key, location);
    return location;
  }

  function locate(address) {
    if (api === null)
      return undefined;
    const key = address.toString();
    const cached = locations.get(key);
    if (cached !== undefined)
      return cached;
    if (Process.findModuleByAddress(address) !== null)
      return remember(key, null);
    // Threads Mono does not know have no domain.
    const current = api.currentDomain();
    if (current.isNull())
      return undefined;
    // Shared code lives in the root domain, Unity's scripts in a child domain.
    const root = api.rootDomain();
    for (const domain of root.isNull() || root.equals(current) ? [current] : [current, root]) {
      const info = api.findJitInfo(domain, address);
      if (!info.isNull())
        return remember(key, {
          method: api.jitMethod(info),
          offset: address.sub(api.codeStart(info))
        });
    }
    return remember(key, null);
  }

  function name(address) {
    const location = locations.get(address.toString());
    if (!location)
      return null;
    const key = location.method.toString();
    let text = names.get(key);
    if (text === undefined) {
      text = cString(api.fullName(location.method, 0));
      names.set(key, text);
    }
    return text === null ? null : `${text}+${location.offset}`;
  }

  function describe(address) {
    return locate(address) ? name(address) : null;
  }

  function cString(pointer) {
    const text = pointer.isNull() ? null : pointer.readUtf8String();
    if (!pointer.isNull() && api.free !== null)
      api.free(pointer);
    return text;
  }

  function signatureName(method) {
    return cString(api.fullName(method, 1));
  }

  function identify(address) {
    const location = locations.get(address.toString());
    if (!location)
      return null;
    const image = api.classImage(api.methodClass(location.method));
    return {
      image: api.imageName(image).readUtf8String(),
      file: api.imageFile(image).readUtf8String(),
      token: api.methodToken(location.method),
      method: signatureName(location.method),
      offset: location.offset.toInt32()
    };
  }

  function scriptDomain() {
    // Unity runs its scripts in a child domain created after the root one.
    const domains = [];
    const collect = new NativeCallback(domain => { domains.push(domain); }, 'void',
      ['pointer', 'pointer']);
    api.domainForeach(collect, NULL);
    const root = api.rootDomain();
    const children = domains.filter(domain => !domain.equals(root));
    return children.length > 0 ? children[children.length - 1] : root;
  }

  function onMonoThread(work) {
    // Mono compiles only for threads it knows. A thread of our own attaches
    // and ends afterwards, so Mono forgets it (see above).
    const create = new NativeFunction(Module.getGlobalExportByName('pthread_create'),
      'int', ['pointer', 'pointer', 'pointer', 'pointer']);
    const join = new NativeFunction(Module.getGlobalExportByName('pthread_join'),
      'int', ['pointer', 'pointer']);
    let result, failure = null;
    const body = new NativeCallback(() => {
      try {
        const domain = scriptDomain();
        const thread = api.threadAttach(domain);
        try {
          result = work(domain);
        } finally {
          api.threadDetach(thread);
        }
      } catch (error) {
        failure = error;
      }
      return NULL;
    }, 'pointer', ['pointer']);
    const handle = Memory.alloc(Process.pointerSize);
    const code = create(handle, NULL, body, NULL);
    if (code !== 0)
      throw new Error(`pthread_create failed (${code})`);
    join(handle.readPointer(), NULL);
    if (failure !== null)
      throw failure;
    return result;
  }

  function compile(identity) {
    if (!refresh())
      throw new Error('no Mono runtime in the process');
    return onMonoThread(domain => {
      let image = api.imageLoaded(Memory.allocUtf8String(identity.file));
      if (image.isNull())
        image = api.imageLoaded(Memory.allocUtf8String(identity.image));
      if (image.isNull())
        throw new Error(`assembly ${identity.image} is not loaded (yet)`);
      const method = api.getMethod(image, identity.token, NULL);
      if (method.isNull() || signatureName(method) !== identity.method)
        throw new Error(`${identity.method} is not in ${identity.image} (game updated?)`);
      const code = api.compileMethod(method);
      const root = api.rootDomain();
      let info = code.isNull() ? NULL : api.findJitInfo(domain, code);
      if (info.isNull() && !code.isNull() && !root.equals(domain))
        info = api.findJitInfo(root, code);
      if (info.isNull())
        throw new Error(`Mono did not compile ${identity.method}`);
      return { start: api.codeStart(info), size: api.codeSize(info) };
    });
  }

  refresh();
  return { refresh, locate, name, describe, identify, compile };
})();
"""
