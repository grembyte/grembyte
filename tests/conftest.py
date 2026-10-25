"""Settings every test shares."""

import pytest


@pytest.fixture(autouse=True)
def _private_catalog_cache(tmp_path, monkeypatch):
    """Keep the catalog cache (see hermes.features.catalog_cache) out of the user's."""
    monkeypatch.setenv("HERMES_CACHE_DIR", str(tmp_path / "catalog-cache"))
