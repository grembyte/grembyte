# Contributing to Grembyte

Thank you for your interest in Grembyte. Bug reports, reports on games that work or fail, ideas and pull requests are all welcome.

## Reporting bugs

Please use the [bug report form](https://github.com/grembyte/grembyte/issues/new/choose). The most helpful attachment is the ZIP from **Log → Export bug report …** in Grembyte: it contains the activity log, the log and `crash.log`.

Look through the ZIP before you attach it: issues are public, and the logs contain file paths (with your user name) and the names of the processes you attached to. Values from the game's memory are not in it.

Security problems go through a private report instead, see [SECURITY.md](SECURITY.md).

## Before a larger change

For anything bigger than a small fix, please open an issue first and describe what you have in mind. This avoids work on something that does not fit the project or is already in progress.

Not accepted are contributions that aim at online or multiplayer games, bypass anti-cheat systems or hide Grembyte from them.

## Development setup

Python 3.11 or newer is needed. On Linux, the tests and Grembyte itself need `kernel.yama.ptrace_scope=0`.

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[test,docs]"
./.venv/bin/python -m pytest
```

Many tests compile a small C program and attach to it with Frida; they are skipped without `gcc`, with a different `ptrace_scope` or on machines other than x86-64. The [development documentation](docs/development/index.md) explains the test targets, the [architecture](docs/development/architecture.md) and how the documentation and its screenshots are built.

## Pull requests

- Keep a pull request to one topic and base it on `main`.
- Add or update tests for changed behaviour, and run `pytest` before you push. For a small change confined to one place, the tests that cover it are enough (`pytest tests/test_<area>.py`); run the full suite when you touch shared code (attach, hooks, trace, engine adapters, the JavaScript agents) or an interface other modules use, or when you are not sure what a change affects. Changes to the documentation only need `mkdocs build --strict`.
- Comments, docstrings, documentation and commit messages are in English. Commit messages start with a verb in the imperative ("Add …", "Fix …").
- UI texts go into both `grembyte/ui/localization/en.json` and `de.json`. If you do not speak German, add the English text to both and say so in the pull request.
- Icons are Google Material Icons (Outlined), stored in `grembyte/ui/resources/icons/`.
- Update the documentation in `docs/` when you change what users see, and check it with `mkdocs build --strict`.
- Do not commit files extracted from games (assemblies, assets, dumps). They are not ours to publish.

## License

Grembyte is published under the [MIT License](LICENSE). By submitting a contribution, you agree that it is published under the same license.
