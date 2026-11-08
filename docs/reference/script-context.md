# Script context (`ctx`)

The object a script in the [Scripts](../guide/scripts.md) tab receives. It is separate from the [hook context](hook-context.md).

| Field | Meaning |
| --- | --- |
| `ctx.find(className[, objectName])` | Find a live object in Unity or Unreal. In Unity these are Unity objects (GameObjects, components); plain C# objects are reached through `ctx.static`. Unreal also accepts the object's name within its outer; class default objects and Widget Blueprint templates are not returned. |
| `ctx.findClass(className)` | Unreal: get a class object, for example for a `TSubclassOf` parameter. |
| `ctx.forEach(className, callback)` | Call `callback` for each live object of the class; stop early by returning `false`. |
| `ctx.outerName(object)` | Unreal: get an object's name including its outer, useful with `ctx.find`. |
| `ctx.call(method, ...args)` | Call a static game method, by a dotted name or the `Namespace.Class:Method` form shown in catalogs. A parameter list after it, as **Copy signature** gives it, is ignored: the method taking as many arguments as given is called. |
| `ctx.static(field)` | Mono and IL2CPP: read a static field, e.g. `ctx.static("HermesQuest.GameState.Current")` – the way to objects that are no Unity objects. |
| `ctx.gas` | Unreal games with the Gameplay Ability System: `attributes()` lists the sets; `get(name)` returns `{base, current}`, or `null` if there is no player; `setBase(name, value)` changes the base value through the game's code, keeping active effects; `findSet(regex)` returns the name of the player's first set the regular expression (a `RegExp` or its text) matches, or `null`. Names are `"Attribute"` or `"Set.Attribute"`; a set whose object Unreal numbers anew on every load (`AttributeSet_Health_2147445233`) is also found without the number (`"AttributeSet_Health.Health"`). |
| `ctx.native` | Machine code without an engine: find functions by their bytes and call them. See [below](#native-code-ctxnative). |
| `ctx.args` | Values of the script's parameters, by name. |
| `ctx.lang` | Current app language: `"de"` or `"en"`. |
| `ctx.log(...)` | Write to the script log. |
| `ctx.state` | Per-script state kept between runs. |
| `ctx.runs` | Number of earlier runs. |
| `ctx.scriptId` | The script's id. |

## Object wrappers

Objects from `ctx.find`, `ctx.forEach` and method results are wrappers:

| Use | Example |
| --- | --- |
| Call an instance method | `player.call("AddCurrency", 100)` |
| Read a field or property | `player.Health` |
| Write a field | `player.Stats.Gold = 500` |
| Read a field whose name clashes | `player.field("call")` |
| Unity: get a component of a GameObject (or of a component's GameObject) by class name, or `null` | `attacker.component("CharacterStats")` |

A wrapper returned from the script is shown as the result and can be opened in the object inspector.

## Native code (`ctx.native`)

Works in every game; in games without an engine adapter it is the way into the game (see [Scripts in native games](../guide/scripts.md#scripts-in-native-games)). Addresses are Frida `NativePointer`s; where a function takes one, a number or a text like `"0x140257270"` works too.

| Function | Meaning |
| --- | --- |
| `module([name])` | The game's executable as a Frida module (`name`, `base`, `size`, `path`), or the loaded module of that name. Under Wine this is the `.exe`, not what Frida's `Process.mainModule` reports. |
| `scan(pattern[, module])` | The one address where the byte pattern is found in the module (default: the executable). `pattern` is hex bytes with `??` (or `?`) for any byte, e.g. `"48 8B 05 ?? ?? ?? ?? C3"`; spaces are optional. No match or several matches throw an error, so a pattern that an update broke does not call the wrong code. Results are remembered while the module stays loaded. |
| `scanAll(pattern[, module])` | Every address where the pattern is found, lowest first. |
| `rip(address, dispOffset, length)` | The target of an x64 instruction's RIP-relative operand: the 32-bit displacement at `address + dispOffset`, counted from the instruction's end at `address + length`. `cmp dword [rip+x], 0` (`83 3D x x x x 00`) has `rip(at, 2, 7)`; `mov rax, [rip+x]` (`48 8B 05 x x x x`) has `rip(at, 3, 7)`. |
| `fn(address, returnType, argTypes[, abi])` | A callable native function (Frida's `NativeFunction`), e.g. `fn(at, "void", ["int", "uint8"])(11, 0)`. Types are Frida's: `"void"`, `"int"`, `"uint8"`, `"float"`, `"pointer"` and so on. The calling convention defaults to the platform's (Windows x64 under Wine, System V on Linux). |

