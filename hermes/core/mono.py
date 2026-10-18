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
``mono.assemblies()`` / ``mono.classes(assembly)``
    The catalog: the loaded assemblies (``{name, file, classes}``) and the
    classes of one with their methods (see ``classes`` below).
``mono.prepare(identity)`` / ``mono.objectClassName(object)``
    For the method monitor: ``compile`` plus the method's signature, and
    the class name of a managed object.
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

import os
import re

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
        : new NativeFunction(free, 'void', ['pointer'], { scheduling: 'exclusive' }),
      // Only the catalog needs these; without them the rest still works.
      catalog: optional(() => ({
        assemblyForeach: exported('mono_assembly_foreach', 'void', ['pointer', 'pointer']),
        assemblyImage: exported('mono_assembly_get_image', 'pointer', ['pointer']),
        tableRows: exported('mono_image_get_table_rows', 'int', ['pointer', 'int']),
        classGet: exported('mono_class_get', 'pointer', ['pointer', 'uint32']),
        classType: exported('mono_class_get_type', 'pointer', ['pointer']),
        typeName: exported('mono_type_get_name', 'pointer', ['pointer']),
        classMethods: exported('mono_class_get_methods', 'pointer', ['pointer', 'pointer']),
        methodName: exported('mono_method_get_name', 'pointer', ['pointer']),
        methodFlags: exported('mono_method_get_flags', 'uint32', ['pointer', 'pointer']),
        methodSignature: exported('mono_method_signature', 'pointer', ['pointer']),
        returnType: exported('mono_signature_get_return_type', 'pointer', ['pointer'])
      })),
      // Only the method monitor needs these.
      types: optional(() => ({
        methodSignature: exported('mono_method_signature', 'pointer', ['pointer']),
        returnType: exported('mono_signature_get_return_type', 'pointer', ['pointer']),
        params: exported('mono_signature_get_params', 'pointer', ['pointer', 'pointer']),
        paramCount: exported('mono_signature_get_param_count', 'uint32', ['pointer']),
        paramNames: exported('mono_method_get_param_names', 'void', ['pointer', 'pointer']),
        isInstance: exported('mono_signature_is_instance', 'int', ['pointer']),
        typeKind: exported('mono_type_get_type', 'int', ['pointer']),
        isByref: exported('mono_type_is_byref', 'int', ['pointer']),
        typeName: exported('mono_type_get_name', 'pointer', ['pointer']),
        typeClass: exported('mono_class_from_mono_type', 'pointer', ['pointer']),
        classType: exported('mono_class_get_type', 'pointer', ['pointer']),
        isEnum: exported('mono_class_is_enum', 'int', ['pointer']),
        enumBase: exported('mono_class_enum_basetype', 'pointer', ['pointer']),
        isValueType: exported('mono_class_is_valuetype', 'int', ['pointer']),
        valueSize: exported('mono_class_value_size', 'int', ['pointer', 'pointer']),
        objectClass: exported('mono_object_get_class', 'pointer', ['pointer'])
      }))
    };
  }

  function optional(bindAll) {
    try {
      return bindAll();
    } catch (_error) {
      return null;
    }
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

  function findImage(identity) {
    // The catalog knows assemblies by name only.
    let image = identity.file ? api.imageLoaded(Memory.allocUtf8String(identity.file)) : NULL;
    if (image.isNull())
      image = api.imageLoaded(Memory.allocUtf8String(identity.image));
    if (image.isNull() && api.catalog !== null)
      image = loadedImages(api.catalog).find(
        candidate => api.imageName(candidate).readUtf8String() === identity.image) || NULL;
    if (image.isNull())
      throw new Error(`assembly ${identity.image} is not loaded (yet)`);
    return image;
  }

  function compileOnThread(domain, identity) {
    const method = api.getMethod(findImage(identity), identity.token, NULL);
    if (method.isNull() || signatureName(method) !== identity.method)
      throw new Error(`${identity.method} is not in ${identity.image} (game updated?)`);
    const code = api.compileMethod(method);
    const root = api.rootDomain();
    let info = code.isNull() ? NULL : api.findJitInfo(domain, code);
    if (info.isNull() && !code.isNull() && !root.equals(domain))
      info = api.findJitInfo(root, code);
    if (info.isNull())
      throw new Error(`Mono did not compile ${identity.method}`);
    return { method, start: api.codeStart(info), size: api.codeSize(info) };
  }

  function compile(identity) {
    if (!refresh())
      throw new Error('no Mono runtime in the process');
    return onMonoThread(domain => {
      const { start, size } = compileOnThread(domain, identity);
      return { start, size };
    });
  }

  // --- Signatures: what the method monitor needs to decode arguments ------

  const TYPE = {
    VOID: 0x01, BOOLEAN: 0x02, CHAR: 0x03, I1: 0x04, U1: 0x05, I2: 0x06, U2: 0x07,
    I4: 0x08, U4: 0x09, I8: 0x0a, U8: 0x0b, R4: 0x0c, R8: 0x0d, STRING: 0x0e,
    VALUETYPE: 0x11, GENERICINST: 0x15, I: 0x18, U: 0x19
  };

  function typesApi() {
    if (!refresh())
      throw new Error('no Mono runtime in the process');
    if (api.types === null)
      throw new Error('this Mono runtime lacks the functions to read signatures');
    return api.types;
  }

  function typeInfo(t, type) {
    // { kind, byref, name, enumName?, structSize? }: enums become their base
    // type, value types that are no enum are structs ('VALUETYPE').
    const info = {
      kind: t.typeKind(type),
      byref: t.isByref(type) !== 0,
      name: cString(t.typeName(type)) || '?'
    };
    if (info.kind === TYPE.VALUETYPE || info.kind === TYPE.GENERICINST) {
      const klass = t.typeClass(type);
      if (!klass.isNull() && t.isEnum(klass) !== 0) {
        info.enumName = info.name;
        info.kind = t.typeKind(t.enumBase(klass));
      } else if (!klass.isNull() && t.isValueType(klass) !== 0) {
        info.kind = TYPE.VALUETYPE;
        info.structSize = t.valueSize(klass, NULL);
      }
    }
    return info;
  }

  function prepare(identity) {
    // Compile the method like compile() and describe its signature:
    // { start, size, instance, params: [{ name, type }], returns: type }.
    const t = typesApi();
    return onMonoThread(domain => {
      const { method, start, size } = compileOnThread(domain, identity);
      const signature = t.methodSignature(method);
      if (signature.isNull())
        throw new Error(`no signature for ${identity.method}`);
      const count = t.paramCount(signature);
      const names = Memory.alloc(Process.pointerSize * Math.max(count, 1));
      t.paramNames(method, names);
      const iterator = Memory.alloc(Process.pointerSize);
      iterator.writePointer(NULL);
      const params = [];
      for (let i = 0; i < count; i++) {
        const type = t.params(signature, iterator);
        const name = names.add(i * Process.pointerSize).readPointer();
        params.push({
          name: name.isNull() ? `arg${i}` : name.readUtf8String(),
          type: typeInfo(t, type)
        });
      }
      return {
        start, size, params,
        instance: t.isInstance(signature) !== 0,
        returns: typeInfo(t, t.returnType(signature))
      };
    });
  }

  const classNames = new Map();

  function objectClassName(object) {
    // Class of a managed object; metadata only, any thread will do.
    const t = typesApi();
    const klass = t.objectClass(object);
    const key = klass.toString();
    let text = classNames.get(key);
    if (text === undefined) {
      text = cString(t.typeName(t.classType(klass))) || '?';
      classNames.set(key, text);
    }
    return text;
  }

  // --- Catalog: the classes and methods of the loaded assemblies ----------

  const TYPEDEF_TABLE = 2;
  const TYPEDEF_TOKEN = 0x02000000;
  const METHOD_STATIC = 0x10;

  function catalogApi() {
    if (!refresh())
      throw new Error('no Mono runtime in the process');
    if (api.catalog === null)
      throw new Error('this Mono runtime lacks the functions for a catalog');
    return api.catalog;
  }

  function loadedImages(c) {
    const images = [];
    const collect = new NativeCallback(assembly => {
      images.push(c.assemblyImage(assembly));
    }, 'void', ['pointer', 'pointer']);
    c.assemblyForeach(collect, NULL);
    return images;
  }

  function assemblies() {
    // Loading classes needs a thread Mono knows.
    const c = catalogApi();
    return onMonoThread(() => loadedImages(c).map(image => ({
      name: api.imageName(image).readUtf8String(),
      file: api.imageFile(image).readUtf8String(),
      classes: Math.max(0, c.tableRows(image, TYPEDEF_TABLE) - 1)
    })));
  }

  function methodEntry(c, method) {
    // [name, parameters, return type, static, token, full signature]
    const full = signatureName(method) || '';
    const open = full.indexOf(' (');
    const signature = c.methodSignature(method);
    return [
      c.methodName(method).readUtf8String(),
      open < 0 ? '()' : full.substring(open + 1),
      signature.isNull() ? '' : cString(c.typeName(c.returnType(signature))) || '',
      (c.methodFlags(method, NULL) & METHOD_STATIC) !== 0,
      api.methodToken(method),
      full
    ];
  }

  function classes(assembly) {
    // [[class name, [method entry, ...]], ...] of one assembly; row 1 of
    // its type table is the <Module> pseudo class.
    const c = catalogApi();
    return onMonoThread(() => {
      const image = loadedImages(c).find(
        candidate => api.imageName(candidate).readUtf8String() === assembly);
      if (image === undefined)
        throw new Error(`assembly ${assembly} is not loaded`);
      const iterator = Memory.alloc(Process.pointerSize);
      const result = [];
      const rows = c.tableRows(image, TYPEDEF_TABLE);
      for (let row = 2; row <= rows; row++) {
        const klass = c.classGet(image, TYPEDEF_TOKEN | row);
        if (klass.isNull())
          continue;
        const methods = [];
        iterator.writePointer(NULL);
        for (;;) {
          const method = c.classMethods(klass, iterator);
          if (method.isNull())
            break;
          methods.push(methodEntry(c, method));
        }
        result.push([cString(c.typeName(c.classType(klass))), methods]);
      }
      return result;
    });
  }

  refresh();
  return {
    refresh, locate, name, describe, identify, compile, assemblies, classes, prepare,
    objectClassName, TYPE
  };
})();
"""


# Mono runtime libraries; libmono-native.so (System.Native) is none.
_RUNTIME_NAMES = re.compile(r"^libmono(bdwgc|sgen)?(-2\.0)?\.so")


def has_mono_runtime(pid, proc_root="/proc"):
    """Return whether the process has loaded a Mono runtime."""
    try:
        with open(f"{proc_root}/{pid}/maps", encoding="utf-8", errors="replace") as maps:
            return any(
                _RUNTIME_NAMES.match(os.path.basename(line.split(maxsplit=5)[-1].strip()))
                for line in maps
                if "/" in line
            )
    except OSError:
        return False
