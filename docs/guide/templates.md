# Hook templates

A manipulation is a JavaScript function that runs in the game after every hooked write. It receives a `ctx` object and returns the value to store; `undefined` or `null` keeps the game's value. Integer results are truncated and limited to the range of the type.

```js title="The built-in Multiplier"
function (ctx) {
  const { factor, direction } = ctx.params;
  if (direction === 'increase' && ctx.delta <= 0)
    return undefined;
  if (direction === 'decrease' && ctx.delta >= 0)
    return undefined;
  return ctx.old + ctx.delta * factor;
}
```

The fields of `ctx` are listed in the [hook context reference](../reference/hook-context.md).

## Managing templates

Manipulations are stored in the database as **hook templates** and managed under **View → Hook templates …**. At first start Grembyte creates the examples **Multiplier**, **Fixed value** and **Clamp** for value hooks and **Result × factor** and **Method script** for [method hooks](../engines/index.md); they can be changed or deleted like templates of your own. Their texts are English whatever the language of the interface, since templates are data that exports share.

<figure markdown="span">
  ![Hook templates](../assets/screenshots/hook-templates-dark.webp#only-dark)
  ![Hook templates](../assets/screenshots/hook-templates-light.webp#only-light)
  <figcaption>The built-in Multiplier with its parameters and its function.</figcaption>
</figure>

A template consists of a name, an optional description, its **Kind**, the JavaScript function and parameters. The kind says what the template is for: a **Value hook** changes a written value (`ctx.old`, `ctx.new`), a **Method hook** the arguments or the result of a method (`ctx.args`, `ctx.result`, see [method hooks](../reference/hook-context.md#method-hooks)). The hook dialogs offer only templates of their kind. Each parameter has

- a **name** – `ctx.params.<name>` in the script,
- a **label** shown in the hook dialog,
- a **type**: *Number*, *Choice* or *Code* for multi-line text,
- a **default**, and whether it is **optional**.

Choice values are written as `value=Label; value=Label`. The hook dialog builds its form from the parameters. A template with the number parameter `threshold` that only lets large gains through:

```js
ctx => ctx.delta >= ctx.params.threshold ? ctx.new : ctx.old
```

## Testing without a game

**Test** runs the function without a game process: enter the value type, the old value (`ctx.old`) and the value written by the game (`ctx.new`) and run it. It shows the stored value, the output of `ctx.log`/`console.log`, and errors. `ctx.state` is kept between test runs until **Reset state** clears it.

A method template is tested with a **Result of the method** instead. Without the game there are no arguments and objects: `ctx.args` and `ctx.refs` are empty, `ctx.this` and `ctx.caller` are `null`, and `ctx.call()` throws.

!!! note
    The test runs in Qt's JavaScript engine rather than in Frida, so Frida APIs such as `ptr()` or `Memory` are not available there.

## Templates and existing hooks

A hook stores a **copy** of the template it was created from, so later changes to the template do not affect existing hooks. In the hook's **Edit** dialog Grembyte points out a changed template and offers **Use changed template**. If the template is deleted, the hook keeps working with its copy.

## Examples

```js title="Never lose gold"
ctx => Math.max(ctx.new, ctx.old)
```

```js title="Count the calls and log every tenth"
ctx => {
  ctx.state.calls = (ctx.state.calls || 0) + 1;
  if (ctx.state.calls % 10 === 0) ctx.log(`call ${ctx.state.calls}: ${ctx.old} → ${ctx.new}`);
  return ctx.new;
}
```

```js title="Halve damage taken, keep healing as it is"
ctx => ctx.delta < 0 ? ctx.old + ctx.delta / 2 : ctx.new
```
