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
    throw new Error('the name pool (FNamePool) was not found; Unreal Engine before 4.23?');
  }

  // FChunkedFixedUObjectArray: Objects, PreAllocatedObjects, MaxElements,
  // NumElements, MaxChunks, NumChunks. Chunks hold 65536 FUObjectItems, whose
  // size differs between versions and games (Object pointer first).
  const CHUNK = 0x10000;
  const ITEM_SIZES = [0x18, 0x10, 0x14, 0x20, 0x28];
  // UObjectBase: vtable, ObjectFlags, InternalIndex, ClassPrivate, NamePrivate, OuterPrivate
  const OBJECT_INDEX = 0x0c, OBJECT_CLASS = 0x10, OBJECT_NAME = 0x18, OBJECT_OUTER = 0x20;

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
    throw new Error('the object table (GUObjectArray) was not found');
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
      throw new Error('no Unreal Engine game in the process');
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
      throw new Error('package /Script/CoreUObject not found');
    const inCore = new Map();
    s.address.forEach((address, i) => {
      if (s.outer[i] === core)
        inCore.set(`${objectName(s, i)}|${s.klass[i]}`, address);
    });
    const classClass = s.address.find((address, i) =>
      s.outer[i] === core && s.klass[i] === address && objectName(s, i) === 'Class');
    if (classClass === undefined)
      throw new Error('class Class not found in /Script/CoreUObject');
    const coreClass = name => {
      const found = inCore.get(`${name}|${classClass}`);
      if (found === undefined)
        throw new Error(`class ${name} not found in /Script/CoreUObject`);
      return found;
    };
    const struct = coreClass('Struct'), functionClass = coreClass('Function');
    const objectClass = coreClass('Object'), scriptStruct = coreClass('ScriptStruct');
    const coreStruct = name => {
      const found = inCore.get(`${name}|${scriptStruct}`);
      if (found === undefined)
        throw new Error(`struct ${name} not found in /Script/CoreUObject`);
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
      throw new Error(`cannot find ${what} (unknown Unreal Engine layout)`);
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
      throw new Error('cannot find UStruct::ChildProperties (unknown Unreal Engine layout)');
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
      throw new Error('FTransform has no Rotation');
    L.extra = search((L.offset + 4 + 7) & ~7, L.offset + 0x60, 8,
      at => rotation.add(at).readPointer().equals(quat), 'the end of FProperty');

    // UFunction: FunctionFlags, NumParms, ParmsSize, ReturnValueOffset, ...
    // Func. UObject's ExecuteUbergraph(int32) is the sample: 1 parameter, 4
    // bytes, no return value. It runs Blueprint code: its Func is the
    // script interpreter (UObject::ProcessInternal).
    const ubergraph = ptr(objectClass).add(L.children).readPointer();
    if (ubergraph.isNull() || nameOfAddress(Number(ubergraph.toString())) !== 'ExecuteUbergraph')
      throw new Error('UObject::ExecuteUbergraph not found');
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
      case 'ArrayProperty': return `TArray<${inner(0)}>`;
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
      throw new Error(`package ${assembly} is not loaded`);
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
      for (const fn of functionsOf(klass.address)) {
        if (!wanted.has(token(klass, fn)))
          continue;
        const entry = methodEntry(klass, fn);
        result.push({ className: klass.name, method: fn.address, entry: entry.slice(0, 6),
                      flags: entry[6], implFlags: entry[7] });
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

  function releaseCode() {
    // Give the Blueprint functions their interpreter back; the stubs stay,
    // a thread may still be in one.
    for (const { fn, code } of redirected.values()) {
      try {
        fn.add(ue.layout.func).writePointer(code);
      } catch (_error) {
        // freed meanwhile
      }
    }
    redirected.clear();
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
    // The UFunction of an identity (see identify).
    const found = methodsByToken(identity.image, [identity.token]);
    if (found.length === 0)
      throw new Error(`${identity.method} is not in ${identity.image} (game updated?)`);
    return found[0].method;
  }

  function compile(identity) {
    const code = codeOf(resolve(identity));
    if (code === null)
      throw new Error(`${identity.method} has no code`);
    return code;
  }

  refresh();
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
    // For checks and troubleshooting: where the reflection was found.
    reflection: () => {
      const found = reflection();
      return { module: module.name, names: found.names, objects: found.objects.table,
               itemSize: found.objects.itemSize, layout: found.layout };
    }
  };
})();

registerEngine(unreal);
