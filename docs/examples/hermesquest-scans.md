# HermesQuest: scans and pointer scan

Start [HermesQuest](index.md), attach to it with **Select process** (`HermesQuest.x86_64`, or `HermesQuest.exe` under Wine/Proton) and keep the **F1** view of the game at hand: it shows every value exactly.

The [scanning guide](../guide/scanning.md) explains the scan types; here they are applied to a game whose values behave like those of real games.

## Gold: an exact integer

Gold starts at 0, and searching for 0 finds millions of addresses. So earn some first:

1. Press **2** to spawn a goblin and walk into it until it dies. The log says how much gold it dropped, e.g. "You gain 10.25 experience and 5 gold."
2. In Grembyte choose the data type `int32`, scan type **Exact value**, enter the gold (`5`) and click **First scan**.
3. Kill another enemy, enter the new amount and click **Next scan**. After two or three rounds one address is left.
4. **Track** it and call it "Gold".

!!! tip "Why `int32`?"
    `Gold` is declared as `int` in the game's C# code, which is always 4 bytes. If you don't know the type, **Guess automatically** tries `int32`, `int64`, `float` and `double` at once.

## Mana: a float that changes on its own

Mana is a `float` that regenerates by 0.13 per second in real time. Between two scans it changes, even if you do nothing – exactly what makes values like stamina or energy bars hard to find in real games.

1. Spawn a rat (**1**) next to you and press **F**: Fire Bolt costs 7.5 mana. The panel beside the map shows mana with one decimal place, e.g. `MP 42.5/50`.
2. Data type `float`, **Exact value** `42.5`, float comparison **Rounded**. "Rounded" finds every value that shows as 42.5 with one decimal place (42.45 to 42.55), whatever the hidden decimals are. With **Exact** you would only find it within the same frame.
3. Spawn another rat and cast again. Scan **Decreased by …** `7.5` with tolerance `0.5`: the tolerance absorbs the mana that regenerated between the two scans. Don't wait long between casting and scanning – 0.5 mana regenerate in under four seconds.
4. Wait a few seconds and scan **Increased** – only mana grows by itself.
5. Track the address and call it "Mana".

Tracked, the value visibly counts up in the address list until it reaches 50.

## Experience: a double

`Experience` is a `double` (8 bytes). Enemies bring uneven amounts: a rat 4.5, a goblin 10.25, a skeleton 15.5.

1. Data type `double`, **Exact value** with the experience the F1 view shows, e.g. `14.75`.
2. Kill a skeleton (**3**), then **Increased by …** `15.5`, without tolerance: whole kills are exact.

!!! question "Why does `float` not find it?"
    A `double` and a `float` with the same value have entirely different bytes. A scan with the wrong type finds nothing – or, for small integers, chance hits. When a value shows decimals that don't fit, try the other floating-point type.

## Pointer scan: surviving "New game"

Press **N**: the game builds a new player, and all tracked addresses now point into memory the game no longer uses. A **pointer chain** finds the value again. Mana is a good target, because you can find it again right after a new game.

1. Right-click "Mana" → **Find pointers …**.
2. Look at **Chains start in**. In a Unity game the program file is only a small launcher. With the game's assemblies marked under **Assemblies** (e.g. `Assembly-CSharp`), **Marked assemblies** is ticked: chains start in the static fields of its C# classes. Without, **Engine modules** is: they start in the Mono runtime (`libmonobdwgc-2.0.so`, or `mono-2.0-bdwgc.dll` on Windows) or in `UnityPlayer`, and there are many more of them.
3. Click **New scan**. It finds many candidates.
4. Press **N** in the game. Find mana again as above: spawn a rat, cast, **Exact value** `42.5` rounded, then **Decreased by …** `7.5` ± `0.5`.
5. In the pointer dialog click **Take from scan**, then **Filter with this address**. Chains that only matched by chance drop out.
6. Repeat steps 4 and 5 once or twice. Then pick the shortest remaining chain and click **Use pointer**.

From now on "Mana" resolves after every new game and every restart of HermesQuest.

!!! tip "How to read a chain"
    The game reaches mana as `GameState.Current → Player → Stats → Mana`. The last offsets of a good chain are exactly these steps: the offset of `Player` in `GameState`, of `Stats` in `Character` and of `Mana` in `CharacterStats`. The object inspector shows each field's offset (see [Unity (Mono)](../engines/unity-mono.md)), so you can check a chain against the game's own structure. What comes before is the way from the Mono runtime to the static field `GameState.Current`.

!!! note "No chain found?"
    Raise **Max. depth** by one or two. Managed games often need deeper chains than native ones, because the way to a static field leads through the runtime's own tables.

With the chain in place you can give **Gold** and **Experience** chains too: they differ from Mana's only in the last offset, because all three are fields of the same `CharacterStats` object. Grembyte does not derive them for you: scan for each, then filter its pointer scan as above.

## A shortcut for Mono games

For scripts and method hooks you need no pointer chain at all: they reach the player through the game's own static field, `GameState.Current`, which is always right – see [Scripts](hermesquest-scripts.md). Pointer chains matter for tracked addresses, freezing and value hooks.
