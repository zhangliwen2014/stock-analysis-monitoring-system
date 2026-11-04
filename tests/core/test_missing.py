"""缺失四级：屏幕只说这四个词，未知档不能静默变成空白（D241）。"""

from __future__ import annotations

from sams.core.missing import (
    MISSING_LABELS,
    NOT_APPLICABLE,
    NOT_COVERED,
    NOT_DISCLOSED,
    UNCOMPUTABLE,
    missing_label,
)


def test_the_four_kinds_map_to_the_four_chinese_words():
    assert missing_label(NOT_APPLICABLE) == "不适用"
    assert missing_label(NOT_DISCLOSED) == "未披露"
    assert missing_label(UNCOMPUTABLE) == "无法计算"
    assert missing_label(NOT_COVERED) == "未覆盖"
    assert set(MISSING_LABELS) == {
        NOT_APPLICABLE,
        NOT_DISCLOSED,
        UNCOMPUTABLE,
        NOT_COVERED,
    }


def test_no_missing_state_renders_as_empty():
    assert missing_label("") == ""


def test_an_unknown_kind_is_visible_not_silent():
    """漏登记一个新档时，屏幕要露出那个词，而不是假装没有缺失。"""
    text = missing_label("some_new_kind")
    assert text != ""
    assert "some_new_kind" in text
    assert "未知" in text
