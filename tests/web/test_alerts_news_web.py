"""资讯告警在收件箱与详情页上的那一面（S3-C / D246）。

资讯命中复用 `alerts`（`rule_id` 为 NULL、`payload.kind=news`），所以列表要能
一眼认出「这不是价格规则」，并且点得到原文——一条点不开的资讯，用户无法核对。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.notify import MID


def _seed_news(ctx, *, user_id):
    return ctx.alerts_store.create(
        user_id,
        rule_id=None,
        rule_name="资讯",
        account_id=None,
        account_name="",
        market="A",
        code="600519",
        symbol_name="贵州茅台",
        severity=MID,
        stage="hit",
        value_text="贵州茅台：关于回购股份的公告",
        threshold_text="关键词：回购",
        action_note="点开原文核对；这不是买卖建议",
        triggered_at=datetime(2026, 10, 31, 10, 0, tzinfo=UTC),
        facts={
            "kind": "news",
            "title": "贵州茅台：关于回购股份的公告",
            "url": "https://example.test/N1",
            "keyword": "回购",
            "published_at": "2026-10-31T09:00:00+00:00",
            "source": "eastmoney_f10",
            "as_of": "2026-10-31T10:00:00+00:00",
        },
    )


def test_the_inbox_marks_news_and_links_to_the_original(logged):
    ctx = logged.context
    alert = _seed_news(ctx, user_id=ctx.users.get_by_username("alice").id)

    text = logged.client.get("/alerts").text
    assert "资讯" in text
    assert "贵州茅台：关于回购股份的公告" in text
    assert 'href="https://example.test/N1"' in text
    assert 'target="_blank"' in text
    assert f"/alerts/{alert.id}" in text


def test_the_detail_page_links_to_the_original(logged):
    ctx = logged.context
    alert = _seed_news(ctx, user_id=ctx.users.get_by_username("alice").id)

    text = logged.client.get(f"/alerts/{alert.id}").text
    assert "https://example.test/N1" in text
    assert "原文" in text
    assert "eastmoney_f10" in text
