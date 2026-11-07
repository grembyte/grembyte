"""Load the JavaScript of a Frida agent from the .js file next to its module."""

from pathlib import Path


def load_agent_source(module_file, name=None):
    """Return the text of ``name`` (default: the module's name with .js) beside ``module_file``."""
    path = Path(module_file)
    return (path.with_name(name) if name else path.with_suffix(".js")).read_text(encoding="utf-8")


# Prepended to every agent source that reports errors (see hermes.core.messages).
MESSAGES_SOURCE = load_agent_source(__file__, "messages.js")
