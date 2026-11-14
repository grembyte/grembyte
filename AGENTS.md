# AGENTS.md

Guidance for AI coding agents working in this repository. Human contributors: see
[CONTRIBUTING.md](CONTRIBUTING.md), which this file follows.

## Language

- Comments, docstrings, documentation (`docs/`, `README.md`, `ROADMAP.md`) and commit
  messages are in **English**, even when the conversation is in another language.
- Commit messages start with a verb in the imperative ("Add …", "Fix …").

## Translatable texts

- Every text the user sees is translatable. Never hard-code UI strings in Python: put them
  in `grembyte/ui/localization/en.json` **and** `de.json` (at least these two) and read them
  through `translations["…"]`.
- Messages from the JavaScript agents are translatable too: throw
  `grembyteError('error.<area>.<name>', { params })` (`grembyte/agent/messages.js`) and add the
  key to both JSON files; the UI renders them with `grembyte.core.messages.describe`.
- `tests/test_localization.py` checks that both catalogs have the same keys and
  placeholders and that every `grembyteError` key is translated.
- Reuse existing keys where the meaning matches. Use the prefix of the feature you are
  changing (`objects.…`, `inspector.…`, `error.monitor.…`) for new keys.

## Documentation

- After every change, check whether the documentation still matches: `docs/` (user guide,
  engine pages, development notes), `README.md` and `ROADMAP.md`.
- If something is out of date, **ask the user** whether the documentation should be updated
  before changing it. Name the pages and what no longer fits.
- After a docs change, check it with `./.venv/bin/mkdocs build --strict`. Screenshots are
  rendered by a script (`docs/development/screenshots.md`); do not edit them by hand.

## UI conventions

- Icons: only Google Material Icons (Outlined), stored in `grembyte/ui/resources/icons/`, used
  through `material_icon` / `set_button_icon` (`grembyte/ui/widgets/icons.py`).
- Colours come from the theme (`theme_qcolor("muted")` etc.), never hard-coded, so that
  every theme in `grembyte/ui/themes/` works.
- Long dropdowns use `BoundedComboBox` (`grembyte/ui/widgets/code_explorer.py`); a plain
  `QComboBox` under the Fusion style covers the whole window.

## Code and tests

- Before you report a change as done, run the tests relevant to it with
  `./.venv/bin/python -m pytest tests/test_<area>.py` (several files or `-k` as needed): the
  tests of the modules you changed and of the callers that use them, plus
  `tests/test_localization.py` when translation texts changed. Add or update tests for
  changed behaviour. Many tests attach Frida to small C programs and stand-in engines (Mono,
  IL2CPP, Unreal); they need `gcc` and `ptrace_scope=0`.
- Do not run the full suite on your own; it takes a few minutes. When a change touches shared
  code (attach, hooks, trace, engine adapters, the JavaScript agents, application start and
  exit), changes an interface other modules use, or you are not sure what it affects,
  recommend running the full suite in your report and say why. Run it only when the user
  asks for it.
- Say in your report which tests you ran. Changes to documentation only need
  `mkdocs build --strict`, not `pytest`.
- The engine facade (`grembyte/engines/engine_facade.js`) throws for members an adapter does
  not define, so do not probe for optional members with `if (engine.foo)`. Check
  `engine.id` instead, or add the member to every adapter.
- `docs/development/architecture.md` describes the packages and how to add an engine;
  `docs/development/implementation-notes.md` records why things are done the way they are
  (crashes under Wine/Proton, engine quirks). Read the relevant part before changing
  attach, hook or trace code, and add new findings there.

## Safety and repository hygiene

- Never commit files extracted from games (assemblies, assets, dumps).
- Contributions must not target online/multiplayer games or bypass/hide from anti-cheat.
- Commit and push only when the user asks.
