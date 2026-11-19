"""Write the page on the agents' RPC interface (docs/development/agent-interface.md).

    python scripts/agent_interface.py           # write the page
    python scripts/agent_interface.py --check   # exit 1 if the page is out of date

Every agent that defines ``rpc.exports`` gets a table: its exports, the name
Python calls them by (Frida turns ``exports_sync.add_monitor`` into
``addMonitor``) and where Python calls them. The exports are read with
ast-grep (``ast-grep-cli``, in requirements.txt); the Python side with ``ast``.

Callers are found by name, not by type: a call ``x.add_monitor(...)`` or a
string ``"add_monitor"`` (dispatch through ``getattr``) counts. *Called in*
searches the modules that load the agent and the base classes (from other
modules) of their classes; *Same name elsewhere* lists the other modules with
that name, mostly the window calling the worker's method of the same name.
Names without an underscore (``stop``, ``inspect``) are too common for that
column and get none.
"""

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "grembyte"
PAGE = ROOT / "docs" / "development" / "agent-interface.md"
REPOSITORY = "https://github.com/grembyte/grembyte/blob/main/"

# The keys of the object assigned to rpc.exports.
RULE = r"""
id: rpc-export
language: javascript
rule:
  any:
    - kind: pair
    - kind: method_definition
    - kind: shorthand_property_identifier
  inside:
    kind: object
    inside:
      kind: assignment_expression
      has:
        field: left
        regex: '^rpc\.exports$'
"""

HEADER = """\
# Agent interface

<!-- Written by scripts/agent_interface.py; do not edit by hand. -->

The agents that run in the game answer Grembyte through Frida's RPC: each defines
`rpc.exports`, and Python calls an export through `script.exports_sync` in snake case
(`exports_sync.add_monitor(...)` runs `addMonitor`). This page lists, per agent, its
exports and where Python calls them, so that a change on one side finds the other.

The callers are found by name: a call `x.name(...)` or the string `"name"` (dispatch
through `getattr`), so a same-named method of another object can show up.

- *Loaded by*: the modules that create the agent.
- *Called in*: calls in those modules, and in the base classes their classes inherit
  from another module (such as `AgentWorker` in `features/trace.py`).
- *Same name elsewhere*: the same name in other modules. These are mostly the window
  calling the worker's Python method of that name, which then calls the export. Common
  names without an underscore (`stop`, `inspect`) are left out there.

Regenerate the page after changing an agent's exports:

```bash
./.venv/bin/python scripts/agent_interface.py
```
"""


def ast_grep():
    """The ast-grep binary: beside the running Python (the venv) or on PATH."""
    beside = Path(sys.executable).with_name("ast-grep")
    found = str(beside) if beside.exists() else shutil.which("ast-grep")
    if found is None:
        sys.exit("ast-grep not found; install it with: pip install ast-grep-cli")
    return found


def relative(path):
    """``path`` relative to the repository, with forward slashes."""
    return path.resolve().relative_to(ROOT).as_posix()


def embedded_agents():
    """JavaScript agents kept as strings in Python modules: {module: source}."""
    agents = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "rpc.exports" in node.value):
                agents[path] = node.value
    return agents


def scan_exports(paths, binary):
    """{path: [(export, line)]} of every rpc.exports found under ``paths``."""
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as rule:
        rule.write(RULE)
    try:
        output = subprocess.run(
            [binary, "scan", "--rule", rule.name, "--json=compact", *map(str, paths)],
            check=True, capture_output=True, text=True).stdout
    finally:
        Path(rule.name).unlink()
    exports = defaultdict(list)
    for match in json.loads(output):
        name = re.match(r"\s*(?:async\s+)?([A-Za-z_$][\w$]*)", match["text"]).group(1)
        exports[Path(match["file"]).resolve()].append(
            (name, match["range"]["start"]["line"] + 1))
    return exports


def collect_agents():
    """[(agent label, loader module, [(export, line)], embedded)] sorted by agent."""
    binary = ast_grep()
    agents = []
    for path, exports in scan_exports([PACKAGE], binary).items():
        agents.append((relative(path), path.with_suffix(".py"), sorted(exports, key=lambda e: e[1]),
                       False))
    embedded = embedded_agents()
    with tempfile.TemporaryDirectory() as folder:
        files = {}
        for module, source in embedded.items():
            file = Path(folder) / (module.stem + ".js")
            file.write_text(source, encoding="utf-8")
            files[file.resolve()] = module
        for file, exports in scan_exports(files, binary).items():
            module = files[file]
            agents.append((relative(module), module, sorted(exports, key=lambda e: e[1]), True))
    return sorted(agents)


