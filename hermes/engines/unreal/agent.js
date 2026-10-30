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
      throw new Error(`${identity.method} is not in ${identity.image} (game updated?)`);
    return found[0].method;
  }

  function compile(identity) {
    const code = codeOf(resolve(identity));
    if (code === null)
      throw new Error(`${identity.method} has no code`);
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
      if (!extra(0).readPointer().isNull())
        type.enumName = type.name;
    } else if (kind === 'EnumProperty') {
      const underlying = extra(0).readPointer();
      type.kind = underlying.isNull() ? KIND.U1 : valueType(underlying).kind;
      type.enumName = type.name;
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
    } else if (kind === 'ArrayProperty') {
      type.engine = 'array';
      const inner = arrayInner(field);
      type.element = inner.isNull() ? null : valueType(inner);
      type.elementSize = inner.isNull() ? 0 : inner.add(L.elementSize).readS32();
    } else {
      type.engine = 'other';  // FText, TMap, TSet, delegates: the type only
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

  function readValue(type, address) {
    // A value of a type with ``engine``: { text, string? } or { object }.
    switch (type.engine) {
      case 'bool':
        return { text: String((address.add(type.byteOffset).readU8() & type.mask) !== 0) };
      case 'string': {
        // FString: TArray<TCHAR> with the terminating 0
        const data = address.readPointer();
        const length = Math.max(0, address.add(Process.pointerSize).readS32() - 1);
        if (data.isNull() || length === 0)
          return { text: '""', string: true };
        const text = data.readUtf16String(Math.min(length, MAX_TEXT));
        return { text: JSON.stringify(text) + (length > MAX_TEXT ? '…' : ''), string: true };
      }
      case 'name':
        return { text: fieldName(address) };
      case 'weak':
        return { object: objectAt(address.readS32(), address.add(4).readS32()) };
      case 'interface':
        return { object: address.readPointer() };
      case 'array':
        return { text: arrayText(type, address) };
      default:
        return { text: type.name };
    }
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
      case 'name':
        return { value: fieldName(address) };
      case 'weak':
      case 'interface':
        return readValue(type, address);
      case 'array': {
        if (type.element === null)
          throw new Error(`the elements of ${type.name} are unknown`);
        const data = address.readPointer();
        const count = data.isNull() ? 0 : Math.max(0, address.add(Process.pointerSize).readS32());
        const elements = [];
        for (let i = 0; i < count; i++)
          elements.push(data.add(i * type.elementSize));
        return { elements, element: type.element };
      }
      default:
        throw new Error(`a ${type.name} cannot be read in scripts`);
    }
  }

  function writeValue(type, address, value) {
    // Store a script's value as a type with ``engine``: only bools can be.
    if (type.engine !== 'bool')
      throw new Error(`a ${type.name} cannot be changed in scripts`);
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

  const layouts = new Map();

  function layout(klass) {
    // { name, valueType, rank, headerSize, fields: [{ name, type, offset, static }] }
    // of a class or struct, inherited fields first. Offsets count from the
    // object's or struct's start (no header).
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
          static: false
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
      throw new Error(`${identity.method} has no code: a Blueprint event without a ` +
                      'Blueprint implementing it, or a delegate signature');
    const { params, returns, instance } = signatureOf(fn);
    return { ...code, frame: true, function: fn, params, returns, instance };
  }

  const signatures = new Map();

  function signatureOf(fn) {
    // { instance, parms: true, params: [{ name, type, offset }], returns,
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
      params.push({ name: fieldName(field.add(L.fieldName)), type: valueType(field, byref),
                    offset });
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
    // (call [reg + Func]); the others call into it.
    if (processEvent !== null)
      return processEvent;
    reflection();
    const s = objects();
    const index = s.address.findIndex((_address, i) => s.outer[i] !== 0
      && objectName(s, i) === 'Default__Object');
    if (index < 0)
      throw new Error('Default__Object not found');
    const vtable = ptr(s.address[index]).readPointer();
    const funcOffset = ue.layout.func;
    const found = [];
    for (let slot = 0; slot < 160; slot++) {
      const fn = vtable.add(slot * Process.pointerSize).readPointer();
      if (fn.compare(module.base) < 0 || fn.compare(module.base.add(module.size)) >= 0)
        break;
      let bytes;
      try {
        bytes = new Uint8Array(fn.readByteArray(0x800));
      } catch (_error) {
        // close to the end of the code: what is left of its page
        const left = 0x1000 - Number(fn.and(0xfff).toString());
        bytes = new Uint8Array(fn.readByteArray(left));
      }
      for (let at = 0; at + 6 <= bytes.length; at++) {
        // FF /2 with a 32-bit displacement: call [reg + disp32] (not rsp)
        if (bytes[at] === 0xff && (bytes[at + 1] & 0xf8) === 0x90 && (bytes[at + 1] & 7) !== 4
            && bytes[at + 2] === (funcOffset & 0xff) && bytes[at + 3] === (funcOffset >> 8)
            && bytes[at + 4] === 0 && bytes[at + 5] === 0) {
          found.push(fn);
          break;
        }
      }
    }
    if (found.length !== 1)
      throw new Error(`UObject::ProcessEvent not found (${found.length} candidates)`);
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
        reject(new Error('the game thread did not run game code for 3 s (paused or loading?)'));
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
      throw new Error('game code can only be called on the game thread');
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
        throw new Error(`a ${type.name} cannot be passed from scripts`);
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
      throw new Error(`the FName "${text}" is unknown (no object has it)`);
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
    return own === name || cppName(Number(klass.toString())) + own === name;
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
    throw new Error(`class ${name} not found (not loaded?)`);
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
    throw new Error(`${layout(klass).name} has no ${wantStatic ? 'static ' : ''}function ` +
                    `${name} with ${argumentCount} argument(s)`);
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
      throw new Error(`${wanted} not found`);
    return ptr(s.address[i]);
  }

  // className|name -> the object findObject found last: checked, not
  // searched for again (a search reads every object, tens of milliseconds on
  // the game thread)
  const foundObjects = new Map();

  function stillFound(object, klass, named) {
    // Whether a cached object is still alive, of the class (or a subclass)
    // and, if given, named ``named``.
    if (!isObject(object))
      return false;
    const name = fieldName(object.add(OBJECT_NAME));
    if (name.startsWith('Default__') || (named !== null && name !== named))
      return false;
    return classChain(object.add(OBJECT_CLASS).readPointer())
      .some(current => Number(current.toString()) === klass);
  }

  function findObject(className, named = null) {
    // A live object (not a class default object) of the class ``className``
    // or a subclass, e.g. findObject("AlabamaCheatManager"); with ``named``
    // the one of that name, e.g. findObject("BP_Hero_C", "BP_Hero_C_1").
    // null if none.
    reflection();
    const klass = Number(findClass(className).toString());
    const key = `${className}|${named}`;
    const cached = foundObjects.get(key);
    if (cached !== undefined && stillFound(cached, klass, named))
      return cached;
    foundObjects.delete(key);
    for (let fresh = 0; fresh < 2; fresh++) {
      const s = objects();
      const kinds = new Map();
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
      for (let i = 0; i < s.address.length; i++) {
        if (!derives(s.klass[i]))
          continue;
        const name = objectName(s, i);
        if (name.startsWith('Default__') || (named !== null && name !== named))
          continue;
        const object = ptr(s.address[i]);
        if (isObject(object)) {
          foundObjects.set(key, object);
          return object;
        }
      }
      snapshot = null;  // the object may be newer than the snapshot
    }
    return null;
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
    classChain,
    objectClassName: object => layout(classOf(object)).name,
    virtualMethod,
    methods,
    invoke,
    writeParameter,
    newString: () => {
      throw new Error('Unreal has no string objects: FString arguments are written in place');
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
