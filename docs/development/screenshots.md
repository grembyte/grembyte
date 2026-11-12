# Screenshots

The screenshots in this documentation are rendered by a script, so they can be renewed after every change to the interface:

```bash
./.venv/bin/python scripts/screenshots/run.py                  # every scene, dark and light
./.venv/bin/python scripts/screenshots/run.py hook             # scenes whose name starts with "hook"
./.venv/bin/python scripts/screenshots/run.py --list           # list the scenes
```

The script renders Grembyte offscreen (`QT_QPA_PLATFORM=offscreen`) at twice the normal scale, once with the theme Github-Dark and once with Github-Light, into `docs/assets/screenshots/<scene>-<dark|light>.webp` (lossless WebP, about half the size of PNG). The pages show the image that matches the reader's color scheme:

```markdown
<figure markdown="span">
  ![Write watch](../assets/screenshots/write-watch-dark.webp#only-dark)
  ![Write watch](../assets/screenshots/write-watch-light.webp#only-light)
  <figcaption>What the image shows.</figcaption>
</figure>
```

## How the scenes work

No scene attaches to a game. Each fills Grembyte's widgets with demo data, the way its workers would after talking to the game, so the images are reproducible and need no game installed. Before Grembyte is imported, the script points every folder Grembyte writes to – Qt settings, the database, logs, caches – into a temporary directory; your own data is never touched.

The scenes live in `scripts/screenshots/scenes_*.py`; `studio.py` holds what they share. A scene is a function that returns the widget to capture:

```python
from studio import scene, settle

@scene("memory-viewer", size=(1000, 600), description="Memory around the gold")
def memory_viewer(studio):
    window = studio.show_attached()   # demo program in toolbar and status bar
    ...                               # build the widget and fill it with demo data
    return widget
```

The demo data follows the test game of the [tutorial](../getting-started/first-hook.md): `dummy_game.exe`, the gold at `0x56153A2C41F8` behind `dummy_game.exe+0x4040 → 0x28 → 0x0`.

## Screenshots of real games

Some images need a real game: the game window itself, or the effect of a change in it. They are taken by hand, as `-dark`/`-light` pairs where Grembyte is in the picture, and kept in `docs/assets/photos/` as WebP (quality 88; the PNG originals there are ignored by git and the site). The pages mark the places with a box like this:

!!! screenshot "Screenshot to add: example"
    What the image should show.
    File: `docs/assets/photos/example.png`

Replace the box with the image once it exists. `grep -rn '!!! screenshot' docs/` lists the open places.
