"""个股页：一只票的全部现场——行情、K 线、挂在它身上的规则与论、最近的告警。

规则筛选认 `rule.target_type == "symbol"` 且 `target_ref == 市场/代码`：
这是 `repos/rules.py` 的 `_require_target_shape` 定死的形状。

论点的「为什么」不在 `Thesis` 上（那条 dataclass 只有状态与几列价位），它落在
`thesis_events.note` 里，所以要拿最近那一条有 note 的事件——这一屏给的是此刻这只票
的现场，不是整条时间线（完整时间线在 /theses/{id}）。

仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from sams.core.errors import SamsError
from sams.core.quotes import NO_DATA, quote_state
from sams.web.auth import require_user
from sams.web.display import hms, pct_class, state_label
from sams.web.pages.dashboard import _market_open

router = APIRouter()


def _require_symbol(request: Request, market: str, code: str):
    """市场没配或库里没有这只票都是 404：不给探测者区分「没有」与「不是你的」。"""
    web = request.app.state.web
    ctx = web.contexts.current()
    if market not in ctx.settings.markets:
        raise HTTPException(status_code=404)
    row = ctx.symbols.get(market, code)
    if row is None:
        raise HTTPException(status_code=404)
    return ctx, row


@router.get("/symbol/{market}/{code}")
def symbol_page(request: Request, market: str, code: str):
    user = require_user(request)
    web = request.app.state.web
    market = market.upper()
    ctx, symbol = _require_symbol(request, market, code)
    core = web.core
    now = core.clock.now_utc()

    quote = core.board.get(market, code)
    state = NO_DATA
    if quote is not None:
        state = quote_state(
            quote,
            now=now,
            market_open=_market_open(ctx, market, now),
            stale_s=ctx.settings.quote_stale_s,
        )

    ref = f"{market}/{code}"
    rules = [
        view
        for view in ctx.rule_service.list(user.id)
        if view.rule.target_type == "symbol" and view.rule.target_ref == ref
    ]
    theses = []
    for thesis in ctx.theses.list(user.username):
        if thesis.market == market and thesis.code == code:
            # 只为这一只票上的那几条论读时间线：全量读会把别名的论也展开一遍
            events = ctx.theses_store.events(user.id, thesis.id)
            note = next((event.note for event in reversed(events) if event.note), None)
            theses.append({"thesis": thesis, "note": note})
    alerts = ctx.alerts_store.list_for(user.id, market=market, code=code, limit=10)

    return web.render(
        request,
        "symbol.html",
        title=f"{market}/{code}",
        market=market,
        code=code,
        symbol=symbol,
        price=quote.price if quote else None,
        quote_pct=quote.pct_chg if quote else None,
        pct_class=pct_class(quote.pct_chg if quote else None),
        state=state_label(state),
        quote_at=hms(quote.ts if quote else None, ctx.settings.server.display_tz),
        rules=rules,
        theses=theses,
        alerts=alerts,
    )


@router.get("/api/symbol/{market}/{code}/bars")
def bars_api(request: Request, market: str, code: str, days: int = 120):
    require_user(request)
    web = request.app.state.web
    market = market.upper()
    ctx, _symbol = _require_symbol(request, market, code)
    end = web.core.clock.now_utc().date()
    start = end - timedelta(days=max(1, min(days, 2000)))
    try:
        read = ctx.analysis.series(market, code, start=start, end=end, indicators=("ma",))
    except SamsError as exc:
        # 序列读不出来（没日线、覆盖没确认过）不是接口故障：图空着，把那句话说给前端
        return JSONResponse(
            {"dates": [], "close": [], "ma20": [], "ma60": [], "note": str(exc)}
        )
    table = read.table
    return JSONResponse(
        {
            "dates": [str(value) for value in table["date"].to_list()],
            "close": table["close"].to_list(),
            "ma20": table["ma20"].to_list() if "ma20" in table.columns else [],
            "ma60": table["ma60"].to_list() if "ma60" in table.columns else [],
            "note": None,
        }
    )
