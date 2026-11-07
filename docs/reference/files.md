# Files and environment

## Where Hermes keeps its data

| What | Linux | Windows |
| --- | --- | --- |
| Database (`hermes.db`) | `~/.local/share/hermes-hook/` (`$XDG_DATA_HOME/hermes-hook/`) | `%LOCALAPPDATA%\hermes-hook\` |
| Catalog cache | `cache/` beside the database | `cache\` beside the database |
| Wine/Proton gadgets | `gadget/` beside the database | – |
| Logs (`hermes.log`, `crash.log`, `activity.jsonl`) | `~/.local/state/hermes-hook/` (`$XDG_STATE_HOME/hermes-hook/`) | `%LOCALAPPDATA%\hermes-hook\logs\` |
| Themes of your own | `~/.config/hermes-hook/themes/` (`$XDG_CONFIG_HOME/hermes-hook/themes/`) | `%APPDATA%\hermes-hook\themes\` |
| Window sizes and UI state | Qt settings `hermes-hook/hermes-hook` | Qt settings (registry) |

Bundled themes ship in `hermes/ui/themes/`, as `hermes_themes_<name>.json` like your own.

If there is no database yet but one from earlier versions at `data/hermes.db` in the project directory, Hermes copies it once; the original stays.

## Environment variables

| Variable | Effect |
| --- | --- |
| `HERMES_DB` | Use another database file. |
| `HERMES_CACHE_DIR` | Keep the catalog cache elsewhere. |
| `HERMES_LOG_DIR` | Write logs and the activity log to another folder. |
| `HERMES_LANGUAGE` | `de` or `en` for one start, while the language setting is "as the system". |
| `HERMES_INJECT_DIR` | Folder with the Wine helper executables (see [Wine and Proton](../wine-proton.md)). |
| `HERMES_VENV` | Virtual environment used by `scripts/hermes-hook.sh`. |
