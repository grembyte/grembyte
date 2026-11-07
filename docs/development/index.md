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

Once the repository is public, the site can be published by GitHub Actions. A workflow like the following builds it on every push to `main` (enable it under *Settings → Pages → Source: GitHub Actions*):

```yaml title=".github/workflows/docs.yml"
name: docs
on:
  push:
    branches: [main]
    paths: ["docs/**", "mkdocs.yml"]
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
concurrency:
  group: pages
  cancel-in-progress: true
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install mkdocs-material mkdocs-glightbox
      - run: mkdocs build --strict
      - uses: actions/upload-pages-artifact@v3
        with:
          path: site
      - id: deployment
        uses: actions/deploy-pages@v4
```

## Code style

Comments, docstrings and documentation are in English. UI texts live in `hermes/ui/localization/{de,en}.json`; icons are Google Material Icons (Outlined) in `hermes/ui/resources/icons/`.
