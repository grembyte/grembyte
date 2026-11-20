# Script context (`ctx`)

The object a script in the [Scripts](../guide/scripts.md) tab receives. It is separate from the [hook context](hook-context.md).

| Field | Meaning |
| --- | --- |
| `ctx.find(className[, objectName])` | Find a live object in Unity or Unreal. In Unity these are Unity objects (GameObjects, components); plain C# objects are reached through `ctx.static`. Unreal also accepts the object's name within its outer; class default objects and Widget Blueprint templates are not returned. |
| `ctx.findClass(className)` | Unreal: get a class object, for example for a `TSubclassOf` parameter. |
| `ctx.forEach(className, callback)` | Call `callback` for each live object of the class; stop early by returning `false`. |
| `ctx.object(address)` | The object at an address as a [wrapper](#object-wrappers), its class read from the object itself; `null` for a null pointer. The address is a `NativePointer`, a number or a text like `"0x7f12…"`. For pointers the reflection does not name, e.g. `ctx.object(player.address.add(0x17d0).readPointer())`. |
| `ctx.isPlaying()` | Unreal: whether the player is in the game – a local player controller with a pawn, the game not paused, and the camera on the pawn. `false` in the main menu, the pause menu, usually in cutscenes and while loading. Menus that neither pause the game nor move the camera (an inventory in some games) are not noticed. |
| `ctx.outerName(object)` | Unreal: get an object's name including its outer, useful with `ctx.find`. |
| `ctx.enum(enumName)` | Unreal and Mono: the values of an enum by their names, e.g. `ctx.enum("ELSecondStat").E_HEALTH_POINT_MAX` is `71`. Unreal names the enum as the object inspector shows it (`ELSecondStat`), without the prefix of its values and without UHT's `_MAX`; Mono by its full class name (`Game.DamageType`). A value with several names keeps the first. IL2CPP does not name the values of enums: use the numbers there. See [below](#enums-and-unreal-arrays). |
| `ctx.enumName(enumName, value)` | Unreal and Mono: the name of an enum's value, e.g. `ctx.enumName("ELSecondStat", 71)` is `"E_HEALTH_POINT_MAX"`; `null` if no value has that number. |
| `ctx.call(method, ...args)` | Call a static game method, by a dotted name or the `Namespace.Class:Method` form shown in catalogs. A parameter list after it, as **Copy signature** gives it, is ignored: the method taking as many arguments as given is called. |
| `ctx.static(field)` | Mono and IL2CPP: read a static field, e.g. `ctx.static("GrembyteQuest.GameState.Current")` – the way to objects that are no Unity objects. |
| `ctx.gas` | Unreal games with the Gameplay Ability System: `attributes()` lists the sets; `get(name)` returns `{base, current}`, or `null` if there is no player; `setBase(name, value)` changes the base value through the game's code, keeping active effects; `findSet(regex)` returns the name of the player's first set the regular expression (a `RegExp` or its text) matches, or `null`. Names are `"Attribute"` or `"Set.Attribute"`; a set whose object Unreal numbers anew on every load (`AttributeSet_Health_2147445233`) is also found without the number (`"AttributeSet_Health.Health"`). |
| `ctx.native` | Machine code without an engine: find functions by their bytes and call them. See [below](#native-code-ctxnative). |
| `ctx.args` | Values of the script's parameters, by name. |
| `ctx.lang` | Current app language: `"de"` or `"en"`. |
| `ctx.log(...)` | Write to the script log. |
| `ctx.state` | Per-script state kept between runs. |
| `ctx.runs` | Number of earlier runs. |
| `ctx.scriptId` | The script's id. |

## Object wrappers

Objects of the game – from `ctx.find`, `ctx.forEach`, `ctx.static` and method results in scripts, `ctx.this`, object arguments and results in [method hooks](hook-context.md#method-hooks) – are wrappers. Every use reads from or writes to the game at that moment:

| Use | Example |
| --- | --- |
| Read a field or property (a property runs its getter in the game) | `player.Health`, `player.IsDead` |
| Write a field, or a property (which calls its setter) | `player.Stats.Gold = 500` |
| Call a method, instance or static, with its result | `player.Heal(10)` |
| Read or write a static field of the object's class (Mono, IL2CPP), the same as `ctx.static` | `gameState.s_playerCharacter` |
| The class name | `player.className` |
| The address of the object | `player.address` |
| Unity: the name of a GameObject or component, else `null`; Unreal: the object's name | `player.name` |
| Unity: a component of a GameObject (or of a component's GameObject) by class name, or `null` | `attacker.component("CharacterStats")` |
| Call a method by name, read a field or a property by name | `player.call("Heal", 10)`, `player.field("Gold")`, `player.get("IsDead")` |

The wrapper's own names come first: `name`, `className`, `address`, `field`, `get`, `call` and `component`. If the game has a field or method of one of these names, use `.field("name")`, `.get("name")` or `.call("name", …)`.

A wrapper returned from the script is shown as the result and can be opened in the object inspector.

## Enums and Unreal arrays

An enum field reads as its number. `ctx.enum` turns names into numbers, `ctx.enumName` numbers into names; the names are read once and kept, so both are quick in a loop too. In Unreal games, scripts look an enum up before their next run on Grembyte's own thread, as they do with classes, so the game's thread does not search for it.

An Unreal `TArray` reads as a JavaScript array; an array of structs as wrappers of its elements, whose fields can be read and written like an object's. Lies of P, for example, keeps the player's values in a list of 196 entries, one per `ELSecondStat`:

```js title="Unreal: change a stat of the player by the enum's name"
const STAT = ctx.enum("ELSecondStat");          // { E_DEFAULT: 1, …, E_HEALTH_POINT_MAX: 71, … }
const list = ctx.find("PlayerController").Pawn.StatComponent.StatDataList.StatList;
const stat = type => list.find(entry => entry.StatType === type);

stat(STAT.E_WEIGHT_LIMIT).Value = 2000;
ctx.log(ctx.enumName("ELSecondStat", 71));      // "E_HEALTH_POINT_MAX"
```

```js title="Unreal: list the values that are set, by name"
const list = ctx.find("PlayerController").Pawn.StatComponent.StatDataList.StatList;
for (const entry of list) {
  if (entry.Value !== 0)
    ctx.log(ctx.enumName("ELSecondStat", entry.StatType), entry.Value);
}
```

```js title="The names of all values at once, by number"
const names = Object.fromEntries(
  Object.entries(ctx.enum("ELSecondStat")).map(([name, value]) => [value, name]));
names[71];   // "E_HEALTH_POINT_MAX"
```

Going through the player's controller (`ctx.find("PlayerController").Pawn`) finds the player's own list: `ctx.find("LStatList")` alone may give an enemy's. The list is made anew when the game loads a save or an area, so a repeating script looks it up on every run rather than keeping it.

## Memory at an address

Scripts run in Frida's JavaScript runtime, in every game: Unity (Mono, IL2CPP), Unreal and native. Besides `ctx`, Frida's own API is there – `ptr`, `NativePointer`, `Memory`, `Process`, `Module`. With it a script reads and writes memory directly, at a fixed address or at an offset the reflection does not name.

`ptr(value)` makes a `NativePointer` from a number or a text like `"0x147D94408"`. A wrapper's `address` and the results of `ctx.native` are `NativePointer`s already.

| Function | Meaning |
| --- | --- |
| `p.add(offset)`, `p.sub(offset)` | The address `offset` bytes further or back; `p` itself does not change. |
| `p.readPointer()`, `p.writePointer(q)` | Read or write a pointer (8 bytes in 64-bit games, 4 in 32-bit ones). |
| `p.readS8()` … `p.readS64()`, `p.readU8()` … `p.readU64()` | Read a signed or unsigned integer of 8, 16, 32 or 64 bits. 64-bit ones come as Frida's `Int64`/`UInt64`; `.toNumber()` makes a number of them. |
| `p.writeS32(value)`, `p.writeU8(value)` and so on | Write an integer of that size. |
| `p.readFloat()`, `p.readDouble()`, `p.writeFloat(value)`, `p.writeDouble(value)` | Read or write a floating-point number. |
| `p.readUtf8String([length])`, `p.readUtf16String([length])` | Read text up to its terminating zero or of `length` characters. Unity's and Unreal's strings are UTF-16. |
| `p.readByteArray(length)` | Read raw bytes as an `ArrayBuffer`, e.g. for `hexdump(…)`. |
| `p.isNull()`, `p.equals(q)`, `p.toString()` | Test for null, compare, show as `"0x…"`. |

Fixed addresses change when the game is updated, and absolute ones also when the module is loaded elsewhere. An address relative to the module (`ctx.native.module().base.add(…)`), a pattern (`ctx.native.scan`) or an offset from an object the script finds by name lasts longer.

```js title="Unity: a field the reflection does not show, by its offset"
const stats = ctx.find("CharacterStats");
const p = stats.address.add(0x1c4);
ctx.log(`raw value ${p.readS32()}`);
p.writeS32(99);
```

```js title="Unreal: follow a pointer, then wrap what it points to"
const pawn = ctx.find("BP_PlayerCharacter_C");
const component = pawn.address.add(0x17d0).readPointer();
if (!component.isNull()) {
  const obj = ctx.object(component);
  ctx.log(obj.className, obj.name);
}
```

```js title="A fixed address in the game's executable"
const p = ctx.native.module().base.add(0x7d94408);  // or ptr("0x147D94408")
ctx.log(p.readFloat());
p.writeFloat(250.0);
```

A wrong address throws an access violation, which ends up in the script log. Writing to a wrong address that is mapped can crash the game: check the address in the [memory viewer](../guide/tools.md#memory-viewer) first. Frida's [JavaScript API](https://frida.re/docs/javascript-api/#nativepointer) lists all functions.

## Native code (`ctx.native`)

Works in every game; in games without an engine adapter it is the way into the game (see [Scripts in native games](../guide/scripts.md#scripts-in-native-games)). Addresses are Frida `NativePointer`s; where a function takes one, a number or a text like `"0x140257270"` works too.

| Function | Meaning |
| --- | --- |
| `module([name])` | The game's executable as a Frida module (`name`, `base`, `size`, `path`), or the loaded module of that name. Under Wine this is the `.exe`, not what Frida's `Process.mainModule` reports. |
| `scan(pattern[, module])` | The one address where the byte pattern is found in the module (default: the executable). `pattern` is hex bytes with `??` (or `?`) for any byte, e.g. `"48 8B 05 ?? ?? ?? ?? C3"`; spaces are optional. No match or several matches throw an error, so a pattern that an update broke does not call the wrong code. Results are remembered while the module stays loaded. |
| `scanAll(pattern[, module])` | Every address where the pattern is found, lowest first. |
| `rip(address, dispOffset, length)` | The target of an x64 instruction's RIP-relative operand: the 32-bit displacement at `address + dispOffset`, counted from the instruction's end at `address + length`. `cmp dword [rip+x], 0` (`83 3D x x x x 00`) has `rip(at, 2, 7)`; `mov rax, [rip+x]` (`48 8B 05 x x x x`) has `rip(at, 3, 7)`. |
| `fn(address, returnType, argTypes[, abi])` | A callable native function (Frida's `NativeFunction`), e.g. `fn(at, "void", ["int", "uint8"])(11, 0)`. Types are Frida's: `"void"`, `"int"`, `"uint8"`, `"float"`, `"pointer"` and so on. The calling convention defaults to the platform's (Windows x64 under Wine, System V on Linux). |

