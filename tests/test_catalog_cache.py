"""The catalog cache: one file per assembly build, keyed by its MVID."""

import gzip
import os
import time

from grembyte.features import catalog_cache

_CLASSES = [["Game.Loot", [["Take", "(int&,int)", "System.Void", True, 0x06000002,
                            "Game.Loot:Take (int&,int)"]]]]
_MVID = "6F9619FF-8B86-D011-B42D-00C04FC964FF"


def test_stored_classes_come_back_for_the_same_build(tmp_path):
    catalog_cache.store("mono", "Assembly-CSharp", _MVID, _CLASSES, tmp_path)

    assert catalog_cache.load("mono", "Assembly-CSharp", _MVID, tmp_path) == _CLASSES
    assert catalog_cache.load("mono", "Assembly-CSharp", "another build", tmp_path) is None
    assert catalog_cache.load("mono", "Assembly-CSharp.Other", _MVID, tmp_path) is None
    assert catalog_cache.load("il2cpp", "Assembly-CSharp", _MVID, tmp_path) is None


def test_builds_of_one_name_stay_side_by_side(tmp_path):
    # Two games, each with its Assembly-CSharp
    catalog_cache.store("mono", "Assembly-CSharp", "game-a", _CLASSES, tmp_path)
    catalog_cache.store("mono", "Assembly-CSharp", "game-b", [], tmp_path)
    catalog_cache.store("mono", "Assembly-CSharp", "game-b", {"targets": []}, tmp_path,
                        kind=catalog_cache.CALLS)

    assert catalog_cache.load("mono", "Assembly-CSharp", "game-a", tmp_path) == _CLASSES
    assert catalog_cache.load("mono", "Assembly-CSharp", "game-b", tmp_path) == []
    assert catalog_cache.load("mono", "Assembly-CSharp", "game-b", tmp_path,
                              kind=catalog_cache.CALLS) == {"targets": []}


def test_unused_files_are_pruned(tmp_path):
    for mvid in ("old", "used", "new"):
        catalog_cache.store("mono", "Assembly-CSharp", mvid, _CLASSES, tmp_path)
    folder = tmp_path / "mono" / "Assembly-CSharp"
    long_ago = time.time() - (catalog_cache.MAX_AGE_DAYS + 1) * 24 * 3600
    for mvid in ("old", "used"):
        os.utime(folder / f"{mvid}.json.gz", (long_ago, long_ago))
    (folder / "lost.json.gz.part").write_bytes(b"")
    os.utime(folder / "lost.json.gz.part", (long_ago, long_ago))
    # Reading a file counts as using it.
    assert catalog_cache.load("mono", "Assembly-CSharp", "used", tmp_path) == _CLASSES

    catalog_cache.prune(tmp_path)
    assert sorted(path.name for path in folder.iterdir()) == ["new.json.gz", "used.json.gz"]


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


def test_the_cache_lies_in_grembyte_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMBYTE_CACHE_DIR", str(tmp_path))
    catalog_cache.store("mono", "Assembly-CSharp", _MVID, _CLASSES)

    assert catalog_cache.cache_dir() == tmp_path
    assert (tmp_path / "mono" / "Assembly-CSharp" / f"{_MVID}.json.gz").is_file()
