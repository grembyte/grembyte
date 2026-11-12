# Files and environment

## Where Grembyte keeps its data

| What | Linux | Windows |
| --- | --- | --- |
| Database (`grembyte.db`) | `~/.local/share/grembyte/` (`$XDG_DATA_HOME/grembyte/`) | `%LOCALAPPDATA%\grembyte\` |
| Catalog cache | `cache/` beside the database | `cache\` beside the database |
| Wine/Proton gadgets | `gadget/` beside the database | – |
| Logs (`grembyte.log`, `crash.log`, `activity.jsonl`) | `~/.local/state/grembyte/` (`$XDG_STATE_HOME/grembyte/`) | `%LOCALAPPDATA%\grembyte\logs\` |
| Themes of your own | `~/.config/grembyte/themes/` (`$XDG_CONFIG_HOME/grembyte/themes/`) | `%APPDATA%\grembyte\themes\` |
| Window sizes and UI state | Qt settings `grembyte/grembyte` | Qt settings (registry) |

Bundled themes ship in `grembyte/ui/themes/`, as `grembyte_themes_<name>.json` like your own.

If there is no database yet but one from earlier versions at `data/grembyte.db` in the project directory, Grembyte copies it once; the original stays.

## Environment variables

| Variable | Effect |
| --- | --- |
| `GREMBYTE_DB` | Use another database file. |
| `GREMBYTE_CACHE_DIR` | Keep the catalog cache elsewhere. |
| `GREMBYTE_LOG_DIR` | Write logs and the activity log to another folder. |
| `GREMBYTE_LANGUAGE` | `de` or `en` for one start, while the language setting is "as the system". |
| `GREMBYTE_INJECT_DIR` | Folder with the Wine helper executables (see [Wine and Proton](../wine-proton.md)). |
| `GREMBYTE_VENV` | Virtual environment used by `scripts/grembyte.sh`. |
