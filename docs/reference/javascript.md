# JavaScript in Grembyte

Grembyte uses JavaScript wherever you tell it *what* to do with a value or with the game: hook templates, method hooks and scripts. You need little of the language for that. This page explains that part, along the [HermesQuest](../examples/index.md) examples – no previous knowledge of JavaScript assumed.

## Where JavaScript is used

| Where | Shape | Runs | Gets | Its `return` |
| --- | --- | --- | --- | --- |
| [Hook template](../guide/templates.md) (value hook) | a **function**: `ctx => …` or `function (ctx) { … }` | after every hooked write, in the writing thread | `ctx.old`, `ctx.new`, `ctx.delta`, `ctx.params`, `ctx.state` | the value to store; nothing keeps the game's value |
| [Method hook](../examples/hermesquest-hooks.md#method-hooks) with *Method script* | the **body** of a function: just the statements | before or after every call of the method | `ctx.args`, `ctx.this`, `ctx.result`, `ctx.phase`, `ctx.call`, `ctx.static`, `ctx.find` | after the call: the new result |
| [Script](../examples/hermesquest-scripts.md) | the **body** of a function | by hand or on a timer, on the game's main thread | `ctx.static`, `ctx.find`, `ctx.call`, `ctx.args`, `ctx.state`, `ctx.log` | shown as the script's result |
| Options of a choice parameter | the **body** of a function | when the run dialog asks for them | the same `ctx` as scripts | the list of options |

All of them get one object, `ctx` (for *context*), through which they reach everything else. The [hook context](hook-context.md) and [script context](script-context.md) references list its fields.

A **function** is code with a name for its input; a **body** is just the code inside. A template is a function because Grembyte stores and tests it on its own; a script is a body because Grembyte wraps it into a function for you:

```js
// A function (hook template): input ctx, result after =>
ctx => ctx.new * 2

// A body (script): statements, ctx is simply there
const stats = ctx.static("HermesQuest.GameState.Current").Player.Stats;
return stats.Gold;
```

## Statements, comments and names

A script is a list of **statements**, run from top to bottom. A statement usually ends with `;` – JavaScript tolerates missing ones, but writing them avoids surprises.

```js
// A comment: from // to the end of the line, ignored.
/* A longer comment
   over several lines. */

const player = ctx.static("HermesQuest.GameState.Current").Player;
let healed = 0;
healed = player.Heal(5);
```

- `const name = value;` gives a value a name. It cannot be given another value later – use it by default.
- `let name = value;` is the same, but may change later (`healed = …`).
- Names are **case-sensitive**: `player` and `Player` are different, and so are the game's fields – `Stats.Health`, never `stats.health`.

## Values

### Numbers

JavaScript has one number type: a 64-bit floating-point number (C#'s `double`). Grembyte converts the game's types for you:

| Game type | In JavaScript | Watch out |
| --- | --- | --- |
| `int`, `short`, `byte` … | number | When writing one back, cut decimals off with `Math.trunc` or `Math.round`. In value hooks Grembyte truncates integer results itself. |
| `long` (`int64`) | number | Exact only up to 2^53 (about 9 × 10^15). |
| `float` | number | The game keeps only about 7 significant digits. |
| `double` | number | Same precision as JavaScript. |

Calculating works as expected: `+ - * /`, `%` for the remainder, parentheses for order. Useful functions:

| | |
| --- | --- |
| `Math.round(x)`, `Math.trunc(x)`, `Math.floor(x)`, `Math.ceil(x)` | round, cut off, round down, round up |
| `Math.min(a, b)`, `Math.max(a, b)` | the smaller, the larger value |
| `Math.abs(x)` | the value without its sign |
| `x.toFixed(2)` | `x` as text with two decimal places, for display |

!!! warning "Comparing floats"
    `0.1 + 0.2 === 0.3` is `false` in JavaScript, as in almost every language: most decimal fractions cannot be stored exactly. A `float` from the game, read into JavaScript's `double`, is a little off as well. Compare with a tolerance instead:

    ```js
    if (Math.abs(stats.Mana - 42.5) < 0.001) …
    ```

### Text

Text (a *string*) is written in quotes: `"Hero"` or `'Hero'`. For text with values in it, use backticks and `${…}`:

```js
ctx.log(`${enemy.Name} has ${enemy.Stats.Health.toFixed(1)} health`);
// Goblin has 7.5 health
```

To test, take apart or clean up texts – names with a running number, numbers in a label – use [regular expressions](regex.md): `obj.name.replace(/_\d+$/, "")`.

### True and false

Comparisons give `true` or `false`:

| | |
| --- | --- |
| `a === b`, `a !== b` | equal, not equal |
| `a < b`, `a <= b`, `a > b`, `a >= b` | smaller, greater … |
| `x && y` | `x` **and** `y` |
| `x \|\| y` | `x` **or** `y` |
| `!x` | **not** `x` |

Always use `===` and `!==`. The shorter `==` converts values before comparing (`"1" == 1` is `true`), which hides mistakes.

### Nothing: `null` and `undefined`

- `null` means *no object*: `game.NearestEnemy(player, 8)` gives `null` when no enemy is near, `ctx.static("HermesQuest.GameState.Current")` before the first game exists.
- `undefined` means *not set*: a field of `ctx.state` you have not stored yet, or a function that ends without `return`.

Using a field of `null` is an error (`cannot read property 'Stats' of null`), so check first:

```js
const enemy = game.NearestEnemy(player, 8);
if (enemy === null)
  return "No enemy near";
ctx.log(enemy.Name);
```

## Decisions: `if`

```js
if (ctx.args.attacker.IsPlayer) {
  return ctx.result * 3;
} else if (ctx.args.target.IsPlayer) {
  return ctx.result / 2;
} else {
  ctx.log("enemy against enemy");
}
```

The block in `{ … }` runs only if the condition in `( … )` is `true`. With a single statement the braces may be left out, as in the [hooks examples](../examples/hermesquest-hooks.md#only-the-player-hits-harder-and-takes-less).

`return` ends the script (or function) at once. Everything after it is skipped – that is how `if (game === null) return;` stops a script early.

## Arrow functions and the conditional operator

Hook templates are usually one line:

```js
ctx => ctx.delta < 0 ? ctx.old + ctx.delta / 2 : ctx.new
```

Two pieces of syntax do the work:

- `ctx => …` is an **arrow function**: it takes `ctx` and gives back what follows the arrow.
- `condition ? a : b` is the **conditional operator**: `a` if the condition is true, else `b`.

So the template reads: *take `ctx`; if the value went down, give back the old value plus half the change, otherwise the value the game wrote.* With the player at 40 health and a hit of 6 the game writes 34; `ctx.delta` is −6, and the hook stores 40 + (−6 / 2) = 37.

The same in the long form, which can hold several statements:

```js
function (ctx) {
  if (ctx.delta < 0)
    return ctx.old + ctx.delta / 2;
  return ctx.new;
}
```

Both forms are fine for templates. Returning nothing (`undefined`) or `null` keeps what the game wrote.

## Lists and objects

A **list** (array) holds values in order, in square brackets; an **object** holds named values, in braces:

```js
const names = ["Strength", "Dexterity", "Intelligence"];
names[0]          // "Strength" – counting starts at 0
names.length      // 3

const option = { label: "Strength (melee damage)", value: "Strength" };
option.label      // "Strength (melee damage)"
```

Going through all entries of a list:

```js
for (const name of names)
  ctx.log(`${name}: ${stats[name]}`);
```

`stats[name]` reads the field whose name is in the variable `name` – the same as `stats.Strength` when `name` is `"Strength"`.

`ctx.state` is such an object. Store what a hook or script should remember in it:

```js
ctx.state.calls = (ctx.state.calls || 0) + 1;
```

`(ctx.state.calls || 0)` is the stored count, or 0 the first time, when it is still `undefined`.

## Game objects

What `ctx.static`, `ctx.find`, `ctx.args`, `ctx.this` or a method give you for an object of the game is a **wrapper**: a JavaScript object that reads from and writes to the game's memory each time you use it.

| You write | Grembyte does |
| --- | --- |
| `player.Stats` | reads the field `Stats` – an object again |
| `stats.Health` | reads the field `Health` from the game's memory, now |
| `stats.Health = 50` | writes 50 into the field |
| `player.IsDead` | runs the property's getter `get_IsDead` in the game |
| `player.Heal(10)` | calls the method `Heal` with 10 and gives its result |
| `ctx.call("HermesQuest.Combat:HitChance", player, enemy)` | calls a static method; objects can be passed along |
| `ctx.static("HermesQuest.GameState.Current")` | reads a static field |
| `player.className` | the object's class, `"HermesQuest.Character"` |

Because every read goes to the game, a value read twice may differ – mana regenerates between the two reads. Read it once into a `const` if you need it several times.

The wrapper's own names come first: `name`, `className`, `field`, `get`, `call`, and in Unity `component`. If the game has a field of one of these names, read it with `obj.field("name")`.

!!! tip "Finding the names"
    Return an object from a script, or click an object in the method monitor: the **object inspector** lists its fields, properties and methods with their exact names and types. The [catalog](../engines/index.md#catalog) lists every class and method.

## Finding mistakes

- **`ctx.log(…)`** writes to the script log; in hooks to the hook log below the Hooks tab and to the debug window. Log values to see what the code works with: `ctx.log(ctx.old, ctx.new, ctx.delta)`.
- **Test** in the template editor runs a value template without the game: enter old and new value and see what it stores. It runs in Qt's JavaScript engine, so Frida's functions (`ptr`, `Memory`) are missing there.
- **Debug** in the Hooks tab logs every call of the selected hooks with old value, game value, result, log output and errors.
- **Errors** appear with their message in the log. A value hook whose script fails keeps the game's value; a script that fails stops this run only.

## Things to remember

- Scripts and method hooks run **inside the game**, on its thread: keep them short. A loop that waits for something blocks the game.
- A value hook runs at **every write** of its instruction, possibly thousands of times a second. Avoid `ctx.log` there except while debugging.
- Writing a field bypasses the game's rules; calling its method follows them.
- After "New game" every object is new. Scripts that ask `ctx.static(…)` each run find the new ones by themselves.
