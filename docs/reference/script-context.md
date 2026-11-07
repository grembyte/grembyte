# Script context (`ctx`)

The object a script in the [Scripts](../guide/scripts.md) tab receives. It is separate from the [hook context](hook-context.md).

| Field | Meaning |
| --- | --- |
| `ctx.find(className[, objectName])` | Find a live object in Unity or Unreal. Unreal also accepts the object's name within its outer; class default objects and Widget Blueprint templates are not returned. |
| `ctx.findClass(className)` | Unreal: get a class object, for example for a `TSubclassOf` parameter. |
| `ctx.forEach(className, callback)` | Call `callback` for each live object of the class; stop early by returning `false`. |
| `ctx.outerName(object)` | Unreal: get an object's name including its outer, useful with `ctx.find`. |
| `ctx.call(method, ...args)` | Call a static game method, by a dotted name or the `Namespace.Class:Method` form shown in catalogs. |
| `ctx.gas` | Unreal games with the Gameplay Ability System: `attributes()` lists the sets; `get(name)` returns `{base, current}`, or `null` if there is no player; `setBase(name, value)` changes the base value through the game's code, keeping active effects. |
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
| Read a field whose name clashes | `player.field("call")` |

A wrapper returned from the script is shown as the result and can be opened in the object inspector.
