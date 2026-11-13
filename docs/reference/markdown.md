# Markdown

Markdown is plain text with a few characters that format it: `## Gold` becomes a heading, `- ` a list item, `` `AddGold` `` code. The [notes](../guide/notes.md) are written in Markdown; their **Preview** tab shows the text formatted. This page covers the commands the preview understands. They are those of GitHub (the same text looks almost the same in a GitHub issue or README), with a few differences listed under [What the preview does not show](#what-the-preview-does-not-show).

## Text

| Write | Shows |
| --- | --- |
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `***bold and italic***` | ***bold and italic*** |
| `~~struck through~~` | <del>struck through</del> |
| `` `AddGold(int)` `` | `AddGold(int)` – code, in a fixed-width font, nothing inside is formatted |
| ``` `` a `tick` inside `` ``` | `` a `tick` inside `` – two backticks around code that holds one |
| `_underlined_` | underlined in Grembyte, *italic* on GitHub – use `*…*` for italic |

Underscores inside a word do nothing: `Player_Gold_Amount` stays as it is.

## Headings

```markdown
# Game
## Gold
### Where it is written
```

`#` to `######` give six levels; a space follows the `#`. In notes, `##` per topic (gold, experience, items) and `###` below it keep them easy to scan.

## Paragraphs and lines

- An empty line starts a new paragraph.
- A single line break is not kept: the lines run on as one paragraph. That way long sentences may be wrapped in the editor.
- Two spaces or a backslash `\` at the end of a line start a new line. Grembyte's preview shows it with the spacing of a new paragraph.

## Lists

```markdown
- Gold
- Experience
  - from quests
  - from enemies
1. Open the inventory
2. Call `AddItem`
- [x] Gold found
- [ ] Find the stamina
```

| Write | Gives |
| --- | --- |
| `- `, `* ` or `+ ` at the start | a bulleted list |
| `1. ` or `1) ` | a numbered list (it counts on by itself, whatever the next numbers are) |
| indented to the text of the item above (two spaces under `- `, three under `1. `) | an item of that item's list |
| `- [ ] ` / `- [x] ` | a task with an empty / ticked check box |

The check boxes of the preview only show; tick a task by writing `x` between the brackets.

## Code

A word or name in backticks is code: `` `PlayerInventory:AddGold (int)` ``. Several lines go between lines of three backticks; a language may follow the first:

````markdown
```js
const gold = ctx.call("PlayerInventory:get_Gold");
ctx.log(`gold: ${gold}`);
```
````

The preview shows code in a fixed-width font, code blocks in a shaded box. The language decides the colors, in the preview and in the **Write** tab: `js`, `javascript`, `ts`, `typescript` and `json` are colored as JavaScript, `c`, `cpp` and `h` as C; code of other languages, or with none, is shown in one color. A paragraph indented by four spaces is code as well (in the preview; the Write tab does not color it).

!!! tip "Put names of the game in backticks"
    Names of classes, methods and fields often hold characters Markdown reads as commands: `List<Item>` looks like an HTML tag, `*` in `int*` or `_` at the start of `_gold` like emphasis. In backticks, everything stays as written.

## Links

| Write | Gives |
| --- | --- |
| `[Wiki](https://example.com/wiki)` | a link named *Wiki* |
| `https://example.com` | the address as a link |
| `<https://example.com>` | the same |

Links open in your web browser.

## Tables

```markdown
| Value      | Offset | Type   |
| ---------- | -----: | ------ |
| Gold       |  `0x0` | int32  |
| Experience | `0x18` | double |
```

The second line separates the header from the rows. A colon sets how a column is aligned: `:---` left, `:---:` centered, `---:` right (useful for numbers). The columns need not line up in the text. A `|` inside a cell is written `\|`.

## Quotes and lines

| Write | Gives |
| --- | --- |
| `> Text` | the text indented, as a quote |
| `---` on a line of its own (with empty lines around it) | a horizontal line |

An import that adds text to a note puts such a line between the old and the new text (see [Notes](../guide/notes.md#export-import-and-deleting)).

## Characters with a meaning

The backslash, the backtick and `* _ # + - . ! [ ] ( ) < > |` can mean something. To show one as it is, put a backslash before it: `\*not italic\*`, `1\. not a list`. In code (backticks) no backslash is needed.

## What the preview does not show

The text stays as written; only the preview differs from GitHub:

- **HTML** (`<b>`, `<kbd>`, `<details>` …) is not supported. Something that looks like a tag, `List<Item>` outside backticks, can even hide the rest of the paragraph.
- **Footnotes** (`[^1]`), **emoji** codes (`:smile:`) and **highlighting** (`==text==`) stay plain text.
- **Images** (`![name](https://…)`) from the web are not loaded.
- **Code** has no colors.
- `_text_` is underlined, not italic (see [Text](#text)).

## An example

```markdown
## Gold
- Class `PlayerInventory`, field `gold` (int)
- `PlayerInventory:AddGold (int)` – script *Gold +1000*
- Pointer: `game.exe+0x4040 → 0x28 → 0x0`

## Character
| Value | Offset | Type   |
| ----- | -----: | ------ |
| HP    |  `0x4` | int32  |
| Mana  | `0x14` | float  |

## Open
- [x] Gold found
- [ ] What resets the stamina? Trace *Sprint*, see `StaminaComponent`
```

The [notes page](../guide/notes.md) shows how this looks in the preview.
