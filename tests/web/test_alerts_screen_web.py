"""选股新命中在收件箱里的那一面（S4-B / D256）：认得出「选股」，点得到个股页。

新命中复用告警中心（`rule_id` 为 NULL、`payload.kind=screen`），所以列表要能把
它与价格规则、资讯分开；点开的落点是个股页——筛出来之后要看的是这只票的现场。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.notify import MID


def _seed_screen(ctx, *, user_id):
    return ctx.alerts_store.create(
        user_id,
        rule_id=None,
        rule_name="方案《便宜》",
        account_id=None,
        account_name="",
        market="A",
        code="600519",
        symbol_name="贵州茅台",
        severity=MID,
        stage="hit",
        value_text="贵州茅台（600519）",
        threshold_text="自动监控新命中",
        action_note="点开个股页核对；这不是买卖建议",
        triggered_at=datetime(2026, 10, 31, 10, 0, tzinfo=UTC),
        facts={
            "kind": "screen",
            "preset_id": "p1",
            "preset_name": "便宜",
            "first_seen": "2026-10-31",
            "conditions": "现值市赚率 PR（小数比值（0.5 = 50%）） ≤ 0.5",
        },
    )


def test_the_inbox_marks_screen_hits_and_links_to_the_symbol(logged):
    ctx = logged.context
    alert = _seed_screen(ctx, user_id=ctx.users.get_by_username("alice").id)

    text = logged.client.get("/alerts").text
    assert '<span class="badge">选股</span>' in text
    assert "方案《便宜》" in text
    assert 'href="/symbol/A/600519"' in text
    assert f"/alerts/{alert.id}" in text


def test_the_detail_page_marks_a_screen_hit(logged):
    ctx = logged.context
    alert = _seed_screen(ctx, user_id=ctx.users.get_by_username("alice").id)

    text = logged.client.get(f"/alerts/{alert.id}").text

    assert '<span class="badge">选股</span>' in text
    assert "方案《便宜》" in text
