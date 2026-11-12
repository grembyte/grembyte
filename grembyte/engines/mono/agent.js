const mono = (() => {
  // The runtime library: libmono*.so natively, mono*.dll under Wine.
  const RUNTIME_NAME = /^libmono|^mono(-2\.0-(bdwgc|sgen))?\.dll$/i;
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
        throw grembyteError('error.mono.no_export', { module: module.name, name });
      return found;
    };
    // Exclusive: the calls are short, and some run in exception handlers.
    const exported = (name, ret, args) =>
      new NativeFunction(address(name), ret, args, { scheduling: 'exclusive' });
    // Cooperative: these may take Mono's locks or run a garbage collection,
    // so they must not hold the agent's lock that game threads wait for.
    const blocking = (name, ret, args) => new NativeFunction(address(name), ret, args);
    // Game code: its exceptions go to the process's handlers, where Mono turns
    // a null access into a NullReferenceException and a write watch takes its
    // trap (Frida's default would abort the call with "breakpoint triggered")
    const gameCode = (name, ret, args) =>
      new NativeFunction(address(name), ret, args, { exceptions: 'propagate' });
    const free = module.findExportByName('mono_free');
    return {
      module,
      // Not mono_domain_foreach: old Mono (Unity's libmono.so) allocates GC
      // memory in it, which crashes on a thread the GC does not know.
      domainById: blocking('mono_domain_get_by_id', 'pointer', ['int']),
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
        objectClass: exported('mono_object_get_class', 'pointer', ['pointer']),
        classFields: exported('mono_class_get_fields', 'pointer', ['pointer', 'pointer']),
        fieldName: exported('mono_field_get_name', 'pointer', ['pointer']),
        fieldType: exported('mono_field_get_type', 'pointer', ['pointer']),
        fieldOffset: exported('mono_field_get_offset', 'uint32', ['pointer']),
        fieldFlags: exported('mono_field_get_flags', 'uint32', ['pointer']),
        classParent: exported('mono_class_get_parent', 'pointer', ['pointer']),
        classRank: exported('mono_class_get_rank', 'int', ['pointer']),
        elementClass: exported('mono_class_get_element_class', 'pointer', ['pointer']),
        elementSize: exported('mono_array_element_size', 'int', ['pointer'])
      })),
      // Only Unity object names need these. Cooperative: they run game code.
      invoke: optional(() => ({
        methodFromName: exported('mono_class_get_method_from_name', 'pointer',
          ['pointer', 'pointer', 'int']),
        runtimeInvoke: gameCode('mono_runtime_invoke', 'pointer',
          ['pointer', 'pointer', 'pointer', 'pointer']),
        // Unity runs its script methods through it: a way onto its main thread.
        runtimeInvokeAddress: address('mono_runtime_invoke'),
        virtualMethod: exported('mono_object_get_virtual_method', 'pointer',
          ['pointer', 'pointer']),
        typeObject: blocking('mono_type_get_object', 'pointer', ['pointer', 'pointer']),
        classFromName: exported('mono_class_from_name', 'pointer',
          ['pointer', 'pointer', 'pointer']),
        classMethods: exported('mono_class_get_methods', 'pointer', ['pointer', 'pointer']),
        methodName: exported('mono_method_get_name', 'pointer', ['pointer']),
        methodFlags: exported('mono_method_get_flags', 'uint32', ['pointer', 'pointer'])
      })),
      // Only for methods of generic classes (List`1<int>); see resolveMethod.
      // Not mono_class_bind_generic_parameters: Unity's MonoBleedingEdge does
      // not export it. Binding goes through a reflection name instead, like
      // Type.GetType("List`1[[System.Int32, mscorlib]]").
      generics: optional(() => ({
        corlib: exported('mono_get_corlib', 'pointer', []),
        classFromName: blocking('mono_class_from_name', 'pointer',
          ['pointer', 'pointer', 'pointer']),
        typeFromName: blocking('mono_reflection_type_from_name', 'pointer',
          ['pointer', 'pointer']),
        typeClass: exported('mono_class_from_mono_type', 'pointer', ['pointer']),
        nestedTypes: blocking('mono_class_get_nested_types', 'pointer', ['pointer', 'pointer']),
        className: exported('mono_class_get_name', 'pointer', ['pointer']),
        classMethods: blocking('mono_class_get_methods', 'pointer', ['pointer', 'pointer'])
      })),
      // Only the object inspector needs these: where a class keeps its static
      // fields. Creating a class's vtable allocates; never runs its .cctor.
      statics: optional(() => ({
        classVtable: blocking('mono_class_vtable', 'pointer', ['pointer', 'pointer']),
        fieldData: exported('mono_vtable_get_static_field_data', 'pointer', ['pointer'])
      })),
      // The value of a constant (an enum's names), from the metadata.
      constant: optional(() => exported('mono_field_get_data', 'pointer', ['pointer'])),
      // The size of a class's static data, for the pointer scan's chain bases.
      staticSize: optional(() => exported('mono_class_data_size', 'int', ['pointer'])),
      // Only for passing strings to game code.
      stringNew: optional(() => blocking('mono_string_new', 'pointer', ['pointer', 'pointer'])),
      // Storing a reference in an object tells the garbage collector.
      writeBarrier: optional(() => exported('mono_gc_wbarrier_set_field', 'void',
        ['pointer', 'pointer', 'pointer'])),
      // The module version id the catalog cache keys an assembly by.
      imageGuid: optional(() => exported('mono_image_get_guid', 'pointer', ['pointer'])),
      // Only the call graph needs these: the IL code of a method.
      il: optional(() => ({
        header: blocking('mono_method_get_header', 'pointer', ['pointer']),
        code: exported('mono_method_header_get_code', 'pointer',
          ['pointer', 'pointer', 'pointer']),
        free: exported('mono_metadata_free_mh', 'void', ['pointer'])
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
      if (!RUNTIME_NAME.test(module.name)
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
      engine: 'mono',
      image: api.imageName(image).readUtf8String(),
      file: api.imageFile(image).readUtf8String(),
      token: api.methodToken(location.method),
      method: signatureName(location.method),
      offset: location.offset.toInt32()
    };
  }

  const DOMAIN_GAP = 16;
  const MAX_DOMAIN_ID = 4096;

  function scriptDomain() {
    // Unity runs its scripts in a child domain created after the root one.
    // Ids count up; unloaded domains leave gaps, so stop after a run of them.
    const root = api.rootDomain();
    let last = root;
    for (let id = 0, missing = 0; missing < DOMAIN_GAP && id < MAX_DOMAIN_ID; id++) {
      const domain = api.domainById(id);
      if (domain.isNull()) {
        missing++;
        continue;
      }
      missing = 0;
      if (!domain.equals(root))
        last = domain;
    }
    return last;
  }

  // How often the agent's thread looks whether a Mono thread is done, and
  // how long a finished one may still need to leave our callback.
  const MONO_THREAD_POLL = 5;
  const MONO_THREAD_LEAVE = 200;

  function onMonoThread(work) {
    // A promise of work(domain), run on a thread of our own: Mono compiles
    // and loads classes only for threads it knows. The thread attaches and
    // ends afterwards, so Mono forgets it (see above).
    //
    // The agent's thread must not wait for it (pthread_join): Frida may need
    // that thread for the JavaScript the other one runs, and both would wait
    // forever, blocking every script of the agent. It keeps running its
    // event loop instead and looks now and then whether the work is done.
    const state = { done: false, result: undefined, failure: null };
    const body = new NativeCallback(() => {
      try {
        const domain = scriptDomain();
        const thread = api.threadAttach(domain);
        try {
          state.result = work(domain);
        } finally {
          api.threadDetach(thread);
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
    // Cutting the session must not unload the code the thread runs.
    Script.pin();
    return new Promise((resolve, reject) => {
      const poll = () => {
        if (!state.done) {
          setTimeout(poll, MONO_THREAD_POLL);
          return;
        }
        // The thread is still returning through ``body``: keep it a moment.
        setTimeout(() => {
          body.toString();
          Script.unpin();
        }, MONO_THREAD_LEAVE);
        if (state.failure !== null)
          reject(state.failure);
        else
          resolve(state.result);
      };
      poll();
    });
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
      throw grembyteError('error.mono.assembly_not_loaded_yet', { assembly: identity.image });
    return image;
  }

  // mono_method_get_flags: METHOD_ATTRIBUTE_ABSTRACT, _PINVOKE_IMPL and the
  // implementation flags of code that is not IL (native, runtime, internal call)
  const METHOD_ABSTRACT = 0x0400;
  const METHOD_PINVOKE = 0x2000;
  const IMPL_NOT_IL = 0x0003 | 0x1000;

  function codeless(method, name) {
    // The error why a method has no code Mono could compile, or null.
    const flagsOf = (api.catalog || api.invoke || {}).methodFlags;
    if (flagsOf === undefined)
      return null;
    const implFlags = Memory.alloc(4);
    const flags = flagsOf(method, implFlags);
    if (flags & METHOD_ABSTRACT)
      return grembyteError('error.mono.abstract', { method: name });
    if ((flags & METHOD_PINVOKE) || (implFlags.readU32() & IMPL_NOT_IL))
      return grembyteError('error.mono.native', { method: name });
    return null;
  }

  function compileOnThread(domain, identity) {
    const method = resolveMethod(identity);
    const reason = codeless(method, identity.method);
    if (reason !== null)
      throw reason;
    const code = codeOf(domain, method);
    if (code === null) {
      const head = splitSignature(identity.method);
      if (head !== null && /`\d+$/.test(head.head))
        throw grembyteError('error.mono.generic_definition', { method: identity.method });
      throw grembyteError('error.mono.not_compiled', { method: identity.method });
    }
    return { method, ...code };
  }

  // --- Generic classes: the methods of List`1<int> ------------------------
  // The token of a method names its definition ('R3.ReactiveProperty`1:set_Value
  // (T)'), which has no code. Mono names the methods of an instance like
  // 'R3.ReactiveProperty`1<int>:set_Value (int)' (e.g. a write watch's hit);
  // their class is bound to the type arguments of that name. Identities made
  // from a catalog entry keep the definition's parameters: '...`1<int>:set_Value (T)'.
  // Code that all reference types share names its type arguments 'T_REF'
  // ('Ns.PhaseVM`1<TItem_REF>:OnPointsChanged', e.g. a caller). Any class
  // bound to reference types there runs that code: one the game derives from
  // (PhaseVM`1<Ns.ItemVM>, which meets the constraints of T), else object.

  const SHARED_REF = /^[A-Za-z_][\w`]*_REF$/;

  function sharedRef(parsed) {
    // Whether a parsed type argument is the placeholder of shared code.
    return parsed.args.length === 0 && SHARED_REF.test(parsed.name);
  }

  function sharedParams(wanted, bound) {
    // Whether parameters with 'T_REF' match those of a bound class.
    const pattern = wanted.split(/([A-Za-z_][\w`]*_REF(?![\w`]))/).map((part, i) =>
      (i % 2 === 1 ? '.+?' : part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))).join('');
    return new RegExp(`^${pattern}$`).test(bound);
  }

  function resolveMethod(identity) {
    // The method of an identity (on a thread Mono knows).
    const notFound = () => grembyteError('error.mono.method_not_found',
      { method: identity.method, assembly: identity.image });
    const definition = api.getMethod(findImage(identity), identity.token, NULL);
    if (definition.isNull())
      throw notFound();
    const signature = signatureName(definition);
    if (signature === identity.method)
      return definition;
    const wanted = splitSignature(identity.method);
    const found = splitSignature(signature || '');
    if (wanted === null || found === null || wanted.name !== found.name)
      throw notFound();
    const head = parseType(wanted.head);
    if (head.args.length === 0 || head.name !== parseType(found.head).name)
      throw notFound();
    const method = instanceMethod(definition, head, identity.token);
    const bound = (splitSignature(signatureName(method) || '') || {}).params;
    if (wanted.params !== found.params && wanted.params !== bound
        && !sharedParams(wanted.params, bound || ''))
      throw notFound();
    return method;
  }

  function splitSignature(signature) {
    // { head, name, params } of 'Class:Method (params)', or null.
    let depth = 0;
    for (let i = 0; i < signature.length; i++) {
      const c = signature[i];
      if (c === '<')
        depth++;
      else if (c === '>')
        depth--;
      else if (c === ':' && depth === 0) {
        const rest = signature.substring(i + 1);
        const open = rest.indexOf(' (');
        return {
          head: signature.substring(0, i),
          name: open < 0 ? rest : rest.substring(0, open),
          params: open < 0 ? '' : rest.substring(open + 1)
        };
      }
    }
    return null;
  }

  function parseType(text) {
    // A type as Mono describes it: 'Ns.Map`2<int, Ns.Item[]>' is
    // { name: 'Ns.Map`2', args: [{ name: 'int', ... }, { ..., suffix: '[]' }], suffix: '' }.
    let at = 0;
    const fail = () => grembyteError('error.mono.unreadable_type', { type: text });
    const suffix = () => {
      const start = at;
      for (;;) {
        if (text[at] === '[') {
          const end = text.indexOf(']', at);
          if (end < 0)
            throw fail();
          at = end + 1;
        } else if (text[at] === '&' || text[at] === '*') {
          at++;
        } else {
          return text.substring(start, at);
        }
      }
    };
    const type = () => {
      const start = at;
      while (at < text.length && !',>[]&* '.includes(text[at])) {
        if (text[at] !== '<') {
          at++;
          continue;
        }
        const name = text.substring(start, at);
        // Type arguments of a generic class, also for a class nested in one
        // (Outer`1/Inner<int>); compiler-generated names start with '<'.
        if (/`\d+/.test(name) && at > start && !'./'.includes(text[at - 1])) {
          const args = [];
          do {
            at++;
            while (text[at] === ' ')
              at++;
            args.push(type());
          } while (text[at] === ',');
          if (text[at] !== '>')
            throw fail();
          at++;
          return { name, args, suffix: suffix() };
        }
        // Part of a compiler-generated name: '<>c', '<Start>d__5'
        let depth = 0;
        do {
          if (text[at] === '<')
            depth++;
          else if (text[at] === '>')
            depth--;
          at++;
        } while (depth > 0 && at < text.length);
      }
      if (at === start)
        throw fail();
      return { name: text.substring(start, at), args: [], suffix: suffix() };
    };
    const result = type();
    if (at !== text.length)
      throw fail();
    return result;
  }

  // Mono's short names of the built-in types, and C#'s
  const TYPE_ALIASES = {
    void: 'Void', bool: 'Boolean', char: 'Char', byte: 'Byte', sbyte: 'SByte',
    int16: 'Int16', uint16: 'UInt16', short: 'Int16', ushort: 'UInt16', int: 'Int32',
    uint: 'UInt32', long: 'Int64', ulong: 'UInt64', single: 'Single', float: 'Single',
    double: 'Double', decimal: 'Decimal', string: 'String', object: 'Object',
    intptr: 'IntPtr', uintptr: 'UIntPtr'
  };

  function genericsApi() {
    if (api.generics === null || api.catalog === null)
      throw grembyteError('error.mono.no_generics');
    return api.generics;
  }

  function instanceMethod(definition, head, token) {
    // The method ``token`` of the definition's class bound to head.args.
    const g = genericsApi();
    const image = api.classImage(api.methodClass(definition));
    if (head.args.some(sharedRef)) {
      const shared = sharedInstance(g, image, head, token);
      if (shared !== null)
        return shared;
    }
    const name = reflectionName(g, head);
    const type = g.typeFromName(Memory.allocUtf8String(name), image);
    if (type.isNull())
      throw grembyteError('error.mono.unknown_type', { type: name });
    const method = methodByToken(g, g.typeClass(type), token);
    if (method === null)
      throw grembyteError('error.mono.method_not_in_type',
                        { method: cString(api.fullName(definition, 1)), type: name });
    return method;
  }

  function methodByToken(g, klass, token) {
    const iterator = Memory.alloc(Process.pointerSize);
    iterator.writePointer(NULL);
    for (let method = g.classMethods(klass, iterator); !method.isNull();
      method = g.classMethods(klass, iterator)) {
      if (api.methodToken(method) === token)
        return method;
    }
    return null;
  }

  function sharedInstance(g, image, head, token) {
    // The method ``token`` of a base class of a loaded class that binds
    // head's 'T_REF' arguments to reference types, or null. The assembly of
    // the generic class comes first.
    const c = api.catalog;
    const t = api.types;
    if (t === null)
      return null;
    const images = [image, ...loadedImages(c).filter(other => !other.equals(image))];
    const seen = new Set();
    for (const candidate of images) {
      const rows = c.tableRows(candidate, TYPEDEF_TABLE);
      for (let row = 2; row <= rows; row++) {
        for (let klass = c.classGet(candidate, TYPEDEF_TOKEN | row); !klass.isNull();
          klass = t.classParent(klass)) {
          const key = klass.toString();
          if (seen.has(key))
            break;
          seen.add(key);
          if (bindsShared(g, t, head, cString(c.typeName(c.classType(klass))) || '')) {
            const method = methodByToken(g, klass, token);
            if (method !== null)
              return method;
          }
        }
      }
    }
    return null;
  }

  function bindsShared(g, t, head, name) {
    // Whether a class name binds head's arguments: the same where head names
    // a type, a reference type where it has 'T_REF'.
    if (!name.startsWith(`${head.name}<`))
      return false;
    let parsed;
    try {
      parsed = parseType(name);
    } catch (_error) {
      return false;
    }
    if (parsed.name !== head.name || parsed.args.length !== head.args.length)
      return false;
    return parsed.args.every((arg, i) => (sharedRef(head.args[i])
      ? isReference(g, t, arg) : typeText(arg) === typeText(head.args[i])));
  }

  function isReference(g, t, parsed) {
    if (parsed.suffix.includes('['))
      return true;
    if (parsed.suffix !== '')
      return false;
    const alias = TYPE_ALIASES[parsed.name];
    if (alias !== undefined)
      return alias === 'Object' || alias === 'String';
    const klass = classByName(g, parsed.name);
    return !klass.isNull() && t.isValueType(klass) === 0;
  }

  function typeText(parsed) {
    const args = parsed.args.length === 0 ? '' : `<${parsed.args.map(typeText).join(',')}>`;
    return `${parsed.name}${args}${parsed.suffix}`;
  }

  function reflectionName(g, parsed) {
    // The name of a parsed type as Type.GetType takes it:
    // 'Ns.Map`2<int, Ns.Outer/Item[]>' is
    // 'Ns.Map`2[[System.Int32, mscorlib],[Ns.Outer+Item[], Assembly-CSharp]]'.
    const alias = sharedRef(parsed) ? 'Object' : TYPE_ALIASES[parsed.name];
    let name = alias === undefined ? parsed.name.split('/').join('+') : `System.${alias}`;
    if (parsed.args.length > 0)
      name += '[' + parsed.args.map(arg =>
        `[${reflectionName(g, arg)}, ${assemblyOf(g, arg)}]`).join(',') + ']';
    for (const part of parsed.suffix.match(/\[[^\]]*\]|[&*]/g) || []) {
      if (part[0] !== '[')
        throw grembyteError('error.mono.unsupported_type_argument', { part });
      name += part;
    }
    return name;
  }

  function assemblyOf(g, parsed) {
    // The name of the assembly that defines a parsed type.
    if (sharedRef(parsed))
      return api.imageName(g.corlib()).readUtf8String();
    const klass = TYPE_ALIASES[parsed.name] !== undefined ? NULL : classByName(g, parsed.name);
    if (TYPE_ALIASES[parsed.name] === undefined && klass.isNull())
      throw grembyteError('error.mono.class_not_found', { name: parsed.name });
    const image = klass.isNull() ? g.corlib() : api.classImage(klass);
    return api.imageName(image).readUtf8String();
  }

  function classByName(g, fullName) {
    // A class of a loaded assembly by Mono's name: 'Ns.Outer/Inner'.
    const [outer, ...nested] = fullName.split('/');
    const dot = outer.lastIndexOf('.');
    const space = Memory.allocUtf8String(dot < 0 ? '' : outer.substring(0, dot));
    const name = Memory.allocUtf8String(outer.substring(dot + 1));
    let klass = NULL;
    for (const image of loadedImages(api.catalog)) {
      klass = g.classFromName(image, space, name);
      if (!klass.isNull())
        break;
    }
    for (const inner of nested) {
      if (klass.isNull())
        break;
      const iterator = Memory.alloc(Process.pointerSize);
      iterator.writePointer(NULL);
      let found = NULL;
      for (let candidate = g.nestedTypes(klass, iterator); !candidate.isNull();
        candidate = g.nestedTypes(klass, iterator)) {
        if (g.className(candidate).readUtf8String() === inner) {
          found = candidate;
          break;
        }
      }
      klass = found;
    }
    return klass;
  }

  function codeOf(domain, method) {
    // { start, size } of the code Mono has (or compiles now) for a method,
    // or null; on a thread attached to ``domain``.
    const code = api.compileMethod(method);
    const root = api.rootDomain();
    let info = code.isNull() ? NULL : api.findJitInfo(domain, code);
    if (info.isNull() && !code.isNull() && !root.equals(domain))
      info = api.findJitInfo(root, code);
    if (info.isNull())
      return null;
    return { start: api.codeStart(info), size: api.codeSize(info) };
  }

  // --- Batches: much work on a thread of Mono's own, in C ----------------
  // Running JavaScript on a thread of our own for thousands of methods, with
  // the agent's thread waiting for it (onMonoThread), can deadlock inside
  // Frida, and blocks every script of the agent meanwhile. A batch runs
  // plain C instead, and the agent's thread only looks at its progress.

  const BATCH_COMPILE = 0;
  const BATCH_LOCATE = 1;
  const BATCH_CHECK = 2;
  const BATCH_POLL = 50;
  const BATCH_SOURCE = `
#include <glib.h>

typedef struct {
  gpointer domain;
  gint kind;
  gint count;
  gpointer * input;
  gpointer * first;
  gpointer * second;
  volatile gint done;
  volatile gint cancel;
  volatile gint finished;
} Batch;

extern gpointer mono_thread_attach (gpointer domain);
extern void mono_thread_detach (gpointer thread);
extern gpointer mono_compile_method (gpointer method);
extern gpointer mono_jit_info_table_find (gpointer domain, gpointer address);
extern gpointer mono_get_root_domain (void);
extern gpointer mono_jit_info_get_code_start (gpointer info);
extern gint mono_jit_info_get_code_size (gpointer info);
extern gpointer mono_jit_info_get_method (gpointer info);
extern gpointer mono_method_get_header (gpointer method);
extern const guint8 * mono_method_header_get_code (gpointer header, guint32 * size,
    guint32 * max_stack);
extern void mono_metadata_free_mh (gpointer header);
extern gpointer mono_method_get_class (gpointer method);
extern guint32 mono_class_get_flags (gpointer klass);
extern gpointer mono_class_get_image (gpointer klass);
extern gpointer mono_get_method (gpointer image, guint32 token, gpointer klass);
extern gpointer mono_class_get (gpointer image, guint32 token);

#define TYPE_ATTRIBUTE_IMPORT 0x1000
#define TOKEN_METHOD 1
#define TOKEN_TYPE 2
#define TOKEN_ANY 3

/* The length of the IL instruction at ip (0: cannot tell), and which kind
   of token its operand is (0: none). */
static guint32 instruction (const guint8 * ip, const guint8 * end, gint * kind) {
  guint8 op = ip[0];
  *kind = 0;
  if (op == 0xfe) {
    if (ip + 1 >= end)
      return 0;
    switch (ip[1]) {
      case 0x06: case 0x07:                       /* ldftn, ldvirtftn */
        *kind = TOKEN_METHOD;
        return 6;
      case 0x15: case 0x16: case 0x1c:            /* initobj, constrained., sizeof */
        *kind = TOKEN_TYPE;
        return 6;
      case 0x09: case 0x0a: case 0x0b: case 0x0c: case 0x0d: case 0x0e:
        return 4;
      case 0x12: case 0x19:
        return 3;
      default:
        return 2;
    }
  }
  if ((op >= 0x0e && op <= 0x13) || op == 0x1f || (op >= 0x2b && op <= 0x37) || op == 0xde)
    return 2;
  if (op == 0x20 || op == 0x22 || op == 0x29 || op == 0x72 || (op >= 0x38 && op <= 0x44)
      || (op >= 0x7b && op <= 0x80) || op == 0xdd)
    return 5;
  if (op == 0x21 || op == 0x23)
    return 9;
  if (op == 0x27 || op == 0x28 || op == 0x6f || op == 0x73) {  /* jmp, call, callvirt, newobj */
    *kind = TOKEN_METHOD;
    return 5;
  }
  if (op == 0x70 || op == 0x71 || op == 0x74 || op == 0x75 || op == 0x79 || op == 0x81
      || op == 0x8c || op == 0x8d || op == 0x8f || op == 0xa3 || op == 0xa4 || op == 0xa5
      || op == 0xc2 || op == 0xc6) {
    *kind = TOKEN_TYPE;
    return 5;
  }
  if (op == 0xd0) {                                           /* ldtoken */
    *kind = TOKEN_ANY;
    return 5;
  }
  if (op == 0x45) {                                           /* switch */
    if (ip + 5 > end)
      return 0;
    guint32 count = ip[1] | ip[2] << 8 | ip[3] << 16 | (guint32) ip[4] << 24;
    if (count > (guint32) (end - ip) / 4)
      return 0;
    return 5 + 4 * count;
  }
  return 1;
}

static gboolean is_import (gpointer klass) {
  return klass != NULL && (mono_class_get_flags (klass) & TYPE_ATTRIBUTE_IMPORT) != 0;
}

/* Whether a method uses COM interop ([ComImport] classes or interfaces).
   Compiling such code makes Mono set up COM support, which hangs on Linux. */
static gboolean uses_com (gpointer method) {
  gpointer own = mono_method_get_class (method);
  if (is_import (own))
    return TRUE;
  gpointer header = mono_method_get_header (method);
  if (header == NULL)
    return FALSE;
  guint32 size = 0, max_stack = 0;
  const guint8 * code = mono_method_header_get_code (header, &size, &max_stack);
  const guint8 * end = code + size;
  gpointer image = mono_class_get_image (own);
  gboolean found = FALSE;
  const guint8 * ip = code;
  while (code != NULL && ip < end && !found) {
    gint kind;
    guint32 length = instruction (ip, end, &kind);
    if (length == 0 || length > (guint32) (end - ip))
      break;
    if (kind != 0) {
      const guint8 * operand = ip + length - 4;
      guint32 token = operand[0] | operand[1] << 8 | operand[2] << 16
          | (guint32) operand[3] << 24;
      guint32 table = token >> 24;
      gpointer klass = NULL;
      if (kind != TOKEN_TYPE && (table == 0x06 || table == 0x0a || table == 0x2b)) {
        gpointer callee = mono_get_method (image, token, NULL);
        klass = callee == NULL ? NULL : mono_method_get_class (callee);
      } else if (kind != TOKEN_METHOD && (table == 0x01 || table == 0x02 || table == 0x1b)) {
        klass = mono_class_get (image, token);
      }
      found = is_import (klass);
    }
    ip += length;
  }
  mono_metadata_free_mh (header);
  return found;
}

static gpointer find_info (Batch * batch, gpointer root, gpointer address) {
  gpointer info = mono_jit_info_table_find (batch->domain, address);
  if (info == NULL && root != batch->domain)
    info = mono_jit_info_table_find (root, address);
  return info;
}

/* compile: input methods -> first code start, second code size (or NULL)
   locate: input addresses -> first method, second code start (or NULL)
   check: input methods -> first non-NULL if the method uses COM interop */
gpointer run_batch (gpointer data) {
  Batch * batch = data;
  gpointer thread = mono_thread_attach (batch->domain);
  gpointer root = mono_get_root_domain ();
  for (gint i = 0; i < batch->count && !batch->cancel; i++) {
    if (batch->kind == 2) {
      batch->first[i] = GSIZE_TO_POINTER ((gsize) uses_com (batch->input[i]));
      batch->done = i + 1;
      continue;
    }
    gpointer address = batch->input[i];
    if (batch->kind == 0)
      address = mono_compile_method (address);
    gpointer info = address == NULL ? NULL : find_info (batch, root, address);
    if (info == NULL) {
      batch->first[i] = NULL;
    } else if (batch->kind == 0) {
      batch->first[i] = mono_jit_info_get_code_start (info);
      batch->second[i] = GSIZE_TO_POINTER ((gsize) mono_jit_info_get_code_size (info));
    } else {
      batch->first[i] = mono_jit_info_get_method (info);
      batch->second[i] = mono_jit_info_get_code_start (info);
    }
    batch->done = i + 1;
  }
  mono_thread_detach (thread);
  batch->finished = 1;
  return NULL;
}
`;
  // Offsets in Batch: pointers are 4 bytes in a 32-bit game (Wine), 8 otherwise.
  const P = Process.pointerSize;
  const B_DOMAIN = 0, B_KIND = P, B_COUNT = P + 4, B_INPUT = P === 8 ? 16 : 12,
    B_FIRST = B_INPUT + P, B_SECOND = B_FIRST + P, B_DONE = B_SECOND + P,
    B_CANCEL = B_DONE + 4, B_FINISHED = B_CANCEL + 4,
    B_SIZE = Math.ceil((B_FINISHED + 4) / P) * P;
  let batchCode = null;
  const batches = new Set();

  function batchFunctions() {
    if (batchCode === null) {
      const symbols = {};
      for (const name of ['mono_thread_attach', 'mono_thread_detach', 'mono_compile_method',
                          'mono_jit_info_table_find', 'mono_get_root_domain',
                          'mono_jit_info_get_code_start', 'mono_jit_info_get_code_size',
                          'mono_jit_info_get_method', 'mono_method_get_header',
                          'mono_method_header_get_code', 'mono_metadata_free_mh',
                          'mono_method_get_class', 'mono_class_get_flags',
                          'mono_class_get_image', 'mono_get_method', 'mono_class_get'])
        symbols[name] = api.module.getExportByName(name);
      const cm = new CModule(BATCH_SOURCE, symbols);
      batchCode = { cm };
    }
    return batchCode;
  }

  function runBatch(kind, inputs, progress) {
    // A promise of [[first, second], ...] for the inputs (see BATCH_SOURCE).
    if (!refresh())
      return Promise.reject(grembyteError('error.mono.no_runtime'));
    const code = batchFunctions();
    const count = inputs.length;
    const size = Process.pointerSize * Math.max(count, 1);
    const batch = Memory.alloc(B_SIZE);
    const input = Memory.alloc(size);
    const first = Memory.alloc(size);
    const second = Memory.alloc(size);
    inputs.forEach((value, i) => input.add(i * Process.pointerSize).writePointer(value));
    batch.add(B_DOMAIN).writePointer(scriptDomain());
    batch.add(B_KIND).writeS32(kind);
    batch.add(B_COUNT).writeS32(count);
    batch.add(B_INPUT).writePointer(input);
    batch.add(B_FIRST).writePointer(first);
    batch.add(B_SECOND).writePointer(second);
    try {
      spawnThread(code.cm.run_batch, batch);
    } catch (error) {
      return Promise.reject(error);
    }
    // Should the session be cut meanwhile, Frida must not unload the C code
    // the batch thread runs: the script stays until the batch has finished.
    Script.pin();
    const entry = { batch, input };
    batches.add(entry);
    return new Promise((resolve, reject) => {
      const poll = () => {
        if (batch.add(B_FINISHED).readS32() === 0) {
          if (progress)
            progress(batch.add(B_DONE).readS32());
          entry.timer = setTimeout(poll, BATCH_POLL);
          return;
        }
        batches.delete(entry);
        Script.unpin();
        if (batch.add(B_CANCEL).readS32() !== 0) {
          reject(new Error('cancelled'));
          return;
        }
        // The closure keeps the buffers alive until here.
        resolve(inputs.map((_value, i) => [
          first.add(i * Process.pointerSize).readPointer(),
          second.add(i * Process.pointerSize).readPointer()
        ]));
      };
      poll();
    });
  }

  function cancelBatches() {
    // Ask running batches to stop; their promises reject once they have.
    for (const entry of batches)
      entry.batch.add(B_CANCEL).writeS32(1);
    return batches.size;
  }

  function batchesRunning() {
    return batches.size > 0;
  }

  async function checkAll(methods, progress) {
    // Whether each method uses COM interop (see uses_com): [true/false].
    const results = await runBatch(BATCH_CHECK, methods, progress);
    return results.map(([found]) => !found.isNull());
  }

  async function compileAll(methods, progress) {
    // The code of many methods: [{ start, size } or null], see runBatch.
    const results = await runBatch(BATCH_COMPILE, methods, progress);
    return results.map(([start, size]) =>
      (start.isNull() ? null : { start, size: size.toInt32() }));
  }

  async function describeAll(addresses) {
    // ``describe`` for many addresses of JIT code, from the agent's thread.
    const results = await runBatch(BATCH_LOCATE, addresses);
    return results.map(([method, start], i) => {
      if (method.isNull())
        return null;
      const key = method.toString();
      let text = names.get(key);
      if (text === undefined) {
        text = cString(api.fullName(method, 0));
        names.set(key, text);
      }
      return text === null ? null : `${text}+${addresses[i].sub(start)}`;
    });
  }

  async function compile(identity) {
    // A promise of { start, size }.
    if (!refresh())
      throw grembyteError('error.mono.no_runtime');
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
      throw grembyteError('error.mono.no_runtime');
    if (api.types === null)
      throw grembyteError('error.mono.no_signatures');
    return api.types;
  }

  // class -> { "0": "None", ... } of an enum, or null
  const enumCache = new Map();
  const MAX_ENUM_VALUES = 1024;
  // How a constant of an enum's base type is read
  const CONSTANT_READERS = {
    [TYPE.I1]: data => data.readS8(), [TYPE.U1]: data => data.readU8(),
    [TYPE.I2]: data => data.readS16(), [TYPE.U2]: data => data.readU16(),
    [TYPE.CHAR]: data => data.readU16(), [TYPE.BOOLEAN]: data => data.readU8(),
    [TYPE.I4]: data => data.readS32(), [TYPE.U4]: data => data.readU32(),
    [TYPE.I8]: data => data.readS64(), [TYPE.U8]: data => data.readU64()
  };

  function blobData(blob) {
    // The data of a metadata blob, after its compressed length (ECMA-335 II.23.2).
    const first = blob.readU8();
    if ((first & 0x80) === 0)
      return blob.add(1);
    return blob.add((first & 0xc0) === 0x80 ? 2 : 4);
  }

  function enumValues(t, klass, baseKind) {
    // { "1": "Minor", ... }: the names of an enum by their value, from its
    // constants (cached); null if they cannot be read.
    const key = klass.toString();
    if (enumCache.has(key))
      return enumCache.get(key);
    const read = CONSTANT_READERS[baseKind];
    let result = null;
    if (api.constant !== null && read !== undefined) {
      result = {};
      const iterator = Memory.alloc(Process.pointerSize);
      iterator.writePointer(NULL);
      let count = 0;
      for (let field = t.classFields(klass, iterator); !field.isNull() && count < MAX_ENUM_VALUES;
        field = t.classFields(klass, iterator)) {
        const flags = t.fieldFlags(field);
        if ((flags & FIELD_STATIC) === 0 || (flags & FIELD_LITERAL) === 0)
          continue;
        const blob = api.constant(field);
        if (blob.isNull())
          continue;
        const value = read(blobData(blob)).toString();
        // The first name of a value wins (enums may give one several names)
        if (!(value in result))
          result[value] = t.fieldName(field).readUtf8String();
        count++;
      }
      if (Object.keys(result).length === 0)
        result = null;
    }
    if (enumCache.size >= MAX_CACHE)
      enumCache.clear();
    enumCache.set(key, result);
    return result;
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
        const values = enumValues(t, klass, info.kind);
        if (values !== null)
          info.enumValues = values;
      } else if (!klass.isNull() && t.isValueType(klass) !== 0) {
        info.kind = TYPE.VALUETYPE;
        info.structSize = t.valueSize(klass, NULL);
        info.klass = klass;
      }
    }
    return info;
  }

  async function prepare(identity) {
    // Compile the method like compile() and describe its signature (a
    // promise): { start, size, instance, params: [{ name, type }], returns: type }.
    typesApi();
    return onMonoThread(domain => {
      const { method, start, size } = compileOnThread(domain, identity);
      return { start, size, ...signatureOf(method) };
    });
  }

  const signatures = new Map();

  function signatureOf(method) {
    // { instance, params: [{ name, type }], returns: type } of a method (cached).
    const key = method.toString();
    let result = signatures.get(key);
    if (result !== undefined)
      return result;
    const t = typesApi();
    const signature = t.methodSignature(method);
    if (signature.isNull())
      throw grembyteError('error.mono.no_signature', { method });
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
    result = {
      params,
      instance: t.isInstance(signature) !== 0,
      returns: typeInfo(t, t.returnType(signature))
    };
    signatures.set(key, result);
    return result;
  }

  // --- Objects: classes, their fields, arrays -----------------------------

  const FIELD_STATIC = 0x10;
  const FIELD_LITERAL = 0x40;
  // The offset Mono gives a thread or context static field: it has no place
  // in the class's static data.
  const SPECIAL_STATIC_OFFSET = 0xffffffff;
  const layouts = new Map();
  let knownImages = new Set();

  function isLoadedImage(image) {
    if (knownImages.has(image.toString()))
      return true;
    if (api.catalog === null)
      return false;
    // Assemblies may have been loaded since.
    knownImages = new Set(loadedImages(api.catalog).map(String));
    return knownImages.has(image.toString());
  }

  function classOf(object) {
    // The class of a managed object, or null if ``object`` is none (any
    // more): a MonoObject starts with its vtable, which starts with the
    // class, and a real class belongs to a loaded assembly. Only plain
    // memory reads until then, so a stale address cannot mislead Mono.
    typesApi();
    try {
      const klass = object.readPointer().readPointer();
      return isLoadedImage(api.classImage(klass)) ? klass : null;
    } catch (_error) {
      return null;
    }
  }

  function layout(klass) {
    // { name, valueType, rank, fields: [{ name, type, offset, static }] }
    // with inherited fields first; for arrays also element type and size.
    // Static fields also have ``owner`` (the class whose static data holds
    // them, see staticData) and ``storage``: 'data' there, 'const' (no
    // storage; the value is in the metadata only) or 'thread' (per thread).
    const key = klass.toString();
    let result = layouts.get(key);
    if (result !== undefined)
      return result;
    const t = typesApi();
    const chain = [];
    for (let current = klass; !current.isNull(); current = t.classParent(current))
      chain.unshift(current);
    const fields = [];
    const iterator = Memory.alloc(Process.pointerSize);
    for (const current of chain) {
      iterator.writePointer(NULL);
      for (;;) {
        const field = t.classFields(current, iterator);
        if (field.isNull())
          break;
        const flags = t.fieldFlags(field);
        const entry = {
          name: t.fieldName(field).readUtf8String(),
          type: typeInfo(t, t.fieldType(field)),
          offset: t.fieldOffset(field),
          static: (flags & FIELD_STATIC) !== 0
        };
        if (entry.static) {
          entry.owner = current;
          entry.storage = (flags & FIELD_LITERAL) !== 0 ? 'const'
            : entry.offset === SPECIAL_STATIC_OFFSET ? 'thread' : 'data';
        }
        fields.push(entry);
      }
    }
    result = {
      name: cString(t.typeName(t.classType(klass))) || '?',
      valueType: t.isValueType(klass) !== 0,
      rank: t.classRank(klass),
      fields
    };
    if (result.rank > 0) {
      const element = t.elementClass(klass);
      result.element = typeInfo(t, t.classType(element));
      result.elementSize = t.elementSize(klass);
    }
    layouts.set(key, result);
    return result;
  }

  // domain|class -> the class's static data; Mono allocates it once (fixed,
  // the garbage collector does not move it).
  const staticAreas = new Map();

  function staticData(klass) {
    // Where the static fields of ``klass`` are (their offsets count from
    // here) in the domain of the calling thread, or null: no such functions,
    // a thread Mono does not know, or a class that failed to load. Before its
    // static constructor ran the fields read as zero.
    if (!refresh() || api.statics === null)
      return null;
    const domain = api.currentDomain();
    if (domain.isNull())
      return null;
    const key = `${domain}|${klass}`;
    let data = staticAreas.get(key);
    if (data === undefined) {
      const vtable = api.statics.classVtable(domain, klass);
      data = vtable.isNull() ? NULL : api.statics.fieldData(vtable);
      if (staticAreas.size >= MAX_CACHE)
        staticAreas.clear();
      staticAreas.set(key, data);
    }
    return data.isNull() ? null : data;
  }

  function ownStaticFields(t, klass) {
    // [[name, offset], ...] of the static fields ``klass`` itself keeps in
    // its static data (no constants, no thread statics).
    const fields = [];
    const iterator = Memory.alloc(Process.pointerSize);
    iterator.writePointer(NULL);
    for (let field = t.classFields(klass, iterator); !field.isNull();
      field = t.classFields(klass, iterator)) {
      const flags = t.fieldFlags(field);
      const offset = t.fieldOffset(field);
      if ((flags & FIELD_STATIC) !== 0 && (flags & FIELD_LITERAL) === 0
          && offset !== SPECIAL_STATIC_OFFSET)
        fields.push([t.fieldName(field).readUtf8String(), offset]);
    }
    return fields;
  }

  async function classStaticAreas(requests) {
    // For the pointer scan: where the classes of assemblies keep their
    // static fields, as chain bases that a restart does not move. ``requests``
    // is [[assembly, class names or null for all], ...]; the answer
    // [[assembly, class, address, size, [[field, offset], ...]], ...] for
    // the classes with static data. Generic definitions have none of their
    // own (each instance has), so they are left out. Reading a class's
    // static data creates its vtable, never runs its static constructor.
    const c = catalogApi();
    const t = typesApi();
    if (api.statics === null)
      throw grembyteError('error.mono.no_statics');
    return onMonoThread(domain => {
      const result = [];
      for (const [assembly, wanted] of requests) {
        let image;
        try {
          image = imageNamed(c, assembly);
        } catch (_error) {
          continue;  // not loaded (yet)
        }
        const names = wanted === null ? null : new Set(wanted);
        const rows = c.tableRows(image, TYPEDEF_TABLE);
        for (let row = 2; row <= rows; row++) {
          const klass = c.classGet(image, TYPEDEF_TOKEN | row);
          if (klass.isNull())
            continue;
          const className = cString(c.typeName(c.classType(klass)));
          if (className === null || className.endsWith('>')
              || (names !== null && !names.has(className)))
            continue;
          const fields = ownStaticFields(t, klass);
          if (fields.length === 0)
            continue;
          const vtable = api.statics.classVtable(domain, klass);
          const data = vtable.isNull() ? NULL : api.statics.fieldData(vtable);
          if (data.isNull())
            continue;
          const known = api.staticSize === null ? 0 : api.staticSize(klass);
          const size = known > 0 ? known
            : Math.max(...fields.map(([, offset]) => offset)) + Process.pointerSize;
          result.push([assembly, className, data.toString(), size, fields]);
        }
      }
      return result;
    });
  }

  function staticBase(assembly, className) {
    // The static data of a class by its catalog name, for a pointer chain
    // based on it (see staticAreas), or null. On a thread Mono knows, e.g. a
    // game thread in a hook.
    if (!refresh() || api.statics === null || api.catalog === null)
      return null;
    const c = api.catalog;
    let image;
    try {
      image = imageNamed(c, assembly);
    } catch (_error) {
      return null;
    }
    const rows = c.tableRows(image, TYPEDEF_TABLE);
    for (let row = 2; row <= rows; row++) {
      const klass = c.classGet(image, TYPEDEF_TOKEN | row);
      if (!klass.isNull() && cString(c.typeName(c.classType(klass))) === className)
        return staticData(klass);
    }
    return null;
  }

  // --- Unity object names ----------------------------------------------------
  // GameObjects and components keep their name on Unity's native side; the
  // managed object only holds a pointer to it (m_CachedPtr). Reading it means
  // calling UnityEngine.Object.get_name, which Unity only allows on its main
  // thread: call unityName only on a game thread that runs game code. Threads
  // where it never worked are not asked again.

  const MAX_FAILURES = 3;
  const nameMethods = new Map();
  const nameThreads = new Map();
  const naming = new Set();

  function nameMethod(klass) {
    // UnityEngine.Object.get_name if ``klass`` derives from UnityEngine.Object.
    const key = klass.toString();
    let method = nameMethods.get(key);
    if (method !== undefined)
      return method;
    const t = typesApi();
    method = null;
    for (let current = klass; !current.isNull(); current = t.classParent(current)) {
      if (layout(current).name === 'UnityEngine.Object') {
        const found = api.invoke.methodFromName(current, Memory.allocUtf8String('get_name'), 0);
        method = found.isNull() ? null : found;
        break;
      }
    }
    nameMethods.set(key, method);
    return method;
  }

  function unityName(object) {
    // The name of a GameObject or component, or null.
    if (api === null || api.types === null || api.invoke === null)
      return null;
    const thread = Process.getCurrentThreadId();
    const state = nameThreads.get(thread) || { ok: 0, failed: 0 };
    if ((state.ok === 0 && state.failed >= MAX_FAILURES) || naming.has(thread))
      return null;
    const klass = classOf(object);
    const method = klass === null ? null : nameMethod(klass);
    if (method === null)
      return null;
    const exception = Memory.alloc(Process.pointerSize);
    exception.writePointer(NULL);
    naming.add(thread);  // get_name could run a monitored method
    let result;
    try {
      result = api.invoke.runtimeInvoke(method, object, NULL, exception);
    } finally {
      naming.delete(thread);
    }
    // An exception: not the main thread, or the object was destroyed.
    const failed = !exception.readPointer().isNull() || result.isNull();
    if (failed)
      state.failed++;
    else
      state.ok++;
    nameThreads.set(thread, state);
    if (failed)
      return null;
    const length = result.add(2 * Process.pointerSize).readS32();
    return result.add(2 * Process.pointerSize + 4).readUtf16String(Math.min(length, 120));
  }

  // --- Unity's main thread ----------------------------------------------------
  // Game code that Unity only allows on its main thread (Unity API, game
  // logic) runs there via onMainThread: the next time the main thread starts
  // a script method through mono_runtime_invoke, the queued work runs first.
  // A game that sleeps out of focus (Unity 4: Might & Magic X) calls none; it
  // is woken in its message loop instead (wakeMainThread). Which thread is the
  // main one, see mainThreadId (platform layer).

  const MAIN_TIMEOUT = 3000;
  const mainQueue = [];
  let mainHooks = null;  // the listeners waiting for the main thread
  let draining = false;

  function isMainThread() {
    return Process.getCurrentThreadId() === mainThreadId();
  }

  function drainMain() {
    // Run the queued work; call it on any thread, it only acts on the main one.
    if (draining || !isMainThread())
      return;
    draining = true;  // our own invocations pass mono_runtime_invoke, too
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
    if (mainHooks !== null) {
      const hooks = mainHooks;
      mainHooks = null;
      // not from within their own callbacks
      setTimeout(() => hooks.forEach(hook => hook.detach()), 0);
    }
  }

  function onMainThread(work) {
    // A promise of work()'s result, computed on Unity's main thread.
    if (api === null || api.invoke === null)
      return Promise.reject(grembyteError('error.mono.no_invoke'));
    mainThreadId();  // found here, not first in the hook on a game thread
    return new Promise((resolve, reject) => {
      const job = { work, resolve, reject };
      job.timer = setTimeout(() => {
        const index = mainQueue.indexOf(job);
        if (index >= 0)
          mainQueue.splice(index, 1);
        reject(grembyteError('error.mono.main_thread_timeout'));
      }, MAIN_TIMEOUT);
      mainQueue.push(job);
      if (mainHooks === null)
        // Also when the game sleeps out of focus (see wakeMainThread)
        mainHooks = [Interceptor.attach(api.invoke.runtimeInvokeAddress, {
          onEnter() {
            drainMain();
          }
        }), ...wakeMainThread(drainMain)];
    });
  }

  // --- Properties: parameterless getters ---------------------------------------

  const propertyLists = new Map();

  function classChain(klass) {
    const chain = [];
    for (let current = klass; !current.isNull(); current = api.types.classParent(current))
      chain.push(current);
    return chain;  // the class first, System.Object last
  }

  function properties(klass) {
    // [{ name, method, static, type, setter }] of the getters of ``klass``
    // and its base classes (overridden ones once); ``setter`` is the
    // property's set_ method or null. A GameObject also lists its
    // components (``special: 'components'``). Cached.
    const key = klass.toString();
    let result = propertyLists.get(key);
    if (result !== undefined)
      return result;
    const t = typesApi();
    result = [];
    if (api.invoke === null) {
      propertyLists.set(key, result);
      return result;
    }
    const seen = new Set();
    const iterator = Memory.alloc(Process.pointerSize);
    const chain = classChain(klass);
    if (chain.some(current => layout(current).name === 'UnityEngine.GameObject'))
      result.push({ name: 'GetComponents()', special: 'components', static: false,
                    type: { kind: 0x1d, byref: false, name: 'UnityEngine.Component[]' } });
    const setters = new Map();
    for (const current of chain) {
      iterator.writePointer(NULL);
      for (;;) {
        const method = api.invoke.classMethods(current, iterator);
        if (method.isNull())
          break;
        const name = api.invoke.methodName(method).readUtf8String();
        if (name.startsWith('set_')) {
          const signature = t.methodSignature(method);
          if (!setters.has(name) && !signature.isNull() && t.paramCount(signature) === 1)
            setters.set(name, method.toString());
          continue;
        }
        if (!name.startsWith('get_') || seen.has(name))
          continue;
        const signature = t.methodSignature(method);
        if (signature.isNull() || t.paramCount(signature) !== 0)
          continue;
        seen.add(name);
        result.push({
          name: name.substring(4),
          method: method.toString(),
          static: (api.invoke.methodFlags(method, NULL) & METHOD_STATIC) !== 0,
          type: typeInfo(t, t.returnType(signature))
        });
      }
    }
    for (const property of result)
      if (property.special === undefined)
        property.setter = setters.get(`set_${property.name}`) || null;
    propertyLists.set(key, result);
    return result;
  }

  // --- Methods: what the object inspector offers to call ------------------------

  const methodLists = new Map();

  function methods(klass) {
    // [{ name, method, static, owner }] of the methods of ``klass`` and its
    // base classes, the most derived first (an override once); no
    // constructors and no property accessors (see properties). Cached.
    const key = klass.toString();
    let result = methodLists.get(key);
    if (result !== undefined)
      return result;
    result = [];
    if (api.invoke === null) {
      methodLists.set(key, result);
      return result;
    }
    const t = typesApi();
    const seen = new Set();
    const iterator = Memory.alloc(Process.pointerSize);
    for (const current of classChain(klass)) {
      const owner = layout(current).name;
      iterator.writePointer(NULL);
      for (let method = api.invoke.classMethods(current, iterator); !method.isNull();
           method = api.invoke.classMethods(current, iterator)) {
        const name = api.invoke.methodName(method).readUtf8String();
        const signature = t.methodSignature(method);
        if (signature.isNull() || name === '.ctor' || name === '.cctor')
          continue;
        const count = t.paramCount(signature);
        if ((name.startsWith('get_') && count === 0) || (name.startsWith('set_') && count === 1))
          continue;
        // Overloads differ in their parameters: "Name (int,string)"
        const full = signatureName(method) || name;
        const id = `${name}${full.substring(Math.max(0, full.indexOf(' (')))}`;
        if (seen.has(id))
          continue;
        seen.add(id);
        result.push({ name, method, owner,
                      static: (api.invoke.methodFlags(method, NULL) & METHOD_STATIC) !== 0 });
      }
    }
    methodLists.set(key, result);
    return result;
  }

  function invoke(method, object, params) {
    // { result, exception } of calling a managed method; main thread only.
    const exception = Memory.alloc(Process.pointerSize);
    exception.writePointer(NULL);
    const result = api.invoke.runtimeInvoke(method, object, params, exception);
    return { result, exception: exception.readPointer() };
  }

  function getProperty(object, property, isStruct) {
    // Call a getter of ``object`` (an unboxed value for a struct).
    if (property.special === 'components')
      return components(object);
    let method = ptr(property.method);
    if (!property.static && !isStruct)
      method = api.invoke.virtualMethod(object, method);  // the override
    return invoke(method, property.static ? NULL : object, NULL);
  }

  function methodTakingType(klass, name) {
    // The method ``name`` of ``klass`` itself taking one System.Type, or null.
    const iterator = Memory.alloc(Process.pointerSize);
    const paramIterator = Memory.alloc(Process.pointerSize);
    iterator.writePointer(NULL);
    let found = null;
    for (let method = api.invoke.classMethods(klass, iterator); !method.isNull();
         method = api.invoke.classMethods(klass, iterator)) {
      if (api.invoke.methodName(method).readUtf8String() !== name)
        continue;
      const signature = api.types.methodSignature(method);
      paramIterator.writePointer(NULL);
      if (!signature.isNull() && api.types.paramCount(signature) === 1
          && cString(api.types.typeName(api.types.params(signature, paramIterator)))
             === 'System.Type')
        found = method;
    }
    return found;
  }

  function components(gameObject) {
    // GameObject.GetComponents(typeof(Component))
    const klass = classOf(gameObject);
    let getComponents = null;
    for (const current of classChain(klass))
      getComponents = methodTakingType(current, 'GetComponents') || getComponents;
    if (getComponents === null)
      throw grembyteError('error.mono.no_get_components');
    const image = api.classImage(klass);
    const component = api.invoke.classFromName(image, Memory.allocUtf8String('UnityEngine'),
                                               Memory.allocUtf8String('Component'));
    if (component.isNull())
      throw grembyteError('error.mono.no_component_class');
    const params = Memory.alloc(Process.pointerSize);
    params.writePointer(api.invoke.typeObject(api.currentDomain(), api.types.classType(component)));
    return invoke(getComponents, gameObject, params);
  }

  // --- Calling game methods by name ---------------------------------------------

  const classesByName = new Map();

  function findClass(fullName) {
    // A class of a loaded assembly by its full name, e.g. "Game.PartyHelper".
    let klass = classesByName.get(fullName);
    if (klass !== undefined)
      return klass;
    if (api.catalog === null || api.invoke === null)
      throw grembyteError('error.mono.no_class_lookup');
    const dot = fullName.lastIndexOf('.');
    const space = Memory.allocUtf8String(dot < 0 ? '' : fullName.substring(0, dot));
    const name = Memory.allocUtf8String(fullName.substring(dot + 1));
    klass = null;
    for (const image of loadedImages(api.catalog)) {
      const found = api.invoke.classFromName(image, space, name);
      if (!found.isNull()) {
        klass = found;
        break;
      }
    }
    if (klass === null)
      throw grembyteError('error.mono.class_not_found', { name: fullName });
    classesByName.set(fullName, klass);
    return klass;
  }

  function findMethod(klass, name, argumentCount, wantStatic) {
    // The first method ``name`` of the class or a base class taking
    // ``argumentCount`` arguments; ``wantStatic`` true/false or null for any.
    const iterator = Memory.alloc(Process.pointerSize);
    for (const current of classChain(klass)) {
      iterator.writePointer(NULL);
      for (let method = api.invoke.classMethods(current, iterator); !method.isNull();
           method = api.invoke.classMethods(current, iterator)) {
        if (api.invoke.methodName(method).readUtf8String() !== name)
          continue;
        const isStatic = (api.invoke.methodFlags(method, NULL) & METHOD_STATIC) !== 0;
        if ((wantStatic === null || isStatic === wantStatic)
            && signatureOf(method).params.length === argumentCount)
          return method;
      }
    }
    const values = { class_name: layout(klass).name, method: name, count: argumentCount };
    throw wantStatic ? grembyteError('error.mono.no_static_method', values)
      : grembyteError('error.mono.no_method', values);
  }

  function findObject(className, named = null) {
    // A live Unity object (GameObject, component, ScriptableObject ...) of the
    // class ``className`` or a subclass, e.g. findObject("Game.PlayerController");
    // with ``named`` the one of that name, e.g. findObject(
    // "UnityEngine.GameObject", "Player(Clone)"). null if none. Asks
    // UnityEngine.Object.FindObjectsOfType(Type), so only on Unity's main
    // thread, and objects of inactive GameObjects are not found.
    const [object] = findObjects(className, named, 1);
    return object === undefined ? null : object;
  }

  // (Frida's JavaScript on Windows has no Infinity: it is NaN there.)
  function findObjects(className, named = null, limit = Number.MAX_SAFE_INTEGER) {
    // Up to ``limit`` of the objects findObject could give
    const klass = findClass(className);
    const unityObject = classChain(klass).find(
      current => layout(current).name === 'UnityEngine.Object');
    if (unityObject === undefined)
      throw grembyteError('error.mono.not_unity_object', { class_name: className });
    const findObjects = methodTakingType(unityObject, 'FindObjectsOfType');
    if (findObjects === null)
      throw grembyteError('error.mono.no_find_objects');
    const params = Memory.alloc(Process.pointerSize);
    params.writePointer(api.invoke.typeObject(api.currentDomain(), api.types.classType(klass)));
    const { result, exception } = invoke(findObjects, NULL, params);
    if (!exception.isNull())
      throw grembyteError('error.mono.find_objects_failed');
    const found = [];
    if (result.isNull())
      return found;
    // A managed array: header, bounds, length, then the elements
    const length = result.add(3 * Process.pointerSize).readU32();
    const data = result.add(4 * Process.pointerSize);
    for (let i = 0; i < length && found.length < limit; i++) {
      const object = data.add(i * Process.pointerSize).readPointer();
      if (!object.isNull() && (named === null || unityName(object) === named))
        found.push(object);
    }
    return found;
  }

  // --- Object browser: the live objects, through Unity's liveness calculation ---
  // Unity's Mono (Boehm GC) has no heap walk: mono_gc_walk_heap is SGen's and
  // does nothing there. Unity's own liveness calculation (Resources.
  // UnloadUnusedAssets, the memory profiler) finds the objects reachable from
  // the static fields, or from one root object, and hands them to a callback
  // in batches of 64. It marks every object it visits in the lowest bit of
  // its vtable pointer, so it runs with the world stopped, and the marks are
  // cleared before the world starts again. Two passes: from the statics, and
  // from an array of all Unity objects (FindObjectsOfType), which Unity's
  // native side holds through GC handles, not statics.
  //
  // Two API generations: Unity 2017-2020 (also the old libmono) have
  // calculation_begin/end, which stop and start the world themselves and call
  // back when they do (to restart it while their arrays grow); Unity 2021 and
  // later have allocate_struct, stop/start_gc_world, finalize and free_struct,
  // and ask a callback for the 8 KiB blocks of their arrays. While the world
  // is stopped no JavaScript may run (it could wait for a lock a stopped
  // thread holds), so the callbacks are a few instructions of machine code:
  // ``collect`` keeps (object, class) pairs in a buffer, ``allocate`` hands
  // out blocks from a pool allocated beforehand, ``nothing`` is the world
  // callback.

  // Objects recorded per read; the rest are counted, not kept
  const LIVENESS_CAPACITY = 2 * 1024 * 1024;
  // The newer API's array blocks: enough for about 16 million objects
  const LIVENESS_POOL = 128 * 1024 * 1024;
  // The buffer's header: count, capacity, pool next, pool end; then the pairs
  const LIVENESS_HEADER = 64;
  // How long browseInstances answers from the objects browseClasses read
  const BROWSE_AGE = 60000;
  // Instances a class lists at most
  const MAX_INSTANCES = 5000;
  // Text of a string object shown as its name
  const MAX_STRING_NAME = 80;
  let liveness;  // undefined: not looked for yet; null: not available
  let browsed = null;  // { read, byClass: Map(klass -> [addresses]) }

  function livenessCode() {
    // The three callbacks (see above), for the platform's calling convention.
    const code = allocateCode(Process.pageSize);
    let entries;
    let failure = null;
    Memory.patchCode(code, Process.pageSize, memory => {
      try {
        const w = new X86Writer(memory, { pc: code });
        entries = Process.pointerSize === 4 ? writeLivenessCode32(w)
          : writeLivenessCode(w, Process.platform === 'windows');
      } catch (error) {
        failure = error;  // patchCode would not say
      }
    });
    if (failure !== null)
      throw failure;
    return entries;
  }

  function writeLivenessCode(w, windows) {
    // { collect, allocate, nothing }: where each starts. Only rax, r10, r11
    // and the arguments' registers are used: volatile in both conventions.
    const [first, second, third] = windows ? ['rcx', 'rdx', 'r8'] : ['rdi', 'rsi', 'rdx'];
    const second32 = windows ? 'edx' : 'esi';
    const entries = {};
    // void collect(MonoObject **objects, int count, Buffer *buffer)
    entries.collect = w.pc;
    w.putMovRegReg(second32, second32);  // zero-extend the count
    w.putLabel('next');
    w.putTestRegReg(second, second);
    w.putJccShortLabel('je', 'done', 'no-hint');
    w.putMovRegRegPtr('r10', first);  // the object
    w.putMovRegRegPtr('r11', 'r10');  // its vtable, marked in bit 0
    w.putAndRegU32('r11', 0xfffffffe);  // sign-extended: clears bit 0 only
    w.putMovRegRegPtr('r11', 'r11');  // the vtable's class
    w.putMovRegRegPtr('rax', third);  // count
    w.putCmpRegOffsetPtrReg(third, 8, 'rax');  // capacity - count
    w.putJccShortLabel('jbe', 'counted', 'no-hint');
    w.putShlRegU8('rax', 4);
    w.putAddRegReg('rax', third);
    w.putMovRegOffsetPtrReg('rax', LIVENESS_HEADER, 'r10');
    w.putMovRegOffsetPtrReg('rax', LIVENESS_HEADER + 8, 'r11');
    w.putLabel('counted');
    w.putIncRegPtr('qword', third);
    w.putAddRegImm(first, 8);
    w.putDecReg(second);
    w.putJmpShortLabel('next');
    w.putLabel('done');
    w.putRet();
    // void *allocate(void *block, int size, Buffer *buffer): a block of the
    // pool, or NULL; freeing (size 0) does nothing
    entries.allocate = w.pc;
    w.putMovRegReg(second32, second32);
    w.putTestRegReg(second, second);
    w.putJccShortLabel('je', 'none', 'no-hint');
    w.putMovRegRegOffsetPtr('rax', third, 16);  // pool next
    w.putMovRegReg('r10', 'rax');
    w.putAddRegReg('r10', second);
    w.putCmpRegOffsetPtrReg(third, 24, 'r10');  // pool end - new next
    w.putJccShortLabel('jb', 'none', 'no-hint');
    w.putMovRegOffsetPtrReg(third, 16, 'r10');
    w.putRet();
    w.putLabel('none');
    w.putXorRegReg('rax', 'rax');
    w.putRet();
    // void nothing(void)
    entries.nothing = w.pc;
    w.putRet();
    w.flush();
    return entries;
  }

  function writeLivenessCode32(w) {
    // The callbacks of writeLivenessCode for 32-bit x86 (cdecl: the arguments
    // on the stack). The buffer keeps its layout: each pointer in the low half
    // of a 64-bit slot, the high half zero; the count in the low half too.
    const entries = {};
    // void collect(MonoObject **objects, int count, Buffer *buffer)
    entries.collect = w.pc;
    w.putPushReg('esi');
    w.putPushReg('edi');
    w.putPushReg('ebx');
    w.putMovRegRegOffsetPtr('esi', 'esp', 16);  // objects
    w.putMovRegRegOffsetPtr('ecx', 'esp', 20);  // count
    w.putMovRegRegOffsetPtr('edi', 'esp', 24);  // buffer
    w.putLabel('next');
    w.putTestRegReg('ecx', 'ecx');
    w.putJccShortLabel('je', 'done', 'no-hint');
    w.putMovRegRegPtr('eax', 'esi');  // the object
    w.putMovRegRegPtr('edx', 'eax');  // its vtable, marked in bit 0
    w.putAndRegU32('edx', 0xfffffffe);
    w.putMovRegRegPtr('edx', 'edx');  // the vtable's class
    w.putMovRegRegPtr('ebx', 'edi');  // count
    w.putCmpRegOffsetPtrReg('edi', 8, 'ebx');  // capacity - count
    w.putJccShortLabel('jbe', 'counted', 'no-hint');
    w.putShlRegU8('ebx', 4);
    w.putAddRegReg('ebx', 'edi');
    w.putMovRegOffsetPtrReg('ebx', LIVENESS_HEADER, 'eax');
    w.putMovRegOffsetPtrU32('ebx', LIVENESS_HEADER + 4, 0);
    w.putMovRegOffsetPtrReg('ebx', LIVENESS_HEADER + 8, 'edx');
    w.putMovRegOffsetPtrU32('ebx', LIVENESS_HEADER + 12, 0);
    w.putLabel('counted');
    w.putIncRegPtr('dword', 'edi');
    w.putAddRegImm('esi', 4);
    w.putDecReg('ecx');
    w.putJmpShortLabel('next');
    w.putLabel('done');
    w.putPopReg('ebx');
    w.putPopReg('edi');
    w.putPopReg('esi');
    w.putRet();
    // void *allocate(void *block, int size, Buffer *buffer)
    entries.allocate = w.pc;
    w.putMovRegRegOffsetPtr('ecx', 'esp', 8);  // size
    w.putTestRegReg('ecx', 'ecx');
    w.putJccShortLabel('je', 'none', 'no-hint');
    w.putMovRegRegOffsetPtr('edx', 'esp', 12);  // buffer
    w.putMovRegRegOffsetPtr('eax', 'edx', 16);  // pool next
    w.putAddRegReg('ecx', 'eax');
    w.putCmpRegOffsetPtrReg('edx', 24, 'ecx');  // pool end - new next
    w.putJccShortLabel('jb', 'none', 'no-hint');
    w.putMovRegOffsetPtrReg('edx', 16, 'ecx');
    w.putRet();
    w.putLabel('none');
    w.putXorRegReg('eax', 'eax');
    w.putRet();
    // void nothing(void)
    entries.nothing = w.pc;
    w.putRet();
    w.flush();
    return entries;
  }

  function livenessApi() {
    if (liveness !== undefined)
      return liveness;
    liveness = null;
    const m = api.module;
    const has = name => m.findExportByName(name) !== null;
    const fn = (name, ret, args) => new NativeFunction(m.findExportByName(name), ret, args);
    if (!['mono_unity_liveness_calculation_from_statics', 'mono_get_object_class',
      'mono_gchandle_new', 'mono_gchandle_free'].every(has))
      return null;
    const common = {
      objectClass: fn('mono_get_object_class', 'pointer', []),
      fromStatics: fn('mono_unity_liveness_calculation_from_statics', 'void', ['pointer']),
      fromRoot: has('mono_unity_liveness_calculation_from_root')
        ? fn('mono_unity_liveness_calculation_from_root', 'void', ['pointer', 'pointer']) : null,
      handleNew: fn('mono_gchandle_new', 'uint32', ['pointer', 'int']),
      handleFree: fn('mono_gchandle_free', 'void', ['uint32'])
    };
    if (has('mono_unity_liveness_calculation_begin')
        && has('mono_unity_liveness_calculation_end')) {
      liveness = { ...common, kind: 'begin',
        begin: fn('mono_unity_liveness_calculation_begin', 'pointer',
          ['pointer', 'uint32', 'pointer', 'pointer', 'pointer', 'pointer']),
        end: fn('mono_unity_liveness_calculation_end', 'void', ['pointer']) };
    } else if (['mono_unity_liveness_allocate_struct', 'mono_unity_liveness_stop_gc_world',
      'mono_unity_liveness_finalize', 'mono_unity_liveness_start_gc_world',
      'mono_unity_liveness_free_struct'].every(has)) {
      liveness = { ...common, kind: 'blocks',
        allocate: fn('mono_unity_liveness_allocate_struct', 'pointer',
          ['pointer', 'uint32', 'pointer', 'pointer', 'pointer']),
        stop: fn('mono_unity_liveness_stop_gc_world', 'void', []),
        finalize: fn('mono_unity_liveness_finalize', 'void', ['pointer']),
        start: fn('mono_unity_liveness_start_gc_world', 'void', []),
        free: fn('mono_unity_liveness_free_struct', 'void', ['pointer']) };
    } else {
      return null;
    }
    liveness.code = livenessCode();
    liveness.buffer = Memory.alloc(LIVENESS_HEADER + 16 * LIVENESS_CAPACITY);
    liveness.pool = liveness.kind === 'blocks' ? Memory.alloc(LIVENESS_POOL) : null;
    return liveness;
  }

  function unityObjectsArray() {
    // A managed array of every UnityEngine.Object (FindObjectsOfType), or
    // NULL in a game without them. On the main thread.
    let unityObject;
    try {
      unityObject = findClass('UnityEngine.Object');
    } catch (_error) {
      return NULL;
    }
    const find = methodTakingType(unityObject, 'FindObjectsOfType');
    if (find === null)
      return NULL;
    const params = Memory.alloc(Process.pointerSize);
    params.writePointer(api.invoke.typeObject(api.currentDomain(),
                                              api.types.classType(unityObject)));
    const { result, exception } = invoke(find, NULL, params);
    return exception.isNull() ? result : NULL;
  }

  function readLiveObjects() {
    // { pairs: [[object, class], ...] as numbers, count, stoppedMs } of the
    // objects reachable from the statics and the Unity objects. On Unity's
    // main thread: the calculation needs a thread Mono knows, and
    // FindObjectsOfType runs there.
    const l = livenessApi();
    if (l === null)
      throw grembyteError('error.mono.no_liveness');
    const buffer = l.buffer;
    buffer.writeU64(0);
    buffer.add(8).writeU64(LIVENESS_CAPACITY);
    if (l.pool !== null) {
      buffer.add(16).writePointer(l.pool);
      buffer.add(24).writePointer(l.pool.add(LIVENESS_POOL));
    }
    // Held by a GC handle: a collection must not free it before the world stops
    const roots = l.fromRoot === null ? NULL : unityObjectsArray();
    const handle = roots.isNull() ? 0 : l.handleNew(roots, 0);
    const filter = l.objectClass();
    let stoppedMs;
    try {
      const started = Date.now();
      if (l.kind === 'begin') {
        const state = l.begin(filter, 1000, l.code.collect, buffer, l.code.nothing,
                              l.code.nothing);
        l.fromStatics(state);
        if (!roots.isNull())
          l.fromRoot(roots, state);
        l.end(state);
      } else {
        const state = l.allocate(filter, 1000, l.code.collect, buffer, l.code.allocate);
        l.stop();
        l.fromStatics(state);
        if (!roots.isNull())
          l.fromRoot(roots, state);
        l.finalize(state);
        l.start();
        l.free(state);
      }
      stoppedMs = Date.now() - started;
    } finally {
      if (handle !== 0)
        l.handleFree(handle);
    }
    const count = Number(buffer.readU64());
    const kept = Math.min(count, LIVENESS_CAPACITY);
    const view = new DataView(buffer.add(LIVENESS_HEADER).readByteArray(16 * kept));
    const pairs = [];
    for (let i = 0; i < kept; i++) {
      pairs.push([view.getUint32(16 * i, true) + view.getUint32(16 * i + 4, true) * 0x100000000,
                  view.getUint32(16 * i + 8, true)
                    + view.getUint32(16 * i + 12, true) * 0x100000000]);
    }
    return { pairs, count, stoppedMs };
  }

  function objectName(object, klass) {
    // A Unity object's name, a string's text, or null (on the main thread)
    const name = layout(klass).name;
    if (name === 'System.String') {
      const length = object.add(2 * Process.pointerSize).readS32();
      return object.add(2 * Process.pointerSize + 4).readUtf16String(
        Math.min(length, MAX_STRING_NAME));
    }
    return nameMethod(klass) === null ? null : unityName(object);
  }

  function singletonField(klass, object) {
    // "Class.Field" of a static field of ``klass`` or a base class (as
    // Singleton<T> keeps it) holding ``object``, or null
    const t = typesApi();
    for (let current = klass; !current.isNull(); current = t.classParent(current)) {
      const fields = ownStaticFields(t, current);
      if (fields.length === 0)
        continue;
      const data = staticData(current);
      if (data === null)
        continue;
      for (const [name, offset] of fields) {
        try {
          if (data.add(offset).readPointer().equals(object))
            return `${layout(current).name.replace(/^.*\./, '')}.${name}`;
        } catch (_error) {
          // no pointer there
        }
      }
    }
    return null;
  }

  function filterWords(filter) {
    return String(filter || '').toLowerCase().split(/\s+/).filter(word => word !== '');
  }

  function matchesWords(text, words) {
    const lower = text.toLowerCase();
    return words.every(word => lower.includes(word));
  }

  function browseClasses(options = {}) {
    // As the Unreal adapter's: the classes with live objects, here read by
    // the liveness calculation on Unity's main thread. ``package`` is the
    // class's assembly, ``role`` the static field holding the one object of
    // a class (a singleton's Instance). ``internal`` changes nothing.
    typesApi();
    const words = filterWords(options.filter);
    return onMainThread(() => {
      const started = Date.now();
      const { pairs, count, stoppedMs } = readLiveObjects();
      const byClass = new Map();
      for (const [object, klass] of pairs) {
        let list = byClass.get(klass);
        if (list === undefined) {
          list = [];
          byClass.set(klass, list);
        }
        list.push(object);
      }
      browsed = { read: Date.now(), byClass };
      const classes = [];
      for (const [number, objects] of byClass) {
        const klass = ptr(number);
        let name, assembly;
        try {
          name = layout(klass).name;
          assembly = api.imageName(api.classImage(klass)).readUtf8String();
        } catch (_error) {
          continue;
        }
        let role = null, matched = 0;
        try {
          if (objects.length === 1)
            role = singletonField(klass, ptr(objects[0]));
          if (words.length > 0 && (name === 'System.String' || nameMethod(klass) !== null)) {
            for (const object of objects) {
              const text = objectName(ptr(object), klass);
              if (text !== null && matchesWords(text, words))
                matched++;
            }
          }
        } catch (_error) {
          // a class whose statics or names cannot be read
        }
        classes.push({ klass: klass.toString(), name, cppName: name, package: assembly,
                       count: objects.length, role, matched });
      }
      classes.sort((a, b) => a.name.localeCompare(b.name));
      return { classes, objects: pairs.length, truncated: count > pairs.length,
               stats: { stoppedMs, totalMs: Date.now() - started } };
    });
  }

  function browseInstances(klass, options = {}) {
    // As the Unreal adapter's, from the objects browseClasses read last (the
    // liveness calculation stops the world): those still of their class.
    typesApi();
    const words = filterWords(options.filter);
    const limit = options.limit > 0 ? options.limit : MAX_INSTANCES;
    return onMainThread(() => {
      if (browsed === null || Date.now() - browsed.read > BROWSE_AGE) {
        const { pairs } = readLiveObjects();
        const byClass = new Map();
        for (const [object, owner] of pairs) {
          if (!byClass.has(owner))
            byClass.set(owner, []);
          byClass.get(owner).push(object);
        }
        browsed = { read: Date.now(), byClass };
      }
      const wanted = ptr(klass);
      const instances = [];
      let count = 0;
      for (const number of browsed.byClass.get(Number(wanted.toString())) || []) {
        const object = ptr(number);
        try {
          if (!object.readPointer().readPointer().equals(wanted))
            continue;  // freed, and its memory used anew
        } catch (_error) {
          continue;
        }
        const name = objectName(object, wanted);
        if (words.length > 0 && (name === null || !matchesWords(name, words)))
          continue;
        count++;
        if (instances.length < limit)
          instances.push({ address: object.toString(), name, outer: null });
      }
      instances.sort((a, b) => (a.name || '').localeCompare(b.name || '')
        || (ptr(a.address).compare(ptr(b.address))));
      return { instances, count, truncated: count > instances.length };
    });
  }

  function writeReference(object, field, value) {
    // Store reference ``value`` in the field at ``field`` of ``object``.
    if (api.writeBarrier !== null && object !== null)
      api.writeBarrier(object, field, value);
    else
      field.writePointer(value);
  }

  function newString(text) {
    if (api.stringNew === null)
      throw grembyteError('error.mono.no_strings');
    return api.stringNew(api.currentDomain(), Memory.allocUtf8String(text));
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
      throw grembyteError('error.mono.no_runtime');
    if (api.catalog === null)
      throw grembyteError('error.mono.no_catalog');
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

  async function assemblies() {
    // Loading classes needs a thread Mono knows.
    const c = catalogApi();
    return onMonoThread(() => loadedImages(c).map(image => ({
      name: api.imageName(image).readUtf8String(),
      file: api.imageFile(image).readUtf8String(),
      classes: Math.max(0, c.tableRows(image, TYPEDEF_TABLE) - 1),
      mvid: imageGuid(image)
    })));
  }

  function imageGuid(image) {
    // Mono keeps the string; it is not to be freed.
    const guid = api.imageGuid === null ? NULL : api.imageGuid(image);
    return guid.isNull() ? null : guid.readUtf8String();
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

  function parameterNames(method) {
    const t = api.types;
    if (t === null)
      return [];
    const signature = t.methodSignature(method);
    if (signature.isNull())
      return [];
    const count = t.paramCount(signature);
    const names = Memory.alloc(Process.pointerSize * Math.max(count, 1));
    t.paramNames(method, names);
    return Array.from({ length: count }, (_, index) => {
      const name = names.add(index * Process.pointerSize).readPointer();
      return name.isNull() ? `arg${index}` : name.readUtf8String();
    });
  }

  function flagsOf(c, method, implFlags) {
    // [flags, implementation flags] as Mono has them; implFlags: 4 bytes.
    implFlags.writeU32(0);
    const flags = c.methodFlags(method, implFlags);
    return [flags, implFlags.readU32()];
  }

  function imageNamed(c, assembly) {
    const image = loadedImages(c).find(
      candidate => api.imageName(candidate).readUtf8String() === assembly);
    if (image === undefined)
      throw grembyteError('error.mono.assembly_not_loaded', { assembly });
    return image;
  }

  function eachClass(c, assembly, visit) {
    // visit(class name, [method, ...]) for the classes of one assembly; row
    // 1 of its type table is the <Module> pseudo class. On a Mono thread.
    const image = imageNamed(c, assembly);
    const iterator = Memory.alloc(Process.pointerSize);
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
        methods.push(method);
      }
      visit(cString(c.typeName(c.classType(klass))), methods);
    }
  }

  async function classes(assembly) {
    // [[class name, [method entry + flags + implementation flags, ...]], ...]
    // of one assembly; the trace chooses by the flags.
    const c = catalogApi();
    return onMonoThread(() => {
      const result = [];
      const implFlags = Memory.alloc(4);
      eachClass(c, assembly, (className, methods) => {
        result.push([className, methods.map(
          method => [...methodEntry(c, method), ...flagsOf(c, method, implFlags),
                     parameterNames(method)])]);
      });
      return result;
    });
  }

  async function methodsByToken(assembly, tokens) {
    // [{ className, method, entry, flags, implFlags }] of the methods with
    // these tokens, for the trace: ``entry`` as in classes.
    const c = catalogApi();
    return onMonoThread(() => {
      const image = imageNamed(c, assembly);
      const implFlags = Memory.alloc(4);
      const result = [];
      for (const token of tokens) {
        const method = api.getMethod(image, token, NULL);
        if (method.isNull())
          continue;
        const [flags, impl] = flagsOf(c, method, implFlags);
        result.push({ className: cString(c.typeName(c.classType(api.methodClass(method)))),
                      method, entry: methodEntry(c, method), flags, implFlags: impl });
      }
      return result;
    });
  }

  // --- Call graph: the IL code of methods and what its tokens name ------------

  async function methodBodies(assembly, tokens) {
    // The IL code of the methods with these tokens in one buffer of records:
    // token (u32), size (u32), code. Size 0: no code (abstract, native, ...).
    const c = catalogApi();
    if (api.il === null)
      throw grembyteError('error.mono.no_il');
    return onMonoThread(() => {
      const image = imageNamed(c, assembly);
      const sizes = Memory.alloc(8);
      const bodies = tokens.map(token => [token, ilCode(image, token, sizes)]);
      const total = bodies.reduce((sum, [, code]) => sum + 8 + (code ? code.byteLength : 0), 0);
      const out = new Uint8Array(total);
      const view = new DataView(out.buffer);
      let at = 0;
      for (const [token, code] of bodies) {
        view.setUint32(at, token, true);
        view.setUint32(at + 4, code ? code.byteLength : 0, true);
        at += 8;
        if (code) {
          out.set(new Uint8Array(code), at);
          at += code.byteLength;
        }
      }
      return out.buffer;
    });
  }

  function ilCode(image, token, sizes) {
    // A method's IL code (ArrayBuffer), null if it has none. On a Mono thread.
    const method = api.getMethod(image, token, NULL);
    if (method.isNull())
      return null;
    const header = api.il.header(method);
    if (header.isNull())
      return null;
    try {
      const code = api.il.code(header, sizes, sizes.add(4));
      const size = sizes.readU32();
      return code.isNull() || size === 0 ? null : code.readByteArray(size);
    } finally {
      api.il.free(header);  // frees only a header Mono made for the call
    }
  }

  async function resolveTokens(assembly, tokens) {
    // [assembly, token, signature] of the methods that tokens in the code of
    // ``assembly`` name (MethodDef, MemberRef, MethodSpec); null for one Mono
    // cannot resolve. A generic instance keeps its definition's token and
    // names its type arguments in the signature (see resolveMethod).
    const c = catalogApi();
    return onMonoThread(() => {
      const image = imageNamed(c, assembly);
      return tokens.map(token => {
        const method = api.getMethod(image, token, NULL);
        if (method.isNull())
          return null;
        const owner = api.classImage(api.methodClass(method));
        return [api.imageName(owner).readUtf8String(), api.methodToken(method),
                signatureName(method) || ''];
      });
    });
  }

  refresh();
  return {
    id: 'mono',
    refresh, locate, name, describe, identify, compile, assemblies, classes, methodsByToken,
    methodBodies, resolveTokens,
    // JIT code lies in no module file: nothing for the code explorer
    nativeSymbols: () => [],
    checkAll, compileAll, describeAll, cancelBatches, batchesRunning, prepare,
    // compileAll changes nothing in the game that would need undoing
    releaseCode() {},
    objectClassName, classOf, layout, staticData, staticAreas: classStaticAreas, staticBase,
    onRuntimeThread: onMonoThread, unityName,
    onMainThread, drainMain, properties, getProperty, methods, signatureOf, findClass,
    findMethod, findObject, findObjects, browseClasses, browseInstances, newString, invoke, classChain, writeReference, resolve: resolveMethod,
    virtualMethod: (object, method) => api.invoke.virtualMethod(object, method)
  };
})();

registerEngine(mono);
