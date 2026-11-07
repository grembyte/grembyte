# Scanning for values

The value search on the right of the main window finds the addresses where the game keeps a value. You narrow them down in steps: a **First scan** over the whole memory, then **Next scans** over the remaining results after the value changed in the game.

<figure markdown="span">
  ![Value search](../assets/screenshots/main-window-dark.webp#only-dark)
  ![Value search](../assets/screenshots/main-window-light.webp#only-light)
</figure>

## Data types

| Type | Size | Use |
| --- | --- | --- |
| `int8`, `uint8` | 1 byte | Flags, small counters |
| `int16`, `uint16` | 2 bytes | Older games, packed structures |
| `int32`, `uint32` | 4 bytes | The usual integer: gold, ammunition, levels |
| `int64`, `uint64` | 8 bytes | Large counters, money in some games |
| `float` | 4 bytes | Health, mana, positions, multipliers |
| `double` | 8 bytes | Experience, timers, values in managed games |

**Guess automatically** tries `int32`, `int64`, `float` and `double` at once. The smaller types match almost everywhere and have to be chosen explicitly.

**Aligned addresses only** skips addresses that are not a multiple of the type's size. Games almost always align their values, so the scan is faster and has fewer chance hits.

## Scan types

First scan:

| Scan type | Finds |
| --- | --- |
| Exact value | Addresses holding the value |
| Greater than / less than | Values above or below the number |
| Range | Values between two numbers |
| Unknown initial value | Every address; useful when the game shows no number, e.g. a health bar |

Next scan, compared with the previous scan:

| Scan type | Keeps addresses whose value |
| --- | --- |
| Changed / unchanged | changed or stayed the same |
| Increased / decreased | went up or down |
| Increased by … / decreased by … / changed by … | changed by the given amount |
| Exact value, greater, less, range | as in the first scan, against the current value |

!!! tip "Unknown values"
    For a health bar without a number: **Unknown initial value**, take damage, **Decreased**, wait, **Unchanged**, heal, **Increased** – a few rounds leave few addresses.

## Fuzzy scans

Games rarely show a value exactly as they store it. Two options help:

- **Tolerance (±)** for exact and by-X scans: `Increased by … 25` with tolerance `5` finds increases from 20 to 30.
- **Float comparison** for `float` and `double`: *Exact*, *Rounded* or *Truncated* to the decimal places you typed. `12.3` rounded finds 12.25 to 12.35.

The test game has examples of both:

- **Mana** (`float`, shown with one decimal place) regenerates by 0.13 every second; a spell costs 7.5. Scan **Exact value** with the value shown (e.g. `20.1`) and **Rounded**; with *Exact* the address is hard to find, because the stored value is e.g. 20.13. Then **Cast spell** and scan **Decreased by …** `7.5` with tolerance `0.5` – the regeneration between the scans would make a comparison without tolerance fail.
- **Experience** (`double`, shown without decimals): **Exact value** with the number shown and **Rounded**; after **Fight** scan **Increased by …** `12` with tolerance `3`.
- **Gold** (`int32`): after **Find treasure** scan **Increased by …** `25` with tolerance `5`.

## Taking results

Results show the address, type and live value. Select one or several and click **Track** (or double-click) to move them to the [tracked addresses](addresses.md). A right-click offers the memory viewer and the [write and read watch](write-watch.md) for a result directly.

The scan area folds away with **Hide address search** next to the tabs, **View → Address search** or ++ctrl+shift+f++; Hermes remembers the choice.
