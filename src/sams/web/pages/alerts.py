"""/alerts 收件箱：筛选、详情、处理与重发（spec §6.3）。

状态机住在 `AlertRepo.set_status`（它自己挡归属与驳回空原因），这一层只加两样它没有的东西：
「不存在」与「不是你的」答同一个 404，以及把人敲得出来的筛选值折成库里那个键——
`list_for` 认的 `market` 是 `canonical_market` 的产物，而下拉之外还有人直接改 URL，
不折一次 `?market=a` 会对着一堆确实存在的告警说「没有命中的告警」；折完了还要挡一句，
因为筛出一个空列表同样是那句假话（`SYS` 那一档见 `_market_choices`：系统告警与真告警同表）。
2026-10-25 起本页只走 `ReviewService` 与 `acknowledge/defer/undo`：`set_status` 的
最后两个调用点（旧 acted/dismiss）已在闭环改造里迁走。

重发走 `NotifyService.deliver_existing`：正文从库里重建、按**此刻**的通道与免打扰重新选路，
所以「当时没发出去」与「现在也发不出去」是两句不同的话——`monitor/queue.py` 里那句
「可在收件箱重发」说的就是这一页。

时刻全部在 Python 里折到 `display_tz` 才上屏（`display.moment_label`）：模板不碰 datetime。
仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from sams.core.codes import canonical_market
from sams.core.errors import Conflict, DatabaseLocked, SamsError, ValidationError
from sams.core.notify import SEVERITIES, SYSTEM_MARKET
from sams.storage.repos.alerts import (
    ACKNOWLEDGED,
    ACTION_KINDS,
    DISMISS_CODES,
    DISMISSED,
    HANDLED,
    NEW,
    STATUS,
    UNDO_WINDOW,
    Alert,
)
from sams.web.auth import audit, csrf_protect, require_user
from sams.web.display import (
    EVENT_ACTION_LABELS,
    action_label,
    alert_status,
    dismiss_label,
    moment_label,
)
from sams.web.forms import checked, form_data, one

logger = logging.getLogger("sams.web.alerts")

router = APIRouter()

#: 表单原文的依赖，提到模块级一份（lint 不许在参数默认值里调用 `Depends`，见 `pages/rules.py`）
_form_body = Depends(form_data)

#: 一页五十条；多读那一条只为知道「还有下一页吗」，不为此发第二趟查询
PAGE_SIZE = 50

#: 状态下拉与那道检查读的是同一份：仓库那三个状态加一个「全部」哨兵。
#: 默认只看未处理是这张页存在的理由（收件箱，不是归档柜）。
STATUS_CHOICES: tuple[tuple[str, str], ...] = (
    *((value, alert_status(value)) for value in STATUS),
    ("all", "全部"),
)
_STATUS_VALUES = tuple(value for value, _ in STATUS_CHOICES)

#: 系统那一档在下拉里的名字。它不是一个市场：`market = SYSTEM_MARKET` 的那些行是组件
#: 自己响的那一句（`services/scheduler/alarms.py`，以及 /settings/test-alert 那条 `code='test'`）
MARKET_SYSTEM_LABEL = "系统"

#: 分组是按页各算一次的（一次查询、页内归组），所以同一标的可能在两页里各出一个标题；
#: 不重排分页，只把这件事说一句。措辞避开「上一页/下一页」：那两个词在测试里指的是那两条链接
GROUP_SPLIT_NOTE = "分组按页各算一次：同一标的的告警可能被拆在相邻的两页里，各带一次标题。"
#: 空列表那两句话问的是两件事：筛选没命中 vs 命中了但这一页码上没有东西
INBOX_EMPTY_NOTE = "没有命中的告警。"
PAGE_EMPTY_NOTE = "这一页没有内容：往前翻一页看看。"

#: 稍后的三个快捷档（§6.3）。`open` 不是时长是时刻：走日历算下一段开盘，
#: 且只认「明天开盘」这四个字——「几天后」不在 spec 的名单里，别自创
DEFER_HOURS = {"1h": 1, "3h": 3}
DEFER_OPEN = "open"
DEFER_CHOICES = (("1h", "1 小时"), ("3h", "3 小时"), (DEFER_OPEN, "明天开盘"))


def _day_bound(value: str, tz_name: str, *, end: bool = False) -> datetime | None:
    """「某一天」按 display_tz 切：since 取当天零点；until 是排他上界，取**次日**零点。

    仓库那一边 `until` 用的是 `<`（`list_for` 的文档），所以这一头的次日零点才是
    「含那一天整天」；取当天零点的话「到今天」会把今天整天漏掉，而屏幕上那个日期框
    说的是包含。
    """
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"日期格式应为 YYYY-MM-DD（收到 {value!r}）"
        ) from exc
    if end:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz_name))


def _clean_choice(
    raw: str, allowed: Sequence[str], *, label: str, default: str = ""
) -> str:
    """筛选值的一个形状：空 = 不筛（或取 `default`），填了就必须是 `allowed` 里那一个。

    放行一个错值等于让它筛出空列表：那一屏会说「没有命中的告警」而告警明明在，
    而这句话是假的——比一个 400 难查得多。名单、措辞、`strip` 的口径都在这里，
    三个输入轴不再各写一套（同一句话由哪一层先说出口，取决于哪一份名单先漂走）。

    仓库自己也查状态（`AlertRepo.list_for`），但**这一头先答**：页面的职责是在一趟
    查询跑起来之前就把人敲错的那一格说清楚，而不是把 SQL 的拒绝转述一遍。
    """
    value = raw.strip() or default
    if value and value not in allowed:
        raise HTTPException(
            status_code=400, detail=f"未知{label} {value!r}：只有 {'、'.join(allowed)}"
        )
    return value


def _market_choices(ctx) -> tuple[tuple[str, str], ...]:
    """市场下拉与那道检查读的同一份：配置里那几个，末尾加系统那一档。

    系统告警与真告警同表，只靠 `market` 那一列分开（`AlertRepo.has_system_alert` 的注释）：
    下拉里没有它，这一类告警在收件箱里就没有入口；而手改 `?market=SYS` 筛出来的那一屏
    连一个选中的 option 都渲染不出来，页面会一边写着「全部市场」一边只列 SYS。
    """
    pairs = [(market, market) for market in sorted(ctx.settings.markets)]
    if SYSTEM_MARKET not in ctx.settings.markets:
        pairs.append((SYSTEM_MARKET, MARKET_SYSTEM_LABEL))
    return tuple(pairs)


def _clean_market(raw: str, allowed: Sequence[str]) -> str:
    # 空串不能进 `canonical_market`（它拒空），而「不筛市场」在这张页上就是一个空值
    value = raw.strip()
    return _clean_choice(
        canonical_market(value) if value else "", allowed, label="市场"
    )


def _clean_page(raw: str) -> int:
    """页码不是数字就是敲错了，不是偏好：静默折回第 1 页会把「筛选变了」伪装成
    「结果变了」，而这一页的其它输入答的都是 400（见 `_clean_choice`）。"""
    text = raw.strip()
    if not text:
        return 1
    try:
        page = int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"页码要的是整数（收到 {text!r}）"
        ) from exc
    if page < 1:
        raise HTTPException(status_code=400, detail=f"页码从 1 开始（收到 {page}）")
    return page


def _row(alert: Alert, tz: str) -> dict:
    """列表里那一行。现值/阈值/建议动作不是 `Alert` 的字段，它们在 `payload` 里
    （措辞改版不动表，见 `repos/alerts.py`），所以在这里取，模板只管印。"""
    payload = alert.payload
    return {
        "id": alert.id,
        "symbol_name": alert.symbol_name,
        "at": moment_label(alert.triggered_at, tz),
        "severity": alert.severity,
        "rule_name": alert.rule_name,
        "value_text": str(payload.get("value_text") or ""),
        "threshold_text": str(payload.get("threshold_text") or ""),
        "status": alert.status,
        # 测试告警（D131）在列表里也得认得出来：`monitor/router.py` 那句
        # 「收件箱里带『测试』徽标」说的就是这一格，不是详情页
        "is_test": alert.is_test,
        # 「你让它稍后提醒」在列表里也要认得出来：稍后不改状态，这一格是它唯一的痕迹
        "deferred": moment_label(alert.deferred_until, tz) if alert.deferred_until else "",
    }


def _page_url(target: int, chosen: dict[str, str]) -> str:
    """翻页链接：值都是折过的（市场、档位、日期），并且一律走 `urlencode`——
    手拼 `key=value` 的链接在值里出现 `&`、`=` 或空格时就断了。"""
    pairs = {"page": str(target)}
    pairs.update({key: value for key, value in chosen.items() if value})
    return f"/alerts?{urlencode(pairs)}"


def _list_page(
    request: Request,
    *,
    groups: dict,
    chosen: dict,
    page: int,
    has_more: bool,
    markets: tuple[tuple[str, str], ...],
):
    web = request.app.state.web
    return web.render(
        request,
        "alerts.html",
        title="告警",
        groups=groups,
        chosen=chosen,
        page=page,
        prev_url=_page_url(page - 1, chosen) if page > 1 else "",
        next_url=_page_url(page + 1, chosen) if has_more else "",
        # 空列表那句话由页码决定：越界的那一页说「没有命中的告警」是假话
        empty_note=PAGE_EMPTY_NOTE if page > 1 else INBOX_EMPTY_NOTE,
        # 分组只在真的分页时才可能被拆开，不分页的那一屏不该多这一句
        group_note=GROUP_SPLIT_NOTE if (has_more or page > 1) else "",
        # 三张名单全部来自契约：状态见 STATUS_CHOICES、档位见 SEVERITIES、市场见配置加系统
        status_choices=STATUS_CHOICES,
        severities=SEVERITIES,
        markets=markets,
    )


def _detail_page(
    request: Request,
    *,
    status_code: int = 200,
    error: str = "",
    dispatch=None,
    reason: str = "",
    draft: dict | None = None,
):
    """详情这一屏。`dispatch` 不带注解：它的类（`DispatchResult`）住在 `sams.monitor`，
    而 web 不许 import 那一层（`tests/test_layering.py`）——同一折法见 `bootstrap.py`。

    表单要的数据在这里备齐（本人的同标的观点、本人的规则、本人的账户、两张码表），
    模板只管印——模板里循环仓库是第二份取数逻辑，分页/过滤会漂出第一份的视线。
    """
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    alert = ctx.alerts_store.get(request.path_params["alert_id"], user_id=user.id)
    if alert is None:
        raise HTTPException(status_code=404, detail="没有这条告警")
    tz = ctx.settings.server.display_tz
    payload = alert.payload
    extra = payload.get("extra") or {}
    deliveries = [
        {
            # 那一列到秒：同一条告警的三次尝试只差几秒，只到分钟会印成一模一样的一列
            "at": moment_label(row.ts, tz, seconds=True),
            "channel_id": row.channel_id or "",
            "outcome": row.outcome,
            "attempts": row.attempts,
            "error": row.error,
        }
        for row in ctx.alerts_store.deliveries(alert.id)
    ]
    events = [
        {
            "at": moment_label(row.ts, tz, seconds=True),
            "action": EVENT_ACTION_LABELS.get(row.action, row.action),
            "from": alert_status(row.from_status),
            "to": alert_status(row.to_status),
            "note": row.note,
        }
        for row in ctx.alerts_store.events.list_for(alert.id)
    ]
    terminal = alert.status in (HANDLED, DISMISSED)
    can_undo = bool(
        terminal
        and alert.handled_at is not None
        and ctx.clock.now_utc() - alert.handled_at < UNDO_WINDOW
    )
    # 引用跳转：trade 没有 order 详情页（计划头「明确不做」），终态区留明文单号
    ref_url = ""
    if alert.action_kind == "thesis" and alert.action_ref:
        ref_url = f"/theses/{alert.action_ref}"
    elif alert.action_kind == "rule" and alert.action_ref:
        ref_url = f"/rules/{alert.action_ref}/edit"
    related_theses = [
        # `Thesis` 没有 `note` 这一列（计划代码块里那句 `t.note or t.id` 落不了地）：
        # 直接取 id 前段，同一格式的截断语义不变
        {"id": t.id, "label": f"{t.style} · {t.id[:24]}"}
        for t in ctx.theses.list(user.username)
        if (t.market, t.code) == (alert.market, alert.code)
    ]
    return web.render(
        request,
        "alert.html",
        status_code=status_code,
        title=f"告警 {alert.market}/{alert.code}",
        alert=alert,
        deliveries=deliveries,
        dispatch=dispatch,
        value_text=str(payload.get("value_text") or ""),
        threshold_text=str(payload.get("threshold_text") or ""),
        action_note=str(payload.get("action_note") or ""),
        body=str(extra.get("body") or ""),
        triggered_at=moment_label(alert.triggered_at, tz, seconds=True),
        handled_at=moment_label(alert.handled_at, tz, seconds=True),
        deferred=moment_label(alert.deferred_until, tz) if alert.deferred_until else "",
        intraday=bool(payload.get("intraday")),
        quote_source=str(payload.get("quote_source") or ""),
        # 闭环动作区：已读只在 new 出现；处置/忽略/稍后在 new 与 acknowledged 都可按（§6.3）
        can_act=alert.status in (NEW, ACKNOWLEDGED),
        can_ack=alert.status == NEW,
        terminal=terminal,
        can_undo=can_undo,
        events=events,
        action_kind_text=action_label(alert.action_kind),
        action_ref_url=ref_url,
        dismiss_code_text=dismiss_label(alert.dismiss_code),
        # 表单的两张码表与三个下拉（全部取自服务层，模板零查询）
        action_kinds=[(k, action_label(k)) for k in ACTION_KINDS],
        dismiss_codes=[(c, dismiss_label(c)) for c in DISMISS_CODES],
        defer_choices=DEFER_CHOICES,
        related_theses=related_theses,
        rules=[
            {"id": r.id, "label": f"{r.name}（{r.kind}）"}
            for r in ctx.rules_store.list_for(user.id)
        ],
        accounts=[
            {"id": a.id, "label": a.name} for a in ctx.sim_accounts.list_for(user.id)
        ],
        error=error,
        # 被拒的重渲染交回人刚敲的那份原文（同 `pages/rules.py` 的 `_values`）
        reason=reason,
        draft=draft or {},
    )


def _require_own(ctx, alert_id: str, user_id: str) -> Alert:
    """归属闸：先按 `user_id` 读，读不到就是 404——「不存在」与「不是你的」答同一句，
    而仓库那一行一次都没被碰。

    这里不兜 `StorageError`：`get` 拿不到时给的是 `None` 而不是抛，所以 `DatabaseLocked`
    原样冒到全局处理器答 503（那是「过一会儿再来」，不是「没有这条告警」）。
    """
    alert = ctx.alerts_store.get(alert_id, user_id=user_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="没有这条告警")
    return alert


@router.get("/alerts")
def inbox(request: Request):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    query = request.query_params
    tz = ctx.settings.server.display_tz
    markets = _market_choices(ctx)
    allowed_markets = tuple(value for value, _ in markets)
    status = _clean_choice(
        query.get("status") or "", _STATUS_VALUES, label="状态", default=NEW
    )
    severity = _clean_choice(query.get("severity") or "", SEVERITIES, label="档位")
    market = _clean_market(query.get("market") or "", allowed_markets)
    since = (query.get("since") or "").strip()
    until = (query.get("until") or "").strip()
    page = _clean_page(query.get("page") or "")

    rows = ctx.alerts_store.list_for(
        user.id,
        status=None if status == "all" else status,
        severity=severity or None,
        market=market or None,
        since=_day_bound(since, tz),
        until=_day_bound(until, tz, end=True),
        limit=PAGE_SIZE + 1,
        offset=(page - 1) * PAGE_SIZE,
    )
    has_more = len(rows) > PAGE_SIZE
    groups: dict[tuple[str, str], list[dict]] = {}
    for alert in rows[:PAGE_SIZE]:
        groups.setdefault((alert.market, alert.code), []).append(_row(alert, tz))
    chosen = {
        "status": status,
        "severity": severity,
        # 下拉里选中的是折过的那一个（`a` 交回来的是 `A`），链接与框都跟着它
        "market": market,
        "since": since,
        "until": until,
    }
    return _list_page(
        request,
        groups=groups,
        chosen=chosen,
        page=page,
        has_more=has_more,
        markets=markets,
    )


@router.get("/alerts/{alert_id}")
def detail(request: Request):
    require_user(request)
    return _detail_page(request)


def _draft(data: dict[str, list[str]]) -> dict[str, str]:
    """被拒重渲染交回的草稿：闭环表单会提交的键全量回，模板按名字取。

    只回半份的话，刚选的账户与方向被洗回默认第一项——照着默认值再按一次，
    成交就记到错账户上（§6.3「交回人刚敲的那份」）。
    """
    return {
        key: one(data, key)
        for key in (
            "action_kind", "note", "qty", "price", "fee", "code",
            "account_id", "side", "ts", "thesis_ref", "rule_ref",
            "sync_annotation",
        )
    }


def _number(raw: str, label: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValidationError(f"{label} 要是数字（收到 {raw!r}）") from exc


def _trade_ts(raw: str, tz_name: str) -> datetime | None:
    """`datetime-local` 控件交来的裸时刻落在 display_tz；空 = 让账本按此刻记。
    补记真实成交的人，留空比替他猜一个时刻诚实。"""
    if not raw:
        return None
    try:
        local = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"成交时间读不出来：{raw!r}") from exc
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(tz_name))
    return local


def _sync_text(ctx, alert: Alert, outcome: str) -> str:
    """「同时写一条标的批注」的预填正文（D137）：告警摘要 + 这次的结论 + 回链。
    成品在这里组装，`ReviewService._sync_annotation` 与下面的 best-effort 只收文本。"""
    base = ctx.settings.server.public_base_url
    link = f"{base}/alerts/{alert.id}" if base else f"/alerts/{alert.id}"
    return "\n".join(
        [
            f"[闭环] {alert.market}:{alert.code} {alert.symbol_name or ''}".strip(),
            f"规则：{alert.rule_name or '—'}；现值 {str(alert.payload.get('value_text') or '—')}"
            f" / 阈值 {str(alert.payload.get('threshold_text') or '—')}",
            outcome,
            f"告警详情：{link}",
        ]
    )


def _act_trade(
    ctx, user, alert: Alert, data: dict[str, list[str]], note: str, sync: str | None
):
    """trade 的引用是这一次**新造的**成交 id，只有 `act_with_fill` 给得出（D125）。"""
    account_id = one(data, "account_id").strip()
    if not account_id:
        raise ValidationError("trade 要选一个账户：成交得落在某个人的持仓上")
    fee_text = one(data, "fee").strip()
    return ctx.review.act_with_fill(
        user.id,
        alert.id,
        account_id=account_id,
        side=one(data, "side").strip(),
        qty=_number(one(data, "qty"), "数量"),
        price=_number(one(data, "price"), "价格"),
        fee=_number(fee_text, "费用") if fee_text else 0.0,
        ts=_trade_ts(one(data, "ts").strip(), ctx.settings.server.display_tz),
        note=note,
        sync_annotation=sync,
    )


@router.post("/alerts/{alert_id}/acted")
def acted(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    """处置：五类动作（D125）。全部走 `ReviewService`——`set_status` 这条路从此无人走。

    引用选择器按动作分开命名（`thesis_ref`/`rule_ref` 而不是共用 `action_ref` 输入框）：
    没有 JS 的浏览器也要能把三种引用的来源各交各的，而 trade 的引用压根不该从表单来。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    kind = one(data, "action_kind").strip()
    note = one(data, "note").strip()
    if kind not in ACTION_KINDS:
        return _detail_page(
            request,
            status_code=400,
            error="未知动作类型：只有 " + "、".join(action_label(k) for k in ACTION_KINDS),
            draft=_draft(data),
        )
    sync = (
        _sync_text(
            ctx, alert, f"处置：{action_label(kind)}" + (f"——{note}" if note else "")
        )
        if checked(data, "sync_annotation")
        else None
    )
    try:
        if kind == "trade":
            moved = _act_trade(ctx, user, alert, data, note, sync)
        else:
            ref = one(data, "thesis_ref" if kind == "thesis" else "rule_ref").strip()
            moved = ctx.review.act(
                user.id,
                alert.id,
                action_kind=kind,
                action_ref=ref,
                note=note,
                sync_annotation=sync,
            )
    except DatabaseLocked:
        raise
    except Conflict as exc:
        # 「态不对」是 409：与批注的乐观并发同一口径
        return _detail_page(request, status_code=409, error=str(exc), draft=_draft(data))
    except SamsError as exc:
        return _detail_page(request, status_code=400, error=str(exc), draft=_draft(data))
    audit(
        request,
        action="alert.acted",
        target=alert.id,
        ok=True,
        detail=f"{kind}:{moved.action_ref or ''}",
    )
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


