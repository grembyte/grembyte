# Scripts

Scripts are JavaScript function bodies that run on the game's main thread and can call its methods: give the player health every few seconds, set an attribute after loading, or run a cheat function by hand. They live in the **Scripts** tab next to **Hooks**, are stored per program and restored when that program is attached. **New …** and **Edit** open the [script editor](#the-script-editor) with the code, the schedule and the parameters.

<figure markdown="span">
  ![Scripts tab](../assets/screenshots/scripts-tab-dark.webp#only-dark)
  ![Scripts tab](../assets/screenshots/scripts-tab-light.webp#only-light)
  <figcaption>Scripts for an Unreal game with their schedule, and the script log below.</figcaption>
</figure>

**Run now** runs the selected script at once (asking for its parameters), **Pause** and **Resume** stop and restart its schedule, and **Remove** deletes it. The context menu also has **Create copy**. Below the table the script log shows what scripts write with `ctx.log` and what they return; **Inspect** behind a returned object opens it in the [object inspector](../engines/index.md#object-inspector), and **Clear** empties the log.

Scripts reach game objects and methods through the engine adapters of [Unity and Unreal games](../engines/index.md). In a game without an adapter – a native game with an engine of its own – they work on machine code instead: see [Scripts in native games](#scripts-in-native-games).

## The script editor

The editor does not block the rest of Grembyte: while it is open you can look things up in the [object inspector](../engines/index.md#object-inspector), the [object browser](object-browser.md) or your [notes](notes.md). **Pin to main window** moves it into the **Editor** tab beside **Tracked addresses**, **Hooks** and **Scripts**: there it has the window's width, the scan area folds away while the tab is in front, and the tools [docked at the side](tools.md#docking-windows) stay beside it. **Detach** gives it a window of its own again. The tab shows only while an editor is in it.

<figure markdown="span">
  ![Script editor in the Editor tab](../assets/screenshots/script-editor-tab-dark.webp#only-dark)
  ![Script editor in the Editor tab](../assets/screenshots/script-editor-tab-light.webp#only-light)
  <figcaption>A script in the Editor tab, the object inspector of the component it changes docked beside it.</figcaption>
</figure>

- **Edit** opens the editor where the last one was: in the Editor tab or in a window. The context menu of a script also offers **Edit in a window** and **Edit in the Editor tab**, for this once.
- One script is open at a time: **Edit** on another one closes the editor first, and the editor asks before it discards changes that are not saved.
- The method hook editor works the same way, with the same entries in the context menu of a method hook. It can be open beside a script's: the Editor tab holds both as tabs.

**Save** puts the code into the running script. If the game ended meanwhile, the script is saved for the next time the game is attached; if Grembyte is attached to another game by now, the editor refuses to save and stays open, so nothing typed is lost. When the game ends or another one is attached, an editor whose content is saved closes by itself; one with changes stays open.

## When a script runs

| Mode | Runs |
| --- | --- |
| **Once, after a delay** | after the configured delay, each time the script starts |
| **Repeatedly, every …** | every configured interval; the next interval starts after the previous run finished, so runs do not overlap |
| **Only when run by hand** | only when you click **Run now**, or press its hotkey in the game |

### Hotkeys

A script that runs only by hand can have a **Hotkey**, for example ++ctrl+shift+g++. Pressed while the game has the focus, it runs the script with the defaults of its parameters, without asking. This also works when the game was started through [grembyte-cli](launch-option.md), with no window open. The table shows the hotkey next to *On request*.

A script that runs **repeatedly** can have one as well: it **pauses** the script, and the next press resumes it, as the pause button does (the state is kept for the next start too). Healing the hero every few seconds, for example, only for a boss fight. The table shows the hotkey next to *Every … s*. A script that runs once after some seconds has no hotkey.

- A hotkey is one key (a letter, a digit, ++f1++ to ++f12++, ++insert++, ++delete++, ++home++, ++end++, ++page-up++, ++page-down++, ++pause++ or ++space++) with any of ++ctrl++, ++shift++, ++alt++ and ++meta++. It fires once per press, only with exactly its modifiers: ++ctrl+g++ does not fire on ++ctrl+shift+g++.
- The game gets the keys as well; Grembyte only reads them. Pick a combination the game does not use.
- A paused manual script does not react to its hotkey; a paused repeating one is resumed by it.
- Once the script has run, a short **sound** says how it went (pausing and resuming a repeating script sound as switching off and on), so a hotkey is heard in a fullscreen game too: one for success, one for an error. A script that returns `true` or `false` sounds as switched on or off, each its own way; a toggle that changes a value on the first press and restores it from `ctx.state` on the second can say which it did:

    ```js
    ctx.state.on = !ctx.state.on;
    stats.Speed = ctx.state.on ? 2 : 1;
    return ctx.state.on;
    ```

    The sounds are the freedesktop sound theme's (`bell`, `dialog-warning`, `device-added`, `device-removed`), played by Grembyte through `pw-play` or `paplay`, not by the game. They sound also when the desktop's notification sounds are muted: [Settings](settings.md) → *Sound when a hotkey ran its script* turns them off, `grembyte-cli --no-sound` for one launch.
- On Linux, Grembyte reads the keyboard through X11. Games under Wayland run through XWayland, so this covers native games and Proton alike. A game that is a Wayland client itself is not seen.

## Parameters

Scripts can have parameters of the types text, number, yes/no or choice. A manual run asks for their values; scheduled runs and hotkeys use their defaults. The script reads them from `ctx.args`.

<figure markdown="span">
  ![Script parameters](../assets/screenshots/script-params-dark.webp#only-dark){ width="560" }
  ![Script parameters](../assets/screenshots/script-params-light.webp#only-light){ width="560" }
</figure>

A choice parameter gets its options from a short JavaScript function evaluated in the game, returning values or `{label, value}` objects. The list filters as you type; **Reload options** in the run dialog runs the options code again, e.g. after the game loaded more items. The [Unreal page](../engines/unreal.md#example-a-script-with-a-choice-parameter) shows one in Gothic 1 Remake.

## Writing scripts

The script's `ctx` gives access to the game; all fields are in the [script context reference](../reference/script-context.md). Objects of the game are [wrappers](../reference/script-context.md#object-wrappers): read and write their fields and properties and call their methods as in C# (`player.Heal(10)`). A returned object is shown as the result and can be opened in the object inspector.

While you type, the code field offers the fields of `ctx` that the attached game's engine has, Frida's pointer methods and the JavaScript keywords; **Ctrl+Space** opens the list by hand. See [completion](../reference/javascript.md#completion).

```js title="Heal the player by 10 health (GrembyteQuest, Unity Mono)"
const player = ctx.static("GrembyteQuest.GameState.Current").Player;
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

Without an engine adapter Grembyte looks for a function the game calls every frame on its main thread, such as `SDL_PollEvent` or `SwapBuffers`, and runs scripts each time it returns. The first run logs which one it found. A game that exports none of the known ones, for example one with SDL linked in statically, gets an error instead.

There are no class or method names to go by, so `ctx.native` finds code by its bytes: a pattern of the function's first instructions, with `??` for the bytes that change between builds (addresses, offsets). A pattern survives game updates that leave the function alone, unlike a fixed address. The [code explorer](code-explorer.md) finds the code and copies its pattern, or a whole script skeleton. The [script context reference](../reference/script-context.md#native-code-ctxnative) lists the functions.

Native scripts run with Frida's full JavaScript API, as all scripts do: memory is read and written through `NativePointer` (`pointer.add(4).readS32()`, see [Memory at an address](../reference/script-context.md#memory-at-an-address)), and `Process`, `Module` and `Memory` are available as in any Frida script. A wrong call can crash the game: check a function's parameters in the disassembly first, and try the script by hand before giving it a timer.

An example with a real game: [Underkeep's hidden debug menu](../examples/underkeep-debug-menu.md), opened and made usable by two native scripts.

<small>Gothic 1 Remake is named on this page only to illustrate how Grembyte works. All game names, trademarks and game content belong to their respective owners. Grembyte is not affiliated with, endorsed or sponsored by them.</small>
