# Files and environment

## Where Grembyte keeps its data

| What | Linux | Windows |
| --- | --- | --- |
| Database (`grembyte.db`) | `~/.local/share/grembyte/` (`$XDG_DATA_HOME/grembyte/`) | `%LOCALAPPDATA%\grembyte\` |
| Catalog cache | `~/.local/share/grembyte/cache/` (`$GREMBYTE_CACHE_DIR`) | `%LOCALAPPDATA%\grembyte\cache\` |
| Wine/Proton gadgets | `~/.local/share/grembyte/gadget/` | – |
| Logs (`grembyte.log`, `crash.log`, `activity.jsonl`) | `~/.local/state/grembyte/` (`$XDG_STATE_HOME/grembyte/`) | `%LOCALAPPDATA%\grembyte\logs\` |
| Themes of your own | `~/.config/grembyte/themes/` (`$XDG_CONFIG_HOME/grembyte/themes/`) | `%LOCALAPPDATA%\grembyte\themes\` |
| Link between the window and [grembyte-cli](../guide/launch-option.md) (`cli-<pid>.json`, `cli-<pid>.sock`) | `$XDG_RUNTIME_DIR/grembyte/` (else `grembyte-<user>/` in the temp folder) | – |
| Window sizes and UI state | Qt settings `grembyte/grembyte` | Qt settings (registry) |

Bundled themes ship in `grembyte/ui/themes/`, as `grembyte_themes_<name>.json` like your own.

If there is no database yet but one from earlier versions at `data/hermes.db` in the project directory, Grembyte copies it once; the original stays.

### Files from Hermes Hook

Up to version 0.6 Grembyte was called Hermes Hook and kept its files in folders named `hermes-hook`. On its first start Grembyte moves these folders to `grembyte`, renames `hermes.db`, `hermes.log` and your themes (`hermes_themes_*.json` to `grembyte_themes_*.json`) and copies the window sizes from the old Qt settings. A file that already exists under the new name is kept. The log lists what was taken over.

The environment variables changed their names too: set `GREMBYTE_DB` instead of `HERMES_DB`, and so on. A menu entry or Start menu shortcut installed for Hermes Hook is replaced when you run the install script again (see [Installation](../getting-started/installation.md)).

## Environment variables

| Variable | Effect |
| --- | --- |
| `GREMBYTE_DB` | Use another database file. |
| `GREMBYTE_CACHE_DIR` | Keep the catalog cache elsewhere. |
| `GREMBYTE_LOG_DIR` | Write logs and the activity log to another folder. |
| `GREMBYTE_LANGUAGE` | `de` or `en` for one start, while the language setting is "as the system". |
| `GREMBYTE_INJECT_DIR` | Folder with the Wine helper executables (see [Wine and Proton](../wine-proton.md)). |
| `GREMBYTE_VENV` | Virtual environment used by `scripts/grembyte.sh`. |
