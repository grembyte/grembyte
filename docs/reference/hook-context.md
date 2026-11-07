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
| `ctx.log(…)` | Output in the hook log and the debug window |

Integer results are truncated and limited to the range of the type. `int64` and `uint64` values are exact only up to 2^53.

Method hooks in Unity and Unreal games can also use `ctx.call`, `ctx.static` and `ctx.find` to reach other game code; see [Engines](../engines/index.md#method-hooks).

The [Scripts](../guide/scripts.md) tab has a `ctx` of its own, described in the [script context reference](script-context.md).
