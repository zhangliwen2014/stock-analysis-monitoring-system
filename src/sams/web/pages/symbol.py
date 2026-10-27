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

import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from sams.core.codes import canonical_code, canonical_market, looks_like_index_code
from sams.core.errors import NotAdjustable, ValidationError
from sams.core.quotes import NO_DATA, quote_state
from sams.services.analysis import parse_period
from sams.storage.repos.symbol_backfills import DEFERRED
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

    主档没有但代码表在册（is_active）= 「全市场认识这只票、本地档案还没追上」：兜底建行走
    `SymbolInterest.ensure`（顺带排补数），不再 404（D146）。退市码不兜底：给它建行排队
    是每一拍白试一次的死占用。兜不住才 404，措辞不变——不给探测者区分「没有」与「不是你的」。
    """
    web = request.app.state.web
    ctx = web.contexts.current()
    market = canonical_market(market)
    # 指数码在归一之前按**原始**那一段拦下：sh000001 归一后就是 000001=平安银行，
    # 兜底还会为指数建一行个股档案——指数不是个股页，它的地方是大盘条（D145）
    if looks_like_index_code(market, code):
        raise HTTPException(status_code=404)
    code = canonical_code(market, code)
    if market not in ctx.settings.markets:
        # 这条 404 与兜底无关：市场不在本机配置清单里本就不可达，兜底只是会晚一步崩 KeyError
        raise HTTPException(status_code=404)
    row = ctx.symbols.get(market, code)
    if row is None:
        listed = ctx.symbol_list_store.get(market, code)
        if listed is not None and listed["is_active"]:
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

    # 空态那一屏按队列的三档各说各的：deferred（今天试满）、pending（已排队）、没排过队
    no_bars = ctx.bars.count(market, code) == 0
    backfill_note = None
    backfill_deferred = False
    if no_bars:
        backfill_row = ctx.backfills_store.open_for(market, code)
        if backfill_row is None:
            backfill_note = "没有排队：点下面「立即排一拍」，否则等今晚盘后任务。"
        elif backfill_row.status == DEFERRED:
            backfill_deferred = True
            # 括注只在该行真记了错误时才有：空 last_error 印出来是「都没成（）」
            error = f"（{backfill_row.last_error}）" if backfill_row.last_error else ""
            backfill_note = (
                f"今天已试过 {backfill_row.tries} 次都没成{error}，今晚随盘后任务再试。"
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
        backfill_deferred=backfill_deferred,
    )


#: 主图与副图各自能挂哪些指标。这份名单同时是屏幕上那排开关的来源，
#: 加一种指标只改这里（前端读局部给出的名字，不自己记一份）
MAIN_INDICATORS: tuple[str, ...] = ("ma", "boll", "ema")
SUB_INDICATORS: tuple[str, ...] = (
    "vol", "macd", "kdj", "rsi", "atr", "wr", "dmi", "cci", "obv",
)
#: 每档周期的默认窗口（日历日）：周线月线要看的是「这几年」，日线看的是「这半年」（D160）
DEFAULT_WINDOW: dict[str, int] = {
    "day": 180,
    "week": 730,
    "month": 1825,
    "quarter": 2000,
    "year": 2000,
    # N 日线要看的是「这些年」：一根 5 日 K 只盖五个工作日，180 天窗口只剩三十来根，
    # 形态读不出来（同一档窗口在季/年那一行也是为这个才给到上限的）
    "5d": 2000,
    "10d": 2000,
}
#: 副图最多三格：再多就不是看图而是刷屏（D162）
MAX_SUB_PANES = 3
#: 周期条：(参数值, 屏幕上那几个字)。分钟档在计划 B，那时再来这张表里加行
CHART_PERIODS: tuple[tuple[str, str], ...] = (
    ("day", "日"), ("week", "周"), ("month", "月"), ("quarter", "季"),
    ("year", "年"), ("5d", "5日"), ("10d", "10日"),
)
#: 开关上那几个字。白名单加了名字这里就必须加一行，缺行会在模板上留一个空按钮
CHART_INDICATOR_LABELS: dict[str, str] = {
    "ma": "均线", "boll": "布林", "ema": "指数均线",
    "vol": "成交量", "macd": "MACD", "kdj": "KDJ", "rsi": "RSI", "atr": "ATR",
    "wr": "威廉", "dmi": "动向", "cci": "CCI", "obv": "OBV",
}
#: `vol` 不是 `INDICATORS` 里的名字：那一格画的是裸量柱（量不复权，`indicators` 模块第 3 条）
_VOLUME_COLUMN = "volume"
#: 族名 → 列名前缀。`dmi` 的三列是 `pdi/mdi/adx`，前缀不等于族名，所以只能列全名字
_INDICATOR_PREFIX = {
    "ma": "ma", "boll": "boll_", "ema": "ema", "macd": "macd", "kdj": "kdj_",
    "rsi": "rsi", "atr": "atr", "wr": "wr", "dmi": "", "cci": "cci", "obv": "obv",
}
#: `dmi` 那一族的前缀是空的，只能把三列名字列全（见上）
_EXPLICIT_COLUMNS = {"dmi": ("pdi", "mdi", "adx")}


def _split_names(
    raw: str, allowed: tuple[str, ...], *, limit: int | None = None
) -> tuple[str, ...]:
    """把 `ma,boll` 这样的串切成白名单里的名字：重复的合并，不在名单里的当场拒。

    拒的那句要报出可选的是哪些：屏幕上打错一个字母的人不该只看到「400」。
    """
    given = [item.strip().lower() for item in (raw or "").split(",")]
    picked = tuple(dict.fromkeys(item for item in given if item))
    unknown = [item for item in picked if item not in allowed]
    if unknown:
        raise ValidationError(f"不认识的指标 {', '.join(unknown)}：可选的是 {', '.join(allowed)}")
    if limit is not None and len(picked) > limit:
        raise ValidationError(f"副图最多同时 {limit} 个，收到的是 {len(picked)} 个")
    return picked


def _windows(raw: str) -> tuple[int, ...]:
    """`5,10,20,60` → `(5, 10, 20, 60)`；留空 = 用指标自己的那一组。

    句子里带「最多六条」与「1..500」是因为这三类填错（非数字、0 或负数、堆十几条）
    在同一个人手里会反复出现：`int()` 抛的 `ValueError` 不折成 `ValidationError`
    就越过全局处理器，换回一屏 500。
    """
    message = "均线窗口要 1..500 之间的正整数，最多六条"
    picked: list[int] = []
    for item in (raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError as exc:
            raise ValidationError(message) from exc
        if not 1 <= value <= 500:
            raise ValidationError(message)
        picked.append(value)
    deduped = tuple(dict.fromkeys(picked))
    if len(deduped) > 6:
        raise ValidationError(message)
    return deduped


def _columns_of(table: pl.DataFrame, name: str) -> list[str]:
    if name in _EXPLICIT_COLUMNS:
        return [column for column in _EXPLICIT_COLUMNS[name] if column in table.columns]
    prefix = _INDICATOR_PREFIX[name]
    # 前缀短的族会吞掉前缀长的邻居（`ma` 把 `macd*` 也认下来，而它俩不在同一格）：
    # 长的先排除，一族内的列才只属于这一族
    longer = [other for other in _INDICATOR_PREFIX.values() if len(other) > len(prefix)]
    return [
        column
        for column in table.columns
        if column.startswith(prefix) and not any(column.startswith(other) for other in longer)
    ]


def _group(table: pl.DataFrame, name: str) -> dict[str, list]:
    """某一族在表上的那几列：`{列名: 值列表}`，没有对应列就给空字典。

    空字典而不是 KeyError，也不是 `None` 数组：那一格没数与「有数但都是空」在屏幕上
    是两件事，前者该让前端把格子收起来。
    """
    if name == "vol":
        if _VOLUME_COLUMN not in table.columns:
            return {}
        return {_VOLUME_COLUMN: table[_VOLUME_COLUMN].to_list()}
    return {column: table[column].to_list() for column in _columns_of(table, name)}


def _column(frame: pl.DataFrame, name: str) -> list:
    """序列上的一档：源没给那一列（比如某档日线没有 high）就给空数组，而不是 500。"""
    return frame[name].to_list() if name in frame.columns else []


@router.get("/api/symbol/{market}/{code}/bars")
def bars_api(
    request: Request,
    market: str,
    code: str,
    days: int = 0,
    period: str = "day",
    main: str = "ma",
    sub: str = "vol",
    ma: str = "",
):
    user = require_user(request)
    web = request.app.state.web
    ctx, market, code, _symbol = _require_symbol(request, user, market, code)
    # 三道白名单检查在读库之前：拼错的指标名与乱填的窗口不该让三道闸全跑完才出声。
    # 抛 `ValidationError` 就够——全局处理器把它渲染成 400，端点自己不再拼状态码
    mains = _split_names(main, MAIN_INDICATORS)
    subs = _split_names(sub, SUB_INDICATORS, limit=MAX_SUB_PANES)
    ma_windows = _windows(ma)
    end = web.core.clock.now_utc().date()
    window = days or DEFAULT_WINDOW.get(period, DEFAULT_WINDOW["day"])
    start = end - timedelta(days=max(1, min(window, 2000)))
    # `vol` 不在 `INDICATORS` 里（那一格画的是裸量柱），而 `asked` 为空时仍给 ("ma",)：
    # `metrics(series, ())` 会产出一张只有 `date` 的表，那条空 hstack 路径不该走进来
    asked = tuple(name for name in (*mains, *(name for name in subs if name != "vol")))
    try:
        read = ctx.analysis.series(
            market,
            code,
            start=start,
            end=end,
            period=period,
            indicators=asked or ("ma",),
            # `ma=` 只改主图那一族：其余族走函数自己的默认窗口
            windows={"ma": ma_windows} if "ma" in mains and ma_windows else None,
        )
    except NotAdjustable as exc:
        # 序列读不出来（没日线、覆盖没确认过）不是接口故障：图空着，把那句话说给前端。
        # 只认这一种：`DatabaseLocked` 是库被调度线程握着，那句话要留给 503 去说——
        # 把它写成「没采集，去跑 sams sync daily」会让人反复重跑一条本来好好的命令
        # 空态也给**与新形状同形**的键：前端拿到缺键的 JSON 会在画图之前先崩，
        # 而那正是这一条用例要防的 500
        return JSONResponse(
            {
                "dates": [],
                "period": period,
                "open": [],
                "high": [],
                "low": [],
                "close": [],
                "close_hfq": [],
                "volume": [],
                "main": {},
                "sub": {},
                "bars": [],
                "pending": [],
                "anchored_on": "",
                "warmup_floor": None,
                "note": str(exc),
            }
        )
    table = read.table
    frame = read.series.frame.filter(pl.col("date") >= start)
    return JSONResponse(
        {
            "dates": [str(value) for value in table["date"].to_list()],
            "period": read.period,
            # 蜡烛四档取 `frame` 的 `*_hfq`：`read.table` 那张表只给终端打印（`keep` 白名单），
            # 没有开/高/低三档。图上那一档与均线同基（`metrics()` 里 MA 全算在 `close_hfq` 上），
            # 给裸价就等于把均线画到价格线上方——那一段里每一次分红都算成「还没收复」
            "open": _column(frame, "open_hfq"),
            "high": _column(frame, "high_hfq"),
            "low": _column(frame, "low_hfq"),
            "close": _column(frame, "close_hfq"),
            "close_hfq": _column(frame, "close_hfq"),
            "volume": _column(frame, _VOLUME_COLUMN),
            "main": {name: _group(table, name) for name in mains},
            "sub": {name: _group(table, name) for name in subs},
            # `bars`/`pending` 只有重采样出来的周期才有：一根周 K 只盖了一天还是没走完，
            # 是它在屏幕上唯一能自证的地方
            "bars": table["bars"].to_list() if "bars" in table.columns else [],
            "pending": [str(day) for day in read.pending_periods],
            "anchored_on": str(read.anchored_on),
            "warmup_floor": str(read.warmup_floor) if read.warmup_floor else None,
            "note": None,
        }
    )


@router.get("/api/partials/chart")
def chart_partial(
    request: Request, market: str, code: str, period: str = "day",
    main: str = "ma", sub: str = "vol", ma: str = "",
):
    """个股页那一块图：周期条 + 指标开关 + 挂着 `data-chart-url` 的那个 div。

    为什么在服务端渲染而不是前端自己切：CSP 是 `'self'`（不许内联脚本），
    而「现在选中的是哪一档」这份状态一旦落在前端，换档后屏上说的就可能是上一档的话（D158）。

    周期名要过 `parse_period`：不认识的名字在这里就说 400，而不是悄悄换成日线——
    那一屏会举着「日」的高亮画一段没人请求过的东西，而 `bars` 接口对同一个写法已经是 400。
    """
    require_user(request)
    web = request.app.state.web
    parse_period(period)  # 只为止声，选中态仍存调用方给的那一串
    ctx = web.contexts.current()
    market = canonical_market(market)
    code = canonical_code(market, code)
    if market not in ctx.settings.markets:
        raise HTTPException(status_code=404)
    return web.render(
        request,
        "partials/chart.html",
        market=market,
        code=code,
        periods=CHART_PERIODS,
        period=period,
        main=main,
        sub=sub,
        ma=ma,
        mains=MAIN_INDICATORS,
        subs=SUB_INDICATORS,
        labels=CHART_INDICATOR_LABELS,
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
