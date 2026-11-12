"""资讯页 /news（S3-D）：自选、持仓、观察的公告与新闻，一条时间线。

这一页只回答「最近发生了什么」：关键词命中高亮并可只看命中；原文一律外链，
本页不抓正文。默认看 A 股——HK/US 没有新闻源（D247），选到它们的市场要把
「未接入」说清楚，而不是摆一张空表。

关键词的增删也在这里（S3-C / D245）：没有这个入口，命中与推送就没有生产者。
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import Conflict, SamsError, ValidationError
from sams.services.news import NEWS_MARKETS, NEWS_WINDOW_DAYS
from sams.storage.repos.symbol_backfills import NEWS
from sams.storage.repos.theses import OBSERVING
from sams.web.auth import csrf_protect, require_user
from sams.web.display import moment_label
from sams.web.feed import feed_rows, group_rows
from sams.web.forms import form_data, one

router = APIRouter()

#: 一屏三十条：够翻两三天，也不会让一页把库翻穿
PAGE_SIZE = 30
#: 每个源读这么多再合并：分页在本页内存里做，窗口（30 天）比这个上限窄得多
FEED_LIMIT = 200
_MARKET_LABELS = {"A": "A 股", "HK": "港股", "US": "美股"}

_form_body = Depends(form_data)


@router.get("/news")
def news_page(
    request: Request,
    market: str = "A",
    code: str = "",
    q: str = "",
    hits: str = "",
    page: int = 1,
):
    return _page(request, market=market, code=code, q=q, hits=hits, page=page)


@router.post("/news/keywords")
def add_keyword(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    market = one(data, "market").strip() or "A"
    code = one(data, "code").strip()
    keyword = one(data, "keyword").strip()
    channels = tuple(
        value.strip() for value in data.get("channel_ids", []) if value.strip()
    )
    try:
        ctx.news_keywords_store.add(
            user.id, market, keyword, code=code or None, channel_ids=channels
        )
    except SamsError as exc:
        status = 409 if isinstance(exc, Conflict) else 400
        return _page(
            request,
            market=market,
            code=code,
            keyword_error=str(exc),
            keyword_draft={"keyword": keyword, "code": code, "channels": set(channels)},
            status_code=status,
        )
    return RedirectResponse(_back(market, code), status_code=303)


@router.post("/news/keywords/{keyword_id}/delete")
def delete_keyword(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    market = one(data, "market").strip() or "A"
    code = one(data, "code").strip()
    ctx.news_keywords_store.remove(request.path_params["keyword_id"], user.id)
    return RedirectResponse(_back(market, code), status_code=303)


def _page(
    request: Request,
    *,
    market: str = "A",
    code: str = "",
    q: str = "",
    hits: str = "",
    page: int = 1,
    keyword_error: str = "",
    keyword_draft: dict | None = None,
    status_code: int = 200,
):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    market = canonical_market(market)
    if market not in ctx.settings.markets:
        raise ValidationError(
            f"未知市场 {market!r}：可选的是 {'、'.join(ctx.settings.markets)}"
        )
    only_hits = hits in ("1", "on", "true")
    text = q.strip()
    codes = _scope(ctx, user, market, code)
    names = {item: _name(ctx, market, item) for item in codes}
    if text:
        folded = text.casefold()
        codes = [
            item
            for item in codes
            if folded in item.casefold() or folded in names[item].casefold()
        ]

    now = ctx.clock.now_utc()
    zone = ctx.settings.markets[market].zone()
    items = (
        ctx.news.feed(
            user.id,
            market,
            codes,
            since=now - timedelta(days=NEWS_WINDOW_DAYS),
            until=now,
            limit=FEED_LIMIT,
            keyword_only=only_hits,
        )
        if codes
        else []
    )
    today = now.astimezone(zone).date()
    rows = feed_rows(items, zone=zone, today=today, names=names)
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    current = min(max(page, 1), pages)
    shown = rows[(current - 1) * PAGE_SIZE : current * PAGE_SIZE]
    base = {"market": market}
    if code:
        base["code"] = canonical_code(market, code)
    if text:
        base["q"] = text
    if only_hits:
        base["hits"] = "1"

    channels = [channel for channel in ctx.channels_store.list_for(user.id)]
    channel_names = {channel.id: channel.name for channel in channels}
    keywords = []
    for row in ctx.news_keywords_store.list_for(user.id, market=market):
        pushed = [
            channel_names.get(channel_id, f"已删除通道 {channel_id[:6]}")
            for channel_id in row.channel_ids
        ]
        scope = (
            "全部"
            if row.code is None
            else f"{row.code} {names.get(row.code, _name(ctx, market, row.code))}"
        )
        keywords.append(
            {
                "id": row.id,
                "keyword": row.keyword,
                "scope": scope,
                "channels": "、".join(pushed) if pushed else "只进摘要",
                "at": moment_label(row.created_at, ctx.settings.server.display_tz),
            }
        )
    draft = keyword_draft or {"keyword": "", "code": base.get("code", ""), "channels": set()}

    return web.render(
        request,
        "news.html",
        status_code=status_code,
        title="资讯",
        market=market,
        market_label=_MARKET_LABELS.get(market, market),
        market_options=[
            (name, _MARKET_LABELS.get(name, name))
            for name in sorted(ctx.settings.markets)
        ],
        code=base.get("code", ""),
        q=text,
        only_hits=only_hits,
        covered=market in NEWS_MARKETS,
        window_days=NEWS_WINDOW_DAYS,
        as_of=moment_label(now, ctx.settings.server.display_tz),
        groups=group_rows(shown, today=today),
        page=current,
        pages=pages,
        prev_url=_page_url(base, current - 1) if current > 1 else "",
        next_url=_page_url(base, current + 1) if current < pages else "",
        empty_message=_empty_message(
            ctx=ctx,
            market=market,
            market_label=_MARKET_LABELS.get(market, market),
            codes=codes,
            text=text,
            only_hits=only_hits,
            covered=market in NEWS_MARKETS,
            total=len(rows),
        ),
        keywords=keywords,
        keyword_error=keyword_error,
        keyword_draft=draft,
        channels=channels,
    )


def _back(market: str, code: str) -> str:
    base: dict[str, str] = {"market": market}
    if code:
        base["code"] = code
    return "/news?" + urlencode(base)


def _scope(ctx, user, market: str, code: str) -> list[str]:
    """有代码就只看这一只（个股页「全部」链进来）；否则是自选 + 持仓 + 观察。"""
    if code:
        return [canonical_code(market, code)]
    ordered: list[str] = []

    def add(item_market: str, item_code: str) -> None:
        if item_market == market and item_code not in ordered:
            ordered.append(item_code)

    for account in ctx.sim_accounts.list_for(user.id):
        for position in ctx.sim_ledger.positions(account.id):
            if position.qty != 0:
                add(position.market, position.code)
    for item in ctx.watchlists.list(user.id):
        add(item.market, item.code)
    for thesis in ctx.theses_store.list(user.id, status=OBSERVING):
        add(thesis.market, thesis.code)
    return ordered


def _name(ctx, market: str, code: str) -> str:
    found = ctx.symbols.get(market, code) or ctx.symbol_list_store.get(market, code) or {}
    return str(found.get("name") or code)


def _page_url(base: dict[str, str], page: int) -> str:
    return "/news?" + urlencode({**base, "page": page})


def _queue_news_scope(ctx, market: str, codes: list[str]) -> bool:
    """把这一屏的标的逐只排进 news 补采队列（D341，与个股页 D339 同一意图）。

    准入闸与个股页同一道（D314）：代码表不认识的票跳过。`enqueue` 幂等，列表反复刷新
    不刷屏。一只都没排上时返回 False，让文案别说「已排进」那句假话。市场已在路由层
    校验过（`news_page` 入口挡未配置市场），这里只需逐只 admits。
    """
    queued = False
    for code in codes:
        if not ctx.symbol_list_store.admits(market, code):
            continue
        ctx.backfills_store.enqueue(market, code, NEWS)
        queued = True
    return queued


def _empty_message(
    *,
    ctx,
    market: str,
    market_label: str,
    codes: list[str],
    text: str,
    only_hits: bool,
    covered: bool,
    total: int,
) -> str:
    if total:
        return ""
    if only_hits:
        return "暂无命中：关键词只对入库之后的资讯生效，加词或改词不会回扫历史。"
    if text and not codes:
        return "自选、持仓或观察里没有匹配的标的。"
    if not covered:
        return f"{market_label}新闻源未接入（v0 只接 A 股），这里只有公告。"
    if not codes:
        return f"自选、持仓与观察里还没有{market_label}的标的。"
    # 最后这一支会真的排队（D341）：网页用户没有终端，印命令只会让他对着 serve 占着的
    # 库敲出 DatabaseLocked。排上了才说「已排进」，一只都没排上就说代码表
    if _queue_news_scope(ctx, market, codes):
        tail = "已排进后台补采队列：补完刷新这一页。"
    else:
        tail = "代码表里没有这些票，补采排不上——先等代码表同步。"
    return f"最近 {NEWS_WINDOW_DAYS} 天没有公告或新闻。盘后任务会自动采集；{tail}"
