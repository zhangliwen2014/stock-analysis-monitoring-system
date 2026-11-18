"""编码判据只有一份：写入口与存量修复共用 `repair_mojibake`（R3-26/D360）。"""

from __future__ import annotations

from sams.core.text import repair_mojibake


def test_a_latin1_mojibake_string_folds_back_to_the_chinese_it_was():
    broken = "现场验证".encode("utf-8").decode("latin-1")
    assert repair_mojibake(broken) == "现场验证"


def test_ascii_is_left_alone():
    assert repair_mojibake("600519 BUY") == "600519 BUY"


def test_real_chinese_is_left_alone():
    assert repair_mojibake("验收-混排") == "验收-混排"


def test_genuine_latin1_text_is_not_guessed_into_something_else():
    """真 latin-1 的 é 过不了 UTF-8 校验：折不动就原样返回，不猜。"""
    assert repair_mojibake("café") == "café"
