"""资讯页 /news 与个股「资讯」卡（S3-D）。

同一份读模型：/news 是自选、持仓、观察的时间线；个股卡是这一只票的最近五条。
页面只回答「哪一天、哪只票、什么标题、来源、原文」，命中词是徽标，不是新的判词。
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import polars as pl

from sams.datasource.f10 import empty_announcements
from sams.datasource.news import empty_news
from sams.storage.repos.symbol_backfills import NEWS

A_ZONE = ZoneInfo("Asia/Shanghai")


def _uid(ctx) -> str:
    return ctx.users.get_by_username("alice").id


def _symbol(ctx, market="A", code="600519", name="贵州茅台"):
    currency = {"A": "CNY", "HK": "HKD", "US": "USD"}[market]
    ctx.symbols.upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "name": [name],
                "currency": [currency],
            }
        )
    )


def _watch(ctx, code="600519", market="A") -> None:
    ctx.watchlists.add(_uid(ctx), market, code)


def _news(ctx, item_id="N1", code="600519", *, title=None, at=None, market="A"):
    ctx.news_store.upsert_many(
        pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "source": "eastmoney_f10",
                    "item_id": item_id,
                    "published_at": at or ctx.clock.now_utc() - timedelta(hours=1),
                    "title": title or f"资讯 {item_id}",
                    "summary": None,
                    "url": f"https://example.test/{item_id}",
                }
            ],
            schema=dict(empty_news().schema),
        )
    )


def _announcement(ctx, art_code="AN1", code="600519", *, title=None, on=None, market="A"):
    ctx.announcements_store.upsert(
        pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "art_code": art_code,
                    "published_on": on or ctx.clock.now_utc().astimezone(A_ZONE).date()
                    - timedelta(days=1),
                    "title": title or f"公告 {art_code}",
                    "category": None,
                    "url": f"https://example.test/{art_code}.html",
                    "source": "eastmoney_f10",
                }
            ],
            schema=dict(empty_announcements().schema),
        )
    )


def _hit(ctx, item_id="N1", *, code="600519", keyword="回购"):
    rule = ctx.news.keywords.add(_uid(ctx), "A", keyword, code=code)
    ctx.news.hits.record(
        _uid(ctx),
        "eastmoney_f10",
        item_id,
        rule.id,
        hit_at=ctx.clock.now_utc() - timedelta(minutes=1),
    )


def test_login_is_required(web_app) -> None:
    response = web_app.client.get("/news", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_header_names_the_read_moment_not_a_data_day(logged) -> None:
    """「数据截至 X」是假话（D288）：X 是本次读数的时刻，而每一条有自己的发布日。

    列表那一列本来就按发布时间标（今天给时分、更早给「月-日 时:分」），头部那句
    只该说「这一屏读于何时」，不能占掉「数据日」那个位置。
    """
    ctx = logged.context
    _symbol(ctx)
    _watch(ctx)
    _news(ctx, "N1", title="公司拟回购股份")

    page = logged.client.get("/news").text

    assert "数据截至" not in page
    assert "读于" in page


def test_the_nav_links_to_news_next_to_the_digest(logged) -> None:
    page = logged.client.get("/calendar").text
    assert 'href="/news"' in page
    assert page.index('href="/digest"') < page.index('href="/news"')


def test_the_feed_merges_announcements_and_news_for_my_symbols(logged) -> None:
    ctx = logged.context
    _symbol(ctx)
    _watch(ctx)
    _news(ctx, "N1", title="公司拟回购股份")
    _announcement(ctx, "AN1", title="三季度报告")
    _news(ctx, "N2", code="000001", title="不是我自选的")

    page = logged.client.get("/news").text

    assert "公司拟回购股份" in page and "三季度报告" in page
    assert "不是我自选的" not in page
    assert "600519" in page and "贵州茅台" in page
    assert 'href="https://example.test/N1"' in page
    assert 'target="_blank"' in page and 'rel="noopener"' in page
    assert "公告" in page and "新闻" in page
    assert page.index("公司拟回购股份") < page.index("三季度报告")


def test_hits_are_marked_and_can_be_the_only_rows(logged) -> None:
    ctx = logged.context
    _symbol(ctx)
    _watch(ctx)
    _news(ctx, "N1", title="公司拟回购股份")
    _news(ctx, "N2", title="日常经营公告")
    _hit(ctx)

    everything = logged.client.get("/news").text
    only_hits = logged.client.get("/news?hits=1").text

    assert "命中：回购" in everything
    assert "日常经营公告" in everything
    assert "公司拟回购股份" in only_hits
    assert "日常经营公告" not in only_hits


def test_the_code_filter_narrows_to_one_symbol(logged) -> None:
    ctx = logged.context
    _symbol(ctx)
    _symbol(ctx, code="601318", name="中国平安")
    _watch(ctx, "600519")
    _watch(ctx, "601318")
    _news(ctx, "N1", title="茅台的回购")
    _news(ctx, "N2", code="601318", title="平安的回购")

    page = logged.client.get("/news?code=601318").text

    assert "平安的回购" in page
    assert "茅台的回购" not in page
    assert "清除代码过滤" in page


def test_a_market_without_a_news_source_says_so(logged) -> None:
    ctx = logged.context
    _symbol(ctx, market="HK", code="00700", name="腾讯控股")
    _watch(ctx, "00700", market="HK")

    page = logged.client.get("/news?market=HK").text

    assert "港股新闻源未接入" in page


def test_the_empty_list_queues_news_for_my_symbols(logged) -> None:
    """空态排进补采队列，而不是印一条网页用户跑不了的命令（D341）。"""
    ctx = logged.context
    _symbol(ctx)
    _watch(ctx)

    page = logged.client.get("/news?market=A").text

    assert "sams sync news" not in page
    assert "已排进后台补采队列" in page
    assert ctx.backfills_store.open_for("A", "600519", kind=NEWS) is not None


def test_the_empty_list_does_not_promise_a_queue_for_unknown_codes(logged) -> None:
    """代码表认识的市场里一只表外票：不排，也不说「已排进」（D314/D341）。"""
    ctx = logged.context
    ctx.symbol_list_store.upsert_batch(
        "A",
        [{"code": "600519", "name": "贵州茅台", "py_initials": "gzmt",
          "py_full": "guizhoumaotai", "currency": "CNY"}],
    )
    _symbol(ctx, code="000001", name="平安银行")
    _watch(ctx, "000001")

    page = logged.client.get("/news?market=A").text

    assert "代码表里没有这些票" in page
    assert "已排进后台补采队列" not in page
    assert "sams sync news" not in page
    assert ctx.backfills_store.open_for("A", "000001", kind=NEWS) is None


def test_a_list_with_news_does_not_queue(logged) -> None:
    """已有资讯的票照常渲染，不排 news（D341）。"""
    ctx = logged.context
    _symbol(ctx)
    _watch(ctx)
    _news(ctx, "N1", title="茅台的回购")

    page = logged.client.get("/news?market=A").text

    assert "茅台的回购" in page
    assert "已排进后台补采队列" not in page
    assert ctx.backfills_store.open_for("A", "600519", kind=NEWS) is None


def test_pagination_keeps_the_filters(logged) -> None:
    ctx = logged.context
    _symbol(ctx)
    _watch(ctx)
    now = ctx.clock.now_utc()
    for index in range(35):
        _news(ctx, f"N{index}", title=f"旧闻 {index}", at=now - timedelta(hours=index + 1))

    first = logged.client.get("/news?q=茅台").text
    second = logged.client.get("/news?q=茅台&page=2").text

    assert "第 1 / 2 页" in first
    assert "page=2" in first
    assert "q=%E8%8C%85%E5%8F%B0" in first
    assert "旧闻 0" in first and "旧闻 34" not in first
    assert "旧闻 34" in second
    assert "page=1" in second


def test_the_symbol_card_shows_the_latest_five_and_links_to_all(logged) -> None:
    ctx = logged.context
    _symbol(ctx)
    now = ctx.clock.now_utc()
    for index in range(3):
        _news(ctx, f"N{index}", title=f"新闻 {index}", at=now - timedelta(hours=index + 1))
        _announcement(
            ctx,
            f"AN{index}",
            title=f"公告 {index}",
            on=now.astimezone(A_ZONE).date() - timedelta(days=index + 1),
        )
    _hit(ctx, "N0")

    page = logged.client.get("/api/partials/f10?market=A&code=600519").text

    assert "资讯" in page
    assert 'href="/news?market=A&amp;code=600519"' in page
    assert "新闻 0" in page and "公告 1" in page
    assert "公告 2" not in page  # 最近五条之外
    assert "命中：回购" in page
    assert 'target="_blank"' in page


def _post_keyword(logged, **data):
    body = {"csrf": logged.csrf, "market": "A"}
    body.update(data)
    return logged.client.post("/news/keywords", data=body, follow_redirects=False)


def test_i_can_save_a_keyword_and_see_it_on_the_page(logged):
    ctx = logged.context

    resp = _post_keyword(logged, keyword="回购")

    assert resp.status_code == 303
    page = logged.client.get("/news?market=A").text
    assert "回购" in page
    # 没选通道 = 只进摘要：页面得说清，否则人会等一条不会来的推送
    assert "只进摘要" in page
    rows = ctx.news_keywords_store.list_for(_uid(ctx), market="A")
    assert [row.channel_ids for row in rows] == [()]


def test_a_keyword_can_push_to_a_channel(logged):
    ctx = logged.context
    channel = ctx.channels_store.create(_uid(ctx), "desktop", "本机", {})

    resp = _post_keyword(logged, keyword="回购", channel_ids=[channel.id])

    assert resp.status_code == 303
    page = logged.client.get("/news?market=A").text
    assert "本机" in page
    rows = ctx.news_keywords_store.list_for(_uid(ctx), market="A")
    assert rows[0].channel_ids == (channel.id,)


def test_a_duplicate_keyword_comes_back_as_a_visible_error(logged):
    ctx = logged.context
    assert _post_keyword(logged, keyword="回购").status_code == 303

    again = _post_keyword(logged, keyword="回购")

    assert again.status_code == 409
    assert "已经存在" in again.text
    assert len(ctx.news_keywords_store.list_for(_uid(ctx), market="A")) == 1


def test_an_empty_keyword_is_rejected_with_a_visible_error(logged):
    resp = _post_keyword(logged, keyword="  ")

    assert resp.status_code == 400
    assert "1–40" in resp.text


def test_deleting_a_keyword_leaves_another_users_alone(logged):
    ctx = logged.context
    assert _post_keyword(logged, keyword="回购").status_code == 303
    alice = ctx.news_keywords_store.list_for(_uid(ctx), market="A")[0]
    bob = ctx.users.create("bob", "Bob-1234")
    ctx.news_keywords_store.add(bob.id, "A", "回购")

    resp = logged.client.post(
        f"/news/keywords/{alice.id}/delete",
        data={"csrf": logged.csrf, "market": "A"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert ctx.news_keywords_store.list_for(_uid(ctx), market="A") == []
    assert len(ctx.news_keywords_store.list_for(bob.id, market="A")) == 1


def test_keywords_are_listed_for_the_selected_market(logged):
    ctx = logged.context
    ctx.news_keywords_store.add(_uid(ctx), "HK", "验收-港股词")

    assert "验收-港股词" not in logged.client.get("/news?market=A").text
    assert "验收-港股词" in logged.client.get("/news?market=HK").text
