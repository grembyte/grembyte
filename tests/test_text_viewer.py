"""The text viewer for long strings, e.g. a save game's JSON."""

import json

from PyQt6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module

from hermes.ui.widgets.text_viewer import TextViewerDialog, parse_json, summary

from tests.test_hooks import fixture_qt_app, fixture_translations  # pylint: disable=unused-import

_SAVE = json.dumps({"m_StatisticsBuffers": [{"Key": "PlayTimeStatistic", "Value": []},
                                            {"Key": "Level", "Value": [3, 4]}],
                    "m_PlayerIndex": 1})


def test_json_is_recognized_only_for_objects_and_arrays():
    assert parse_json(_SAVE)["m_PlayerIndex"] == 1
    assert parse_json('  [1, 2]') == [1, 2]
    assert parse_json('"text"') is None and parse_json("42") is None
    assert parse_json("{not json") is None
    assert [summary(value) for value in ({"a": 1}, [1, 2, 3], "x", None, 2.5)] == [
        "{1}", "[3]", '"x"', "null", "2.5"]
    assert summary("x" * 300).endswith("…")


def test_json_shows_as_a_tree_filled_on_expanding(qt_app, translations):
    # pylint: disable=unused-argument
    viewer = TextViewerDialog(translations, "statistic", _SAVE)
    try:
        assert viewer.tabs.tabText(0) == "Baum" and viewer.tabs.currentIndex() == 0
        assert "JSON" in viewer.info_label.text()
        top = viewer.tree.topLevelItem(0)
        assert (top.text(0), top.text(1)) == ("m_StatisticsBuffers", "[2]")
        assert top.childCount() == 0  # not filled before it is expanded
        top.setExpanded(True)
        assert [top.child(i).text(0) for i in range(top.childCount())] == ["[0]", "[1]"]
        # Formatted in the text tab
        assert viewer.plain.toPlainText().startswith('{\n  "m_StatisticsBuffers": [')

        viewer.tree.setCurrentItem(top.child(1))
        viewer.copy_button.click()
        assert json.loads(QGuiApplication.clipboard().text()) == {"Key": "Level",
                                                                  "Value": [3, 4]}
        viewer.tabs.setCurrentWidget(viewer.plain)
        viewer.copy_button.click()
        assert QGuiApplication.clipboard().text() == _SAVE
    finally:
        viewer.close()


def test_plain_text_and_a_cut_text(qt_app, translations):
    # pylint: disable=unused-argument
    viewer = TextViewerDialog(translations, "name")
    try:
        assert not viewer.copy_button.isEnabled()  # still loading
        viewer.set_text("hello world", length=20)
        assert viewer.tabs.count() == 1 and viewer.plain.toPlainText() == "hello world"
        assert viewer.info_label.text() == "Die ersten 11 von 20 Zeichen"
        assert viewer.copy_button.isEnabled()
        viewer.show_missing()
        assert "nicht mehr verfügbar" in viewer.info_label.text()
    finally:
        viewer.close()
