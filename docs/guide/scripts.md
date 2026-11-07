# Scripts

Scripts are JavaScript function bodies that run on the game's main thread and can call its methods: give the player health every few seconds, set an attribute after loading, or run a cheat function by hand. They live in the **Scripts** tab next to **Hooks**, are stored per program and restored when that program is attached. **New …** and **Edit** open the script editor with the code, the schedule and the parameters.

<figure markdown="span">
  ![Scripts tab](../assets/screenshots/scripts-tab-dark.webp#only-dark)
  ![Scripts tab](../assets/screenshots/scripts-tab-light.webp#only-light)
  <figcaption>Scripts for an Unreal game with their schedule, and the script log below.</figcaption>
</figure>

Scripts reach game objects and methods through the engine adapters, so they need a [Unity or Unreal game](../engines/index.md).

## When a script runs

| Mode | Runs |
| --- | --- |
| **Once** | after the configured delay, each time the script starts |
| **Repeat** | every configured interval; the next interval starts after the previous run finished, so runs do not overlap |
| **Manual** | only when you click **Run now** |

## Parameters

Scripts can have parameters of the types text, number, yes/no or choice. A manual run asks for their values; scheduled runs use their defaults. The script reads them from `ctx.args`.

<figure markdown="span">
  ![Script parameters](../assets/screenshots/script-params-dark.webp#only-dark){ width="560" }
  ![Script parameters](../assets/screenshots/script-params-light.webp#only-light){ width="560" }
</figure>

A choice parameter gets its options from a short JavaScript function evaluated in the game, returning values or `{label, value}` objects. The list filters as you type.

## Writing scripts

The script's `ctx` gives access to the game; all fields are in the [script context reference](../reference/script-context.md). Objects returned by `ctx.find` are wrappers: call instance methods with `.call(name, ...args)`, read fields and properties as properties, and use `.field(name)` where a name clashes. A returned object is shown as the result and can be opened in the object inspector.

```js title="Restore 50 health in Avowed (Unreal)"
ctx.find("AlabamaCheatManager").call("GiveHealth", 50);
```

```js title="Keep an attribute at a value (Unreal, Gameplay Ability System)"
const might = ctx.gas.get("Might");
if (might && might.base < 14) ctx.gas.setBase("AttributeSetCore.Might", 14);
```

```js title="Count runs and remember state"
ctx.state.total = (ctx.state.total || 0) + 1;
ctx.log(`run ${ctx.runs + 1}, total ${ctx.state.total}`);
```

Scripts are different from [hook templates](templates.md): a template changes one value at one write, a script runs on its own and calls the game.