@router.post("/alerts/{alert_id}/dismiss")
def dismiss(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    """忽略：原因码必选（D124），`misreport`/`other` 的说明由仓库挡（≥2 字）。"""
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    code = one(data, "code").strip()
    typed = one(data, "reason")
    reason = typed.strip()
    if code not in DISMISS_CODES:
        return _detail_page(
            request,
            status_code=400,
            error="忽略必须先选原因码：" + "、".join(dismiss_label(c) for c in DISMISS_CODES),
            reason=typed,
            draft=_draft(data),
        )
    sync = (
        _sync_text(
            ctx,
            alert,
            f"忽略（{dismiss_label(code)}）" + (f"——{reason}" if reason else ""),
        )
        if checked(data, "sync_annotation")
        else None
    )
    try:
        moved = ctx.alerts_store.dismiss(alert.id, user_id=user.id, code=code, reason=reason)
    except DatabaseLocked:
        raise
    except Conflict as exc:
        return _detail_page(
            request, status_code=409, error=str(exc), reason=typed, draft=_draft(data)
        )
    except SamsError as exc:
        return _detail_page(
            request, status_code=400, error=str(exc), reason=typed, draft=_draft(data)
        )
    if sync:
        # 与 `ReviewService._sync_annotation` 同方向：批注写崩不把已成立的忽略拖回去（§12.1）
        try:
            ctx.annotations.create(
                user.id,
                target_type="symbol",
                target_ref=f"{alert.market}:{alert.code}",
                body_md=sync,
            )
        except SamsError as exc:
            logger.warning(
                "告警 %s 忽略已成立，但同步批注失败（不影响忽略）：%s", alert.id, exc
            )
    # 驳回是「忽略率」唯一的输入，而那一列库里就有：日志只记「谁按的、按了哪一条」
    audit(request, action="alert.dismiss", target=alert.id, ok=True, detail=code)
    return RedirectResponse(f"/alerts/{moved.id}", status_code=303)


@router.post("/alerts/{alert_id}/ack")
def ack(request: Request, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    try:
        ctx.alerts_store.acknowledge(alert.id, user_id=user.id)
    except DatabaseLocked:
        raise
    except Conflict as exc:
        return _detail_page(request, status_code=409, error=str(exc))
    audit(request, action="alert.ack", target=alert.id, ok=True)
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


def _defer_until(ctx, alert: Alert, choice: str) -> datetime:
    """`open` = 下一个开盘时刻：今天还没开盘就用今天，否则顺着日历往后找（§6.3）。"""
    if choice in DEFER_HOURS:
        return ctx.clock.now_utc() + timedelta(hours=DEFER_HOURS[choice])
    probe = ctx.calendar.day(alert.market, ctx.clock.now_utc().date())
    tz = ZoneInfo(probe.tz)
    local_now = ctx.clock.now_utc().astimezone(tz)
    day = ctx.calendar.day(alert.market, local_now.date())
    window = next((w for w in day.windows if w.name == "day"), None)
    if day.is_trading and window is not None:
        hour, minute = (int(part) for part in window.open.split(":")[:2])
        open_at = datetime(day.day.year, day.day.month, day.day.day, hour, minute, tzinfo=tz)
        if open_at > local_now:
            return open_at.astimezone(UTC)
    nxt = ctx.calendar.next_trading_day(alert.market, local_now.date())
    nday = ctx.calendar.day(alert.market, nxt)
    nwindow = next((w for w in nday.windows if w.name == "day"), None)
    if nwindow is None:
        raise ValidationError("日历里找不到那段交易时段：改用 1 小时或 3 小时")
    hour, minute = (int(part) for part in nwindow.open.split(":")[:2])
    return datetime(
        nxt.year, nxt.month, nxt.day, hour, minute, tzinfo=ZoneInfo(nday.tz)
    ).astimezone(UTC)


@router.post("/alerts/{alert_id}/defer")
def defer(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    choice = one(data, "until").strip()
    if choice not in DEFER_HOURS and choice != DEFER_OPEN:
        return _detail_page(
            request,
            status_code=400,
            error="未知稍后档位：只有 " + "、".join(label for _, label in DEFER_CHOICES),
        )
    try:
        ctx.alerts_store.defer(
            alert.id, user_id=user.id, until=_defer_until(ctx, alert, choice)
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        status = 409 if isinstance(exc, Conflict) else 400
        return _detail_page(request, status_code=status, error=str(exc))
    audit(request, action="alert.defer", target=alert.id, ok=True, detail=choice)
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


@router.post("/alerts/{alert_id}/undo")
def undo(request: Request, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    try:
        ctx.alerts_store.undo(alert.id, user_id=user.id)
    except DatabaseLocked:
        raise
    except Conflict as exc:
        # 窗口过期/最新事件不是关闭动作：都是 409，那句话由仓库说
        return _detail_page(request, status_code=409, error=str(exc))
    audit(request, action="alert.undo", target=alert.id, ok=True)
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


@router.post("/alerts/{alert_id}/resend")
def resend(request: Request, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    try:
        # 这一趟是同步的：最多三次真尝试、带退避，就发生在这个网页线程里，
        # 所以按钮按下去可能要等一会儿——那是它唯一一个看得见的性质
        dispatch = ctx.notify.deliver_existing(alert.id)
    except DatabaseLocked:
        # 库被调度线程握着：那句话该由全局处理器答 503，不是「这次没发出去」
        raise
    except SamsError as exc:
        # 选路拒绝（比如那个用户已经不在、规则指定的通道全停用）回到详情那一屏说：
        # 按钮是人刚按的，一屏 500 或一句「已重发」都是假答案。状态机一点没动
        return _detail_page(request, status_code=400, error=str(exc))
    audit(
        request,
        action="alert.resend",
        target=alert.id,
        ok=True,
        detail=f"any_sent={dispatch.any_sent} deliveries={len(dispatch.deliveries)}",
    )
    return _detail_page(request, dispatch=dispatch)


@router.get("/api/partials/open-count")
def open_count(request: Request):
    """导航徽标的那一个数：htmx 定期取，不整页刷新。只回一个数——这就是这张卡的最小形状。

    口径与 `/review` 顶部、`latest_open` 同一条（`new + acknowledged`、不含测试，
    D124/D131）：徽标和复盘说的必须是同一件事。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    return PlainTextResponse(str(ctx.alerts_store.count_open(user.id)))
