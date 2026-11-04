"""东财个股新闻的纯解析：JSONP 外壳、`<em>` 高亮、上海时区。

录一份真实形状的 payload 就能钉住字段漂移，不必联网。`<em>` 是搜索接口的命中高亮，
不是标题的一部分——把它留在标题里，页面上会出现一堆尖括号，关键词匹配也会被它干扰。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import DatasourceError, FieldDrift
from sams.datasource.news import parse_jsonp, parse_stock_news

PAYLOAD = {
    "code": 0,
    "msg": "OK",
    "result": {
        "cmsArticleWebOld": [
            {
                "date": "2026-10-30 18:36:28",
                "code": "202610303916753887",
                "title": "贵州茅台：第三季度净利润<em>199</em>.<em>51</em>亿元",
                "content": "贵州茅台(<em>600519</em>.SH)公告称，前三季度营业收入增长1.96%。",
                "mediaName": "每日经济新闻",
                "url": "http://finance.eastmoney.com/a/202610303916753887.html",
            }
        ]
    },
}


def test_jsonp_wrapper_is_unwrapped():
    wrapped = 'jQuery123({"code": 0, "result": {"cmsArticleWebOld": []}})'
    assert parse_jsonp(wrapped) == {"code": 0, "result": {"cmsArticleWebOld": []}}


def test_plain_json_is_accepted_too():
    assert parse_jsonp('{"code": 0}') == {"code": 0}


def test_jsonp_without_an_object_is_field_drift():
    with pytest.raises(FieldDrift):
        parse_jsonp("not-json-at-all")


def test_news_rows_carry_time_title_summary_source_and_link():
    frame = parse_stock_news(PAYLOAD, market="A", code="600519", source="eastmoney_f10")
    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["market"] == "A"
    assert row["code"] == "600519"
    assert row["item_id"] == "202610303916753887"
    assert row["title"] == "贵州茅台：第三季度净利润199.51亿元"
    assert row["summary"] == "贵州茅台(600519.SH)公告称，前三季度营业收入增长1.96%。"
    assert row["url"] == "http://finance.eastmoney.com/a/202610303916753887.html"
    assert row["published_at"] == datetime(2026, 10, 30, 10, 36, 28, tzinfo=UTC)


def test_missing_result_block_is_field_drift():
    with pytest.raises(FieldDrift):
        parse_stock_news({"code": 0}, market="A", code="600519", source="eastmoney_f10")


def test_an_empty_article_list_is_a_valid_empty_frame():
    frame = parse_stock_news(
        {"result": {"cmsArticleWebOld": []}},
        market="A",
        code="600519",
        source="eastmoney_f10",
    )
    assert frame.is_empty()
    assert "published_at" in frame.columns


def test_hk_is_refused_by_the_a_share_only_adapter():
    from sams.datasource.eastmoney_f10 import EastmoneyF10Adapter

    adapter = EastmoneyF10Adapter(get=lambda url, params: {}, get_news=lambda url, params: "")
    with pytest.raises(DatasourceError, match="新闻源未接入"):
        adapter.stock_news("HK", "00700", datetime(2026, 1, 1, tzinfo=UTC).date(),
                           datetime(2026, 1, 2, tzinfo=UTC).date())
