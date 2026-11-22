# Tracked addresses

The **Tracked addresses** tab keeps the addresses you work with, with a description, the type, the live value and, if found, a pointer chain. They are stored per program and loaded again the next time you attach to it.

<figure markdown="span">
  ![Tracked addresses](../assets/screenshots/main-window-dark.webp#only-dark)
  ![Tracked addresses](../assets/screenshots/main-window-light.webp#only-light)
  <figcaption>Addresses with pointer chains (purple) resolve again after a restart; "HP" is frozen at 100.</figcaption>
</figure>

## Adding addresses

- **Track** in the scan results, or a double-click on a result.
- **+ Address …** for an address you know, e.g. from another tool.
- **Pointer chain …** for a value reached through pointers, e.g. `game.exe+0x43E600 -> 0x28 -> 0x0`: start at an address in a module (`module+offset`, also with further offsets), then each step reads the pointer at the address so far and adds its offset. Separate the steps with `->`, `=>`, `:` or `→` (as the [pointer scan](pointer-scan.md) and the [code explorer](code-explorer.md#pseudocode) show chains). The dialog shows where the chain leads now; the entry follows it again with every refresh and after a restart.
- From the object inspector, the hooks tab and scripts, which can hand addresses over. In Unreal and Unity Mono games a field from the object inspector is located by its object's field (see [Fields of objects](#fields-of-objects)).

<figure markdown="span">
  ![Adding a pointer chain](../assets/screenshots/pointer-chain-dialog-dark.webp#only-dark)
  ![Adding a pointer chain](../assets/screenshots/pointer-chain-dialog-light.webp#only-light)
  <figcaption><strong>Pointer chain …</strong> with the gold of the test game: below the input, where the chain leads now.</figcaption>
</figure>

!!! note "Plus or arrow"
    `game.exe+0x43E600+0xB0` is the address `0xB0` bytes behind `game.exe+0x43E600`. `game.exe+0x43E600 -> 0xB0` reads the pointer stored at `game.exe+0x43E600` and adds `0xB0` to it. If a register held the object's address itself (`rcx` = `game.exe+0x43E600`), its field is at the sum; only if the object's address was read from there is it an arrow.

!!! example "Step by step with the test game"
    The [pointer chain example](../examples/dummy-game-pointer-chain.md) reads the chain of the gold from the game's code, enters it with **Pointer chain …**, and explains with a simple picture how to tell a step of a chain from an offset.

Double-click the description to rename an entry. If an address lies in the program's static memory (a global variable), Grembyte stores it as module + offset right away; it stays valid after a restart without a pointer scan.

## Fields of objects

In Unreal and Unity Mono games Grembyte can tell which object an address lies in. **Find the object it lies in** in the context menu of an entry or a scan result, or **Find the objects of all results** for the whole result list (the first 20,000), opens a window listing for each address its class, object and field, e.g. `UIW_ProfileItem_Ergo_C` · `UIW_ProfileItem_Ergo_C_0` · `PrevSoul`. Addresses in no object are hidden until you untick **Only addresses in an object**. Of 40 scan results with the same value, the few in a named field usually stand out at once. **Open in inspector** (or a double-click) shows the object in the [object inspector](../engines/index.md#object-inspector), **Show references** its [reference graph](reference-graph.md).

**Add to address list** (for a scan result) or **Locate the entry by this field** (for an entry of the list) makes the field the entry's target: the entry stores the class, the field and the object's name instead of an address, and the pointer column shows `UIW_ProfileItem_Ergo_C.PrevSoul`. **Add to address list** in the object inspector does the same for a field of a live object. Grembyte checks every second whether the object still lives and looks for it again when the game made it anew (a loaded save, a level change) and after a restart: the only object of the class, or the one of that name when there are several. Refreshing, freezing and the watch use the address found; while there is none (in the main menu) the address column shows `?`.

Limits:

- Only fields of live objects: an address in the elements of an Unreal `TArray`, `TMap` or `TSet`, in a static field of a Mono class or in memory of no object stays a plain address. IL2CPP and games without an engine's reflection are not supported.
- When there are several objects of the class, the stored name decides. Unreal names such objects alike in every run most of the time, but not always.
- A [hook](hooks.md) created from the watch of such an entry targets the address and learns its callers: the hook cannot find an object by its field.
- A field of a widget may be only what the interface shows: check that changing it changes the game.

## Writing and freezing

**Write value** writes a new value into the game once.

**Freeze** holds the current value: Grembyte writes it back every 100 ms. Clicking again releases it; **Write value** changes the frozen value. For entries with a pointer chain or a static address the frozen state is stored and still applies after the game restarts. Entries without a chain are released when they are loaded again, because their address need no longer be right then.

!!! question "Freeze or hook?"
    Freezing needs no write watch and works on any value, but may flicker briefly, since the game can change the value between two passes. A [hook](hooks.md) with the template *Fixed value* acts directly at the write instruction, so the game never sees another value.

## Context menu

A right-click on an entry offers:

- **Show in memory viewer**: the [memory](tools.md#memory-viewer) around the address.
- **Change description …** and **Change value …**.
- **Address watch (writes)** and **Address watch (all accesses)**: the [write and read watch](write-watch.md).
- **Find the object it lies in**: see [Fields of objects](#fields-of-objects).
- **Find pointers …**: the [pointer scan](pointer-scan.md); **Remove pointer** drops a chain again.

Scan results offer the memory viewer, the two watches and the object search as well, plus **Find the objects of all results**.

## After a restart

Without a pointer chain the stored address is usually wrong after the game restarts, and the entry shows a stale value. With a chain or a [field target](#fields-of-objects) Grembyte resolves it again; while it cannot (e.g. in the main menu, before a save is loaded) the address column shows `?`, and the entry resolves once the chain leads somewhere again.
