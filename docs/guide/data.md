# Saving, export and import

## What Grembyte keeps per game

Grembyte identifies a game by its program and keeps for it:

- tracked addresses with their pointer chains and frozen values,
- hooks and method hooks,
- scripts,
- [notes](notes.md) in Markdown,
- favorites and the chosen assemblies,
- catalogs read from Unity and Unreal games (in a cache in Grembyte's data folder, see [files](../reference/files.md)), so the catalog also opens offline.

Hook templates and settings are shared by all games. Everything lives in one SQLite database; [Files and environment](../reference/files.md) says where.

When the attached game exits, Grembyte resets the window within a second: scans, watches, freezing and hooks stop; stored addresses and hooks are kept and loaded again the next time you attach.

## Export and import

**File → Export game data …** writes everything saved for a game to a JSON file: per program path its addresses, hooks, method hooks, favorites, scripts and notes, plus the templates its hooks were created from. The candidate chains of pointer scans are only exported when you ask for them, since they can be large.

<figure markdown="span">
  ![Export](../assets/screenshots/game-export-dark.webp#only-dark){ width="640" }
  ![Export](../assets/screenshots/game-export-light.webp#only-light){ width="640" }
</figure>

**File → Import game data …** puts the entries of a file into one saved program, whatever path they were exported from – useful when a game moved, or for another computer. It either replaces what the program holds or mixes the file's entries in, leaving out those it holds already. A template in the file is linked to a local one with the same name and code, otherwise added (under a new name if the name is taken).

Notes are never deleted by an import, not even by **Replace**: a note of the file is merged into the program's note: the parts of its text between lines (`---`) that the note does not hold yet are appended below such a line. A program without notes takes them as they are.

## Where exports go

Every export – game data, the [bug report](tools.md#log-and-activity), a text from the text viewer, an object saved from the [object inspector](../engines/index.md#object-inspector) – starts in the folder you saved the last one to. The first time that is your home folder, on Windows your documents folder.

## Cleaning up

- **File → Delete saved entries …** deletes chosen addresses, pointer scans, hooks, method hooks, favorites, scripts or notes of saved programs, or the programs altogether. Tick the entries per program; the box in front of a row ticks all its entries. For several selected rows (Ctrl or Shift), the space key ticks their entries, a second time their program too, a third time nothing. Deleting an address deletes its pointer scans as well. **Remove process** deletes the program with all its entries and its assembly selection; it is saved anew when you attach to it again.
- **File → Compact database …** frees the space of deleted entries (e.g. large pointer scans) that SQLite otherwise keeps in the file.
- **Obsolete pointer scans** are offered for deletion after the start: scans whose filtering left no candidates, scans of entries with a pointer that were not filtered for 7 days, and scans not filtered for 60 days. Untick the ones to keep; they are offered again only after they were filtered or scanned anew. Grembyte compacts the database afterwards. Pointer scans are stored compressed; scans of older versions are converted once at the first start.
