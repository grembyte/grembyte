"""The catalog cache: one file per assembly build, keyed by its MVID."""

import gzip

from hermes.features import catalog_cache

_CLASSES = [["Game.Loot", [["Take", "(int&,int)", "System.Void", True, 0x06000002,
                            "Game.Loot:Take (int&,int)"]]]]
_MVID = "6F9619FF-8B86-D011-B42D-00C04FC964FF"


def test_stored_classes_come_back_for_the_same_build(tmp_path):
    catalog_cache.store("mono", "Assembly-CSharp", _MVID, _CLASSES, tmp_path)

    assert catalog_cache.load("mono", "Assembly-CSharp", _MVID, tmp_path) == _CLASSES
    assert catalog_cache.load("mono", "Assembly-CSharp", "another build", tmp_path) is None
    assert catalog_cache.load("mono", "Assembly-CSharp.Other", _MVID, tmp_path) is None
    assert catalog_cache.load("il2cpp", "Assembly-CSharp", _MVID, tmp_path) is None


def test_a_new_build_replaces_the_old_one(tmp_path):
    catalog_cache.store("mono", "Assembly-CSharp", "old", _CLASSES, tmp_path)
    catalog_cache.store("mono", "Assembly-CSharp", _MVID, [], tmp_path)

    assert catalog_cache.load("mono", "Assembly-CSharp", "old", tmp_path) is None
    assert catalog_cache.load("mono", "Assembly-CSharp", _MVID, tmp_path) == []
    assert [path.name for path in (tmp_path / "mono" / "Assembly-CSharp").iterdir()] == [
        f"{_MVID}.json.gz"]


def test_assemblies_without_mvid_are_not_cached(tmp_path):
    catalog_cache.store("mono", "Assembly-CSharp", None, _CLASSES, tmp_path)

    assert not any(tmp_path.iterdir())
    assert catalog_cache.load("mono", "Assembly-CSharp", None, tmp_path) is None


def test_odd_names_stay_inside_the_cache(tmp_path):
    catalog_cache.store("mono", "../evil/name", "a/b", _CLASSES, tmp_path / "cache")

    assert catalog_cache.load("mono", "../evil/name", "a/b", tmp_path / "cache") == _CLASSES
    catalog_cache.store("mono", "..", "..", _CLASSES, tmp_path / "cache")
    assert catalog_cache.load("mono", "..", "..", tmp_path / "cache") == _CLASSES
    assert [path.name for path in tmp_path.iterdir()] == ["cache"]


def test_damaged_or_foreign_files_are_ignored(tmp_path):
    catalog_cache.store("mono", "Assembly-CSharp", _MVID, _CLASSES, tmp_path)
    path = tmp_path / "mono" / "Assembly-CSharp" / f"{_MVID}.json.gz"

    path.write_bytes(b"not gzip")
    assert catalog_cache.load("mono", "Assembly-CSharp", _MVID, tmp_path) is None
    with gzip.open(path, "wt", encoding="utf-8") as file:
        file.write('{"format": 0, "assembly": "Assembly-CSharp", "classes": []}')
    assert catalog_cache.load("mono", "Assembly-CSharp", _MVID, tmp_path) is None


def test_the_cache_lies_in_hermes_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CACHE_DIR", str(tmp_path))
    catalog_cache.store("mono", "Assembly-CSharp", _MVID, _CLASSES)

    assert catalog_cache.cache_dir() == tmp_path
    assert (tmp_path / "mono" / "Assembly-CSharp" / f"{_MVID}.json.gz").is_file()
