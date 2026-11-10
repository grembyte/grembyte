# Pointer scan

Games keep most values on the heap, so their address changes with every start, every new game and often every level. What stays the same is the way there: a static pointer in the program, then a few offsets through the objects. The pointer scan finds these chains, e.g.

```
dummy_game.exe+0x4040 → 0x28 → 0x0
```

reads as: take the pointer at offset `0x4040` of the module `dummy_game.exe`, add `0x28` and read the pointer there, add `0x0` – that is the gold.

<figure markdown="span">
  ![Pointer scan](../assets/screenshots/pointer-scan-dark.webp#only-dark)
  ![Pointer scan](../assets/screenshots/pointer-scan-light.webp#only-light)
  <figcaption>The pointer scan dialog with the chain of the test game and some chance hits.</figcaption>
</figure>

## Finding a chain

1. Right-click the tracked address → **Find pointers …** → **New scan**. In the test game it finds `dummy_game.exe+0x… → 0x28 → 0x0` (`g_game` → player → gold), plus some chance hits.
2. Make the value move: click **New game** (or restart the game) and find the value again in the scanner. The pointer dialog can stay open meanwhile.
3. Select the new result in the scanner, then **Take from scan** and **Filter with this address** in the pointer dialog. Chains that only matched because of the layout in memory drop out. A plain restart is often not enough for that, since the layout at start tends to be the same; a new game or a new level changes more.
4. Select the remaining chain → **Use pointer**.

From now on the tracked address resolves correctly after every restart, and hooks created from it follow the value too (see [hooks after a restart](hooks.md#after-a-restart)).

If the address lies in the program's static memory, no pointer scan is needed; the dialog says so.

## Candidates

The chains found are saved for the entry as candidates, so they can still be filtered after the game was restarted.

- **Add all to address list** adds every candidate as an entry of its own; after a restart the list shows which ones still lead to a sensible value. With several rows selected, the button reads **Add selected to address list** and adds only those.
- **Remove saved candidates** deletes them once a stable chain is in use. The entry and its pointer stay; a new scan finds candidates again.

!!! example "The chain from the code instead"
    The code that uses a value often shows its chain, too: the [code explorer](code-explorer.md#pseudocode) writes it next to each line. The [pointer chain example](../examples/dummy-game-pointer-chain.md) reads the chain of the test game's gold from `add_gold` and enters it with **Pointer chain …** on the [address list](addresses.md#adding-addresses) – no scan needed.

!!! tip "Fewer, shorter chains"
    Prefer short chains from the game's own module over long ones through system libraries. When several chains survive filtering, keep the one with the fewest levels; repeat the filter step after a level change if in doubt.
