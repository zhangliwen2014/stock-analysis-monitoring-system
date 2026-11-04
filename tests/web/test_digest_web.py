"""每日摘要页 /digest（S1-C）。

摘要是「一天一条，只说变化」的感知入口：列表按日倒序，停更的日子也在——
没有变化的日子只落行不推送，但网页上必须看得到（否则「产出摘要」就只剩推送那一半）。
详情页的正文与投递结局都来自库里那一行，不重算。
"""

from __future__ import annotations

from datetime import date

from sams.core.notify import FAILED, SENT


def _user_id(ctx) -> str:
    return ctx.users.get_by_username("alice").id


def _seed(
    ctx,
    *,
    market: str = "A",
    as_of: date = date(2026, 10, 30),
    title: str | None = None,
    body: str = "【持仓】\n· 无变化",
    has_changes: bool = False,
    user_id: str | None = None,
):
    digest, _ = ctx.digests_store.save(
        user_id=user_id or _user_id(ctx),
        market=market,
        as_of=as_of,
        title=title or f"{market} · {as_of.isoformat()} 收盘摘要",
        body=body,
        has_changes=has_changes,
        heartbeat=False,
        source_note="数据：盘后任务无失败；覆盖 持仓 0、自选 1、观察 0。",
    )
    return digest


def test_login_is_required(web_app) -> None:
    response = web_app.client.get("/digest", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_nav_links_to_the_digest_page(logged) -> None:
    page = logged.client.get("/calendar").text
    assert 'href="/digest"' in page


def test_an_empty_history_says_how_digests_get_made(logged) -> None:
    page = logged.client.get("/digest").text
    assert "还没有每日摘要" in page


def test_the_list_shows_newest_first_with_the_delivery_outcome(logged) -> None:
    ctx = logged.context
    older = _seed(ctx, as_of=date(2026, 10, 29))
    sent = _seed(ctx, as_of=date(2026, 10, 30), has_changes=True)
    ctx.digests_store.mark_delivery(sent.id, SENT)

    page = logged.client.get("/digest").text

    assert page.index(sent.title) < page.index(older.title)
    assert f'/digest/{sent.id}' in page
    assert "已发送" in page
    # 没推过的那一天在网页上也要有个说法，而不是空白一格
    assert "未推送（无变化）" in page


def test_each_market_keeps_its_own_row_and_a_failed_delivery_is_named(logged) -> None:
    ctx = logged.context
    a = _seed(ctx, market="A", as_of=date(2026, 10, 30))
    hk = _seed(ctx, market="HK", as_of=date(2026, 10, 30))
    ctx.digests_store.mark_delivery(hk.id, FAILED, error="超时")

    page = logged.client.get("/digest").text

    assert "A · 2026-10-30" in page
    assert "HK · 2026-10-30" in page
    assert f'/digest/{a.id}' in page and f'/digest/{hk.id}' in page
    assert "发送失败" in page


def test_the_detail_page_prints_the_stored_body_and_the_outcome(logged) -> None:
    ctx = logged.context
    digest = _seed(
        ctx,
        body="【持仓】\n· 600519 贵州茅台：强「跌破止损」(1392.05 / 1400.00)",
        has_changes=True,
    )
    ctx.digests_store.mark_delivery(digest.id, FAILED, error="SMTP 超时")

    page = logged.client.get(f"/digest/{digest.id}").text

    assert "600519 贵州茅台" in page
    assert "发送失败" in page
    assert "SMTP 超时" in page


def test_another_users_digest_is_a_404(logged) -> None:
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    digest = _seed(ctx, user_id=bob.id)

    assert logged.client.get(f"/digest/{digest.id}").status_code == 404


def test_an_unknown_digest_id_is_a_404(logged) -> None:
    assert logged.client.get("/digest/no-such-digest").status_code == 404
