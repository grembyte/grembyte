const unreal = (() => {
  // The game's executable (<Project>-<Platform>-<Configuration>), or the
  // CoreUObject library of a modular build.
  const RUNTIME_NAME = new RegExp('-(Win64|Win32|WinGDK|Linux|LinuxArm64)-' +
    '(Shipping|Test|Development|DebugGame)(\\.exe)?$|^(UE4|UE5|UnrealEditor)-CoreUObject', 'i');
  const SEARCH_INTERVAL = 2000;
  // How long a snapshot of the object table serves before it is read anew
  const SNAPSHOT_AGE = 15000;
  const TWO32 = 4294967296;
  let module = null;
  let lastSearch = -Infinity;
  let ue = null;
  let snapshot = null;
  // package group -> its classes (see packageIndex)
  let packages = null;

  function refresh() {
    // Cheap: only the module's name. The object table is looked for when a
    // feature first needs it (see reflection).
    if (module !== null)
      return true;
    const now = Date.now();
    if (now - lastSearch < SEARCH_INTERVAL)
      return false;
    lastSearch = now;
    module = Process.enumerateModules().find(candidate => RUNTIME_NAME.test(candidate.name))
      || null;
    return module !== null;
  }

  // --- Finding the name pool and the object table ---------------------------
  // Both are globals in the module's data (FNamePool, GUObjectArray), found by
  // their shape rather than by code signatures, so game updates rarely matter.

  function u64(view, at) {
    return view.getUint32(at, true) + view.getUint32(at + 4, true) * TWO32;
  }

  function dataRanges() {
    // The module's writable data. On Linux most of .bss is an anonymous
    // mapping right after the module, which Frida does not count to it.
    const own = module.enumerateRanges('rw-');
    const ends = new Set(own.map(range => range.base.add(range.size).toString()));
    const following = Process.enumerateRanges('rw-').filter(range =>
      range.file === undefined && ends.has(range.base.toString()));
    const ranges = [];
    for (const range of own.concat(following)) {
      try {
        ranges.push({ base: range.base, view: new DataView(range.base.readByteArray(range.size)) });
      } catch (_error) {
        // a guard page or the like
      }
    }
    return ranges;
  }

  function latin1(pointer, length) {
    return String.fromCharCode(...new Uint8Array(pointer.readByteArray(length)));
  }

  function entryText(entry) {
    // An FNameEntry: a 16-bit header (wide flag, probe hash, length) and the text.
    const header = entry.readU16();
    const length = header >> 6;
    return (header & 1) ? entry.add(2).readUtf16String(length) : latin1(entry.add(2), length);
  }

  // FNamePool: a lock, CurrentBlock, CurrentByteCursor, Blocks[8192]. The
  // first block starts with "None" and "ByteProperty".
  const MAX_BLOCKS = 8192;
  const BLOCK_BYTES = 0x20000;

  function findNamePool(ranges) {
    for (const { base, view } of ranges) {
      for (let at = 8; at + 16 <= view.byteLength; at += 8) {
        const current = view.getUint32(at - 8, true);
        if (current >= MAX_BLOCKS || view.getUint32(at - 4, true) > BLOCK_BYTES
            || u64(view, at) === 0)
          continue;
        const last = at + 8 * current;
        if (last + 8 > view.byteLength || u64(view, last) === 0
            || (last + 16 <= view.byteLength && u64(view, last + 8) !== 0))
          continue;
        try {
          const first = ptr(u64(view, at));
          if (entryText(first) === 'None' && entryText(first.add(6)) === 'ByteProperty')
            return base.add(at);
        } catch (_error) {
          // not readable: no pool
        }
      }
    }
    throw hermesError('error.unreal.no_name_pool');
  }

  // FChunkedFixedUObjectArray: Objects, PreAllocatedObjects, MaxElements,
  // NumElements, MaxChunks, NumChunks. Chunks hold 65536 FUObjectItems, whose
  // size differs between versions and games (Object pointer first).
  const CHUNK = 0x10000;
  const ITEM_SIZES = [0x18, 0x10, 0x14, 0x20, 0x28];
  // UObjectBase: vtable, ObjectFlags, InternalIndex, ClassPrivate, NamePrivate, OuterPrivate
  const OBJECT_FLAGS = 0x08, OBJECT_INDEX = 0x0c, OBJECT_CLASS = 0x10, OBJECT_NAME = 0x18,
    OBJECT_OUTER = 0x20;

  function findObjectArray(ranges) {
    for (const { base, view } of ranges) {
      for (let at = 0; at + 0x20 <= view.byteLength; at += 4) {
        const max = view.getInt32(at + 0x10, true), count = view.getInt32(at + 0x14, true);
        if (count <= 0x1000 || max < count || max > 0x4000000
            || view.getInt32(at + 0x18, true) !== Math.ceil(max / CHUNK)
            || view.getInt32(at + 0x1c, true) !== Math.ceil(count / CHUNK)
            || u64(view, at) === 0)
          continue;
        const table = base.add(at);
        const itemSize = objectItemSize(table.readPointer());
        if (itemSize !== null)
          return { table, itemSize };
      }
    }
    throw hermesError('error.unreal.no_object_array');
  }

  function objectItemSize(chunks) {
    // The item size for which the objects know their own index.
    const items = chunks.readPointer();
    let best = null;
    for (const size of ITEM_SIZES) {
      let found = 0;
      for (let i = 1; i <= 32; i++) {
        try {
          const object = items.add(i * size).readPointer();
          if (!object.isNull() && object.add(OBJECT_INDEX).readS32() === i)
            found++;
        } catch (_error) {
          // not this size
        }
      }
      if (found >= 16 && (best === null || found > best.found))
        best = { size, found };
    }
    return best === null ? null : best.size;
  }

  // --- Names and objects --------------------------------------------------------

  const names = new Map();

  function nameText(index, number) {
    let text = names.get(index);
    if (text === undefined) {
      const block = ue.names.add(8 * (index >>> 16)).readPointer();
      text = entryText(block.add((index & 0xffff) * 2));
      names.set(index, text);
    }
    return number > 0 ? `${text}_${number - 1}` : text;
  }

  function fieldName(pointer) {
    return nameText(pointer.readU32(), pointer.add(4).readU32());
  }

  function readSnapshot() {
    // Every live object: address, class, name, outer (numbers, so that maps
    // can key by them). Read in one go per object; an object freed meanwhile
    // is skipped.
    const table = ue.objects.table;
    const size = ue.objects.itemSize;
    const chunks = table.readPointer();
    const count = table.add(0x14).readS32();
    const s = { address: [], klass: [], name: [], number: [], outer: [], index: new Map(),
                read: Date.now() };
    for (let chunk = 0; chunk * CHUNK < count; chunk++) {
      const inChunk = Math.min(CHUNK, count - chunk * CHUNK);
      const items = chunks.add(8 * chunk).readPointer();
      if (items.isNull())
        continue;
      const view = new DataView(items.readByteArray(inChunk * size));
      for (let i = 0; i < inChunk; i++) {
        const address = u64(view, i * size);
        if (address === 0)
          continue;
        let object;
        try {
          object = new DataView(ptr(address).readByteArray(OBJECT_OUTER + 8));
        } catch (_error) {
          continue;
        }
        s.index.set(address, s.address.length);
        s.address.push(address);
        s.klass.push(u64(object, OBJECT_CLASS));
        s.name.push(object.getUint32(OBJECT_NAME, true));
        s.number.push(object.getUint32(OBJECT_NAME + 4, true));
        s.outer.push(u64(object, OBJECT_OUTER));
      }
    }
    return s;
  }

  function objects() {
    if (snapshot === null || Date.now() - snapshot.read > SNAPSHOT_AGE) {
      snapshot = readSnapshot();
      packages = null;
    }
    return snapshot;
  }

  function objectName(s, i) {
    return nameText(s.name[i], s.number[i]);
  }

  function nameOfAddress(address) {
    const s = objects();
    const i = s.index.get(address);
    return i === undefined ? null : objectName(s, i);
  }

  // --- Layout: where the reflection structures keep their fields -----------------
  // Found on objects every game has (CoreUObject's Class, Object, Vector,
  // Transform), since engine versions and build settings move them.

  function reflection() {
    if (ue !== null && ue.layout !== undefined)
      return ue;
    if (!refresh())
      throw hermesError('error.unreal.no_game');
    const ranges = dataRanges();
    ue = { names: findNamePool(ranges), objects: findObjectArray(ranges) };
    ue.layout = findLayout();
    return ue;
  }

  function findLayout() {
    const s = objects();
    const core = s.address.find((address, i) => s.outer[i] === 0
                                && objectName(s, i) === '/Script/CoreUObject');
    if (core === undefined)
      throw hermesError('error.unreal.no_core_package');
    const inCore = new Map();
    s.address.forEach((address, i) => {
      if (s.outer[i] === core)
        inCore.set(`${objectName(s, i)}|${s.klass[i]}`, address);
    });
    const classClass = s.address.find((address, i) =>
      s.outer[i] === core && s.klass[i] === address && objectName(s, i) === 'Class');
    if (classClass === undefined)
      throw hermesError('error.unreal.no_core_class', { name: 'Class' });
    const coreClass = name => {
      const found = inCore.get(`${name}|${classClass}`);
      if (found === undefined)
        throw hermesError('error.unreal.no_core_class', { name });
      return found;
    };
    const struct = coreClass('Struct'), functionClass = coreClass('Function');
    const objectClass = coreClass('Object'), scriptStruct = coreClass('ScriptStruct');
    const coreStruct = name => {
      const found = inCore.get(`${name}|${scriptStruct}`);
      if (found === undefined)
        throw hermesError('error.unreal.no_core_struct', { name });
      return ptr(found);
    };
    const L = { classClass, functionClass, structClass: struct, scriptStruct, packageClass:
                coreClass('Package'), enumClass: coreClass('Enum') };
    const pointerAt = (base, offset) => u64(new DataView(ptr(base).add(offset).readByteArray(8)), 0);
    const search = (from, to, step, test, what) => {
      for (let at = from; at < to; at += step) {
        try {
          if (test(at))
            return at;
        } catch (_error) {
          // not there
        }
      }
      throw hermesError('error.unreal.unknown_layout', { what });
    };
    L.next = 0x28;  // UField::Next, right after UObjectBase
    L.superStruct = search(0x30, 0x90, 8, at => pointerAt(classClass, at) === struct,
                           'UStruct::SuperStruct');
    L.children = search(L.superStruct + 8, L.superStruct + 0x40, 8, at => {
      const child = s.index.get(pointerAt(objectClass, at));
      return child !== undefined && s.klass[child] === functionClass;
    }, 'UStruct::Children');

    // FField: vtable, FFieldClass, owner, Next, NamePrivate, FlagsPrivate;
    // FProperty: ArrayDim, ElementSize, PropertyFlags, ..., Offset_Internal
    const vector = coreStruct('Vector');
    L.fieldClass = 8;
    const named = (field, offset, text) => !field.isNull() && fieldName(field.add(offset)) === text;
    for (let at = L.children + 8; at < L.children + 0x30 && L.childProperties === undefined;
      at += 8) {
      const first = vector.add(at).readPointer();
      for (const offset of [0x20, 0x28, 0x18, 0x30]) {
        try {
          if (named(first, offset, 'X')) {
            L.childProperties = at;
            L.fieldName = offset;
            break;
          }
        } catch (_error) {
          // not a field
        }
      }
    }
    if (L.childProperties === undefined)
      throw hermesError('error.unreal.unknown_layout', { what: 'UStruct::ChildProperties' });
    const x = vector.add(L.childProperties).readPointer();
    L.fieldNext = search(0x10, L.fieldName, 8,
      at => named(x.add(at).readPointer(), L.fieldName, 'Y'), 'FField::Next');
    const y = x.add(L.fieldNext).readPointer();
    const z = y.add(L.fieldNext).readPointer();
    L.arrayDim = search(L.fieldName + 8, L.fieldName + 0x30, 4, at =>
      x.add(at).readS32() === 1 && [4, 8].includes(x.add(at + 4).readS32())
      && y.add(at).readS32() === 1 && y.add(at + 4).readS32() === x.add(at + 4).readS32(),
    'FProperty::ArrayDim');
    L.elementSize = L.arrayDim + 4;
    L.propertyFlags = L.arrayDim + 8;
    const component = x.add(L.elementSize).readS32();
    L.offset = search(L.propertyFlags + 8, L.propertyFlags + 0x20, 4, at =>
      x.add(at).readS32() === 0 && y.add(at).readS32() === component
      && z.add(at).readS32() === 2 * component, 'FProperty::Offset_Internal');
    L.propertiesSize = search(L.childProperties + 8, L.childProperties + 0x20, 4,
      at => vector.add(at).readS32() === 3 * component, 'UStruct::PropertiesSize');
    L.script = L.propertiesSize + 8;  // after MinAlignment: TArray<uint8> of bytecode

    // What a property type adds to FProperty starts at its end: found as the
    // struct of FTransform's Rotation (an FQuat).
    const transform = coreStruct('Transform');
    const quat = coreStruct('Quat');
    let rotation = transform.add(L.childProperties).readPointer();
    while (!rotation.isNull() && fieldName(rotation.add(L.fieldName)) !== 'Rotation')
      rotation = rotation.add(L.fieldNext).readPointer();
    if (rotation.isNull())
      throw hermesError('error.unreal.no_rotation');
    L.extra = search((L.offset + 4 + 7) & ~7, L.offset + 0x60, 8,
      at => rotation.add(at).readPointer().equals(quat), 'the end of FProperty');

    // UFunction: FunctionFlags, NumParms, ParmsSize, ReturnValueOffset, ...
    // Func. UObject's ExecuteUbergraph(int32) is the sample: 1 parameter, 4
    // bytes, no return value. It runs Blueprint code: its Func is the
    // script interpreter (UObject::ProcessInternal).
    const ubergraph = ptr(objectClass).add(L.children).readPointer();
    if (ubergraph.isNull() || nameOfAddress(Number(ubergraph.toString())) !== 'ExecuteUbergraph')
      throw hermesError('error.unreal.no_execute_ubergraph');
    L.functionFlags = search(L.propertiesSize, 0x180, 4, at =>
      ubergraph.add(at + 4).readU8() === 1 && ubergraph.add(at + 6).readU16() === 4
      && ubergraph.add(at + 8).readU16() === 0xffff, 'UFunction::FunctionFlags');
    const inModule = address => {
      if (address.isNull())
        return false;
      const owner = Process.findModuleByAddress(address);
      return owner !== null && owner.name === module.name;
    };
    L.func = search(L.functionFlags + 0x10, L.functionFlags + 0x48, 8,
      at => inModule(ubergraph.add(at).readPointer()), 'UFunction::Func');
    L.processInternal = ubergraph.add(L.func).readPointer();
    return L;
  }

  // --- Types of properties ----------------------------------------------------------

  const CPF_PARM = 0x80, CPF_OUT_PARM = 0x100, CPF_RETURN_PARM = 0x400,
    CPF_REFERENCE_PARM = 0x8000000;
  const prefixes = new Map();

  function superOf(klass) {
    return Number(ptr(klass).add(ue.layout.superStruct).readPointer().toString());
  }

  function cppName(klass) {
    // UE's C++ name of a class: AActor for actors, UObject for the rest.
    if (klass === 0)
      return '?';
    let prefix = prefixes.get(klass);
    if (prefix === undefined) {
      prefix = 'U';
      for (let current = klass; current !== 0; current = superOf(current)) {
        if (nameOfAddress(current) === 'Actor') {
          prefix = 'A';
          break;
        }
      }
      prefixes.set(klass, prefix);
    }
    return prefix + nameOfAddress(klass);
  }

  function pointerField(field, offset) {
    return Number(field.add(offset).readPointer().toString());
  }

  function arrayInner(field) {
    // FArrayProperty's Inner; UE5 put ArrayFlags before it.
    const at = field.add(ue.layout.extra);
    const first = at.readPointer();
    return !first.isNull() && isProperty(first) ? first : at.add(Process.pointerSize).readPointer();
  }

  function propertyType(field) {
    // The C++ type of an FProperty, e.g. TArray<FVector> or AActor*.
    const L = ue.layout;
    const kind = fieldName(field.add(L.fieldClass).readPointer());
    const extra = offset => pointerField(field, L.extra + offset);
    const inner = offset => {
      const property = field.add(L.extra + offset).readPointer();
      return property.isNull() ? '?' : propertyType(property);
    };
    switch (kind) {
      case 'BoolProperty': return 'bool';
      case 'Int8Property': return 'int8';
      case 'Int16Property': return 'int16';
      case 'IntProperty': return 'int32';
      case 'Int64Property': return 'int64';
      case 'UInt16Property': return 'uint16';
      case 'UInt32Property': return 'uint32';
      case 'UInt64Property': return 'uint64';
      case 'FloatProperty': return 'float';
      case 'DoubleProperty': return 'double';
      case 'StrProperty': return 'FString';
      case 'NameProperty': return 'FName';
      case 'TextProperty': return 'FText';
      case 'ByteProperty': return extra(0) === 0 ? 'uint8' : nameOfAddress(extra(0));
      case 'EnumProperty': return nameOfAddress(extra(8)) || 'enum';
      case 'StructProperty': return `F${nameOfAddress(extra(0))}`;
      case 'ObjectProperty':
      case 'ObjectPtrProperty': return `${cppName(extra(0))}*`;
      case 'ClassProperty':
      case 'ClassPtrProperty': return `TSubclassOf<${cppName(extra(8))}>`;
      case 'WeakObjectProperty': return `TWeakObjectPtr<${cppName(extra(0))}>`;
      case 'LazyObjectProperty': return `TLazyObjectPtr<${cppName(extra(0))}>`;
      case 'SoftObjectProperty': return `TSoftObjectPtr<${cppName(extra(0))}>`;
      case 'SoftClassProperty': return `TSoftClassPtr<${cppName(extra(8))}>`;
      case 'InterfaceProperty': return `TScriptInterface<I${nameOfAddress(extra(0))}>`;
      case 'ArrayProperty': {
        const element = arrayInner(field);
        return `TArray<${element.isNull() ? '?' : propertyType(element)}>`;
      }
      case 'SetProperty': return `TSet<${inner(0)}>`;
      case 'MapProperty': return `TMap<${inner(0)}, ${inner(8)}>`;
      case 'DelegateProperty':
      case 'MulticastInlineDelegateProperty':
      case 'MulticastSparseDelegateProperty':
        return (nameOfAddress(extra(0)) || kind).replace(/__DelegateSignature$/, '');
      default: return kind.replace(/Property$/, '');
    }
  }

  function parameters(fn) {
    // [{ name, type, out, ret }] of a UFunction's parameters, in order.
    const L = ue.layout;
    const result = [];
    for (let field = ptr(fn).add(L.childProperties).readPointer(); !field.isNull();
      field = field.add(L.fieldNext).readPointer()) {
      const flags = field.add(L.propertyFlags).readU32();
      if (!(flags & CPF_PARM))
        continue;
      result.push({
        name: fieldName(field.add(L.fieldName)),
        type: propertyType(field),
        out: (flags & CPF_OUT_PARM) !== 0 && !(flags & CPF_RETURN_PARM),
        ref: (flags & CPF_REFERENCE_PARM) !== 0,
        ret: (flags & CPF_RETURN_PARM) !== 0
      });
    }
    return result;
  }

  // --- Packages: what the catalog calls assemblies --------------------------------
  // The functions of the native classes of one C++ module are in /Script/<Module>.
  // Each Blueprint lives in a package of its own (/Game/Folder/.../BP_Name);
  // they are grouped by their first folder (/Game/Folder).

  const FUNC_NATIVE = 0x400, FUNC_STATIC = 0x2000, FUNC_DELEGATE = 0x100000;
  // ECMA-335's abstract flag, which the trace understands: no code to hook
  const METHOD_ABSTRACT = 0x0400;

  function packageGroup(name) {
    if (name.startsWith('/Script/'))
      return name;
    const parts = name.split('/');
    return parts.length > 3 ? parts.slice(0, 3).join('/') : parts.slice(0, -1).join('/') || name;
  }

  function isClass(s, klass, cache) {
    // Whether an object of class ``klass`` is a class (Class, a Blueprint's
    // generated class, ...): its class derives from Class.
    let result = cache.get(klass);
    if (result === undefined) {
      result = false;
      for (let current = klass; current !== 0; current = superOf(current)) {
        if (current === ue.layout.classClass) {
          result = true;
          break;
        }
      }
      cache.set(klass, result);
    }
    return result;
  }

  function packageIndex() {
    // { group: { classes: [{ address, name, package }], packages: Set } }
    reflection();
    const s = objects();
    if (packages !== null)
      return packages;
    const classKinds = new Map();
    packages = new Map();
    for (let i = 0; i < s.address.length; i++) {
      const outer = s.outer[i];
      if (outer === 0 || !isClass(s, s.klass[i], classKinds))
        continue;
      const owner = s.index.get(outer);
      if (owner === undefined || s.outer[owner] !== 0)
        continue;  // a class inside another object
      const packageName = objectName(s, owner);
      const group = packageGroup(packageName);
      let entry = packages.get(group);
      if (entry === undefined) {
        entry = { classes: [], packages: new Set() };
        packages.set(group, entry);
      }
      entry.packages.add(packageName);
      entry.classes.push({ address: s.address[i], name: objectName(s, i), package: packageName });
    }
    return packages;
  }

  function functionsOf(klass) {
    // The UFunctions a class declares (or overrides), in declaration order.
    const L = ue.layout;
    const s = objects();
    const result = [];
    for (let field = ptr(klass).add(L.children).readPointer(); !field.isNull();
      field = field.add(L.next).readPointer()) {
      const i = s.index.get(Number(field.toString()));
      if (i !== undefined)
        result.push({ address: field, name: objectName(s, i) });
    }
    return result;
  }

  function fnv(text, hash = 0x811c9dc5) {
    for (let i = 0; i < text.length; i++)
      hash = Math.imul(hash ^ text.charCodeAt(i), 0x01000193) >>> 0;
    return hash;
  }

  function token(entry, fn) {
    // Stable over restarts: object indexes are not.
    return fnv(`${entry.package}.${entry.name}:${fn.name}`) & 0x7fffffff;
  }

  function groupNamed(assembly) {
    const group = packageIndex().get(assembly);
    if (group === undefined)
      throw hermesError('error.unreal.package_not_loaded', { package: assembly });
    return group;
  }

  function assemblies() {
    const result = [];
    for (const [name, group] of packageIndex()) {
      // The build id of the catalog cache: what the group declares
      let hash = fnv(`${module.name}|${module.size}`);
      let functions = 0;
      for (const klass of group.classes) {
        for (const fn of functionsOf(klass.address)) {
          hash = fnv(`${klass.package}.${klass.name}:${fn.name};`, hash);
          functions++;
        }
      }
      if (functions > 0)
        result.push({ name, file: group.packages.values().next().value,
                      classes: group.classes.length, mvid: `${hash.toString(16)}-${functions}` });
    }
    return result;
  }

  function functionInfo(fn) {
    // { flags, code, script } of a UFunction: its FunctionFlags, Func, and
    // the size of its Blueprint bytecode.
    const L = ue.layout;
    return {
      flags: fn.add(L.functionFlags).readU32(),
      code: fn.add(L.func).readPointer(),
      script: fn.add(L.script + Process.pointerSize).readS32()
    };
  }

  function methodEntry(klass, fn) {
    // [name, parameters, return type, static, token, full signature, flags,
    // implementation flags] as the Mono adapter has them; flags only say
    // which functions have no code (METHOD_ABSTRACT).
    const params = parameters(fn.address);
    const ret = params.find(param => param.ret);
    const types = params.filter(param => !param.ret)
      .map(param => `${param.type}${param.out || param.ref ? '&' : ''}`).join(',');
    const info = functionInfo(fn.address);
    const codeless = info.code.isNull() || (info.flags & FUNC_DELEGATE) !== 0
      || (!(info.flags & FUNC_NATIVE) && info.script <= 0);
    return [
      fn.name, `(${types})`, ret === undefined ? 'void' : ret.type,
      (info.flags & FUNC_STATIC) !== 0, token(klass, fn),
      `${klass.name}:${fn.name} (${types})`, codeless ? METHOD_ABSTRACT : 0, 0
    ];
  }

  function classes(assembly) {
    // [[class name, [method entry, ...]], ...] of a package group
    const result = [];
    for (const klass of groupNamed(assembly).classes) {
      const methods = [];
      for (const fn of functionsOf(klass.address)) {
        try {
          methods.push(methodEntry(klass, fn));
        } catch (_error) {
          // freed meanwhile (a Blueprint unloaded)
        }
      }
      result.push([klass.name, methods]);
    }
    return result;
  }

  function methodsByToken(assembly, tokens) {
    // [{ className, method, entry, flags, implFlags }] for the trace; method
    // is the UFunction.
    const wanted = new Set(tokens);
    const result = [];
    for (const klass of groupNamed(assembly).classes) {
      try {
        for (const fn of functionsOf(klass.address)) {
          if (!wanted.has(token(klass, fn)))
            continue;
          const entry = methodEntry(klass, fn);
          result.push({ className: klass.name, method: fn.address, entry: entry.slice(0, 6),
                        flags: entry[6], implFlags: entry[7] });
        }
      } catch (_error) {
        // freed meanwhile (a Blueprint unloaded, e.g. while the game loads)
      }
    }
    return result;
  }

  // --- Code of functions -----------------------------------------------------------
  // A native UFunction's Func is its exec thunk (execName), called when a
  // Blueprint or ProcessEvent calls the function. Every Blueprint function
  // has the script interpreter as Func, so its calls cannot be told apart
  // there: it gets a code stub of its own (nops and a jump to the
  // interpreter) as Func, which can be hooked. Blueprint code calling another
  // Blueprint function directly does not pass Func, so those calls are not
  // seen, the ones through ProcessEvent (events, delegates, native callers) are.

  const STUB_SIZE = 32;
  const STUB_NOPS = 16;
  const STUBS_PER_PAGE = 128;
  const stubs = new Map();
  const redirected = new Map();
  let stubPage = null;
  let stubsLeft = 0;

  function newStub(target) {
    if (stubsLeft === 0) {
      stubPage = allocateCode(STUB_SIZE * STUBS_PER_PAGE);
      stubsLeft = STUBS_PER_PAGE;
    }
    const stub = stubPage;
    stubPage = stubPage.add(STUB_SIZE);
    stubsLeft--;
    Memory.patchCode(stub, STUB_SIZE, code => {
      const nops = new Uint8Array(STUB_NOPS).fill(0x90);
      code.writeByteArray(nops.buffer);
      // jmp [rip+0]; .quad target
      code.add(STUB_NOPS).writeByteArray([0xff, 0x25, 0, 0, 0, 0]);
      code.add(STUB_NOPS + 6).writePointer(target);
    });
    return stub;
  }

  function scriptCode(fn, code) {
    // The function's own stub, made its Func.
    const key = fn.toString();
    let stub = stubs.get(key);
    if (stub === undefined) {
      if (Process.arch !== 'x64')
        return null;
      stub = newStub(code);
      stubs.set(key, stub);
    }
    if (!redirected.has(key)) {
      redirected.set(key, { fn, code });
      fn.add(ue.layout.func).writePointer(stub);
    }
    return stub;
  }

  function releaseCode(start) {
    // Give Blueprint functions their Func back: all of them, or the one whose
    // stub is ``start``. The stubs stay, a thread may still be in one. A Func
    // another agent has redirected since (to a stub that jumps to ours) stays.
    for (const [key, { fn, code }] of Array.from(redirected)) {
      const stub = stubs.get(key);
      if (start !== undefined && !stub.equals(start))
        continue;
      redirected.delete(key);
      try {
        const func = fn.add(ue.layout.func);
        if (func.readPointer().equals(stub))
          func.writePointer(code);
      } catch (_error) {
        // freed meanwhile
      }
    }
  }

  function codeOf(fn) {
    // { start, size } to hook for a UFunction, or null.
    const info = functionInfo(fn);
    if (info.code.isNull() || (info.flags & FUNC_DELEGATE))
      return null;
    if (info.flags & FUNC_NATIVE)
      return { start: info.code, size: 1 };
    if (info.script <= 0)
      return null;
    const stub = scriptCode(fn, info.code);
    return stub === null ? null : { start: stub, size: STUB_SIZE };
  }

  function compileAll(methods, progress) {
    reflection();
    const result = methods.map(fn => {
      try {
        return codeOf(fn);
      } catch (_error) {
        return null;
      }
    });
    if (progress)
      progress(methods.length);
    return result;
  }

  function resolve(identity) {
    // The UFunction of an identity (see identify). The objects are read anew
    // if it is not found: its class may have been loaded since the snapshot
    // (hooks are restored while the game is still loading).
    let found = [];
    try {
      found = methodsByToken(identity.image, [identity.token]);
    } catch (_error) {
      // package not loaded in the snapshot
    }
    if (found.length === 0) {
      snapshot = null;
      found = methodsByToken(identity.image, [identity.token]);
    }
    if (found.length === 0)
      throw hermesError('error.unreal.method_missing',
          { method: identity.method, image: identity.image });
    return found[0].method;
  }

  function compile(identity) {
    const code = codeOf(resolve(identity));
    if (code === null)
      throw hermesError('error.unreal.no_code', { method: identity.method });
    return code;
  }

  // --- Values: what the method monitor and the object inspector read ------------
  // Their types are the monitor's (ECMA-335 element types, see engine.TYPE) where a
  // property holds a number, an object, or a struct; what has no such type
  // (FString, FName, TArray, bitfield bools, weak pointers ...) carries
  // ``engine`` and is read by readValue.

  const KIND = {
    VOID: 0x01, BOOLEAN: 0x02, I1: 0x04, U1: 0x05, I2: 0x06, U2: 0x07, I4: 0x08, U4: 0x09,
    I8: 0x0a, U8: 0x0b, R4: 0x0c, R8: 0x0d, VALUETYPE: 0x11, CLASS: 0x12
  };
  const NUMBER_KINDS = {
    Int8Property: KIND.I1, Int16Property: KIND.I2, IntProperty: KIND.I4,
    Int64Property: KIND.I8, UInt16Property: KIND.U2, UInt32Property: KIND.U4,
    UInt64Property: KIND.U8, FloatProperty: KIND.R4, DoubleProperty: KIND.R8
  };
  const OBJECT_KINDS = new Set(['ObjectProperty', 'ObjectPtrProperty', 'ClassProperty',
                                'ClassPtrProperty']);
  const WEAK_KINDS = new Set(['WeakObjectProperty', 'LazyObjectProperty', 'SoftObjectProperty',
                              'SoftClassProperty']);
  const MAX_TEXT = 120;
  const MAX_ELEMENTS = 8;

  // UEnum after UField: FString CppType, then Names, a TArray of
  // TPair<FName, int64> ("EPrimaryAttribute::Might", 0).
  const ENUM_PAIR_SIZE = 16;
  const MAX_ENUM_VALUES = 1024;
  const enumCache = new Map();

  function enumValues(uenum) {
    // { "0": "Might", ... } of a UEnum (cached), or null; without the _MAX
    // entry UHT adds at the end.
    const key = uenum.toString();
    if (enumCache.has(key))
      return enumCache.get(key);
    let result = null;
    try {
      const names = uenum.add(ue.layout.next + Process.pointerSize + 16);
      const data = names.readPointer();
      const count = names.add(8).readS32();
      if (count > 0 && count <= MAX_ENUM_VALUES && count <= names.add(12).readS32()) {
        result = {};
        for (let i = 0; i < count; i++) {
          const pair = data.add(i * ENUM_PAIR_SIZE);
          const name = fieldName(pair).replace(/^.*::/, '');
          if (i === count - 1 && /(^|_)MAX$/.test(name))
            break;
          result[pair.add(8).readS64().toString()] = name;
        }
      }
    } catch (_error) {
      result = null;
    }
    enumCache.set(key, result);
    return result;
  }

  function valueType(field, byref = false) {
    // The monitor's type of an FProperty: { kind, byref, name, ... }.
    const L = ue.layout;
    const kind = fieldName(field.add(L.fieldClass).readPointer());
    const type = { kind: 0, byref, name: propertyType(field) };
    const extra = offset => field.add(L.extra + offset);
    if (NUMBER_KINDS[kind] !== undefined) {
      type.kind = NUMBER_KINDS[kind];
    } else if (kind === 'ByteProperty') {
      type.kind = KIND.U1;
      const uenum = extra(0).readPointer();
      if (!uenum.isNull()) {
        type.enumName = type.name;
        type.enumValues = enumValues(uenum);
      }
    } else if (kind === 'EnumProperty') {
      const underlying = extra(0).readPointer();
      type.kind = underlying.isNull() ? KIND.U1 : valueType(underlying).kind;
      type.enumName = type.name;
      const uenum = extra(8).readPointer();
      if (!uenum.isNull())
        type.enumValues = enumValues(uenum);
    } else if (kind === 'BoolProperty') {
      // A bitfield (FieldMask) in the byte at ByteOffset; true sets ByteMask
      type.engine = 'bool';
      type.byteOffset = extra(1).readU8();
      type.byteMask = extra(2).readU8();
      type.mask = extra(3).readU8();
    } else if (OBJECT_KINDS.has(kind)) {
      type.kind = KIND.CLASS;
    } else if (kind === 'StructProperty') {
      type.kind = KIND.VALUETYPE;
      type.klass = extra(0).readPointer();
      type.structSize = field.add(L.elementSize).readS32();
    } else if (WEAK_KINDS.has(kind)) {
      type.engine = 'weak';
    } else if (kind === 'InterfaceProperty') {
      type.engine = 'interface';
    } else if (kind === 'StrProperty') {
      type.engine = 'string';
    } else if (kind === 'NameProperty') {
      type.engine = 'name';
    } else if (kind === 'TextProperty') {
      type.engine = 'text';
    } else if (kind === 'ArrayProperty') {
      type.engine = 'array';
      type.property = field;  // the inspector opens it (see elements)
      const inner = arrayInner(field);
      type.element = inner.isNull() ? null : valueType(inner);
      type.elementSize = inner.isNull() ? 0 : inner.add(L.elementSize).readS32();
    } else if (kind === 'MapProperty' || kind === 'SetProperty') {
      // KeyProp and ValueProp (a set: ElementProp), then the layout of the elements
      const map = kind === 'MapProperty';
      const key = extra(0).readPointer();
      const value = map ? extra(8).readPointer() : NULL;
      type.engine = map ? 'map' : 'set';
      type.property = field;
      type.element = key.isNull() ? null : valueType(key);
      type.elementSize = key.isNull() ? 0 : key.add(L.elementSize).readS32();
      if (map) {
        type.value = value.isNull() ? null : valueType(value);
        type.valueSize = value.isNull() ? 0 : value.add(L.elementSize).readS32();
      }
    } else {
      type.engine = 'other';  // delegates: the type only
    }
    return type;
  }

  function isObject(address) {
    // Whether a live UObject is at ``address``: the object table's item at
    // its index points back at it.
    const index = address.add(OBJECT_INDEX).readS32();
    const table = ue.objects.table;
    if (index < 0 || index >= table.add(0x14).readS32())
      return false;
    const items = table.readPointer().add(8 * Math.floor(index / CHUNK)).readPointer();
    return items.add((index % CHUNK) * ue.objects.itemSize).readPointer().equals(address);
  }

  function objectAt(index, serial) {
    // The object of a weak pointer (FWeakObjectPtr: index and serial number)
    const table = ue.objects.table;
    if (index < 0 || index >= table.add(0x14).readS32())
      return NULL;
    const item = table.readPointer().add(8 * Math.floor(index / CHUNK)).readPointer()
      .add((index % CHUNK) * ue.objects.itemSize);
    // FUObjectItem: Object, Flags, ClusterRootIndex, SerialNumber
    return item.add(Process.pointerSize + 8).readS32() === serial ? item.readPointer() : NULL;
  }

  function classOf(object) {
    // The class of a UObject, or null if ``object`` is none (any more).
    try {
      reflection();
      return isObject(object) ? object.add(OBJECT_CLASS).readPointer() : null;
    } catch (_error) {
      return null;
    }
  }

  function uobjectName(object) {
    // The FName of a UObject (e.g. BP_Player_C_0), for the monitor's texts.
    try {
      return isObject(object) ? fieldName(object.add(OBJECT_NAME)) : null;
    } catch (_error) {
      return null;
    }
  }

  function objectText(object) {
    if (object.isNull())
      return 'null';
    const klass = classOf(object);
    return klass === null ? `${object} (no object)`
      : `${cppName(Number(klass.toString()))} "${uobjectName(object)}"@${object}`;
  }

  function numberText(type, address) {
    switch (type.kind) {
      case KIND.BOOLEAN: return String(address.readU8() !== 0);
      case KIND.I1: return String(address.readS8());
      case KIND.U1: return String(address.readU8());
      case KIND.I2: return String(address.readS16());
      case KIND.U2: return String(address.readU16());
      case KIND.I4: return String(address.readS32());
      case KIND.U4: return String(address.readU32());
      case KIND.I8: return address.readS64().toString();
      case KIND.U8: return address.readU64().toString();
      case KIND.R4: return String(address.readFloat());
      case KIND.R8: return String(address.readDouble());
      default: return null;
    }
  }

  function stringText(data, length) {
    // A logged string value of ``length`` characters at ``data``
    if (data.isNull() || length === 0)
      return { text: '""', string: true };
    const text = data.readUtf16String(Math.min(length, MAX_TEXT));
    return { text: JSON.stringify(text) + (length > MAX_TEXT ? '…' : ''), string: true };
  }

  // FText: TextData, a shared pointer to the text's history. Behind the
  // history's vtable and reference count (UE5):
  // - a generated text (a number, a format): its display string, an FString;
  // - a localized text: its id (namespace, key), the source string, then the
  //   display string as a shared pointer, made by MakeShared: the FString
  //   right behind its reference controller;
  // - a string table's text: a shared FStringTableReferenceData (its weak
  //   self-reference, TableId, Key, ...) with a weak pointer to the
  //   FStringTableEntry: its table (a weak pointer), the source string, in
  //   some versions the display string as a shared pointer.
  const TEXT_HISTORY_START = 0x10;
  const TEXT_HISTORY_END = 0x50;
  const SHARED_CONTROLLER_SIZE = 0x10;
  const TABLE_ENTRY_SOURCE = 0x10;
  const TABLE_ENTRY_DISPLAY = 0x20;
  const MAX_TEXT_LENGTH = 0x100000;

  function fstringAt(string) {
    // [characters, length] of the FString at ``string``, or null if the
    // memory is none.
    const data = string.readPointer();
    const count = string.add(Process.pointerSize).readS32();
    const max = string.add(Process.pointerSize + 4).readS32();
    if (count === 0 && max >= 0)
      return [NULL, 0];
    if (count < 0 || count > max || max > MAX_TEXT_LENGTH || data.isNull()
        || data.add(2 * (count - 1)).readU16() !== 0)
      return null;
    // No 0 before the terminating one
    const length = count - 1;
    const probe = Math.min(length, MAX_TEXT);
    return data.readUtf16String(probe).length === probe ? [data, length] : null;
  }

  function sharedObjectAt(pointer) {
    // The object of the shared or weak pointer at ``pointer`` if MakeShared
    // made it and it is alive (its controller's shared reference count), or null.
    const object = pointer.readPointer();
    const controller = pointer.add(Process.pointerSize).readPointer();
    if (object.isNull() || !object.equals(controller.add(SHARED_CONTROLLER_SIZE)))
      return null;
    return controller.add(Process.pointerSize).readS32() > 0 ? object : null;
  }

  function tableEntryString(reference) {
    // A string table's text by its FStringTableReferenceData (see above)
    for (let at = TEXT_HISTORY_START; at < TEXT_HISTORY_END; at += Process.pointerSize) {
      const entry = sharedObjectAt(reference.add(at));
      if (entry === null)
        continue;
      const display = sharedObjectAt(entry.add(TABLE_ENTRY_DISPLAY));
      const string = display !== null ? fstringAt(display) : null;
      if (string !== null)
        return string;
      const source = fstringAt(entry.add(TABLE_ENTRY_SOURCE));
      if (source !== null)
        return source;
    }
    return null;
  }

  function sharedDisplayString(history) {
    for (let at = TEXT_HISTORY_START; at < TEXT_HISTORY_END; at += Process.pointerSize) {
      const object = sharedObjectAt(history.add(at));
      if (object !== null) {
        const string = fstringAt(object);
        if (string !== null)
          return string;
        const entry = tableEntryString(object);
        if (entry !== null)
          return entry;
      }
    }
    return null;
  }

  function textString(address) {
    // [characters, length] of an FText's display string, or null where the
    // memory does not look like one (an engine version of another layout).
    try {
      const history = address.readPointer();
      if (history.isNull())
        return [NULL, 0];
      const shared = sharedDisplayString(history);
      if (shared !== null)
        return shared;
      // A generated text's, else a localized text's source string; an empty
      // one behind the first may be its id, so only if there is no other
      // (an empty text, FText::GetEmpty)
      let empty = null;
      for (let at = TEXT_HISTORY_START; at < TEXT_HISTORY_END; at += Process.pointerSize) {
        const string = fstringAt(history.add(at));
        if (string !== null && !(at > TEXT_HISTORY_START && string[1] === 0))
          return string;
        if (string !== null)
          empty = string;
      }
      return empty;
    } catch (_error) {
      return null;
    }
  }

  function readValue(type, address) {
    // A value of a type with ``engine``: { text, string? } or { object }.
    switch (type.engine) {
      case 'bool':
        return { text: String((address.add(type.byteOffset).readU8() & type.mask) !== 0) };
      case 'string':
        // FString: TArray<TCHAR> with the terminating 0
        return stringText(address.readPointer(),
          Math.max(0, address.add(Process.pointerSize).readS32() - 1));
      case 'text': {
        const string = textString(address);
        return string === null ? { text: type.name } : stringText(...string);
      }
      case 'name':
        return { text: fieldName(address) };
      case 'weak':
        return { object: objectAt(address.readS32(), address.add(4).readS32()) };
      case 'interface':
        return { object: address.readPointer() };
      case 'array':
        return { text: arrayText(type, address), target: containerTarget(type, address) };
      case 'map':
      case 'set': {
        let count;
        try {
          count = sparseArray(address).count;
        } catch (_error) {
          count = '?';
        }
        return { text: `${type.name.replace(/&$/, '')}[${count}]`,
                 target: containerTarget(type, address) };
      }
      default:
        return { text: type.name };
    }
  }

  function containerTarget(type, address) {
    // What the object inspector opens for a TArray, TMap or TSet (see elements)
    return { address: address.toString(), property: type.property.toString() };
  }

  function valueJs(type, address) {
    // A value of a type with ``engine`` for the scripts of method hooks:
    // { value }, { object } or { elements, element } (a TArray: the
    // addresses of its elements and their type).
    switch (type.engine) {
      case 'bool':
        return { value: (address.add(type.byteOffset).readU8() & type.mask) !== 0 };
      case 'string': {
        const data = address.readPointer();
        const length = Math.max(0, address.add(Process.pointerSize).readS32() - 1);
        return { value: data.isNull() || length === 0 ? '' : data.readUtf16String(length) };
      }
      case 'text': {
        const string = textString(address);
        if (string === null)
          throw hermesError('error.unreal.text_unreadable', { type: type.name });
        const [data, length] = string;
        return { value: data.isNull() || length === 0 ? '' : data.readUtf16String(length) };
      }
      case 'name':
        return { value: fieldName(address) };
      case 'weak':
      case 'interface':
        return readValue(type, address);
      case 'array': {
        if (type.element === null)
          throw hermesError('error.unreal.elements_unknown', { name: type.name });
        const data = address.readPointer();
        const count = data.isNull() ? 0 : Math.max(0, address.add(Process.pointerSize).readS32());
        const elements = [];
        for (let i = 0; i < count; i++)
          elements.push(data.add(i * type.elementSize));
        return { elements, element: type.element };
      }
      default:
        throw hermesError('error.unreal.script_unreadable', { type: type.name });
    }
  }

  function writeValue(type, address, value) {
    // Store a script's value as a type with ``engine``: only bools can be.
    if (type.engine !== 'bool')
      throw hermesError('error.unreal.script_unwritable', { type: type.name });
    const at = address.add(type.byteOffset);
    at.writeU8((at.readU8() & ~type.mask & 0xff) | (value ? type.byteMask : 0));
  }

  function elementText(type, address) {
    if (type.kind === KIND.CLASS)
      return objectText(address.readPointer());
    const number = numberText(type, address);
    if (number !== null)
      return number;
    if (type.engine === undefined)
      return type.name;
    const value = readValue(type, address);
    return value.object !== undefined ? objectText(value.object) : value.text;
  }

  function arrayText(type, address) {
    // TArray<int32>[3] {1, 2, 3}: the first elements of a simple type
    const data = address.readPointer();
    const count = address.add(Process.pointerSize).readS32();
    const name = type.name.replace(/&$/, '');
    if (count <= 0 || data.isNull() || type.element === null)
      return `${name}[${Math.max(count, 0)}]`;
    if (type.element.kind === KIND.VALUETYPE || type.element.engine === 'other')
      return `${name}[${count}]`;
    const shown = [];
    for (let i = 0; i < Math.min(count, MAX_ELEMENTS); i++)
      shown.push(elementText(type.element, data.add(i * type.elementSize)));
    return `${name}[${count}] {${shown.join(', ')}${count > MAX_ELEMENTS ? ', …' : ''}}`;
  }

  // --- Containers: the entries of a TArray, TMap or TSet (the object inspector) ----
  // A TSet (and a TMap, a TSet of key-value pairs) keeps its elements in a
  // TSparseArray: a TArray of slots, then a TBitArray of the slots in use
  // (four uint32 inline, else on the heap; NumBits, MaxBits), FirstFreeIndex
  // and NumFreeIndices. A slot holds the element, then the hash chain's
  // HashNextId and HashIndex (two int32).

  const SPARSE_BITS = 16;
  const SPARSE_NUM_BITS = 40;
  const SPARSE_MAX_BITS = 44;
  const SPARSE_FREE = 52;
  const INLINE_BITS = 128;

  function sparseArray(address) {
    // { data, slots, count, used(i) } of the TSparseArray at ``address``.
    const data = address.readPointer();
    const slots = address.add(8).readS32();
    const max = address.add(12).readS32();
    const numBits = address.add(SPARSE_NUM_BITS).readS32();
    const maxBits = address.add(SPARSE_MAX_BITS).readS32();
    const free = address.add(SPARSE_FREE).readS32();
    if (slots < 0 || slots > max || free < 0 || free > slots || numBits < slots
        || maxBits < numBits || (slots > 0 && data.isNull()))
      throw hermesError('error.unreal.set_layout');
    const bits = maxBits > INLINE_BITS ? address.add(SPARSE_BITS + 16).readPointer()
      : address.add(SPARSE_BITS);
    return {
      data, slots, count: slots - free,
      used: i => (bits.add(4 * Math.floor(i / 32)).readU32() & (1 << (i % 32))) !== 0
    };
  }

  function alignOf(size) {
    // A guess where nothing tells: the largest power of two (up to 8) dividing the size
    return size % 8 === 0 ? 8 : size % 4 === 0 ? 4 : size % 2 === 0 ? 2 : 1;
  }

  function alignUp(value, alignment) {
    return Math.ceil(value / alignment) * alignment;
  }

  function slotLayout(type) {
    // { valueOffset, stride } of a TSet's or TMap's slots. The property
    // keeps them (UE5: FScriptMapLayout ValueOffset, then FScriptSetLayout
    // HashNextIdOffset, HashIndexOffset, Size; UE4 had a KeyOffset and an
    // ElementOffset more); where they do not fit the sizes, computed.
    const map = type.engine === 'map';
    const keySize = type.elementSize;
    const valueSize = map ? type.valueSize : 0;
    const fits = (valueOffset, hashNext, stride) =>
      (!map || (valueOffset >= keySize && valueOffset < keySize + 16))
      && hashNext >= (map ? valueOffset + valueSize : keySize)
      && hashNext < (map ? valueOffset + valueSize : keySize) + 16
      && stride >= hashNext + 8 && stride <= hashNext + 24 && stride % 4 === 0;
    const at = type.property.add(ue.layout.extra + (map ? 16 : 8));
    const ints = [0, 1, 2, 3, 4, 5].map(i => at.add(4 * i).readS32());
    const candidates = map
      ? [[ints[0], ints[1], ints[3]], [ints[1], ints[3], ints[5]]]  // UE5, UE4
      : [[0, ints[0], ints[2]], [0, ints[1], ints[3]]];
    for (const [valueOffset, hashNext, stride] of candidates)
      if (fits(valueOffset, hashNext, stride))
        return { valueOffset, stride };
    const valueOffset = map ? alignUp(keySize, alignOf(valueSize)) : 0;
    const hashNext = alignUp(map ? valueOffset + valueSize : keySize, 4);
    const alignment = Math.max(4, alignOf(keySize), map ? alignOf(valueSize) : 1);
    return { valueOffset, stride: alignUp(hashNext + 8, alignment) };
  }

  function elements(target, limit) {
    // The entries of the TArray, TMap or TSet ``target`` (see readValue), up
    // to ``limit``: { name, count, entries: [{ name, type, address, offset,
    // key? }] }; ``key`` ({ type, address }) is a TMap entry's key, ``offset``
    // where the entry is in the container's data.
    reflection();
    const field = ptr(target.property);
    if (!isProperty(field))
      throw hermesError('error.unreal.no_property', { field });
    const type = valueType(field);
    const address = ptr(target.address);
    const name = type.name;
    if (type.element === null || (type.engine === 'map' && type.value === null))
      throw hermesError('error.unreal.elements_unknown', { name });
    const entries = [];
    if (type.engine === 'array') {
      const data = address.readPointer();
      const count = data.isNull() ? 0 : Math.max(0, address.add(Process.pointerSize).readS32());
      for (let i = 0; i < Math.min(count, limit); i++) {
        const offset = i * type.elementSize;
        entries.push({ name: `[${i}]`, type: type.element, address: data.add(offset), offset });
      }
      return { name, count, entries };
    }
    if (type.engine !== 'map' && type.engine !== 'set')
      throw hermesError('error.unreal.not_container', { name });
    const sparse = sparseArray(address);
    const { valueOffset, stride } = slotLayout(type);
    for (let i = 0, shown = 0; i < sparse.slots && entries.length < limit; i++) {
      if (!sparse.used(i))
        continue;
      const slot = sparse.data.add(i * stride);
      if (type.engine === 'set') {
        entries.push({ name: `[${shown++}]`, type: type.element, address: slot,
                       offset: i * stride });
      } else {
        entries.push({ name: `[${shown++}]`, type: type.value,
                       address: slot.add(valueOffset), offset: i * stride + valueOffset,
                       key: { type: type.element, address: slot } });
      }
    }
    return { name, count: sparse.count, entries };
  }

  const layouts = new Map();

  function layout(klass) {
    // { name, valueType, rank, headerSize, fields: [{ name, type, offset, static,
    // property, owner }] } of a class or struct, inherited fields first.
    // Offsets count from the object's or struct's start (no header);
    // ``property`` is the FProperty, ``owner`` the class or struct declaring it.
    const key = klass.toString();
    let result = layouts.get(key);
    if (result !== undefined)
      return result;
    reflection();
    const L = ue.layout;
    const address = Number(klass.toString());
    const isStruct = Number(klass.add(OBJECT_CLASS).readPointer().toString()) === L.scriptStruct;
    const chain = [];
    for (let current = address; current !== 0; current = superOf(current))
      chain.unshift(current);
    const fields = [];
    for (const current of chain) {
      for (let field = ptr(current).add(L.childProperties).readPointer(); !field.isNull();
        field = field.add(L.fieldNext).readPointer()) {
        const count = field.add(L.arrayDim).readS32();
        fields.push({
          name: fieldName(field.add(L.fieldName)) + (count > 1 ? `[${count}]` : ''),
          type: valueType(field),
          offset: field.add(L.offset).readS32(),
          static: false,
          property: field,
          owner: ptr(current)
        });
      }
    }
    result = {
      name: isStruct ? `F${nameOfAddress(address)}` : cppName(address),
      valueType: isStruct, rank: 0, headerSize: 0, fields
    };
    layouts.set(key, result);
    return result;
  }

  // --- Method monitor: where a call's arguments are --------------------------------
  // Func is called as (UObject* Context, FFrame& Stack, void* Result). Through
  // ProcessEvent (and for every Blueprint function) Stack is the function's own
  // frame and its Locals the parameters. Called from Blueprint code, a native
  // function gets the caller's frame, its Code at the expressions of the
  // arguments, which the exec thunk evaluates itself: the simple ones (variables,
  // constants, self) are read from the bytecode here, the rest stays unknown.

  // FFrame: FOutputDevice (vtable, two flags), Node, Object, Code, Locals
  const FRAME_NODE = 0x10, FRAME_OBJECT = 0x18, FRAME_CODE = 0x20, FRAME_LOCALS = 0x28;
  const REGISTERS = Process.platform === 'windows' ? ['rcx', 'rdx', 'r8'] : ['rdi', 'rsi', 'rdx'];
  // EExprToken: the bytecode of the expressions read
  const EX = {
    LOCAL_VARIABLE: 0x00, INSTANCE_VARIABLE: 0x01, SELF: 0x17, INT_CONST: 0x1d,
    FLOAT_CONST: 0x1e, OBJECT_CONST: 0x20, NAME_CONST: 0x21, BYTE_CONST: 0x24, INT_ZERO: 0x25,
    INT_ONE: 0x26, TRUE: 0x27, FALSE: 0x28, NO_OBJECT: 0x2a, INT_CONST_BYTE: 0x2c,
    INT64_CONST: 0x35, UINT64_CONST: 0x36, DOUBLE_CONST: 0x37, STRUCT_MEMBER_CONTEXT: 0x42,
    LOCAL_OUT_VARIABLE: 0x48
  };

  // FFrame::OutParms (FOutParmRec: Property, PropAddr, NextOutParm) follows
  // FlowStack and PreviousFrame, where depends on the engine version: it is
  // looked for between these offsets, the first time a call needs it.
  const OUT_PARMS_FIRST = 0x40, OUT_PARMS_LAST = 0xc0, MAX_OUT_PARMS = 64;
  let outParmsOffset = null;

  function outParmAddress(records, property) {
    // PropAddr of ``property`` in a FOutParmRec list, or null.
    let record = records;
    for (let i = 0; i < MAX_OUT_PARMS && !record.isNull(); i++) {
      if (record.readPointer().equals(property))
        return record.add(Process.pointerSize).readPointer();
      record = record.add(2 * Process.pointerSize).readPointer();
    }
    return null;
  }

  function outVariable(frame, property) {
    // Where the out/ref parameter ``property`` of the frame's function is, or null.
    if (outParmsOffset !== null)
      return outParmAddress(frame.add(outParmsOffset).readPointer(), property);
    for (let offset = OUT_PARMS_FIRST; offset <= OUT_PARMS_LAST; offset += Process.pointerSize) {
      try {
        const address = outParmAddress(frame.add(offset).readPointer(), property);
        if (address !== null) {
          outParmsOffset = offset;
          return address;
        }
      } catch (_error) {
        // not the list
      }
    }
    return null;
  }

  function prepare(identity) {
    // The code to hook and the signature of a UFunction: { start, size,
    // frame: true, function, instance, params: [{ name, type, offset }], returns }.
    reflection();
    const fn = resolve(identity);
    const code = codeOf(fn);
    if (code === null)
      throw hermesError('error.unreal.no_code_event', { method: identity.method });
    const { params, returns, instance } = signatureOf(fn);
    return { ...code, frame: true, function: fn, params, returns, instance };
  }

  const signatures = new Map();

  function signatureOf(fn) {
    // { instance, parms: true, params: [{ name, type, offset, out }], returns,
    // returnOffset, parmsSize } of a UFunction (cached): its parameters lie
    // in one block (``parms``), as ProcessEvent and its frame want them.
    const key = fn.toString();
    let result = signatures.get(key);
    if (result !== undefined)
      return result;
    const L = ue.layout;
    const params = [];
    let returns = { kind: KIND.VOID, byref: false, name: 'void' };
    let returnOffset = -1;
    let parmsSize = 0;
    for (let field = fn.add(L.childProperties).readPointer(); !field.isNull();
      field = field.add(L.fieldNext).readPointer()) {
      const flags = field.add(L.propertyFlags).readU32();
      if (!(flags & CPF_PARM))
        continue;
      const offset = field.add(L.offset).readS32();
      parmsSize = Math.max(parmsSize, offset
        + field.add(L.arrayDim).readS32() * field.add(L.elementSize).readS32());
      if (flags & CPF_RETURN_PARM) {
        returns = valueType(field);
        returnOffset = offset;
        continue;
      }
      const byref = (flags & (CPF_OUT_PARM | CPF_REFERENCE_PARM)) !== 0;
      // An output only (Blueprint's rule): nothing to pass, the function writes it
      const out = (flags & CPF_OUT_PARM) !== 0 && (flags & CPF_REFERENCE_PARM) === 0;
      params.push({ name: fieldName(field.add(L.fieldName)), type: valueType(field, byref),
                    offset, out });
    }
    if (returns.kind !== KIND.VOID)
      returns.name = returns.name.replace(/&$/, '');
    result = { instance: (functionInfo(fn).flags & FUNC_STATIC) === 0, parms: true, params,
               returns, returnOffset, parmsSize };
    signatures.set(key, result);
    return result;
  }

  function isProperty(field) {
    try {
      return fieldName(field.add(ue.layout.fieldClass).readPointer()).endsWith('Property');
    } catch (_error) {
      return false;
    }
  }

  function expression(code, frame) {
    // [value, next] of the bytecode expression at ``code``: value is
    // { address } of a variable, or { value } of a constant; null if unknown.
    const L = ue.layout;
    const token = code.readU8();
    const after = code.add(1);
    switch (token) {
      case EX.LOCAL_VARIABLE:
      case EX.INSTANCE_VARIABLE: {
        const property = after.readPointer();
        if (!isProperty(property))
          return null;
        const base = frame.add(token === EX.LOCAL_VARIABLE ? FRAME_LOCALS : FRAME_OBJECT)
          .readPointer();
        return [{ address: base.add(property.add(L.offset).readS32()) }, after.add(8)];
      }
      case EX.LOCAL_OUT_VARIABLE: {
        const property = after.readPointer();
        const address = isProperty(property) ? outVariable(frame, property) : null;
        return address === null ? null : [{ address }, after.add(8)];
      }
      case EX.STRUCT_MEMBER_CONTEXT: {
        // A member of the struct the next expression gives
        const property = after.readPointer();
        const struct = isProperty(property) ? expression(after.add(8), frame) : null;
        if (struct === null || struct[0].address === undefined)
          return null;
        return [{ address: struct[0].address.add(property.add(L.offset).readS32()) }, struct[1]];
      }
      case EX.SELF: return [{ value: { object: frame.add(FRAME_OBJECT).readPointer() } }, after];
      case EX.NO_OBJECT: return [{ value: { object: NULL } }, after];
      case EX.OBJECT_CONST: return [{ value: { object: after.readPointer() } }, after.add(8)];
      case EX.INT_CONST: return [{ value: { text: String(after.readS32()) } }, after.add(4)];
      case EX.FLOAT_CONST: return [{ value: { text: String(after.readFloat()) } }, after.add(4)];
      case EX.DOUBLE_CONST: return [{ value: { text: String(after.readDouble()) } }, after.add(8)];
      case EX.INT64_CONST:
        return [{ value: { text: after.readS64().toString() } }, after.add(8)];
      case EX.UINT64_CONST:
        return [{ value: { text: after.readU64().toString() } }, after.add(8)];
      case EX.BYTE_CONST:
      case EX.INT_CONST_BYTE: return [{ value: { text: String(after.readU8()) } }, after.add(1)];
      case EX.INT_ZERO: return [{ value: { text: '0' } }, after];
      case EX.INT_ONE: return [{ value: { text: '1' } }, after];
      case EX.TRUE: return [{ value: { text: 'true' } }, after];
      case EX.FALSE: return [{ value: { text: 'false' } }, after];
      // FScriptName: ComparisonIndex, DisplayIndex, Number
      case EX.NAME_CONST:
        return [{ value: { text: nameText(after.readU32(), after.add(8).readU32()) } },
                after.add(12)];
      default: return null;
    }
  }

  // --- Callers: the Blueprint function a call came from ----------------------------
  // Called from bytecode, the frame passed is the caller's: its Node. Through
  // ProcessEvent the frame is the function's own; the frames of the script
  // functions running on the thread are further up the stack, and every FFrame
  // starts with the same vtable. The nearest one that holds up (its Node a
  // UFunction, its Object alive, its Code in Node's bytecode) is the caller:
  // stale frames left in a caller's stack memory fail that.

  const CALLER_SEARCH = 0x40000;
  let frameVtable = null;
  const functionTexts = new Map();
  const stackEnds = new Map();

  function functionText(fn) {
    // 'Class:Function' of a UFunction, read from the objects themselves (no
    // snapshot: this runs on game threads).
    const key = fn.toString();
    let text = functionTexts.get(key);
    if (text === undefined) {
      const owner = fn.add(OBJECT_OUTER).readPointer();
      text = `${fieldName(owner.add(OBJECT_NAME))}:${fieldName(fn.add(OBJECT_NAME))}`;
      functionTexts.set(key, text);
    }
    return text;
  }

  function isFunction(address) {
    // A UFunction (or a subclass of it): its class derives from Function.
    if (!isObject(address))
      return false;
    let klass = Number(address.add(OBJECT_CLASS).readPointer().toString());
    for (let depth = 0; klass !== 0 && depth < 8; depth++, klass = superOf(klass)) {
      if (klass === ue.layout.functionClass)
        return true;
    }
    return false;
  }

  function scriptPosition(frame) {
    // 'Class:Function+0x…' of a frame running a function's bytecode, or null.
    const node = frame.add(FRAME_NODE).readPointer();
    const code = frame.add(FRAME_CODE).readPointer();
    if (code.isNull() || !isFunction(node) || !isObject(frame.add(FRAME_OBJECT).readPointer()))
      return null;
    const script = node.add(ue.layout.script).readPointer();
    const length = node.add(ue.layout.script + Process.pointerSize).readS32();
    if (script.isNull() || code.compare(script) < 0 || code.compare(script.add(length)) > 0)
      return null;
    return `${functionText(node)}+0x${code.sub(script).toString(16)}`;
  }

  function stackEnd(sp) {
    // Where the thread's stack ends (cached per thread).
    const thread = Process.getCurrentThreadId();
    let end = stackEnds.get(thread);
    if (end === undefined || sp.compare(end) >= 0) {
      const range = Process.findRangeByAddress(sp);
      end = range === null ? sp : range.base.add(range.size);
      stackEnds.set(thread, end);
    }
    return end;
  }

  function enclosingScript(own, sp) {
    // The nearest script frame above ``sp`` other than ``own``.
    const end = stackEnd(sp);
    const size = Math.min(CALLER_SEARCH, Number(end.sub(sp).toString()));
    if (size <= 0)
      return null;
    for (const hit of Memory.scanSync(sp, size, pointerPattern(frameVtable))) {
      if (hit.address.equals(own))
        continue;
      try {
        const position = scriptPosition(hit.address);
        if (position !== null)
          return position;
      } catch (_error) {
        // not a frame
      }
    }
    return null;
  }

  function pointerPattern(pointer) {
    const hex = pointer.toString(16).replace(/^0x/, '').padStart(2 * Process.pointerSize, '0');
    return hex.match(/../g).reverse().join(' ');
  }

  function callerOf(frame, own, sp) {
    // The Blueprint position a call came from, or null.
    try {
      if (frameVtable === null)
        frameVtable = frame.readPointer();
      if (!own)
        return scriptPosition(frame);
      return frameVtable.isNull() ? null : enclosingScript(frame, sp);
    } catch (_error) {
      return null;
    }
  }

  // How far an interface's part may lie inside the object implementing it
  const MAX_INTERFACE_OFFSET = 0x1000;

  function owningObject(address) {
    // The object at ``address``, or the one an interface pointer points into.
    try {
      for (let back = 0; back <= MAX_INTERFACE_OFFSET; back += Process.pointerSize) {
        if (isObject(address.sub(back)))
          return address.sub(back);
      }
    } catch (_error) {
      // not readable
    }
    return address;
  }

  function callFrame(info, context) {
    // Where the arguments of a call are, on entering Func: { self, args:
    // [{ address } or { value } or null], result, caller, own } (result: where
    // the return value will be written, or null; caller: the Blueprint
    // function and bytecode offset the call came from, or null; own: the
    // arguments are in the function's own frame, else the caller's variables).
    const self = context[REGISTERS[0]];
    const frame = context[REGISTERS[1]];
    const result = context[REGISTERS[2]];
    const own = frame.add(FRAME_NODE).readPointer().equals(info.function);
    const call = { self: info.instance ? self : null, args: info.params.map(() => null),
                   result: result.isNull() ? null : result,
                   caller: callerOf(frame, own, context.sp), own };
    if (own) {
      // Its own frame knows the object; Context may point into it instead
      // (the part of an interface the object implements).
      if (info.instance)
        call.self = frame.add(FRAME_OBJECT).readPointer();
      const locals = frame.add(FRAME_LOCALS).readPointer();
      if (!locals.isNull())
        call.args = info.params.map(param => ({ address: locals.add(param.offset) }));
      return call;
    }
    if (info.instance)
      call.self = owningObject(self);
    let code = frame.add(FRAME_CODE).readPointer();
    if (code.isNull())
      return call;
    for (let i = 0; i < info.params.length; i++) {
      let found;
      try {
        found = expression(code, frame);
      } catch (_error) {
        found = null;
      }
      if (found === null)
        break;  // where the next argument starts is unknown now
      call.args[i] = found[0];
      code = found[1];
    }
    return call;
  }

  refresh();
  // --- Calling functions -------------------------------------------------------------
  // Game code runs through UObject::ProcessEvent(Function, Parms), as Blueprints
  // and C++ do it: the arguments in a block laid out like the function's
  // parameters (see signatureOf), where the result is written, too. Only on
  // the game thread (the main thread): onMainThread queues work until the game
  // thread next enters ProcessEvent, which happens many times a frame.

  const MAIN_TIMEOUT = 3000;
  const mainQueue = [];
  let mainHook = null;
  let draining = false;
  let processEvent = null;
  // FStrings of the arguments of the call being made (the game copies them)
  let callStrings = [];

  function findProcessEvent() {
    // The one virtual function of UObject that calls a UFunction's Func
    // (call [reg + Func]); the others call into it. Where no slot does, the
    // one that calls a short function doing it (some games do not inline
    // UFunction::Invoke).
    if (processEvent !== null)
      return processEvent;
    reflection();
    const s = objects();
    const index = s.address.findIndex((_address, i) => s.outer[i] !== 0
      && objectName(s, i) === 'Default__Object');
    if (index < 0)
      throw hermesError('error.unreal.not_found', { name: 'Default__Object' });
    const vtable = ptr(s.address[index]).readPointer();
    const funcOffset = ue.layout.func;
    const inCode = fn => fn.compare(module.base) >= 0
      && fn.compare(module.base.add(module.size)) < 0;
    const readCode = fn => {
      try {
        return new Uint8Array(fn.readByteArray(0x800));
      } catch (_error) {
        // close to the end of the code: what is left of its page
        const left = 0x1000 - Number(fn.and(0xfff).toString());
        return new Uint8Array(fn.readByteArray(left));
      }
    };
    const callsFunc = bytes => {
      for (let at = 0; at + 6 <= bytes.length; at++) {
        // FF /2 with a 32-bit displacement: call [reg + disp32] (not rsp)
        if (bytes[at] === 0xff && (bytes[at + 1] & 0xf8) === 0x90 && (bytes[at + 1] & 7) !== 4
            && bytes[at + 2] === (funcOffset & 0xff) && bytes[at + 3] === (funcOffset >> 8)
            && bytes[at + 4] === 0 && bytes[at + 5] === 0)
          return true;
      }
      return false;
    };
    const invokes = fn => {
      // Whether fn calls Func before its first ret: a short function like
      // UFunction::Invoke, not the code after it.
      let at = fn;
      for (let count = 0; count < 64; count++) {
        const instruction = Instruction.parse(at);
        if (instruction.mnemonic === 'ret' || instruction.mnemonic === 'int3')
          return false;
        const [operand] = instruction.operands;
        if (instruction.mnemonic === 'call' && operand !== undefined && operand.type === 'mem'
            && operand.value.disp === funcOffset && operand.value.base !== 'rsp')
          return true;
        at = instruction.next;
      }
      return false;
    };
    const callsInvoke = (fn, bytes) => {
      for (let at = 0; at + 5 <= bytes.length; at++) {
        // E8: call rel32
        if (bytes[at] !== 0xe8)
          continue;
        const rel = bytes[at + 1] | (bytes[at + 2] << 8) | (bytes[at + 3] << 16)
          | (bytes[at + 4] << 24);
        const target = fn.add(at + 5 + rel);
        if (!inCode(target))
          continue;
        try {
          if (invokes(target))
            return true;
        } catch (_error) {
          // not readable or not code
        }
      }
      return false;
    };
    const direct = [];
    const throughCall = [];
    for (let slot = 0; slot < 160; slot++) {
      const fn = vtable.add(slot * Process.pointerSize).readPointer();
      if (!inCode(fn))
        break;
      const bytes = readCode(fn);
      if (callsFunc(bytes))
        direct.push(fn);
      else if (direct.length === 0 && callsInvoke(fn, bytes))
        throughCall.push(fn);
    }
    const found = direct.length > 0 ? direct : throughCall;
    if (found.length !== 1)
      throw hermesError('error.unreal.no_process_event', { count: found.length });
    processEvent = {
      address: found[0],
      call: new NativeFunction(found[0], 'void', ['pointer', 'pointer', 'pointer'])
    };
    return processEvent;
  }

  function isGameThread() {
    return Process.getCurrentThreadId() === mainThreadId();
  }

  function drainMain() {
    // Run the queued work; call it on any thread, it only acts on the game thread.
    if (draining || mainQueue.length === 0 || !isGameThread())
      return;
    draining = true;  // our own calls pass ProcessEvent, too
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
      setTimeout(() => hook.detach(), 0);  // not from within its own callback
    }
  }

  function onMainThread(work) {
    // A promise of work()'s result, computed on the game thread.
    let start;
    try {
      start = findProcessEvent().address;
      mainThreadId();  // found here, not first in the hook on the game thread
    } catch (error) {
      return Promise.reject(error);
    }
    return new Promise((resolve, reject) => {
      const job = { work, resolve, reject };
      job.timer = setTimeout(() => {
        const index = mainQueue.indexOf(job);
        if (index >= 0)
          mainQueue.splice(index, 1);
        reject(hermesError('error.unreal.game_thread_timeout'));
      }, MAIN_TIMEOUT);
      mainQueue.push(job);
      if (mainHook === null)
        mainHook = Interceptor.attach(start, {
          onEnter() {
            drainMain();
          }
        });
    });
  }

  function invoke(fn, object, parms) {
    // Call a UFunction with its parameter block (see signatureOf); game thread
    // only. A static function is called on its class's default object.
    if (!isGameThread())
      throw hermesError('error.unreal.not_game_thread');
    let self = object;
    if (self === null || self.isNull())
      self = defaultObject(fn.add(OBJECT_OUTER).readPointer());
    try {
      findProcessEvent().call(self, fn, parms);
    } finally {
      callStrings = [];
    }
    return { result: parms, exception: NULL };
  }

  function writeParameter(type, address, value) {
    // Store an argument of a type with ``engine`` in a parameter block.
    switch (type.engine) {
      case 'string': {
        // FString: TCHAR data, Num and Max with the terminating 0
        const text = value === null || value === undefined ? '' : String(value);
        if (text.length === 0) {
          address.writePointer(NULL);
          address.add(Process.pointerSize).writeS32(0);
          address.add(Process.pointerSize + 4).writeS32(0);
          return;
        }
        const data = Memory.alloc(2 * (text.length + 1));
        data.writeUtf16String(text);
        callStrings.push(data);
        address.writePointer(data);
        address.add(Process.pointerSize).writeS32(text.length + 1);
        address.add(Process.pointerSize + 4).writeS32(text.length + 1);
        return;
      }
      case 'name': {
        const [index, number] = nameOf(String(value));
        address.writeU32(index);
        address.add(4).writeU32(number);
        return;
      }
      case 'bool':
        writeValue(type, address, value);
        return;
      default:
        throw hermesError('error.unreal.script_unpassable', { type: type.name });
    }
  }

  const nameIndexes = new Map();

  function nameOf(text) {
    // [index, number] of an FName the game knows: one an object is named
    // (Name_3 is Name with number 4).
    let found = nameIndexes.get(text);
    if (found !== undefined)
      return found;
    const s = objects();
    const match = /^(.*)_(\d+)$/.exec(text);
    for (let i = 0; i < s.address.length && found === undefined; i++) {
      const base = nameText(s.name[i], 0);
      if (base === text)
        found = [s.name[i], 0];
      else if (match !== null && base === match[1] && String(Number(match[2])) === match[2])
        found = [s.name[i], Number(match[2]) + 1];
    }
    if (found === undefined)
      throw hermesError('error.unreal.unknown_fname', { name: text });
    nameIndexes.set(text, found);
    return found;
  }

  function classChain(klass) {
    const chain = [];
    for (let current = Number(klass.toString()); current !== 0; current = superOf(current))
      chain.push(ptr(current));
    return chain;  // the class first, UObject last
  }

  function classNameMatches(klass, name) {
    // By its name (BP_Player_C) or C++ name (ABP_Player_C, UAlabamaCheatManager)
    const own = nameOfAddress(Number(klass.toString()));
    return own === name || cppName(Number(klass.toString())) === name;
  }

  const classesByName = new Map();

  function findClass(name) {
    // A loaded class (or struct) by its name, e.g. "AlabamaCheatManager".
    reflection();
    const cached = classesByName.get(name);
    if (cached !== undefined && isObject(cached))
      return cached;
    const s = objects();
    const kinds = new Map();
    for (let i = 0; i < s.address.length; i++) {
      if (s.outer[i] === 0 || !isClass(s, s.klass[i], kinds))
        continue;
      const klass = ptr(s.address[i]);
      if (classNameMatches(klass, name)) {
        classesByName.set(name, klass);
        return klass;
      }
    }
    throw hermesError('error.unreal.no_class', { name });
  }

  // Functions found by name: scripts run each run's calls again, and a search
  // by name reads the object table when its snapshot is old (on the game thread)
  const methodsByName = new Map();

  function cachedMethod(key, find) {
    const cached = methodsByName.get(key);
    if (cached !== undefined && isObject(cached))
      return cached;
    const found = find();
    methodsByName.set(key, found);
    return found;
  }

  function findMethod(klass, name, argumentCount, wantStatic) {
    return cachedMethod(`${klass}|${name}|${argumentCount}|${wantStatic}`,
                        () => searchMethod(klass, name, argumentCount, wantStatic));
  }

  function searchMethod(klass, name, argumentCount, wantStatic) {
    // The UFunction ``name`` of the class or a base class taking
    // ``argumentCount`` arguments; ``wantStatic`` true/false or null for any.
    for (const current of classChain(klass)) {
      for (const fn of functionsOf(current)) {
        if (fn.name !== name)
          continue;
        const signature = signatureOf(fn.address);
        if ((wantStatic === null || !signature.instance === wantStatic)
            && signature.params.length === argumentCount)
          return fn.address;
      }
    }
    throw hermesError(wantStatic ? 'error.unreal.no_static_function' : 'error.unreal.no_function',
                      { owner: layout(klass).name, name, count: argumentCount });
  }

  function methods(klass) {
    // [{ name, method, static, owner }] of the UFunctions of ``klass`` and its
    // base classes, the most derived first (an override once), for the
    // object inspector.
    reflection();
    const seen = new Set();
    const result = [];
    for (const current of classChain(klass)) {
      const owner = layout(current).name;
      for (const fn of functionsOf(current)) {
        if (seen.has(fn.name))
          continue;
        seen.add(fn.name);
        let instance = true;
        try {
          instance = signatureOf(fn.address).instance;
        } catch (_error) {
          // listed anyway; calling it reports the problem
        }
        result.push({ name: fn.name, method: fn.address, owner, static: !instance });
      }
    }
    return result;
  }

  function virtualMethod(object, fn) {
    // What ProcessEvent should run for ``fn`` on ``object``: the override of
    // its class (a Blueprint's event graph, a subclass's implementation).
    const klass = object.add(OBJECT_CLASS).readPointer();
    return cachedMethod(`${klass}|${fn}`, () => {
      const name = fieldName(fn.add(OBJECT_NAME));
      for (const current of classChain(klass)) {
        const found = functionsOf(current).find(candidate => candidate.name === name);
        if (found !== undefined)
          return found.address;
      }
      return fn;
    });
  }

  const defaultObjects = new Map();

  function defaultObject(klass) {
    // The class default object (Default__Name) a class's static functions run on.
    const cached = defaultObjects.get(klass.toString());
    if (cached !== undefined && isObject(cached))
      return cached;
    const found = searchDefaultObject(klass);
    defaultObjects.set(klass.toString(), found);
    return found;
  }

  function searchDefaultObject(klass) {
    const s = objects();
    const wanted = `Default__${nameOfAddress(Number(klass.toString()))}`;
    const i = s.address.findIndex((address, k) => s.klass[k] === Number(klass.toString())
      && objectName(s, k) === wanted);
    if (i < 0)
      throw hermesError('error.unreal.not_found', { name: wanted });
    return ptr(s.address[i]);
  }

  // className|name -> the object findObject found last: checked, not
  // searched for again (a search reads every object, tens of milliseconds on
  // the game thread)
  const foundObjects = new Map();

  function outerName(object) {
    // "AttributeStatsWidget.PointsAvailableTracker": the object's name after
    // its outer's; a widget's outer is the WidgetTree of the widget showing it.
    if (!isObject(object))
      throw hermesError('error.unreal.no_object', { address: object });
    const name = fieldName(object.add(OBJECT_NAME));
    let outer = object.add(OBJECT_OUTER).readPointer();
    if (!outer.isNull() && isObject(outer)
        && fieldName(outer.add(OBJECT_CLASS).readPointer().add(OBJECT_NAME)) === 'WidgetTree')
      outer = outer.add(OBJECT_OUTER).readPointer();
    return outer.isNull() || !isObject(outer) ? name
      : `${fieldName(outer.add(OBJECT_NAME))}.${name}`;
  }

  function isNamed(object, named) {
    // Whether ``object`` is named ``named``: a name, or one as outerName gives
    if (named === null)
      return true;
    return named.includes('.') ? outerName(object) === named
      : fieldName(object.add(OBJECT_NAME)) === named;
  }

  // RF_ClassDefaultObject, RF_ArchetypeObject
  const RF_TEMPLATE = 0x10 | 0x20;

  function isTemplate(s, i, classKinds) {
    // Whether object ``i`` is no object the game uses: a class default object,
    // an archetype, or a part of one or of a class (a widget in a Widget
    // Blueprint's tree, the template of the widgets shown).
    for (let k = i; k !== undefined; k = s.index.get(s.outer[k])) {
      if (objectName(s, k).startsWith('Default__')
          || (ptr(s.address[k]).add(OBJECT_FLAGS).readU32() & RF_TEMPLATE) !== 0
          || (k !== i && isClass(s, s.klass[k], classKinds)))
        return true;
    }
    return false;
  }

  function stillFound(object, klass, named) {
    // Whether a cached object is still alive, of the class (or a subclass)
    // and, if given, named ``named``.
    if (!isObject(object) || !isNamed(object, named))
      return false;
    return classChain(object.add(OBJECT_CLASS).readPointer())
      .some(current => Number(current.toString()) === klass);
  }

  function searchObjects(klass, named, limit) {
    // Up to ``limit`` live objects of class ``klass`` (or a subclass) named
    // ``named`` (see isNamed), in the snapshot of the object table.
    const s = objects();
    const kinds = new Map();
    const classKinds = new Map();
    const derives = k => {
      let result = kinds.get(k);
      if (result === undefined) {
        result = false;
        for (let current = k; current !== 0; current = superOf(current)) {
          if (current === klass) {
            result = true;
            break;
          }
        }
        kinds.set(k, result);
      }
      return result;
    };
    const ownName = named === null ? null : named.substring(named.lastIndexOf('.') + 1);
    const found = [];
    for (let i = 0; i < s.address.length && found.length < limit; i++) {
      if (!derives(s.klass[i]) || (ownName !== null && objectName(s, i) !== ownName))
        continue;
      const object = ptr(s.address[i]);
      try {
        if (isObject(object) && !isTemplate(s, i, classKinds) && isNamed(object, named))
          found.push(object);
      } catch (_error) {
        // freed meanwhile
      }
    }
    return found;
  }

  function findObject(className, named = null) {
    // A live object (no class default object or template) of the class
    // ``className`` or a subclass, e.g. findObject("AlabamaCheatManager");
    // with ``named`` the one of that name, e.g. findObject("BP_Hero_C",
    // "BP_Hero_C_1"), or of that name in its outer (see outerName). null if none.
    reflection();
    const klass = Number(findClass(className).toString());
    const key = `${className}|${named}`;
    const cached = foundObjects.get(key);
    if (cached !== undefined && stillFound(cached, klass, named))
      return cached;
    foundObjects.delete(key);
    for (let fresh = 0; fresh < 2; fresh++) {
      const [object] = searchObjects(klass, named, 1);
      if (object !== undefined) {
        foundObjects.set(key, object);
        return object;
      }
      snapshot = null;  // the object may be newer than the snapshot
    }
    return null;
  }

  function findObjects(className, named = null) {
    // All live objects findObject could give, read from the object table anew
    reflection();
    const klass = Number(findClass(className).toString());
    snapshot = null;
    // (Frida's JavaScript on Windows has no Infinity: it is NaN there.)
    return searchObjects(klass, named, Number.MAX_SAFE_INTEGER);
  }

  // --- Gameplay Ability System: the player's attributes -----------------------------
  // GAS games keep attributes (Health, Might, DamageOutgoingMult ...) as
  // FGameplayAttributeData (vtable, BaseValue, CurrentValue) in UAttributeSet
  // subobjects of the actor owning the UAbilitySystemComponent: the player's
  // pawn or player state. CurrentValue is BaseValue with the active effects
  // applied, and GAS computes it anew; damage calculations read attributes
  // through GAS too. So a value written to memory does not last: the base
  // value is set through the game's own code instead, a static UFunction
  // taking (UAbilitySystemComponent*, FGameplayAttribute, float), e.g.
  // Avowed's AlabamaAbilitySystemBlueprintLibrary:
  // SetFloatAttributeBaseFromAbilitySystemComponent. GAS then computes the
  // current value with the effects (a +1 bonus stays one, a x1.4 one scales).
  // Games with AngelscriptGAS (Gothic 1 Remake) have no such function; their
  // attribute sets set it themselves: AngelscriptAttributeSet:
  // TrySetAttributeBaseValue(FName, float), called on the set.

  // FGameplayAttribute's TFieldPath<FProperty> (Attribute): ResolvedField,
  // ResolvedOwner (a weak pointer to the declaring class), Path (TArray<FName>,
  // the game keeps the attribute's name only).
  const FIELD_PATH_OWNER = 8, FIELD_PATH_PATH = 16;
  // How often a found setter (or none) is looked for again
  const SETTER_SEARCH_AGE = 30000;
  // { fn, signature, name, setClass } or null; undefined: not looked for.
  // setClass: the attribute set class whose sets it is called on, else NULL.
  let attributeSetter;
  let setterSearched = 0;
  let playerParts = null;  // { pawn, owners, sets, asc } found last

  function isAttributeData(type) {
    if (type.kind !== KIND.VALUETYPE || type.klass === undefined)
      return false;
    const names = layout(type.klass).fields.map(field => field.name);
    return names.includes('BaseValue') && names.includes('CurrentValue');
  }

  function attributeFields(klass) {
    return layout(klass).fields.filter(field => isAttributeData(field.type));
  }

  function propertyOf(klass, name) {
    // The field (see layout) of an object's property, or undefined
    return layout(klass).fields.find(field => field.name === name);
  }

  function pointerProperty(object, name) {
    const field = propertyOf(object.add(OBJECT_CLASS).readPointer(), name);
    if (field === undefined)
      return NULL;
    const value = object.add(field.offset).readPointer();
    return !value.isNull() && isObject(value) ? value : NULL;
  }

  function searchPlayerParts() {
    // The attribute sets and the ability system component of the first local
    // player: owned by its pawn or player state.
    const controller = findObject('PlayerController');
    if (controller === null)
      return null;
    const pawn = pointerProperty(controller, 'Pawn');
    if (pawn.isNull())
      return null;
    const owners = [pawn, pointerProperty(controller, 'PlayerState')].filter(o => !o.isNull());
    const owned = object => owners.some(owner => object.add(OBJECT_OUTER).readPointer()
      .equals(owner));
    // One read of the object table for both (findObjects reads it anew)
    snapshot = null;
    const all = name => searchObjects(Number(findClass(name).toString()), null,
                                      Number.MAX_SAFE_INTEGER);
    const sets = all('AttributeSet').filter(owned);
    const components = all('AbilitySystemComponent').filter(owned);
    // The component of the owner with the sets
    const asc = components.find(component => sets.some(set =>
      set.add(OBJECT_OUTER).readPointer().equals(component.add(OBJECT_OUTER).readPointer())))
      || components[0] || NULL;
    return { pawn, owners, sets, asc };
  }

  function currentPlayerParts() {
    // The player's parts found last while still current, else searched anew
    // (a search reads every object: not on every refresh).
    const parts = playerParts;
    if (parts !== null) {
      try {
        const controller = findObject('PlayerController');
        const current = controller === null ? NULL : pointerProperty(controller, 'Pawn');
        if (current.equals(parts.pawn) && parts.sets.every(set => isObject(set)
            && parts.owners.some(owner => set.add(OBJECT_OUTER).readPointer().equals(owner)))
            && (parts.asc.isNull() || isObject(parts.asc)))
          return parts;
      } catch (_error) {
        // freed meanwhile: search again
      }
    }
    playerParts = searchPlayerParts();
    return playerParts;
  }

  function setName(set) {
    return fieldName(set.add(OBJECT_NAME));
  }

  function gasAttributes() {
    // { owner, setter, sets: [{ name, className, attributes: [{ name, base,
    // current }] }] } of the player, or null without one (e.g. in the menu).
    reflection();
    const parts = currentPlayerParts();
    if (parts === null)
      return null;
    const setter = findAttributeSetter();
    return {
      owner: fieldName(parts.pawn.add(OBJECT_NAME)),
      setter: setter === null ? null : setter.name,
      component: !parts.asc.isNull(),
      sets: parts.sets.map(set => {
        const klass = set.add(OBJECT_CLASS).readPointer();
        return {
          name: setName(set),
          className: layout(klass).name,
          attributes: attributeFields(klass).map(field => {
            const data = layout(field.type.klass).fields;
            const at = name => set.add(field.offset)
              .add(data.find(entry => entry.name === name).offset).readFloat();
            return { name: field.name, base: at('BaseValue'), current: at('CurrentValue') };
          })
        };
      })
    };
  }

  function findAttributeSetter() {
    // The game's UFunction setting an attribute's base value (see above), or
    // null; found by its parameters among the UFunctions named
    // (Try)Set...Attribute...Base: a static one taking the component, else
    // one of an attribute set taking the attribute's name.
    if (attributeSetter !== undefined
        && (attributeSetter !== null || Date.now() - setterSearched < SETTER_SEARCH_AGE))
      return attributeSetter;
    setterSearched = Date.now();
    attributeSetter = null;
    const s = objects();
    const functionClass = ue.layout.functionClass;
    const setBase = findClass('AttributeSet');
    let onSet = null;
    for (let i = 0; i < s.address.length; i++) {
      if (s.klass[i] !== functionClass
          || !/^(Try)?Set\w*Attribute\w*Base/.test(objectName(s, i)))
        continue;
      const fn = ptr(s.address[i]);
      let signature;
      try {
        signature = signatureOf(fn);
      } catch (_error) {
        continue;
      }
      const owner = fn.add(OBJECT_OUTER).readPointer();
      const setter = { fn, signature, setClass: NULL,
                       name: `${nameOfAddress(Number(owner.toString()))}:${objectName(s, i)}` };
      const isNumber = parameter => parameter !== undefined
        && [KIND.R4, KIND.R8].includes(parameter.type.kind);
      const [component, attribute, value] = signature.params;
      if (!signature.instance && isNumber(value) && component.type.kind === KIND.CLASS
          && /AbilitySystemComponent/.test(component.type.name)
          && attribute.type.name === 'FGameplayAttribute' && !attribute.type.byref) {
        attributeSetter = setter;
        break;
      }
      const [name, number] = signature.params;
      if (onSet === null && signature.instance && signature.params.length === 2
          && name.type.engine === 'name' && isNumber(number)
          && classChain(owner).some(klass => klass.equals(setBase)))
        onSet = Object.assign(setter, { setClass: owner });
    }
    if (attributeSetter === null)
      attributeSetter = onSet;
    return attributeSetter;
  }

  function findAttribute(parts, name) {
    // The set and field of "Set.Attribute" or "Attribute" (the first set
    // with it). "Set" may leave out the number Unreal gives the set's object
    // anew on every load: "AttributeSet_Health" for "AttributeSet_Health_2147445233".
    name = String(name);
    const dot = name.indexOf('.');
    const wantedSet = dot < 0 ? null : name.substring(0, dot);
    const attribute = dot < 0 ? name : name.substring(dot + 1);
    for (const set of parts.sets) {
      const found = wantedSet === null ? null : setName(set);
      if (found !== null && found !== wantedSet && found.replace(/_\d+$/, '') !== wantedSet)
        continue;
      const field = attributeFields(set.add(OBJECT_CLASS).readPointer())
        .find(entry => entry.name === attribute);
      if (field !== undefined)
        return { set, field };
    }
    throw hermesError('error.unreal.no_player_attribute', { name });
  }

  function writeGameplayAttribute(type, address, field, keep) {
    // An FGameplayAttribute naming ``field`` as the game makes one
    const parts = layout(type.klass).fields;
    const at = name => {
      const part = parts.find(entry => entry.name === name);
      if (part === undefined)
        throw hermesError('error.unreal.gameplay_attribute_field', { name });
      return address.add(part.offset);
    };
    const name = field.name;
    const text = Memory.alloc(2 * (name.length + 1));
    text.writeUtf16String(name);
    const path = Memory.alloc(8);
    Memory.copy(path, field.property.add(ue.layout.fieldName), 8);
    keep.push(text, path);
    const nameAt = at('AttributeName');
    nameAt.writePointer(text);
    nameAt.add(Process.pointerSize).writeS32(name.length + 1);
    nameAt.add(Process.pointerSize + 4).writeS32(name.length + 1);
    const owner = field.owner;
    const index = owner.add(OBJECT_INDEX).readS32();
    const item = ue.objects.table.readPointer().add(8 * Math.floor(index / CHUNK)).readPointer()
      .add((index % CHUNK) * ue.objects.itemSize);
    const fieldPath = at('Attribute');
    fieldPath.writePointer(field.property);
    // FWeakObjectPtr: object index and serial number (none yet: resolved by the path)
    const serial = item.add(Process.pointerSize + 8).readS32();
    fieldPath.add(FIELD_PATH_OWNER).writeS32(serial === 0 ? -1 : index);
    fieldPath.add(FIELD_PATH_OWNER + 4).writeS32(serial);
    fieldPath.add(FIELD_PATH_PATH).writePointer(path);
    fieldPath.add(FIELD_PATH_PATH + Process.pointerSize).writeS32(1);
    fieldPath.add(FIELD_PATH_PATH + Process.pointerSize + 4).writeS32(1);
    at('AttributeOwner').writePointer(owner);
  }

  function gasSetBase(name, value) {
    // Set the base value of the player's attribute "Set.Attribute" or
    // "Attribute" through the game's setter; game thread only. Gives
    // { base, current } afterwards.
    if (typeof value !== 'number' || !Number.isFinite(value))
      throw hermesError('error.unreal.not_a_number', { value: String(value) });
    if (!isGameThread())
      throw hermesError('error.unreal.attribute_game_thread');
    reflection();
    const parts = currentPlayerParts();
    if (parts === null)
      throw hermesError('error.unreal.no_player');
    const setter = findAttributeSetter();
    if (setter !== null && setter.setClass.isNull() && parts.asc.isNull())
      throw hermesError('error.unreal.no_ability_system');
    if (setter === null)
      throw hermesError('error.unreal.no_base_setter');
    const { set, field } = findAttribute(parts, name);
    const { signature } = setter;
    const parms = Memory.alloc(Math.max(signature.parmsSize, 8));
    parms.writeByteArray(new ArrayBuffer(Math.max(signature.parmsSize, 8)));
    const writeNumber = number => {
      if (number.type.kind === KIND.R8)
        parms.add(number.offset).writeDouble(value);
      else
        parms.add(number.offset).writeFloat(value);
    };
    const isFalse = (type, offset) => type.engine === 'bool'
      && (parms.add(offset + type.byteOffset).readU8() & type.byteMask) === 0;
    let notFound;
    if (setter.setClass.isNull()) {
      const [component, attribute, number, found] = signature.params;
      const keep = [];
      parms.add(component.offset).writePointer(parts.asc);
      writeGameplayAttribute(attribute.type, parms.add(attribute.offset), field, keep);
      writeNumber(number);
      invoke(setter.fn, null, parms);
      notFound = found !== undefined && isFalse(found.type, found.offset);
    } else {
      // On the set, by the attribute's name: the FName its property has
      if (!classChain(set.add(OBJECT_CLASS).readPointer())
        .some(klass => klass.equals(setter.setClass)))
        throw hermesError('error.unreal.no_base_setter');
      const [attribute, number] = signature.params;
      Memory.copy(parms.add(attribute.offset), field.property.add(ue.layout.fieldName), 8);
      writeNumber(number);
      invoke(setter.fn, set, parms);
      notFound = signature.returnOffset >= 0
        && isFalse(signature.returns, signature.returnOffset);
    }
    if (notFound)
      throw hermesError('error.unreal.attribute_not_found', { name });
    const data = layout(field.type.klass).fields;
    const read = entry => set.add(field.offset)
      .add(data.find(part => part.name === entry).offset).readFloat();
    return { base: read('BaseValue'), current: read('CurrentValue') };
  }

  return {
    id: 'unreal',
    refresh,
    // All code lies in modules: it is named module+offset.
    locate: () => (module === null ? undefined : null),
    name: () => null,
    describe: () => null,
    identify: () => null,
    compile,
    resolve,
    assemblies,
    classes,
    methodsByToken,
    checkAll: methods => methods.map(() => false),
    compileAll,
    describeAll: addresses => addresses.map(() => null),
    cancelBatches: () => 0,
    batchesRunning: () => false,
    releaseCode,
    prepare,
    callFrame,
    classOf,
    layout,
    readValue,
    valueJs,
    writeValue,
    // UObjects need no write barrier
    writeReference: (_owner, address, pointer) => address.writePointer(pointer),
    // Unreal has no getters to evaluate.
    properties: () => [],
    drainMain,
    onMainThread,
    signatureOf,
    findClass,
    findMethod,
    findObject,
    findObjects,
    outerName,
    gasAttributes,
    gasSetBase,
    classChain,
    objectClassName: object => layout(classOf(object)).name,
    virtualMethod,
    methods,
    elements,
    invoke,
    writeParameter,
    newString: () => {
      throw hermesError('error.unreal.no_string_objects');
    },
    onRuntimeThread: work => new Promise(resolve => resolve(work())),
    // The monitor shows Unity's object names; here the UObject's FName.
    unityName: uobjectName,
    // For checks and troubleshooting: where the reflection was found.
    reflection: () => {
      const found = reflection();
      return { module: module.name, names: found.names, objects: found.objects.table,
               itemSize: found.objects.itemSize, layout: found.layout };
    }
  };
})();

registerEngine(unreal);
