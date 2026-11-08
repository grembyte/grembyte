# Scripts

Scripts are JavaScript function bodies that run on the game's main thread and can call its methods: give the player health every few seconds, set an attribute after loading, or run a cheat function by hand. They live in the **Scripts** tab next to **Hooks**, are stored per program and restored when that program is attached. **New …** and **Edit** open the script editor with the code, the schedule and the parameters.

<figure markdown="span">
  ![Scripts tab](../assets/screenshots/scripts-tab-dark.webp#only-dark)
  ![Scripts tab](../assets/screenshots/scripts-tab-light.webp#only-light)
  <figcaption>Scripts for an Unreal game with their schedule, and the script log below.</figcaption>
</figure>

Scripts reach game objects and methods through the engine adapters of [Unity and Unreal games](../engines/index.md). In a game without an adapter – a native game with an engine of its own – they work on machine code instead: see [Scripts in native games](#scripts-in-native-games).

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

A choice parameter gets its options from a short JavaScript function evaluated in the game, returning values or `{label, value}` objects. The list filters as you type. The [Unreal page](../engines/unreal.md#example-a-script-with-a-choice-parameter) shows one in Gothic 1 Remake.

## Writing scripts

The script's `ctx` gives access to the game; all fields are in the [script context reference](../reference/script-context.md). Objects returned by `ctx.find` are wrappers: call instance methods with `.call(name, ...args)`, read fields and properties as properties, and use `.field(name)` where a name clashes. A returned object is shown as the result and can be opened in the object inspector.

```js title="Heal the player by 10 health (HermesQuest, Unity Mono)"
const player = ctx.static("HermesQuest.GameState.Current").Player;
return `Healed ${player.Heal(10).toFixed(1)} health`;
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

## Scripts in native games

Without an engine adapter Hermes looks for a function the game calls every frame on its main thread and runs scripts each time it returns. It hooks the ones the game exports, in this order: `SDL_PollEvent`, `SDL_PumpEvents`, `SDL_GL_SwapWindow`, `glfwPollEvents`, `glfwSwapBuffers`, `PeekMessageW`, `PeekMessageA`, `SwapBuffers`, `wglSwapBuffers`, `glXSwapBuffers`, `eglSwapBuffers`, `vkQueuePresentKHR`, and for GTK and other GLib programs `g_main_context_iteration`. The first of them that runs on the main thread within half a second is kept (if none does, the first that runs on another thread). The first run logs which one it is. A game that exports none of them, for example one with SDL linked in statically, gets an error instead.

There are no class or method names to go by, so `ctx.native` finds code by its bytes: a pattern of the function's first instructions, with `??` for the bytes that change between builds (addresses, offsets). A pattern survives game updates that leave the function alone, unlike a fixed address. The [code explorer](code-explorer.md) finds the code and copies its pattern, or a whole script skeleton. The [script context reference](../reference/script-context.md#native-code-ctxnative) lists the functions.

```js title="Open the hidden debug menu (Underkeep, own engine, under Proton)"
const n = ctx.native;
// OpenPage(int page, bool replace): push rbp / sub rsp, 0x20 / cmp [menu], 0 / mov ebp, ecx / je
const openPageAt = n.scan("40 55 48 83 EC 20 83 3D ?? ?? ?? ?? 00 8B E9 0F 84");
// The cmp at +6 reads the menu object: +0 state (0: closed), +4 page
const menu = n.rip(openPageAt.add(6), 2, 7);
if (menu.readS32() === 0) return "Open the in-game menu first (Esc)";
n.fn(openPageAt, "void", ["int", "uint8"])(11, 0);
return "Debug menu opened";
```

Native scripts run with Frida's full JavaScript API: memory is read and written through `NativePointer` (`menu.add(4).readS32()`), and `Process`, `Module` and `Memory` are available as in any Frida script. A wrong call can crash the game: check a function's parameters in the disassembly first, and try the script by hand before giving it a timer.

