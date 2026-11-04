"""行业层级归一：只做同一体系内的结构归一，不跨分类体系翻译（D236）。

这些用例钉住的是**屏幕与同业共用的那一份路径**：三级原样、全角与斜杠都算层级、
超过三级取更具体的末三级、空答复不是「一级行业叫空字符串」。
"""

from __future__ import annotations

from sams.core.industry import (
    IndustryPath,
    normalize_industry,
    parse_industry,
)


def test_three_level_path_is_kept_verbatim():
    path = parse_industry("食品饮料-饮料-白酒")
    assert path is not None
    assert path.levels == ("食品饮料", "饮料", "白酒")
    assert path.path == "食品饮料-饮料-白酒"


def test_fullwidth_and_ascii_separators_are_the_same_level():
    """全角斜杠/破折号/大于号与 ASCII 破折号必须是同一个层级分隔。"""
    expected = ("食品饮料", "饮料", "白酒")
    for raw in (
        "食品饮料－饮料－白酒",
        "食品饮料—饮料—白酒",
        "食品饮料/饮料/白酒",
        "食品饮料／饮料／白酒",
        "食品饮料>饮料>白酒",
        "食品饮料＞饮料＞白酒",
        "食品饮料 - 饮料 - 白酒",
    ):
        path = parse_industry(raw)
        assert path is not None, raw
        assert path.levels == expected, raw


def test_consecutive_and_trailing_separators_are_collapsed():
    path = parse_industry("  食品饮料 -- 饮料// 白酒 - ")
    assert path is not None
    assert path.levels == ("食品饮料", "饮料", "白酒")
    assert path.path == "食品饮料-饮料-白酒"


def test_more_than_three_levels_keep_the_last_three():
    path = parse_industry("制造/消费/食品饮料/饮料/白酒")
    assert path is not None
    assert path.levels == ("食品饮料", "饮料", "白酒"), "更具体的层级在后，取末三级"


def test_fewer_than_three_levels_keep_what_is_there():
    path = parse_industry("银行")
    assert path is not None
    assert path.levels == ("银行",)


def test_blank_input_is_none_not_an_empty_level():
    for raw in (None, "", "   ", "-", "//"):
        assert parse_industry(raw) is None, repr(raw)
        assert normalize_industry(raw) == "", repr(raw)


def test_normalize_returns_the_canonical_path_or_empty():
    assert normalize_industry("食品饮料／饮料／白酒") == "食品饮料-饮料-白酒"
    assert normalize_industry("牛奶") == "牛奶"


def test_key_and_match_keys_widen_from_specific_to_broad():
    path = parse_industry("食品饮料-饮料-白酒")
    assert isinstance(path, IndustryPath)

    def level_name(count: int) -> str:
        return path.key(count)

    assert level_name(3) == "食品饮料-饮料-白酒"
    assert level_name(2) == "食品饮料-饮料", "放宽是丢掉最细的一级，不是丢掉最宽的一级"
    assert level_name(1) == "食品饮料"
    assert level_name(0) == "", "0 级不是路径"
    assert level_name(9) == "食品饮料-饮料-白酒", "要得比有的多，给的还是全部"

    assert path.match_keys() == ("食品饮料-饮料-白酒", "食品饮料-饮料", "食品饮料")


def test_match_keys_on_a_two_level_path_only_has_two_steps():
    path = parse_industry("银行-股份行")
    assert path is not None
    assert path.match_keys() == ("银行-股份行", "银行")


def test_whitespace_inside_levels_is_trimmed_but_words_are_not_rewritten():
    path = parse_industry(" 家用电器 / 白色家电 ")
    assert path is not None
    assert path.levels == ("家用电器", "白色家电")


def test_aliases_are_not_translated_across_taxonomies():
    """申万与证监会各说各的：这里不回译成另一套体系的名字（D236）。"""
    path = parse_industry("申万:白酒")
    assert path is not None
    assert path.levels == ("申万:白酒",), "冒号不是层级分隔符，整段保留"
