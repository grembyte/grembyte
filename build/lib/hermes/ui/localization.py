"""Load the UI translations for the selected language."""

import json
import locale
import os
from pathlib import Path


_SUPPORTED_LANGUAGES = {"de", "en"}
_CATALOG_DIR = Path(__file__).parent / "localization"


def load_translations(language: str | None = None) -> dict[str, str]:
    """Load a catalog, preferring an explicit choice over the system locale."""
    selected_language = language or os.environ.get("HERMES_LANGUAGE")
    if not selected_language:
        selected_language = locale.getlocale()[0] or "en"

    language_code = selected_language.replace("-", "_").split("_", maxsplit=1)[0].lower()
    if language_code not in _SUPPORTED_LANGUAGES:
        language_code = "en"

    catalog_path = _CATALOG_DIR / f"{language_code}.json"
    return json.loads(catalog_path.read_text(encoding="utf-8"))