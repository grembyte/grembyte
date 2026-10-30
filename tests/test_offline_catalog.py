"""The offline catalog: games read before, shown from the catalog cache."""

import time

from hermes.engines.registry import engine_by_id
from hermes.features import catalog_cache, games
from hermes.features.call_graph import CallGraphWorker, _OfflineApi
from hermes.features.catalog import CachedCatalogWorker, catalog_methods

from tests.test_hooks import _wait_for, fixture_qt_app, fixture_translations  # pylint: disable=unused-import

_CLASSES = [["Game.Loot", [
    ["Take", "(int&,int)", "System.Void", True, 0x06000002, "Game.Loot:Take (int&,int)", 16, 0],
    ["Roll", "()", "System.Int32", False, 0x06000003, "Game.Loot:Roll ()", 0, 0]]]]
_ASSEMBLIES = [
    {"name": "Assembly-CSharp", "mvid": "game-mvid", "file": "C:/Game/Assembly-CSharp.dll",
     "classes": 1},
    {"name": "Plugins", "mvid": "plugins-mvid", "file": "C:/Game/Plugins.dll", "classes": 3},
    {"name": "mscorlib", "mvid": None, "file": "C:/Game/mscorlib.dll", "classes": 900},
]


def _remember(monkeypatch, path="C:\\Games\\Dark Heresy\\WH40KDH.exe", pid=4242):
    monkeypatch.setitem(games._programs, pid, path)  # pylint: disable=protected-access
    games.remember(pid, "mono", _ASSEMBLIES)


def test_games_are_remembered_per_program(monkeypatch):
    _remember(monkeypatch)
    time.sleep(0.01)
    _remember(monkeypatch, "/home/frank/Games/Other/Other.x86_64", pid=4243)
    # The same program again replaces its entry.
    _remember(monkeypatch, pid=4244)

    known = games.known_games()
    assert [game.name for game in known] == ["WH40KDH.exe", "Other.x86_64"]
    game = known[0]
    assert game.engine == "mono" and game.path == "C:\\Games\\Dark Heresy\\WH40KDH.exe"
    assert game.mvids() == {"Assembly-CSharp": "game-mvid", "Plugins": "plugins-mvid",
                            "mscorlib": None}
    assert len(list(games.games_dir().glob("*.json"))) == 2


def test_cached_worker_reads_the_catalog_without_a_process(monkeypatch):
    _remember(monkeypatch)
    catalog_cache.store("mono", "Assembly-CSharp", "game-mvid", _CLASSES)
    game = games.known_games()[0]
    worker = CachedCatalogWorker(game, engine=engine_by_id("mono"))
    loaded, failed, cached = {}, {}, []
    worker.assembly_loaded.connect(loaded.__setitem__)
    worker.assembly_failed.connect(failed.__setitem__)
    worker.assembly_cached.connect(cached.append)
    worker.run()  # on this thread: nothing to wait for

    assert loaded == {"Assembly-CSharp": catalog_methods("Assembly-CSharp", _CLASSES)}
    assert cached == ["Assembly-CSharp"]
    # Plugins was never read; mscorlib is framework and not asked for.
    assert failed == {"Plugins": CachedCatalogWorker.NOT_CACHED}


def test_offline_catalog_dialog(monkeypatch, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access,unused-argument
    from hermes.ui.widgets.catalog import CatalogDialog

    _remember(monkeypatch)
    catalog_cache.store("mono", "Assembly-CSharp", "game-mvid", _CLASSES)
    game = games.known_games()[0]
    dialog = CatalogDialog(translations, None, engine=engine_by_id("mono"), game=game)
    monitored = []
    dialog.monitor_requested.connect(monitored.append)
    try:
        _wait_for(qt_app, lambda: dialog._worker is None)
        assert dialog.model.rowCount() == 2
        assert "(offline, Stand " in dialog.windowTitle() and "WH40KDH.exe" in dialog.windowTitle()
        assert "nicht im Zwischenspeicher: Plugins" in dialog.status_label.text()
        assert dialog.reload_button.isHidden()
        dialog.view.selectRow(0)
        assert dialog.calls_button.isEnabled() and not dialog.monitor_button.isEnabled()
        dialog.view.doubleClicked.emit(dialog.model.index(0, 1))
        assert not monitored
    finally:
        dialog.close()


def test_offline_call_graph_answers_from_the_cache(monkeypatch, qt_app):
    # pylint: disable=protected-access,unused-argument
    _remember(monkeypatch)
    catalog_cache.store("mono", "Assembly-CSharp", "game-mvid", _CLASSES)
    # Take calls Roll; the index was made while the game ran.
    catalog_cache.store("mono", "Assembly-CSharp", "game-mvid",
                        {"targets": [["Assembly-CSharp", 0x06000003, "Game.Loot:Roll ()"]],
                         "calls": [[0x06000002, [["call", 0]]]], "references": {}},
                        kind=catalog_cache.CALLS)
    game = games.known_games()[0]
    worker = CallGraphWorker(None, engine=engine_by_id("mono"), game=game)
    callees, callers, failed = [], [], []
    worker.callees_found.connect(lambda _id, _m, calls, _body: callees.append(calls))
    worker.callers_found.connect(lambda _id, _m, calls, missing: callers.append((calls, missing)))
    worker.request_failed.connect(lambda _id, message: failed.append(message))
    methods = {m.name: m for m in catalog_methods("Assembly-CSharp", _CLASSES)}
    api = _OfflineApi(game)

    worker._answer_callees(api, 1, methods["Take"])
    assert [call.method.name for call in callees[0]] == ["Roll"]
    worker._answer_callers(api, 2, methods["Roll"])
    calls, missing = callers[0]
    assert [call.method.name for call in calls] == ["Take"] and missing == ["Plugins"]

    # A method of an assembly without a call index needs the game.
    catalog_cache.store("mono", "Plugins", "plugins-mvid", _CLASSES)
    worker.start()
    worker.callees(catalog_methods("Plugins", _CLASSES)[0])
    try:
        _wait_for(qt_app, lambda: failed)
        assert failed == [CallGraphWorker.NOT_CACHED]
    finally:
        worker.requestInterruption()
        worker.wait(2000)


def test_known_games_dialog(monkeypatch, qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.known_games import KnownGamesDialog

    empty = KnownGamesDialog(translations)
    assert empty.table.rowCount() == 0
    empty.close()

    _remember(monkeypatch)
    dialog = KnownGamesDialog(translations)
    try:
        assert dialog.table.rowCount() == 1
        assert dialog.table.item(0, 0).text() == "WH40KDH.exe"
        assert dialog.table.item(0, 3).text() == "2"  # assemblies with a build id
        dialog.table.cellDoubleClicked.emit(0, 0)
        assert dialog.game.name == "WH40KDH.exe"
    finally:
        dialog.close()


def test_catalog_search_accepts_wildcards():
    from hermes.ui.widgets.catalog import matches  # pylint: disable=import-outside-toplevel

    method = catalog_methods("Assembly-CSharp", [["Game.PlayerCombat", [
        ["ApplyDamage", "(int)", "System.Void", False, 0x06000004,
         "Game.PlayerCombat:ApplyDamage (int)", 0, 0]]]])[0]
    assert matches(["player*damage"], method)
    assert matches(["game.player?ombat", "apply"], method)
    assert matches(["*"], method) and matches(["player"], method)
    assert not matches(["damage*player"], method)
    assert not matches(["player.damage"], method)  # "." is literal, not "any character"
