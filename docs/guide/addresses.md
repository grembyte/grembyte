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
- From the object inspector, the hooks tab and scripts, which can hand addresses over.

Double-click the description to rename an entry. If an address lies in the program's static memory (a global variable), Hermes stores it as module + offset right away; it stays valid after a restart without a pointer scan.

## Writing and freezing

**Write value** writes a new value into the game once.

**Freeze** holds the current value: Hermes writes it back every 100 ms. Clicking again releases it; **Write value** changes the frozen value. For entries with a pointer chain or a static address the frozen state is stored and still applies after the game restarts. Entries without a chain are released when they are loaded again, because their address need no longer be right then.

!!! question "Freeze or hook?"
    Freezing needs no write watch and works on any value, but may flicker briefly, since the game can change the value between two passes. A [hook](hooks.md) with the template *Fixed value* acts directly at the write instruction, so the game never sees another value.

## Context menu

A right-click on an entry offers:

- **Show in memory viewer**: the [memory](tools.md#memory-viewer) around the address.
- **Change description …** and **Change value …**.
- **Address watch (writes)** and **Address watch (all accesses)**: the [write and read watch](write-watch.md).
- **Find pointers …**: the [pointer scan](pointer-scan.md); **Remove pointer** drops a chain again.

Scan results offer the memory viewer and the two watches as well.

## After a restart

Without a pointer chain the stored address is usually wrong after the game restarts, and the entry shows a stale value. With a chain Hermes resolves it again; while it cannot (e.g. in the main menu, before a save is loaded) the address column shows `?`, and the entry resolves once the chain leads somewhere again.
