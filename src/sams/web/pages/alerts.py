"""/alerts 收件箱：筛选、详情、处理与重发（spec §6.3）。

状态机住在 `AlertRepo.set_status`（它自己挡归属与驳回空原因），这一层只加两样它没有的东西：
「不存在」与「不是你的」答同一个 404，以及把人敲得出来的筛选值折成库里那个键——
`list_for` 认的 `market` 是 `canonical_market` 的产物，而下拉之外还有人直接改 URL，
不折一次 `?market=a` 会对着一堆确实存在的告警说「没有命中的告警」。

重发走 `NotifyService.deliver_existing`：正文从库里重建、按**此刻**的通道与免打扰重新选路，
所以「当时没发出去」与「现在也发不出去」是两句不同的话——`monitor/queue.py` 里那句
「可在收件箱重发」说的就是这一页。

时刻全部在 Python 里折到 `display_tz` 才上屏（`display.moment_label`）：模板不碰 datetime。
仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.core.codes import canonical_market
from sams.core.errors import DatabaseLocked, SamsError
from sams.core.notify import SEVERITIES
from sams.storage.repos.alerts import DISMISSED, HANDLED, NEW, STATUS, Alert
from sams.web.auth import audit, csrf_protect, require_user
from sams.web.display import alert_status, moment_label
from sams.web.forms import form_data, one

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


def _clean_status(raw: str) -> str:
    value = raw.strip() or NEW
    if value not in _STATUS_VALUES:
        raise HTTPException(
            status_code=400, detail=f"未知状态 {value!r}：只有 {'、'.join(_STATUS_VALUES)}"
        )
    return value


def _clean_severity(raw: str) -> str:
    """空 = 不筛；填了就必须是 `SEVERITIES` 里那三个。

    放行一个错值等于让它筛出空列表：那一屏会说「没有命中的告警」而告警明明在，
    而这句话是假的——比一个 400 难查得多。
    """
    value = raw.strip()
    if value and value not in SEVERITIES:
        raise HTTPException(
            status_code=400, detail=f"未知档位 {value!r}：只有 {'、'.join(SEVERITIES)}"
        )
    return value


def _clean_market(raw: str) -> str:
    value = raw.strip()
    # 空串不能进 `canonical_market`（它拒空），而「不筛市场」在这张页上就是一个空值
    return canonical_market(value) if value else ""


def _clean_page(raw: str) -> int:
    try:
        return max(1, int(raw or 1))
    except ValueError:
        return 1


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
    }


def _page_url(target: int, chosen: dict[str, str]) -> str:
    """翻页链接：值都是折过的（市场、档位、日期），并且一律走 `urlencode`——
    手拼 `key=value` 的链接在值里出现 `&`、`=` 或空格时就断了。"""
    pairs = {"page": str(target)}
    pairs.update({key: value for key, value in chosen.items() if value})
    return f"/alerts?{urlencode(pairs)}"


def _list_page(request: Request, *, groups: dict, chosen: dict, page: int, has_more: bool):
    web = request.app.state.web
    ctx = web.contexts.current()
    return web.render(
        request,
        "alerts.html",
        title="告警",
        groups=groups,
        chosen=chosen,
        page=page,
        prev_url=_page_url(page - 1, chosen) if page > 1 else "",
        next_url=_page_url(page + 1, chosen) if has_more else "",
        # 三张名单全部来自契约：状态见 STATUS_CHOICES、档位见 SEVERITIES、市场见配置
        status_choices=STATUS_CHOICES,
        severities=SEVERITIES,
        markets=sorted(ctx.settings.markets),
    )


def _detail_response(
    request: Request, *, status_code: int = 200, error: str = "", dispatch=None
):
    """详情这一屏。`dispatch` 不带注解：它的类（`DispatchResult`）住在 `sams.monitor`，
    而 web 不许 import 那一层（`tests/test_layering.py`）——同一折法见 `bootstrap.py`。"""
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
            # 那一列要到秒：同一条告警的三次尝试只差几秒，只到分钟会印成一模一样的一列
            "at": moment_label(row.ts, tz, seconds=True),
            "channel_id": row.channel_id or "",
            "outcome": row.outcome,
            "attempts": row.attempts,
            "error": row.error,
        }
        for row in ctx.alerts_store.deliveries(alert.id)
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
        # 处理那两个表单只在未处理时才有意义；状态名从常量比，不在模板里写死 "new"
        can_act=alert.status == NEW,
        error=error,
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
    status = _clean_status(query.get("status") or "")
    severity = _clean_severity(query.get("severity") or "")
    market = _clean_market(query.get("market") or "")
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
    return _list_page(request, groups=groups, chosen=chosen, page=page, has_more=has_more)


@router.get("/alerts/{alert_id}")
def detail(request: Request):
    require_user(request)
    return _detail_response(request)


@router.post("/alerts/{alert_id}/acted")
def acted(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    # 备注可以空：「看到了、按已处理记一下」本身就是一句完整的话，仓库也是这么收的
    ctx.alerts_store.set_status(
        alert.id, HANDLED, user_id=user.id, note=one(data, "note").strip()
    )
    audit(request, action="alert.acted", target=alert.id, ok=True, detail="handled")
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


@router.post("/alerts/{alert_id}/dismiss")
def dismiss(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    reason = one(data, "reason").strip()
    if not reason:
        # 这句话属于那个输入框，所以回到详情那一屏说而不是给一屏 error.html。
        # 闸仍然在仓库（`set_status` 对 dismissed 空原因必拒），这里只是把它提前说给人听
        return _detail_response(
            request, status_code=400, error="驳回必须写原因：那一列是「忽略率」唯一的输入"
        )
    ctx.alerts_store.set_status(alert.id, DISMISSED, user_id=user.id, note=reason)
    # 驳回是「忽略率」唯一的输入，而那一列库里就有：日志只记「谁按的、按了哪一条」
    audit(request, action="alert.dismiss", target=alert.id, ok=True)
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
        return _detail_response(request, status_code=400, error=str(exc))
    audit(
        request,
        action="alert.resend",
        target=alert.id,
        ok=True,
        detail=f"any_sent={dispatch.any_sent} deliveries={len(dispatch.deliveries)}",
    )
    return _detail_response(request, dispatch=dispatch)