def snake(name):
    """The name Python calls a camelCase export by (``addMonitor`` -> ``add_monitor``)."""
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def module_name(path):
    """The dotted module name of a file under the repository."""
    return ".".join(path.resolve().relative_to(ROOT).with_suffix("").parts)


class PythonIndex:
    """Where each name is called (``x.name(``) or used as a string, per module."""

    def __init__(self):
        """Index every module of the package."""
        self.uses = defaultdict(lambda: defaultdict(set))  # name -> module path -> lines
        self.imports = defaultdict(set)                    # module name -> importing paths
        self.trees = {}
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = self.trees[path] = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.uses[node.func.attr][path].add(node.lineno)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.fullmatch(r"[a-z_][a-z0-9_]*", node.value):
                        self.uses[node.value][path].add(node.lineno)
                elif (isinstance(node, ast.ImportFrom) and node.module
                      and any("agent_source" in alias.name.lower() for alias in node.names)):
                    self.imports[node.module].add(path)

    def loaders(self, module):
        """The loader module and the modules that import its agent source."""
        return sorted({module, *self.imports.get(module_name(module), ())})

    def scope(self, loaders):
        """{path: line ranges, or None for the whole module} searched for callers:
        the loaders and, in other modules, the base classes of their classes."""
        scope = dict.fromkeys(loaders)
        pending = list(loaders)
        while pending:
            path = pending.pop()
            imported = {}
            for node in self.trees[path].body:
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        imported[alias.asname or alias.name] = node.module
            for node in ast.walk(self.trees[path]):
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    source = imported.get(base.id) if isinstance(base, ast.Name) else None
                    target = ROOT.joinpath(*source.split(".")).with_suffix(".py") if source else None
                    if target not in self.trees or scope.get(target, ()) is None:
                        continue
                    for definition in self.trees[target].body:
                        if isinstance(definition, ast.ClassDef) and definition.name == base.id:
                            span = (definition.lineno, definition.end_lineno)
                            if span not in scope.setdefault(target, []):
                                scope[target].append(span)
                                pending.append(target)
        return scope

    def callers(self, name, scope):
        """(calls within ``scope``, same-named uses elsewhere), as {path: lines}."""
        inside, elsewhere = {}, {}
        for path, lines in sorted(self.uses.get(name, {}).items()):
            spans = scope.get(path, ())
            if spans is None:
                inside[path] = sorted(lines)
                continue
            within = sorted(l for l in lines if any(a <= l <= b for a, b in spans))
            if within:
                inside[path] = within
            if "_" in name and path not in scope:
                elsewhere[path] = sorted(lines)
        return inside, elsewhere


def link(path, line=None):
    """A Markdown link to ``path`` (at ``line``) on GitHub."""
    target = relative(path)
    label = target.removeprefix("grembyte/")
    if line is None:
        return f"[`{label}`]({REPOSITORY}{target})"
    return f"[`{label}:{line}`]({REPOSITORY}{target}#L{line})"


def places(found):
    """``module:first line (+more)`` per module, or a dash."""
    return ", ".join(link(path, lines[0]) + (f" (+{len(lines) - 1})" if len(lines) > 1 else "")
                     for path, lines in found.items()) or "–"


def render():
    """The page as Markdown."""
    index = PythonIndex()
    parts = [HEADER]
    for label, module, exports, embedded in collect_agents():
        loaders = index.loaders(module)
        scope = index.scope(loaders)
        parts.append(f"\n## `{label.removeprefix('grembyte/')}`\n")
        source = "the agent is a string in this module" if embedded else link(ROOT / label)
        parts.append(f"Agent: {source}. Loaded by: {', '.join(link(p) for p in loaders)}.\n")
        parts.append("| Export | Python | Called in | Same name elsewhere |\n"
                     "| --- | --- | --- | --- |")
        for export, line in exports:
            name = snake(export)
            export_cell = f"`{export}`" if embedded else (
                f"[`{export}`]({REPOSITORY}{label}#L{line})")
            inside, elsewhere = index.callers(name, scope)
            parts.append(f"| {export_cell} | `{name}` | {places(inside)} | {places(elsewhere)} |")
    return "\n".join(parts) + "\n"


def main():
    """Write or check the page."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the page is out of date")
    arguments = parser.parse_args()
    page = render()
    if arguments.check:
        current = PAGE.read_text(encoding="utf-8") if PAGE.exists() else ""
        if current != page:
            sys.exit(f"{relative(PAGE)} is out of date; run python scripts/agent_interface.py")
        return
    PAGE.write_text(page, encoding="utf-8")
    print(f"wrote {relative(PAGE)}")


if __name__ == "__main__":
    main()
