"""个股页：一只票的全部现场——行情、K 线、挂在它身上的规则与论、最近的告警。

规则筛选认 `rule.target_type == "symbol"` 且 `target_ref` 归一后等于这只票：
`repos/rules.py` 的 `_require_target_shape` 定死的是「市场/代码」这个**形状**，而存进去
的是原样那串；求值时 `expand()` 会归一，所以这里也必须归一（见 `_ref_matches`）。

论点的「为什么」不在 `Thesis` 上（那条 dataclass 只有状态与几列价位），它落在
`thesis_events.note` 里，所以要拿最近那一条有 note 的事件——这一屏给的是此刻这只票
的现场，不是整条时间线（完整时间线在 /theses/{id}）。

仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import NotAdjustable
from sams.core.quotes import NO_DATA, quote_state
from sams.web.auth import csrf_protect, require_user
from sams.web.display import hms, moment_label, pct_class, state_label
from sams.web.pages.dashboard import _market_open

router = APIRouter()


def _require_symbol(request: Request, user, market: str, code: str):
    """市场没配或库里没有这只票都是 404：不给探测者区分「没有」与「不是你的」。

    进来的 URL 先归一再比：库里的键只有一个形态（`canonical_code`），而人能敲出来的
    有 `700` / `00700` / `HK00700` 好几种。不归一的话主档那一眼就过得去了（`symbols.get`
    自己会归一），接着的每一眼都过不去——行情板的键是 `(quote.market, quote.code)` 精确
    取、论与告警存的都是归一后的代码，于是 /symbol/HK/700 给一只确实存在的票画出
    「无行情 + 三张空表」。归一一次，下游全部用归一后的这一对。

    主档没有但代码表有 = 「全市场认识这只票、本地档案还没追上」：兜底建行走
    `SymbolInterest.ensure`（顺带排补数），不再 404（D146）。两个都没有才 404，
    措辞不变——不给探测者区分「没有」与「不是你的」。
    """
    web = request.app.state.web
    ctx = web.contexts.current()
    market = canonical_market(market)
    code = canonical_code(market, code)
    if market not in ctx.settings.markets:
        # 挡住兜底：ensure 要拿 `markets[market]` 的币种，市场没配置会在服务层 KeyError
        raise HTTPException(status_code=404)
    row = ctx.symbols.get(market, code)
    if row is None and ctx.symbol_list_store.get(market, code) is not None:
        ctx.symbol_interest.ensure(market, code, actor=user.username)
        row = ctx.symbols.get(market, code)
    if row is None:
        raise HTTPException(status_code=404)
    return ctx, market, code, row


def _ref_matches(target_ref: str, market: str, code: str) -> bool:
    """这条规则是不是挂在这只票上。

    `target_ref` 存的是创建时那一串原样（`repos/rules.py` 只 strip），而求值时
    `snapshot.expand()` 会 `canonical_market`/`canonical_code` 再取对象：一条写成
    `HK/700` 的规则真的会在 `HK/00700` 上响。所以这里也必须归一，否则同一只票的
    两种写法在页面上互相看不见——规则列表为空，而那条规则昨天刚响过。
    """
    ref_market, _, ref_code = target_ref.partition("/")
    ref_market = canonical_market(ref_market)
    return ref_market == market and canonical_code(ref_market, ref_code) == code


@router.get("/symbol/{market}/{code}")
def symbol_page(request: Request, market: str, code: str):
    user = require_user(request)
    web = request.app.state.web
    ctx, market, code, symbol = _require_symbol(request, user, market, code)
    core = web.core
    now = core.clock.now_utc()
    tz = ctx.settings.server.display_tz

    quote = core.board.get(market, code)
    state = NO_DATA
    if quote is not None:
        state = quote_state(
            quote,
            now=now,
            market_open=_market_open(ctx, market, now),
            stale_s=ctx.settings.quote_stale_s,
        )

    # 只问「有哪些规则挂在这只票上」，所以读仓库而不是 rule_service.list：后者会为
    # 每一条规则展开标的（读估值与日线），一页的第一眼不该付这个代价，锁库时那趟还是
    # 一屏渲染与 503 的分别（与看板检查清单同一句话，dashboard.py）
    rules = [
        rule
        for rule in ctx.rules_store.list_for(user.id)
        if rule.target_type == "symbol" and _ref_matches(rule.target_ref, market, code)
    ]
    theses = []
    for thesis in ctx.theses.list(user.username):
        if thesis.market == market and thesis.code == code:
            # 只为这一只票上的那几条论读时间线：全量读会把别名的论也展开一遍
            events = ctx.theses_store.events(user.id, thesis.id)
            note = next((event.note for event in reversed(events) if event.note), None)
            theses.append({"thesis": thesis, "note": note})
    alerts = [
        {
            "id": alert.id,
            # 不在模板里 strftime：TIMESTAMPTZ 回读不稳（有时 UTC 有时会话本地），
            # 而上面那行行情已经折到 display_tz，两个时区印在同一屏上看不出来
            "at": moment_label(alert.triggered_at, tz),
            "rule_name": alert.rule_name,
            "stage": alert.stage,
            "status": alert.status,
        }
        for alert in ctx.alerts_store.list_for(user.id, market=market, code=code, limit=10)
    ]

    # 空态那一屏的话分三档：队列 deferred（今天试满了）、pending（刚排的）、和压根没排
    # （主档早就在、只是从没有人求过数）——最后一档说「下一拍会自动补」，不编排队状态
    no_bars = ctx.bars.count(market, code) == 0
    backfill_row = ctx.backfills_store.open_for(market, code)
    backfill_note = None
    if backfill_row is not None:
        if backfill_row.status == "deferred":
            backfill_note = (
                f"今天已试过 {backfill_row.tries} 次都没成（{backfill_row.last_error}），"
                "今晚随盘后任务再试。"
            )
        else:
            backfill_note = "已排队，下一拍自动开始（一拍至多三只，前面的队首先走）。"

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
        quote_at=hms(quote.ts if quote else None, tz),
        rules=rules,
        theses=theses,
        alerts=alerts,
        no_bars=no_bars,
        backfill_note=backfill_note,
    )


@router.get("/api/symbol/{market}/{code}/bars")
def bars_api(request: Request, market: str, code: str, days: int = 120):
    user = require_user(request)
    web = request.app.state.web
    ctx, market, code, _symbol = _require_symbol(request, user, market, code)
    end = web.core.clock.now_utc().date()
    start = end - timedelta(days=max(1, min(days, 2000)))
    try:
        read = ctx.analysis.series(market, code, start=start, end=end, indicators=("ma",))
    except NotAdjustable as exc:
        # 序列读不出来（没日线、覆盖没确认过）不是接口故障：图空着，把那句话说给前端。
        # 只认这一种：`DatabaseLocked` 是库被调度线程握着，那句话要留给 503 去说——
        # 把它写成「没采集，去跑 sams sync daily」会让人反复重跑一条本来好好的命令
        return JSONResponse(
            {"dates": [], "close_hfq": [], "ma20": [], "ma60": [], "note": str(exc)}
        )
    table = read.table
    return JSONResponse(
        {
            "dates": [str(value) for value in table["date"].to_list()],
            # 图上那根收盘价必须与均线同基：`metrics()` 里 MA 全部算在 `close_hfq` 上
            # （indicators.ma → _prices(series, "close_hfq")），给裸 close 就等于把
            # 均线画到价格线上方，那一段里每一次分红都算成「还没收复」
            "close_hfq": table["close_hfq"].to_list(),
            "ma20": table["ma20"].to_list(),
            "ma60": table["ma60"].to_list(),
            "note": None,
        }
    )


@router.post("/symbol/{market}/{code}/sync")
def symbol_sync(request: Request, market: str, code: str, _csrf: None = Depends(csrf_protect)):
    """「立即排一拍」：入队下一拍消费，不承诺同步已经发生（D146 空态按钮）。

    走 `_require_symbol` 是因为按钮本身也该能兜底建行：代码表里的票从空态点一下，
    主档与队列一起补上，而不是先 404 再让人摸不着头脑。
    """
    user = require_user(request)
    ctx, market, code, _row = _require_symbol(request, user, market, code)
    ctx.symbol_interest.ensure(market, code, actor=user.username)
    return RedirectResponse(f"/symbol/{market}/{code}", status_code=303)
