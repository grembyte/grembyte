# Hook context (`ctx`)

The object a [hook template](../guide/templates.md) and a method hook's script receive. The script returns the value to store; `undefined` or `null` keeps the game's value.

| Field | Meaning |
| --- | --- |
| `ctx.old` | Value before the write |
| `ctx.new` | Value the game wrote |
| `ctx.delta` | `ctx.new - ctx.old` |
| `ctx.params` | The template's parameters with the hook's values, by name |
| `ctx.state` | Object kept between calls, e.g. for counters |
| `ctx.this` | Method hooks only: the instance the method runs on; `null` for static methods |
| `ctx.valueType` | The value type, e.g. `"int32"` or `"float"` |
| `ctx.address` | Address that was written |
| `ctx.writePc` | Address of the write instruction |
| `ctx.threadId` | Thread that wrote |
| `ctx.hookId` | Database id of the hook |
| `ctx.log(…)`, `console.log(…)` | Output in the hook log and the debug window |

Integer results are truncated and limited to the range of the type. `int64` and `uint64` values are exact only up to 2^53.

!!! note "Hooks of the ptrace backend"
    With the `ptrace` [backend](../guide/hooks.md) the script runs in Grembyte, not in the game, after the game wrote the value. `ctx.old`, `ctx.new`, `ctx.delta`, `ctx.params`, `ctx.state` and `ctx.valueType` are as above, but `ctx.address` and `ctx.writePc` are `"0x0"`, `ctx.threadId` and `ctx.hookId` are `0`, and what `ctx.log` writes is not shown. Frida's APIs (`ptr()`, `Memory`, …) are not available there.

## Method hooks

A method hook's script runs before or after the hooked method (**When**). Besides `ctx.params`, `ctx.state`, `ctx.this`, `ctx.log(…)` and `ctx.hookId` it gets:

| Field | Meaning |
| --- | --- |
| `ctx.phase` | `"before"` or `"after"` the call |
| `ctx.args.<name>` | The method's arguments by name. Before the call, assigning one (`ctx.args.amount = 0`) changes what the method gets. |
| `ctx.result` | After the call: the method's result (`undefined` for `void` methods). A returned value replaces it. |
| `ctx.refs.<name>` | After the call: what `ref` and `out` arguments hold |
| `ctx.caller` | The innermost hooked call this call runs in, if that hook runs "after the call": an object with `.method`, `.this`, `.args` and `.caller`; otherwise `null` |
| `ctx.method` | The hooked method |
| `ctx.threadId` | Thread the call runs on |
| `ctx.call(method, ...args)` | Call a static game method, as in [scripts](script-context.md) |
| `ctx.static(field)` | Mono and IL2CPP: read a static field |
| `ctx.find(className[, objectName])`, `ctx.forEach(…)`, `ctx.outerName(…)` | Find live objects, as in [scripts](script-context.md) |

Objects – `ctx.this`, object arguments, results – are the same [wrappers as in scripts](script-context.md#object-wrappers): fields, properties and methods directly (`target.IsPlayer`, `obj.GetComponentByName("Stats")`). Before the call, assigning a field (`obj.Field = …`) or property (which calls its setter) changes what the method works with.

In Unreal games a hook called from Blueprint bytecode only gets arguments that are simple variables or constants, and only `ref` arguments can be changed. See [Engines](../engines/index.md#method-hooks) for an overview and the [HermesQuest examples](../examples/hermesquest-hooks.md#method-hooks) for method hooks step by step.

The [Scripts](../guide/scripts.md) tab has a `ctx` of its own, described in the [script context reference](script-context.md).
