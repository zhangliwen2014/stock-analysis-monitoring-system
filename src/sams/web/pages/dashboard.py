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
from sams.core.errors import SamsError, ValidationError
from sams.core.quotes import NO_DATA, quote_state
from sams.web.auth import csrf_protect, require_user
from sams.web.display import hms, pct_class, state_label

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


def _quote_rows(
    ctx: ServiceContext, core: AppCore, user_id: str, now: datetime
) -> list[dict]:
    """自选清单加上此刻的报价。行情板是进程级那一份，所以读它不经过线程上下文。"""
    tz = ctx.settings.server.display_tz
    rows = []
    for item in ctx.watchlists.list(user_id):
        quote = core.board.get(item.market, item.code)
        symbol = ctx.symbols.get(item.market, item.code) or {}
        state = NO_DATA
        if quote is not None:
            state = quote_state(
                quote,
                now=now,
                market_open=_market_open(ctx, item.market, now),
                stale_s=ctx.settings.quote_stale_s,
            )
        rows.append(
            {
                "market": item.market,
                "code": item.code,
                # 主档里的 name 可能是 NULL（新加的代码还没采集过），显示成空串
                "name": symbol.get("name") or "",
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
