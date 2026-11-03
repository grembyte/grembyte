const il2cpp = (() => {
  // GameAssembly.dll (Windows, Wine/Proton) or GameAssembly.so (Linux)
  const RUNTIME_NAME = /^GameAssembly\.(dll|so)$/i;
  const SEARCH_INTERVAL = 2000;
  // ECMA-335 method flags
  const METHOD_STATIC = 0x0010;
  const METHOD_ABSTRACT = 0x0400;
  // A function without unwind data (a leaf) is named after the method
  // before it only this close to its start; where no unwind data could be
  // read at all, this far.
  const LEAF_SPAN = 0x100;
  const MAX_SPAN = 0x10000;
  let module = null;
  let api = null;
  let lastSearch = -Infinity;

  function bind(found) {
    const fn = (name, ret, args) => new NativeFunction(found.getExportByName(name), ret, args);
    const ntdll = Process.platform === 'windows' ? Process.findModuleByName('ntdll.dll') : null;
    const lookup = ntdll === null ? null : ntdll.findExportByName('RtlLookupFunctionEntry');
    return {
      corlib: fn('il2cpp_get_corlib', 'pointer', []),
      domain: fn('il2cpp_domain_get', 'pointer', []),
      assemblies: fn('il2cpp_domain_get_assemblies', 'pointer', ['pointer', 'pointer']),
      assemblyImage: fn('il2cpp_assembly_get_image', 'pointer', ['pointer']),
      imageName: fn('il2cpp_image_get_name', 'pointer', ['pointer']),
      classCount: fn('il2cpp_image_get_class_count', 'size_t', ['pointer']),
      imageClass: fn('il2cpp_image_get_class', 'pointer', ['pointer', 'size_t']),
      classType: fn('il2cpp_class_get_type', 'pointer', ['pointer']),
      classIsGeneric: fn('il2cpp_class_is_generic', 'bool', ['pointer']),
      classMethods: fn('il2cpp_class_get_methods', 'pointer', ['pointer', 'pointer']),
      typeName: fn('il2cpp_type_get_name', 'pointer', ['pointer']),
      methodName: fn('il2cpp_method_get_name', 'pointer', ['pointer']),
      methodClass: fn('il2cpp_method_get_class', 'pointer', ['pointer']),
      classImage: fn('il2cpp_class_get_image', 'pointer', ['pointer']),
      methodToken: fn('il2cpp_method_get_token', 'uint32', ['pointer']),
      methodFlags: fn('il2cpp_method_get_flags', 'uint32', ['pointer', 'pointer']),
      paramCount: fn('il2cpp_method_get_param_count', 'uint32', ['pointer']),
      param: fn('il2cpp_method_get_param', 'pointer', ['pointer', 'uint32']),
      paramName: fn('il2cpp_method_get_param_name', 'pointer', ['pointer', 'uint32']),
      returnType: fn('il2cpp_method_get_return_type', 'pointer', ['pointer']),
      free: found.findExportByName('il2cpp_free') === null ? null
        : fn('il2cpp_free', 'void', ['pointer']),
      // Windows x64: the function containing an address, from the unwind data
      functionEntry: lookup === null ? null
        : new NativeFunction(lookup, 'pointer', ['pointer', 'pointer', 'pointer']),
      // The method monitor and the object inspector; null if any is missing
      objects: optional(() => ({
        threadAttach: fn('il2cpp_thread_attach', 'pointer', ['pointer']),
        threadDetach: fn('il2cpp_thread_detach', 'void', ['pointer']),
        typeKind: fn('il2cpp_type_get_type', 'int', ['pointer']),
        isByref: fn('il2cpp_type_is_byref', 'bool', ['pointer']),
        typeClass: fn('il2cpp_class_from_type', 'pointer', ['pointer']),
        isEnum: fn('il2cpp_class_is_enum', 'bool', ['pointer']),
        enumBase: fn('il2cpp_class_enum_basetype', 'pointer', ['pointer']),
        isValueType: fn('il2cpp_class_is_valuetype', 'bool', ['pointer']),
        valueSize: fn('il2cpp_class_value_size', 'int', ['pointer', 'pointer']),
        isInstance: fn('il2cpp_method_is_instance', 'bool', ['pointer']),
        classParent: fn('il2cpp_class_get_parent', 'pointer', ['pointer']),
        classFields: fn('il2cpp_class_get_fields', 'pointer', ['pointer', 'pointer']),
        fieldName: fn('il2cpp_field_get_name', 'pointer', ['pointer']),
        fieldType: fn('il2cpp_field_get_type', 'pointer', ['pointer']),
        fieldOffset: fn('il2cpp_field_get_offset', 'size_t', ['pointer']),
        fieldFlags: fn('il2cpp_field_get_flags', 'int', ['pointer']),
        classRank: fn('il2cpp_class_get_rank', 'int', ['pointer']),
        elementClass: fn('il2cpp_class_get_element_class', 'pointer', ['pointer']),
        elementSize: fn('il2cpp_class_array_element_size', 'int', ['pointer']),
        methodFromName: fn('il2cpp_class_get_method_from_name', 'pointer',
          ['pointer', 'pointer', 'int']),
        virtualMethod: fn('il2cpp_object_get_virtual_method', 'pointer', ['pointer', 'pointer']),
        runtimeInvoke: fn('il2cpp_runtime_invoke', 'pointer',
          ['pointer', 'pointer', 'pointer', 'pointer']),
        runtimeInvokeAddress: found.getExportByName('il2cpp_runtime_invoke')
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
    const found = Process.enumerateModules().find(candidate => RUNTIME_NAME.test(candidate.name)
      && candidate.findExportByName('il2cpp_domain_get_assemblies') !== null);
    if (found === undefined)
      return false;
    let bound;
    try {
      bound = bind(found);
    } catch (_error) {
      return false;
    }
    // Before il2cpp_init there is no corlib, and il2cpp_domain_get would
    // allocate the domain itself.
    if (bound.corlib().isNull())
      return false;
    module = found;
    api = bound;
    scheduleTable();
    return true;
  }

  function requireRuntime() {
    if (!refresh())
      throw hermesError('error.il2cpp.no_runtime');
    return api;
  }

  function text(pointer) {
    return pointer.isNull() ? null : pointer.readUtf8String();
  }

  function ownedText(pointer) {
    // A string IL2CPP allocated for the caller.
    const result = text(pointer);
    if (!pointer.isNull() && api.free !== null)
      api.free(pointer);
    return result;
  }

  // --- Images and their methods ---------------------------------------------
  // Reading a class's methods makes IL2CPP set the class up (memory of its
  // own, no garbage-collected objects): safe from the agent's thread.

  function images() {
    // [{ image, name, file }]; ``name`` without ".dll", as Mono names assemblies.
    const count = Memory.alloc(8);
    const list = api.assemblies(api.domain(), count);
    const result = [];
    for (let i = 0, n = count.readU64().toNumber(); i < n; i++) {
      const image = api.assemblyImage(list.add(i * Process.pointerSize).readPointer());
      const file = text(api.imageName(image));
      result.push({ image, name: file.replace(/\.dll$/i, ''), file });
    }
    return result;
  }

  function imageNamed(assembly) {
    const found = images().find(candidate => candidate.name === assembly
      || candidate.file === assembly);
    if (found === undefined)
      throw hermesError('error.il2cpp.assembly_not_loaded', { assembly });
    return found;
  }

  function eachClass(image, visit) {
    // visit(klass, [method, ...]) for every class of an image
    const iterator = Memory.alloc(Process.pointerSize);
    for (let i = 0, n = api.classCount(image).toNumber(); i < n; i++) {
      const klass = api.imageClass(image, i);
      if (klass.isNull())
        continue;
      const methods = [];
      iterator.writePointer(NULL);
      for (;;) {
        const method = api.classMethods(klass, iterator);
        if (method.isNull())
          break;
        methods.push(method);
      }
      visit(klass, methods);
    }
  }

  const tokenMaps = new Map();

  function methodByToken(found, token) {
    let map = tokenMaps.get(found.file);
    if (map === undefined) {
      map = new Map();
      eachClass(found.image, (_klass, methods) => {
        for (const method of methods)
          map.set(api.methodToken(method), method);
      });
      tokenMaps.set(found.file, map);
    }
    return map.get(token) || null;
  }

  // --- Names as Mono gives them ----------------------------------------------

  // Mono's short names of the built-in types in signatures
  const SHORT_NAMES = {
    'System.Void': 'void', 'System.Boolean': 'bool', 'System.Char': 'char',
    'System.SByte': 'sbyte', 'System.Byte': 'byte', 'System.Int16': 'int16',
    'System.UInt16': 'uint16', 'System.Int32': 'int', 'System.UInt32': 'uint',
    'System.Int64': 'long', 'System.UInt64': 'ulong', 'System.Single': 'single',
    'System.Double': 'double', 'System.String': 'string', 'System.Object': 'object',
    'System.IntPtr': 'intptr', 'System.UIntPtr': 'uintptr'
  };

  function shortType(name) {
    const at = name.search(/[&*[]/);
    const head = at < 0 ? name : name.substring(0, at);
    const short = SHORT_NAMES[head];
    return short === undefined ? name : short + (at < 0 ? '' : name.substring(at));
  }

  function typeArity(className) {
    // The type parameters of ``Ns.Map<TKey,TValue>`` at the top level: 2
    const open = className.indexOf('<');
    if (open < 0 || !className.endsWith('>'))
      return 0;
    let depth = 0;
    let count = 1;
    for (const c of className.substring(open + 1, className.length - 1)) {
      if (c === '<')
        depth++;
      else if (c === '>')
        depth--;
      else if (c === ',' && depth === 0)
        count++;
    }
    return count;
  }

  const classNames = new Map();

  function classNameOf(klass) {
    // [class name, the class in signatures]: a generic class's definition
    // is ``Ns.List<T>``, its methods ``Ns.List`1:Add (T)`` (as with Mono).
    const key = klass.toString();
    let names = classNames.get(key);
    if (names === undefined) {
      const name = ownedText(api.typeName(api.classType(klass))) || '?';
      const arity = api.classIsGeneric(klass) ? typeArity(name) : 0;
      names = [name, arity > 0 ? `${name.substring(0, name.indexOf('<'))}\`${arity}` : name];
      classNames.set(key, names);
    }
    return names;
  }

  function parameterTypes(method) {
    const types = [];
    for (let i = 0, n = api.paramCount(method); i < n; i++)
      types.push(shortType(ownedText(api.typeName(api.param(method, i))) || '?'));
    return types;
  }

  function parameterNames(method) {
    const names = [];
    for (let i = 0, n = api.paramCount(method); i < n; i++)
      names.push(text(api.paramName(method, i)) || `arg${i}`);
    return names;
  }

  function methodLabel(method) {
    // ``Ns.Class:Method``, as mono_method_full_name without its signature
    return `${classNameOf(api.methodClass(method))[1]}:${text(api.methodName(method))}`;
  }

  function signatureName(method) {
    return `${methodLabel(method)} (${parameterTypes(method).join(',')})`;
  }

  function flagsOf(method, implFlags) {
    implFlags.writeU32(0);
    const flags = api.methodFlags(method, implFlags);
    return [flags, implFlags.readU32()];
  }

  function methodEntry(method, implFlags) {
    // [name, parameters, return type, static, token, full signature,
    //  flags, implementation flags, parameter names], as Mono's catalog
    const [flags, impl] = flagsOf(method, implFlags);
    return [
      text(api.methodName(method)),
      `(${parameterTypes(method).join(',')})`,
      ownedText(api.typeName(api.returnType(method))) || '',
      (flags & METHOD_STATIC) !== 0,
      api.methodToken(method),
      signatureName(method),
      flags,
      impl,
      parameterNames(method)
    ];
  }

  function codeStart(method) {
    // MethodInfo begins with methodPointer; abstract methods point at a
    // stub all of them share, which is no code of theirs.
    const start = method.readPointer();
    if (start.isNull())
      return null;
    const implFlags = Memory.alloc(4);
    return (api.methodFlags(method, implFlags) & METHOD_ABSTRACT) ? null : start;
  }

  // --- Code: which method an address belongs to ---------------------------
  // All code lies in GameAssembly; IL2CPP keeps no table from code to
  // methods. The agent builds one from every method's code pointer (some
  // 230,000 in a large game, under a second) on its own thread, right after
  // it found the runtime; until then nothing is named. Methods of the same code (the linker folds identical
  // functions, generic code is shared) share one entry.

  let table = null;
  let tableScheduled = false;

  function buildTable() {
    const entries = new Map();
    const implFlags = Memory.alloc(4);
    for (const found of images()) {
      eachClass(found.image, (_klass, methods) => {
        for (const method of methods) {
          const start = method.readPointer();
          if (start.isNull() || (api.methodFlags(method, implFlags) & METHOD_ABSTRACT))
            continue;
          const offset = start.sub(module.base).toUInt32();
          const entry = entries.get(offset);
          if (entry === undefined)
            entries.set(offset, { method, shared: 0 });
          else
            entry.shared++;
        }
      });
    }
    const starts = Uint32Array.from(entries.keys()).sort();
    table = { starts, entries };
    return table;
  }

  function codeTable() {
    // On the agent's thread only (it reads IL2CPP's metadata).
    if (table === null && refresh())
      buildTable();
    return table;
  }

  function scheduleTable() {
    // From a game thread: build the table on the agent's thread soon.
    if (tableScheduled || api === null)
      return;
    tableScheduled = true;
    setTimeout(() => {
      try {
        codeTable();
      } catch (_error) {
        tableScheduled = false;
      }
    }, 0);
  }

  function before(starts, offset) {
    // Index of the last start <= offset, or -1
    let low = 0;
    let high = starts.length - 1;
    let found = -1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      if (starts[middle] <= offset) {
        found = middle;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    return found;
  }

  function functionEntry(address) {
    // { begin, end } module offsets of the function holding an address, as
    // the unwind data has it, or null (a leaf function, or no unwind data)
    if (api.functionEntry !== null) {
      const imageBase = Memory.alloc(Process.pointerSize);
      const entry = api.functionEntry(address, imageBase, NULL);
      if (entry.isNull() || !imageBase.readPointer().equals(module.base))
        return null;
      return { begin: entry.readU32(), end: entry.add(4).readU32() };
    }
    const frames = frameTable();
    return frames === null ? null : frames.lookup(address.sub(module.base).toUInt32());
  }

  // ELF: .eh_frame_hdr holds a sorted table of every function's start and
  // its FDE, which gives its length. Only the encodings GCC and Clang write
  // on x86-64 are read (otherwise there is no table, and names are a guess).
  const PT_LOAD = 1;
  const PT_GNU_EH_FRAME = 0x6474e550;
  const PE_UDATA4 = 0x03;
  const PE_SDATA4 = 0x0b;
  const PE_DATAREL_SDATA4 = 0x3b;
  let frames;

  function frameTable() {
    if (frames === undefined) {
      try {
        frames = readFrameTable();
      } catch (_error) {
        frames = null;
      }
    }
    return frames;
  }

  function readFrameTable() {
    const base = module.base;
    if (base.readU32() !== 0x464c457f)  // \x7fELF
      return null;
    const phoff = base.add(0x20).readU64().toNumber();
    const phentsize = base.add(0x36).readU16();
    let lowest = null;
    let header = null;
    for (let i = 0, n = base.add(0x38).readU16(); i < n; i++) {
      const ph = base.add(phoff + i * phentsize);
      const type = ph.readU32();
      const vaddr = ph.add(0x10).readU64().toNumber();
      if (type === PT_LOAD && (lowest === null || vaddr < lowest))
        lowest = vaddr;
      else if (type === PT_GNU_EH_FRAME)
        header = vaddr;
    }
    if (header === null || lowest === null)
      return null;
    const hdr = base.add(header - (lowest - (lowest % 0x1000)));
    const pointerEncoding = hdr.add(1).readU8() & 0x0f;
    if (hdr.readU8() !== 1 || (pointerEncoding !== PE_SDATA4 && pointerEncoding !== PE_UDATA4)
        || hdr.add(2).readU8() !== PE_UDATA4 || hdr.add(3).readU8() !== PE_DATAREL_SDATA4)
      return null;
    const count = hdr.add(8).readU32();
    const view = new DataView(hdr.add(12).readByteArray(count * 8));
    // Table entries are relative to the header; offsets in the module here.
    const delta = Number(hdr.sub(base).toString());
    const startAt = i => view.getInt32(i * 8, true) + delta;
    return {
      lookup(offset) {
        let low = 0;
        let high = count - 1;
        let found = -1;
        while (low <= high) {
          const middle = (low + high) >> 1;
          if (startAt(middle) <= offset) {
            found = middle;
            low = middle + 1;
          } else {
            high = middle - 1;
          }
        }
        if (found < 0)
          return null;
        const begin = startAt(found);
        // FDE: length, CIE pointer, pc_begin (pc-relative), pc_range
        const fde = hdr.add(view.getInt32(found * 8 + 4, true));
        const pcBegin = fde.add(8).add(fde.add(8).readS32()).sub(base).toUInt32();
        if (fde.readU32() === 0xffffffff || pcBegin !== begin)
          return null;
        const end = begin + fde.add(12).readU32();
        return offset < end ? { begin, end } : null;
      }
    };
  }

  function find(address, t) {
    // { method, start, size, shared } of the method whose code holds the
    // address, or null
    if (address.compare(module.base) < 0 || address.compare(module.base.add(module.size)) >= 0)
      return null;
    const offset = address.sub(module.base).toUInt32();
    const known = entry => ({ ...entry, start: module.base.add(entry.begin) });
    const unwind = functionEntry(address);
    if (unwind !== null) {
      const entry = t.entries.get(unwind.begin);
      return entry === undefined ? null
        : known({ ...entry, begin: unwind.begin, size: unwind.end - unwind.begin });
    }
    const index = before(t.starts, offset);
    if (index < 0)
      return null;
    const begin = t.starts[index];
    const span = api.functionEntry === null && frameTable() === null ? MAX_SPAN : LEAF_SPAN;
    if (offset - begin >= span)
      return null;
    const next = index + 1 < t.starts.length ? t.starts[index + 1] : module.size;
    return known({ ...t.entries.get(begin), begin, size: Math.min(next - begin, span) });
  }

  const MAX_CACHE = 4096;
  const locations = new Map();

  function locate(address) {
    // Safe on any thread: it only reads the table, which the agent's thread
    // builds (asked for here if it is missing).
    if (api === null)
      return undefined;
    if (table === null) {
      scheduleTable();
      return undefined;
    }
    const key = address.toString();
    let location = locations.get(key);
    if (location === undefined) {
      if (locations.size >= MAX_CACHE)
        locations.clear();
      const found = find(address, table);
      location = found === null ? null : { ...found, offset: address.sub(found.start) };
      locations.set(key, location);
    }
    return location;
  }

  function lookup(address) {
    // locate on the agent's thread, building the table if need be
    if (codeTable() === null)
      return null;
    return locate(address);
  }

  function nameOf(location) {
    const shared = location.shared > 0 ? ` [shared with ${location.shared}]` : '';
    return `${methodLabel(location.method)}+${location.offset}${shared}`;
  }

  function name(address) {
    const location = table === null ? null : locate(address);
    return location ? nameOf(location) : null;
  }

  function identify(address) {
    const location = table === null ? null : locate(address);
    if (!location)
      return null;
    const file = text(api.imageName(api.classImage(api.methodClass(location.method))));
    return {
      engine: 'il2cpp',
      image: file.replace(/\.dll$/i, ''),
      file,
      token: api.methodToken(location.method),
      method: signatureName(location.method),
      offset: location.offset.toInt32()
    };
  }

  // --- Catalog -----------------------------------------------------------------

  function moduleBuild() {
    // What changes with every build of GameAssembly: the PE time stamp (or,
    // elsewhere, a hash of the module's first page) and its size.
    let stamp;
    if (Process.platform === 'windows') {
      stamp = module.base.add(module.base.add(0x3c).readU32()).add(8).readU32().toString(16);
    } else {
      let hash = 0x811c9dc5;
      for (const byte of new Uint8Array(module.base.readByteArray(0x1000)))
        hash = Math.imul(hash ^ byte, 0x01000193) >>> 0;
      stamp = hash.toString(16);
    }
    return `${stamp}-${module.size.toString(16)}`;
  }

  function assemblies() {
    requireRuntime();
    const build = moduleBuild();
    return images().map(found => ({
      name: found.name,
      file: found.file,
      classes: api.classCount(found.image).toNumber(),
      mvid: `${build}-${found.name}`
    }));
  }

  function classes(assembly) {
    // [[class name, [method entry, ...]], ...] as Mono's catalog
    requireRuntime();
    const found = imageNamed(assembly);
    const implFlags = Memory.alloc(4);
    const result = [];
    eachClass(found.image, (klass, methods) => {
      result.push([classNameOf(klass)[0], methods.map(method => methodEntry(method, implFlags))]);
    });
    return result;
  }

  function methodsByToken(assembly, tokens) {
    // [{ className, method, entry, flags, implFlags }] for the trace
    requireRuntime();
    const found = imageNamed(assembly);
    const implFlags = Memory.alloc(4);
    const result = [];
    for (const token of tokens) {
      const method = methodByToken(found, token);
      if (method === null)
        continue;
      const entry = methodEntry(method, implFlags);
      result.push({ className: classNameOf(api.methodClass(method))[0], method,
                    entry: entry.slice(0, 6), flags: entry[6], implFlags: entry[7] });
    }
    return result;
  }

  function codeOf(method) {
    // { start, size } of a method's code, or null: ahead-of-time compiled,
    // nothing to compile.
    const start = codeStart(method);
    if (start === null)
      return null;
    const t = codeTable();
    const found = t === null ? null : find(start, t);
    return { start, size: found === null ? 1 : found.size };
  }

  function resolve(identity) {
    requireRuntime();
    const method = methodByToken(imageNamed(identity.image), identity.token);
    if (method === null || (identity.method && signatureName(method) !== identity.method))
      throw hermesError('error.il2cpp.method_changed', { method: identity.method });
    return method;
  }

  async function compile(identity) {
    const code = codeOf(resolve(identity));
    if (code === null)
      throw hermesError('error.il2cpp.no_code', { method: identity.method });
    return code;
  }


  // --- Threads IL2CPP knows ------------------------------------------------------
  // Setting a class up may allocate from the garbage collector, which only
  // threads IL2CPP knows may do. Such work runs on a short-lived thread of
  // its own that attaches and detaches again (as with Mono): the agent's
  // thread is not attached, so no garbage collection waits for it.

  const RUNTIME_THREAD_POLL = 5;
  const RUNTIME_THREAD_LEAVE = 200;

  function objectsApi() {
    requireRuntime();
    if (api.objects === null)
      throw hermesError('error.il2cpp.no_objects');
    return api.objects;
  }

  function onRuntimeThread(work) {
    // A promise of work()'s result, computed on an attached thread.
    let o;
    try {
      o = objectsApi();
    } catch (error) {
      return Promise.reject(error);
    }
    const state = { done: false, result: undefined, failure: null };
    const body = new NativeCallback(() => {
      try {
        const thread = o.threadAttach(api.domain());
        try {
          state.result = work();
        } finally {
          o.threadDetach(thread);
        }
      } catch (error) {
        state.failure = error;
      }
      state.done = true;
      return NULL;
    }, 'pointer', ['pointer']);
    try {
      spawnThread(body, NULL);
    } catch (error) {
      return Promise.reject(error);
    }
    Script.pin();
    return new Promise((resolve, reject) => {
      const poll = () => {
        if (!state.done) {
          setTimeout(poll, RUNTIME_THREAD_POLL);
          return;
        }
        setTimeout(() => {
          body.toString();
          Script.unpin();
        }, RUNTIME_THREAD_LEAVE);
        if (state.failure !== null)
          reject(state.failure);
        else
          resolve(state.result);
      };
      poll();
    });
  }

  // --- Signatures --------------------------------------------------------------
  // IL2CPP's type enum is ECMA-335's element type. A method is a C function
  // (this, arguments..., MethodInfo*); a struct it returns through a hidden
  // pointer comes before ``this`` (returnPointerFirst).

  const TYPE = {
    VOID: 0x01, I4: 0x08, VALUETYPE: 0x11, GENERICINST: 0x15
  };

  function typeInfo(type) {
    // { kind, byref, name, enumName?, structSize?, klass? }, as Mono's
    const o = api.objects;
    const info = {
      kind: o.typeKind(type),
      byref: o.isByref(type),
      name: ownedText(api.typeName(type)) || '?'
    };
    if (info.kind === TYPE.VALUETYPE || info.kind === TYPE.GENERICINST) {
      const klass = o.typeClass(type);
      if (!klass.isNull() && o.isEnum(klass)) {
        info.enumName = info.name;
        info.kind = o.typeKind(o.enumBase(klass));
      } else if (!klass.isNull() && o.isValueType(klass)) {
        info.kind = TYPE.VALUETYPE;
        info.structSize = o.valueSize(klass, NULL);
        info.klass = klass;
      }
    }
    return info;
  }

  const signatures = new Map();

  function signatureOf(method) {
    // { instance, params: [{ name, type }], returns: type, returnPointerFirst }
    const key = method.toString();
    let result = signatures.get(key);
    if (result !== undefined)
      return result;
    const o = objectsApi();
    const names = parameterNames(method);
    const params = [];
    for (let i = 0; i < names.length; i++)
      params.push({ name: names[i], type: typeInfo(api.param(method, i)) });
    result = {
      params,
      instance: o.isInstance(method),
      returns: typeInfo(api.returnType(method)),
      returnPointerFirst: true
    };
    signatures.set(key, result);
    return result;
  }

  function prepare(identity) {
    // { start, size, instance, params, returns, returnPointerFirst }
    objectsApi();
    return onRuntimeThread(() => {
      const method = resolve(identity);
      const code = codeOf(method);
      if (code === null)
        throw hermesError('error.il2cpp.no_code', { method: identity.method });
      return { ...code, ...signatureOf(method) };
    });
  }

  // --- Objects -----------------------------------------------------------------
  // Il2CppObject: the class, a monitor, then the fields; strings and arrays
  // are laid out as Mono's. Il2CppClass starts with its image.

  const FIELD_STATIC = 0x10;
  const FIELD_LITERAL = 0x40;
  const THREAD_STATIC = 0xffffffff;
  const layouts = new Map();
  let imageSet = new Set();

  function isImage(image) {
    if (imageSet.has(image.toString()))
      return true;
    imageSet = new Set(images().map(found => found.image.toString()));
    return imageSet.has(image.toString());
  }

  function classOf(object) {
    // The class of a managed object, or null if ``object`` is none (any
    // more): only plain memory reads until its class proves real.
    objectsApi();
    try {
      const klass = object.readPointer();
      return isImage(klass.readPointer()) ? klass : null;
    } catch (_error) {
      return null;
    }
  }

  function layout(klass) {
    // { name, valueType, rank, fields: [{ name, type, offset, static, owner?,
    // storage? }] } with inherited fields first, as Mono's (cached)
    const key = klass.toString();
    let result = layouts.get(key);
    if (result !== undefined)
      return result;
    const o = objectsApi();
    const chain = [];
    for (let current = klass; !current.isNull(); current = o.classParent(current))
      chain.unshift(current);
    const fields = [];
    const iterator = Memory.alloc(Process.pointerSize);
    for (const current of chain) {
      iterator.writePointer(NULL);
      for (;;) {
        const field = o.classFields(current, iterator);
        if (field.isNull())
          break;
        const flags = o.fieldFlags(field);
        const offset = o.fieldOffset(field).toNumber();
        const entry = {
          name: text(o.fieldName(field)),
          type: typeInfo(o.fieldType(field)),
          offset,
          static: (flags & FIELD_STATIC) !== 0
        };
        if (entry.static) {
          entry.owner = current;
          entry.storage = (flags & FIELD_LITERAL) !== 0 ? 'const'
            : (offset >>> 0) === THREAD_STATIC ? 'thread' : 'data';
        }
        fields.push(entry);
      }
    }
    result = {
      name: ownedText(api.typeName(api.classType(klass))) || '?',
      valueType: o.isValueType(klass),
      rank: o.classRank(klass),
      fields
    };
    if (result.rank > 0) {
      result.element = typeInfo(api.classType(o.elementClass(klass)));
      result.elementSize = o.elementSize(klass);
    }
    layouts.set(key, result);
    return result;
  }

  // Il2CppClass has no exported getter for its static data. Its field
  // ``klass`` points at the class itself (to look like Mono's vtable); the
  // fields array follows it and static_fields seven pointers later, in
  // every IL2CPP since Unity 2018. ``klass`` is the last pointer to the
  // class among its first fields (element_class and castClass come earlier).
  const CLASS_SCAN = 0x100;
  let staticFieldsOffset;

  function findStaticFieldsOffset(klass) {
    const o = api.objects;
    const iterator = Memory.alloc(Process.pointerSize);
    iterator.writePointer(NULL);
    const firstField = o.classFields(klass, iterator);
    if (firstField.isNull())
      return undefined;  // ask another class
    let self = -1;
    for (let at = 0; at < CLASS_SCAN; at += Process.pointerSize)
      if (klass.add(at).readPointer().equals(klass))
        self = at;
    if (self < 0 || !klass.add(self + Process.pointerSize).readPointer().equals(firstField))
      return null;
    return self + 8 * Process.pointerSize;
  }

  function staticData(klass) {
    // Where the static fields of ``klass`` are, or null: before IL2CPP set
    // the class up (it allocates them then) or where the layout is unknown.
    if (!refresh() || api.objects === null)
      return null;
    if (staticFieldsOffset === undefined)
      staticFieldsOffset = findStaticFieldsOffset(klass);
    if (staticFieldsOffset === undefined || staticFieldsOffset === null)
      return null;
    const data = klass.add(staticFieldsOffset).readPointer();
    return data.isNull() ? null : data;
  }

  // --- Unity's main thread ------------------------------------------------------
  // As with Mono: Unity starts script methods on its main thread through
  // il2cpp_runtime_invoke (thousands of times a second); queued work runs
  // there first. Job threads call it too, and are left alone.

  const MAIN_TIMEOUT = 3000;
  const mainQueue = [];
  let mainHook = null;
  let draining = false;

  function isMainThread() {
    return Process.getCurrentThreadId() === mainThreadId();
  }

  function drainMain() {
    if (draining || !isMainThread())
      return;
    draining = true;
    try {
      while (mainQueue.length > 0) {
        const job = mainQueue.shift();
        clearTimeout(job.timer);
        try {
          job.resolve(job.work());
        } catch (error) {
          job.reject(error);
        }
      }
    } finally {
      draining = false;
    }
    if (mainHook !== null) {
      const hook = mainHook;
      mainHook = null;
      setTimeout(() => hook.detach(), 0);
    }
  }

  function onMainThread(work) {
    let o;
    try {
      o = objectsApi();
    } catch (error) {
      return Promise.reject(error);
    }
    mainThreadId();
    return new Promise((resolve, reject) => {
      const job = { work, resolve, reject };
      job.timer = setTimeout(() => {
        const index = mainQueue.indexOf(job);
        if (index >= 0)
          mainQueue.splice(index, 1);
        reject(hermesError('error.il2cpp.main_thread_timeout'));
      }, MAIN_TIMEOUT);
      mainQueue.push(job);
      if (mainHook === null)
        mainHook = Interceptor.attach(o.runtimeInvokeAddress, {
          onEnter() {
            drainMain();
          }
        });
    });
  }

  function invoke(method, object, params) {
    // { result, exception } of calling a method; on the main thread
    const exception = Memory.alloc(Process.pointerSize);
    exception.writePointer(NULL);
    const result = api.objects.runtimeInvoke(method, object, params, exception);
    return { result, exception: exception.readPointer() };
  }

  // --- Properties: parameterless getters ---------------------------------------

  const propertyLists = new Map();

  function classChain(klass) {
    const chain = [];
    for (let current = klass; !current.isNull(); current = api.objects.classParent(current))
      chain.push(current);
    return chain;  // the class first, System.Object last
  }

  function properties(klass) {
    // [{ name, method, static, type, setter }] of the getters of ``klass``
    // and its base classes, as Mono's (cached)
    const key = klass.toString();
    let result = propertyLists.get(key);
    if (result !== undefined)
      return result;
    objectsApi();
    result = [];
    const seen = new Set();
    const setters = new Map();
    for (const current of classChain(klass)) {
      eachMethodOf(current, method => {
        const name = text(api.methodName(method));
        const count = api.paramCount(method);
        if (name.startsWith('set_')) {
          if (!setters.has(name) && count === 1)
            setters.set(name, method.toString());
          return;
        }
        if (!name.startsWith('get_') || count !== 0 || seen.has(name))
          return;
        seen.add(name);
        result.push({
          name: name.substring(4),
          method: method.toString(),
          static: !api.objects.isInstance(method),
          type: typeInfo(api.returnType(method))
        });
      });
    }
    for (const property of result)
      property.setter = setters.get(`set_${property.name}`) || null;
    propertyLists.set(key, result);
    return result;
  }

  function eachMethodOf(klass, visit) {
    const iterator = Memory.alloc(Process.pointerSize);
    iterator.writePointer(NULL);
    for (let method = api.classMethods(klass, iterator); !method.isNull();
         method = api.classMethods(klass, iterator))
      visit(method);
  }

  function getProperty(object, property, isStruct) {
    // Call a getter of ``object``; on the main thread. Getters of a struct
    // inside an object are not called: IL2CPP wants a boxed one.
    if (isStruct)
      throw hermesError('error.il2cpp.struct_property', { name: property.name });
    let method = ptr(property.method);
    if (!property.static)
      method = api.objects.virtualMethod(object, method);  // the override
    return invoke(method, property.static ? NULL : object, NULL);
  }

  // --- Unity object names ----------------------------------------------------------
  // UnityEngine.Object.get_name, on the main thread only: Unity refuses it
  // elsewhere, and other threads running game code need not be attached.

  const MAX_NAME = 120;
  const nameMethods = new Map();
  let naming = false;

  function nameMethod(klass) {
    const key = klass.toString();
    let method = nameMethods.get(key);
    if (method !== undefined)
      return method;
    method = null;
    for (const current of classChain(klass)) {
      if (layout(current).name === 'UnityEngine.Object') {
        const found = api.objects.methodFromName(current, Memory.allocUtf8String('get_name'), 0);
        method = found.isNull() ? null : found;
        break;
      }
    }
    nameMethods.set(key, method);
    return method;
  }

  function unityName(object) {
    if (api === null || api.objects === null || naming || !isMainThread())
      return null;
    const klass = classOf(object);
    const method = klass === null ? null : nameMethod(klass);
    if (method === null)
      return null;
    naming = true;  // get_name could run a monitored method
    let called;
    try {
      called = invoke(method, object, NULL);
    } finally {
      naming = false;
    }
    if (!called.exception.isNull() || called.result.isNull())
      return null;
    const string = called.result;
    const length = string.add(2 * Process.pointerSize).readS32();
    return string.add(2 * Process.pointerSize + 4).readUtf16String(Math.min(length, MAX_NAME));
  }

  return {
    id: 'il2cpp',
    refresh,
    locate,
    name,
    // On game threads too (exception handlers): no table yet, no name yet.
    describe: address => {
      const location = locate(address);
      return location ? nameOf(location) : null;
    },
    identify,
    compile,
    resolve,
    assemblies,
    classes,
    methodsByToken,
    // Ahead-of-time code: nothing hangs, nothing to compile or undo.
    checkAll: methods => methods.map(() => false),
    compileAll: methods => methods.map(codeOf),
    describeAll: addresses => addresses.map(address => {
      const location = lookup(address);
      return location ? nameOf(location) : null;
    }),
    cancelBatches: () => 0,
    batchesRunning: () => false,
    releaseCode() {},
    // The method monitor and the object inspector
    prepare,
    signatureOf,
    classOf,
    layout,
    staticData,
    properties,
    getProperty,
    invoke,
    classChain,
    unityName,
    onRuntimeThread,
    onMainThread,
    drainMain,
    objectClassName: object => {
      const klass = classOf(object);
      return klass === null ? null : layout(klass).name;
    },
    virtualMethod: (object, method) => api.objects.virtualMethod(object, method)
  };
})();

registerEngine(il2cpp);
