"""The translation catalogs: every text in both languages, every agent message translated."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOG_DIR = ROOT / "hermes" / "ui" / "localization"
LANGUAGES = ("en", "de")
# hermesError('error.…', …) in the agents; also the keys of a ``cond ? 'a' : 'b'``
_AGENT_KEY = re.compile(r"hermesError\(\s*(?:[^'()]*\?\s*)?'([\w.]+)'(?:\s*:\s*'([\w.]+)')?")
_PLACEHOLDER = re.compile(r"\{(\w*)\}")


def _catalog(language):
    return json.loads((CATALOG_DIR / f"{language}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module", name="catalogs")
def _catalogs():
    return {language: _catalog(language) for language in LANGUAGES}


def test_catalogs_have_the_same_keys(catalogs):
    english = set(catalogs["en"])
    for language, catalog in catalogs.items():
        assert sorted(english - set(catalog)) == [], f"missing in {language}.json"
        assert sorted(set(catalog) - english) == [], f"only in {language}.json"


def test_texts_are_not_empty(catalogs):
    for language, catalog in catalogs.items():
        assert [key for key, text in catalog.items() if not text.strip()] == [], language


def test_translations_keep_their_placeholders(catalogs):
    english = catalogs["en"]
    for language, catalog in catalogs.items():
        differing = [key for key, text in catalog.items()
                     if set(_PLACEHOLDER.findall(text)) != set(_PLACEHOLDER.findall(english[key]))]
        assert differing == [], f"placeholders differ in {language}.json"


def test_agent_messages_are_translated(catalogs):
    used = {}
    for script in (ROOT / "hermes").rglob("*.js"):
        for match in _AGENT_KEY.finditer(script.read_text(encoding="utf-8")):
            for key in filter(None, match.groups()):
                used.setdefault(key, script.relative_to(ROOT).as_posix())
    assert len(used) > 100  # the pattern still finds the agents' messages
    for language, catalog in catalogs.items():
        missing = {key: script for key, script in used.items() if key not in catalog}
        assert missing == {}, f"agent messages missing in {language}.json"
