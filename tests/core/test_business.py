"""核心业务六字段的词表：屏幕、表单、仓库认同一份 key 与长度（D240）。"""

from __future__ import annotations

from sams.core.business import (
    BUSINESS_FIELD_KEYS,
    BUSINESS_FIELDS,
    BUSINESS_SOURCES,
    SOURCE_COMPUTED,
    SOURCE_PROFILE_SUMMARY,
    SOURCE_USER,
    field_spec,
    source_label,
)


def test_the_six_fields_are_in_the_documented_order():
    assert BUSINESS_FIELD_KEYS == (
        "business",
        "revenue_mix",
        "customers",
        "capacity",
        "position",
        "trend",
    )


def test_every_field_has_a_label_hint_and_a_positive_limit():
    for spec in BUSINESS_FIELDS:
        assert spec.label.strip(), spec.key
        assert spec.hint.strip(), spec.key
        assert spec.max_chars > 0, spec.key


def test_field_spec_rejects_an_unknown_key():
    try:
        field_spec("market_share")
    except KeyError:
        pass
    else:
        raise AssertionError("未登记的字段必须拒绝，不能返回一份空白规格")


def test_sources_have_one_chinese_name_each():
    assert BUSINESS_SOURCES == (SOURCE_PROFILE_SUMMARY, SOURCE_COMPUTED, SOURCE_USER)
    assert source_label(SOURCE_PROFILE_SUMMARY) == "公司概况"
    assert source_label(SOURCE_COMPUTED) == "系统计算"
    assert source_label(SOURCE_USER) == "用户录入"
    assert source_label("unknown_source") == "unknown_source", "认不出来源时露出原词，不编一个"
