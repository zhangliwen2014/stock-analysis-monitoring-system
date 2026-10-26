"""看板与自选：登录后落地的第一屏。

行情板没轮到的时候那一行显示「无行情」，不是错误——盘中监控起没起来
由 /healthz 与管理页说话，这里只管把现状摆出来。

每一处仓库调用都在端点体内走 `web.contexts.current()`（D113）：这里没有模块级
缓存，也没有把仓库挂在 app.state 上。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from sams.bootstrap import AppCore, ServiceContext
from sams.core.codes import currency_override
from sams.core.errors import SamsError, ValidationError
from sams.core.quotes import DELAYED, LIVE, NO_DATA, quote_state
from sams.services.symbol_search import normalize_query
from sams.web.auth import csrf_protect, require_user
from sams.web.display import hms, index_close_label, pct_class, state_label

router = APIRouter()


def _day_start(now: datetime, tz_name: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz_name))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _market_open(ctx: ServiceContext, market: str, now: datetime) -> bool:
    """日历读不到时按休市显示：五态里「休市」最不会催人动手。

    库被调度线程握着时这里会撞 `DatabaseLocked`，那是常态而不是事故：看板该继续
    给出它能给的东西，而不是一屏 503。
    """
    try:
        return ctx.calendar.is_market_open(market, now)
    except SamsError:
        return False


#: 币种代码 → 现价那一格的单位（§5.4 规则 3：金额类数一律带币种）。
#: 认不出来的币种原样上屏：写成 `SGD` 至少不撒谎，写成「元」是把两种钱说成一种。
_CURRENCY_UNITS = {"CNY": "元", "HKD": "港元", "USD": "美元"}


def _price_unit(ctx: ServiceContext, market: str, code: str = "") -> str:
    """这一行现价的单位：先认标的的币种例外（B 股、双柜台），再落市场的配置币种。

    折算发生在页面这一侧：模板拿到的就是「港元」，不再拿市场代码去猜币种。
    市场从配置里撤了（自选里留着旧市场的行）时交空串——那一格只剩数字，比编一个单位诚实。
    """
    cfg = ctx.settings.markets.get(market)
    currency = currency_override(market, code) or (cfg.currency if cfg else "")
    return _CURRENCY_UNITS.get(currency, currency)


def _quote_rows(
    ctx: ServiceContext, core: AppCore, user_id: str, now: datetime
) -> list[dict]:
    """自选清单加上此刻的报价。行情板是进程级那一份，所以读它不经过线程上下文。"""
    tz = ctx.settings.server.display_tz
    rows = []
    for item in ctx.watchlists.list(user_id):
        quote = core.board.get(item.market, item.code)
        symbol = ctx.symbols.get(item.market, item.code)
        state = NO_DATA
        if quote is not None:
            state = quote_state(
                quote,
                now=now,
                market_open=_market_open(ctx, item.market, now),
                stale_s=ctx.settings.quote_stale_s,
            )
        master = symbol or {}
        rows.append(
            {
                "market": item.market,
                "code": item.code,
                # 主档没这一行（采集还没跑过这个市场）时名称交代码而不是空串：空白格
                # 读起来像页面坏了，而代码本身就在那一列里站着
                "name": master.get("name") or item.code,
                "unit": _price_unit(ctx, item.market, item.code),
                "note": item.note or "",
                "price": quote.price if quote else None,
                "pct": quote.pct_chg if quote else None,
                "pct_class": pct_class(quote.pct_chg if quote else None),
                "state": state_label(state),
                "quote_at": hms(quote.ts if quote else None, tz),
            }
        )
    return rows


@router.get("/")
def overview(request: Request):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    core = web.core
    now = core.clock.now_utc()
    tz = ctx.settings.server.display_tz

    snapshots = core.board.snapshot()
    markets = []
    for market in sorted(ctx.settings.markets):
        snap = snapshots.get(market)
        markets.append(
            {
                "name": market,
                "open": _market_open(ctx, market, now),
                "count": len(snap.quotes) if snap else 0,
                "poll_at": hms(snap.last_poll_at, tz) if snap else None,
                "error": snap.last_error if snap else None,
                "skip": snap.skip_reason if snap else None,
            }
        )

    day_start = _day_start(now, tz)
    alerts_today = len(ctx.alerts_store.list_for(user.id, since=day_start, limit=200))

    accounts = []
    for account in ctx.sim_accounts.list_for(user.id):
        summary = ctx.portfolio.summary(user.username, account.id)
        accounts.append(
            {
                "name": summary.name,
                "total": summary.total_report,
                "currency": summary.report_currency,
                "pnl": summary.total_pnl_report,
            }
        )

    # 检查清单只问「有没有」：所以读仓库而不走 rule_service.list——那一趟会为每条规则
    # 展开标的（读估值与日线），一页的第一眼不该付这个代价
    checklist = []
    if not ctx.watchlists.list(user.id):
        checklist.append(("把第一只票加进自选", "/watchlist"))
    if not ctx.rules_store.list_for(user.id):
        checklist.append(("建一条止损或目标价规则", "/rules/new"))
    if not ctx.sim_accounts.list_for(user.id):
        checklist.append(("开一个模拟账户录成交", "/portfolio"))
    if not ctx.theses.list(user.username):
        checklist.append(("写下第一条论点", "/theses"))

    failed_channels = [
        channel for channel in ctx.channels_store.list_for(user.id) if channel.last_error
    ]

    return web.render(
        request,
        "overview.html",
        title="看板",
        markets=markets,
        rows=_quote_rows(ctx, core, user.id, now),
        alerts_today=alerts_today,
        accounts=accounts,
        checklist=checklist,
        failed_channels=failed_channels,
    )


@router.get("/api/partials/quotes")
def quotes_partial(request: Request):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    now = web.core.clock.now_utc()
    return web.render(
        request, "partials/quotes.html", rows=_quote_rows(ctx, web.core, user.id, now)
    )


@router.get("/api/partials/indices")
def indices_partial(request: Request):
    """大盘条 fragment：与 quotes partial 同一套「首屏服务端渲染 + htmx 重拉」。"""
    require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    now = web.core.clock.now_utc()
    return web.render(
        request, "partials/indices.html", segments=_index_rows(ctx, web.core, now)
    )


def _index_rows(ctx: ServiceContext, core: AppCore, now: datetime) -> list[dict]:
    """报价板「此刻还活着」就用板，否则用库内最后一行：休市看不到数，是这一屏最大的旧痛点（D145）。"""
    settings = ctx.settings
    segments = []
    for market in sorted(settings.markets):
        rows = []
        market_open = _market_open(ctx, market, now)
        for index_code in settings.indices.for_market(market):
            quote = core.board.get(market, index_code)
            # 判活与自选行同一个口径（`quote_state`）：`is_stale` 只说「源没给时刻」，
            # 不说「它旧」——休市时板上那笔永远不 stale，拿它当判活会把几小时前的价叫「实时」
            state = NO_DATA
            if quote is not None:
                state = quote_state(
                    quote, now=now, market_open=market_open, stale_s=settings.quote_stale_s
                )
            if state in (LIVE, DELAYED):
                rows.append(
                    {
                        "code": index_code,
                        "label": settings.indices.label(index_code),
                        "value": quote.price,
                        # 源的涨跌幅按源自己给的前收盘算，与库里那列的基线不同，可以不相等
                        "pct": quote.pct_chg,
                        "pct_class": pct_class(quote.pct_chg),
                        "at": state_label(state),
                    }
                )
                continue
            latest = ctx.index_daily_store.latest_with_change(market, index_code)
            if latest is None:
                # 板和库都没有：这一行整个不出现，而不是摆一个「—」冒充有数
                continue
            rows.append(
                {
                    "code": index_code,
                    "label": settings.indices.label(index_code),
                    "value": latest.close,
                    # 仓库这列是本仓库按前一根日线算的，与源那一列基线不同，同一日可能给出不同数
                    "pct": latest.change_pct,
                    "pct_class": pct_class(latest.change_pct),
                    "at": index_close_label(latest.day),
                }
            )
        if rows:
            segments.append({"market": market, "rows": rows})
    return segments


@router.get("/api/symbol-search")
def symbol_search_partial(request: Request, code: str = "", market: str = ""):
    """候选面板：htmx 每敲一个字重来一次，空检索词回空面板，不打扰。"""
    require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    rows = [
        {
            "market": c.market,
            "code": c.code,
            "name": c.name,
            "price": c.close,
            "unit": _price_unit(ctx, c.market, c.code),
        }
        for c in ctx.symbol_search.search(code)
    ]
    # 表单上选中的市场排前面，但不排掉别的：选着 A 搜 00700，答案在 HK
    sel = market.strip().upper()
    rows = [r for r in rows if r["market"] == sel] + [r for r in rows if r["market"] != sel]
    # 「查无」只说给真的查过的检索词：`%%%`、光一个市场字母在归一那步就没了，没查不许说没查到
    searched = normalize_query(code) is not None
    return web.render(
        request,
        "partials/symbol_search.html",
        rows=rows,
        none_found=searched and not rows,
    )


def _watch_page(request: Request, *, error: str | None = None, status: int = 200):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    now = web.core.clock.now_utc()
    return web.render(
        request,
        "watchlist.html",
        status_code=status,
        title="自选",
        markets=sorted(ctx.settings.markets),
        rows=_quote_rows(ctx, web.core, user.id, now),
        error=error,
    )


@router.get("/watchlist")
def watchlist_page(request: Request):
    require_user(request)
    return _watch_page(request)


@router.post("/watchlist/add")
def watchlist_add(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    market: str = Form(""),
    code: str = Form(""),
    note: str = Form(""),
):
    require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    market = market.strip().upper()
    if market not in ctx.settings.markets:
        return _watch_page(
            request,
            error=f"市场 {market or '（空）'} 没配：只有 {sorted(ctx.settings.markets)}",
        )
    try:
        ctx.watchlists.add(
            request.state.sams_user.id, market, code.strip(), note=note.strip() or None
        )
    except ValidationError as exc:
        # 代码形态不对（HK 给了四位、US 给了小写中文）都是用户输入错：回原页带话，
        # 而不是抛给全局处理器变成一屏没有表单的 400
        return _watch_page(request, error=str(exc))
    # 加票即登记兴趣：主档建行 + 排补数拍（D144），归一在登记处做
    ctx.symbol_interest.ensure(market, code.strip(), actor=request.state.sams_user.username)
    return RedirectResponse("/watchlist", status_code=303)


@router.post("/watchlist/remove")
def watchlist_remove(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    market: str = Form(""),
    code: str = Form(""),
):
    require_user(request)
    ctx = request.app.state.web.contexts.current()
    try:
        ctx.watchlists.remove(request.state.sams_user.id, market.strip().upper(), code.strip())
    except ValidationError as exc:
        return _watch_page(request, error=str(exc))
    return RedirectResponse("/watchlist", status_code=303)
