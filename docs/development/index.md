# Development

## Setup

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[test,docs]"
```

## Tests

```bash
./.venv/bin/python -m pytest
```

Many tests compile a small C program and attach to it with Frida: hooks, the watch, the native trace and monitor, the Mono features against a stand-in Mono runtime, the IL2CPP features against a stand-in `GameAssembly.so`, the Unreal features against a stand-in Unreal game. They are skipped if `gcc` is missing, `kernel.yama.ptrace_scope` is not `0` or the machine is not x86-64. Tests of the Windows helper need MinGW, some need Linux.

## Test targets

- `test_app/dummy_game.c` – the GTK test game of the [tutorial](../getting-started/first-hook.md).
- `test_app/unity/` – a small Unity project for Mono and IL2CPP builds.
- `test_app/unreal/` – notes on an Unreal test project.

## Documentation

The documentation is built with [MkDocs](https://www.mkdocs.org/) and [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) from `docs/` and `mkdocs.yml`:

```bash
./.venv/bin/mkdocs serve     # live preview at http://127.0.0.1:8000
./.venv/bin/mkdocs build     # static site in site/
```

The screenshots are rendered by a script; see [Screenshots](screenshots.md).

### Publishing on GitHub Pages

The workflow [`.github/workflows/docs.yml`](https://github.com/winterhafen/hermes-hook/blob/main/.github/workflows/docs.yml) builds the site with `mkdocs build --strict` on every push to `main` that changes `docs/` or `mkdocs.yml`, and publishes it at <https://winterhafen.github.io/hermes-hook/>. It can also be started by hand under *Actions → docs → Run workflow*. GitHub Pages must be set to *Settings → Pages → Source: GitHub Actions*.

The workflow pins the versions of `mkdocs-material` and `mkdocs-glightbox`; raise them there after checking a local build with the new versions.

## Code style

Comments, docstrings and documentation are in English. UI texts live in `hermes/ui/localization/{de,en}.json`; icons are Google Material Icons (Outlined) in `hermes/ui/resources/icons/`.
