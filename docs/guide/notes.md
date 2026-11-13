# Notes

While you work on a game you learn where things are: which class holds the gold, which instance the player is, which method adds experience or puts an item in the inventory. **View → Notes …** keeps that per game, in a small window you can dock beside the main window.

<figure markdown="span">
  ![Notes](../assets/screenshots/game-notes-dark.webp#only-dark)
  ![Notes](../assets/screenshots/game-notes-light.webp#only-light)
  <figcaption>Notes on the test game, docked beside the scanner, in the preview.</figcaption>
</figure>

## Opening the notes of a game

- With a game attached, **View → Notes …** opens its notes.
- Without one, Grembyte first asks for the game: the list holds every game it has saved (a game is saved once Grembyte attaches to it), with the number of its notes. Double-click a game or choose **Open notes**. This way you can read and add to your notes while the game is not running.
- If another game is attached while the notes are open, they switch to it automatically; what you wrote for the previous one is saved first.
- With the notes open, **View → Notes …** again switches them to the attached (or chosen) game.

Like a program's other entries, the notes belong to one program path: a game started under two Proton versions has two sets of notes (see [What Grembyte keeps per game](data.md#what-grembyte-keeps-per-game)).

## Writing

The window has two tabs:

- **Write** – the text in Markdown, a few characters that format it: `## Gold` is a heading, `- ` starts a list, `` `AddGold(int)` `` marks code. The [Markdown reference](../reference/markdown.md) lists what the preview understands.
  While you type, the Markdown is colored: headings bold, the formatting characters (`#`, `*`, backticks) dimmed, code and links in their own color. Code blocks marked `js`, `ts` or `json` are colored as JavaScript, `c` or `cpp` as C (see [Code](../reference/markdown.md#code)). The colors follow the theme.
- **Preview** – the text formatted, as on GitHub: headings, lists, tables, code, task lists with check boxes, in the colors of the theme (code blocks colored like in the Write tab, shaded table headers). Links open in your browser.

Grembyte saves the notes

- about two seconds after you stop typing,
- when you click **Save** or press ++ctrl+s++ (this always saves, even when nothing changed),
- when the window closes (also with ++esc++), when it switches to another game, and when Grembyte quits.

The text beside the buttons says whether there are unsaved changes or when the notes were saved last.

## Docking

**Pin to main window** docks the notes as a tab at the right of the main window, **Detach** gives them a window of their own again; the next time they open the same way (see [Docking windows](tools.md#docking-windows)). Unlike the game's tools, the notes stay open when the game ends or another program is chosen.

## What to write down

Notes are free text; a layout that works well:

```markdown
## Gold
- Class `PlayerInventory`, field `gold` (int)
- `PlayerInventory:AddGold (int)` – call it from a script, see script *Gold*

## Experience
- `CharacterStats:AddExperience (int, bool)`, second argument `false`

## Items
- `Inventory:AddItem (ItemDefinition, int)`, items from `GameDatabase.items`

## Open
- [ ] What resets the stamina?
```

Names of classes, methods and fields can be copied from the [catalog](../engines/index.md#catalog) and the [object inspector](../engines/index.md#object-inspector) (context menu **Copy cell** or **Copy row**).

## Export, import and deleting

The notes are part of a game's [export](data.md#export-and-import). An import never deletes notes, not even with **Replace**: the text of the file is appended to the program's note below a line (`---`), unless the note holds it already. **File → Delete saved entries …** has a column for the notes (see [Cleaning up](data.md#cleaning-up)).
