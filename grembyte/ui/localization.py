"""Load the UI translations for the selected language."""

import json
import locale
import os
from pathlib import Path


_SUPPORTED_LANGUAGES = {"de", "en"}
_CATALOG_DIR = Path(__file__).parent / "localization"


def language_code(language: str | None = None) -> str:
    """Resolve a language to one of the UI's supported short codes."""
    selected_language = language or os.environ.get("GREMBYTE_LANGUAGE")
    if not selected_language:
        selected_language = locale.getlocale()[0] or "en"

    code = selected_language.replace("-", "_").split("_", maxsplit=1)[0].lower()
    if code not in _SUPPORTED_LANGUAGES:
        code = "en"
    return code


def load_translations(language: str | None = None) -> dict[str, str]:
    """Load a catalog, preferring an explicit choice over the system locale."""
    code = language_code(language)

    catalog_path = _CATALOG_DIR / f"{code}.json"
    return json.loads(catalog_path.read_text(encoding="utf-8"))
