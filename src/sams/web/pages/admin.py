"""管理页：用户、数据源、通道总览、健康与手动触发（spec §6.3）。

M4-A 的管理员页是「运维的那一半」：账号生命周期、源启用、系统按钮。不动别人的
仓位与告警内容，批注的 break-glass 属于 M4-B。

三道闸的形状值得单说：

* 仓库与服务调用全在端点体内走 `web.contexts.current()`（D113），而 `except SamsError`
  之前一律先放行 `DatabaseLocked`（它是 `SamsError` 的子类）：库被调度线程握着的时候
  答「没有这个用户」是一句自信的错误话，它会让人去重建一个还在的账号。
* 屏幕上的时刻全部在 Python 里折到 display_tz（`display.moment_label`）：`healthz()`
  交回来的是 `_iso()` 写出的 ISO 文字，模板既不解析也不 strftime——同一屏上「最近清理」
  与行情那一行必须属于同一个时钟，否则看上去像停更了八小时。
* 「用户」那一列是人不是 id：管理员认的是名字，而 UUID 摆在屏幕上读不出任何信息，
  又恰好是唯一能贴到别处去的那串字符。`_names` 一次 `list_users()` 建表，不是每行
  一次 `get()`——通道几十条就是几十次查询。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.core.errors import DatabaseLocked, SamsError, ValidationError
from sams.storage.repos.users import ACTIVE
from sams.web.auth import audit, csrf_protect, require_admin
from sams.web.display import moment_label
from sams.web.forms import checked, form_data, one

router = APIRouter()

#: 表单原文的依赖，提到模块级一份（lint 不许在参数默认值里调用 `Depends`，见 `pages/rules.py`）
_form_body = Depends(form_data)


@router.get("/admin")
def admin_home(request: Request):
    require_admin(request)
    return RedirectResponse("/admin/users", status_code=303)


def _names(users) -> dict[str, str]:
    """user_id → 屏幕上的人名：显示名为空就退回登录名（spec §7）。

    一张表建一次、两个页面共用：通道总览几十条时，每行一次 `users.get()` 就是几十次
    查询，而那份名单本来就要整张读一遍。
    """
    return {user.id: (user.display_name or user.username) for user in users}


def _require_target(ctx, user_id: str):
    user = ctx.users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="没有这个用户")
    return user


def _users_page(request: Request, *, status_code: int = 200, error: str = ""):
    ctx = request.app.state.web.contexts.current()
    users = ctx.users.list_users()
    names = _names(users)
    rows = [
        (
            user,
            names[user.id],
            ctx.sessions.active_count(user.id),
            # 状态串比的是仓库那个常量而不是模板里的字面量：改名时这里跟着报错，
            # 而 `{% if user.status == "active" %}` 只会安静地把所有人都显示成「停用」
            user.status == ACTIVE,
        )
        for user in users
    ]
    return request.app.state.web.render(
        request, "admin_users.html", status_code=status_code, rows=rows, error=error
    )


@router.get("/admin/users")
def users_page(request: Request):
    require_admin(request)
    return _users_page(request)


@router.post("/admin/users/create")
def create_user(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    require_admin(request)
    ctx = request.app.state.web.contexts.current()
    username = one(data, "username")
    try:
        # 长度、bcrypt 那 72 字节与重名的闸都在 `UserRepo`，这里只转述它那句话
        user = ctx.users.create(
            username, one(data, "password"), is_admin=checked(data, "is_admin")
        )
    except ValidationError as exc:
        return _users_page(request, status_code=400, error=str(exc))
    audit(request, action="admin.user.create", target=user.username, ok=True)
    return RedirectResponse("/admin/users", status_code=303)


def _user_action(request: Request, user_id: str, *, guard_self: bool):
    """共同的闸：admin 才能进来、目标必须存在、disable/降权不能落自己头上。"""
    admin = require_admin(request)
    ctx = request.app.state.web.contexts.current()
    target = _require_target(ctx, user_id)
    if guard_self and target.id == admin.id:
        raise HTTPException(status_code=400, detail="不能对自己执行这个操作")
    return ctx, target


@router.post("/admin/users/{user_id}/disable")
def disable_user(request: Request, user_id: str, _csrf: None = Depends(csrf_protect)):
    ctx, target = _user_action(request, user_id, guard_self=True)
    ctx.users.disable(target.id)
    # 停用只改状态那一列的话，那个人手上的会话还能继续读告警到 TTL 结束
    ctx.sessions.revoke_all(target.id)
    audit(request, action="admin.user.disable", target=target.username, ok=True)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/enable")
def enable_user(request: Request, user_id: str, _csrf: None = Depends(csrf_protect)):
    ctx, target = _user_action(request, user_id, guard_self=False)
    ctx.users.enable(target.id)
    audit(request, action="admin.user.enable", target=target.username, ok=True)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/set_admin")
def set_admin(
    request: Request,
    user_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    ctx, target = _user_action(request, user_id, guard_self=True)
    # 这一格是隐藏 input，带的是「要变成什么」：`checked()` 只看键在不在，而隐藏 input
    # 永远在表单里——用它读就是每一次都读到 True，「降为普通」按下去那位还是 admin
    flag = one(data, "is_admin") == "1"
    ctx.users.set_admin(target.id, flag)
    audit(
        request,
        action="admin.user.set_admin",
        target=target.username,
        ok=True,
        detail=f"is_admin={flag}",
    )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/reset_password")
def reset_password(
    request: Request,
    user_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    ctx, target = _user_action(request, user_id, guard_self=False)
    try:
        ctx.users.set_password(target.id, one(data, "password"))
    except ValidationError as exc:
        return _users_page(request, status_code=400, error=str(exc))
    # 替人重置密码的下一句话就是「那你该重新登一次」：全吊销，管理员自己也留不下那枚
    ctx.sessions.revoke_all(target.id)
    audit(request, action="admin.user.reset_password", target=target.username, ok=True)
    return RedirectResponse("/admin/users", status_code=303)


def _sources_page(request: Request, *, status_code: int = 200, error: str = "", probe=None):
    """数据源那一屏。`probe` 不带注解：`ProbeResult` 是 services 的值对象，
    而这一层的返回值一律不做注解（同一折法见 `pages/settings.py` 的 `_page`）。"""
    ctx = request.app.state.web.contexts.current()
    return request.app.state.web.render(
        request,
        "admin_sources.html",
        status_code=status_code,
        sources=ctx.source_admin.list(),
        chains=ctx.source_admin.chains(),
        markets=sorted(ctx.settings.markets),
        probe=probe,
        error=error,
    )


@router.get("/admin/sources")
def sources_page(request: Request):
    require_admin(request)
    return _sources_page(request)


@router.post("/admin/sources/set_enabled")
def source_set_enabled(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    require_admin(request)
    ctx = request.app.state.web.contexts.current()
    name = one(data, "name")
    # 同 `set_admin`：隐藏 input 带的是期望状态，读值不读键
    enabled = one(data, "enabled") == "1"
    try:
        # 「停用会不会让某个市场没有能取日线的源」只有 `SourceAdminService` 知道
        ctx.source_admin.set_enabled(name, enabled)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _sources_page(request, status_code=400, error=str(exc))
    audit(
        request,
        action="admin.source.set_enabled",
        target=name,
        ok=True,
        detail=f"enabled={enabled}",
    )
    return RedirectResponse("/admin/sources", status_code=303)


@router.post("/admin/sources/probe")
def source_probe(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    require_admin(request)
    ctx = request.app.state.web.contexts.current()
    today = date.today()
    try:
        start = date.fromisoformat(one(data, "start") or str(today - timedelta(days=5)))
        end = date.fromisoformat(one(data, "end") or str(today))
        report = ctx.source_admin.probe(
            one(data, "name"), one(data, "market"), one(data, "code"), start, end
        )
    except DatabaseLocked:
        raise
    except (ValueError, SamsError) as exc:
        # 那一句是服务/仓库的措辞（「数据源 nope 不存在，先 add」），页面不重写第二套
        return _sources_page(request, status_code=400, error=str(exc))
    audit(request, action="admin.source.probe", target=report.name, ok=report.ok)
    return _sources_page(request, probe=report)


@router.get("/admin/channels")
def channels_page(request: Request):
    """跨用户总览：配置在 `Channel` 里按构造就是掩码视图，明文只有一个出口
    （`ChannelRepo.credentials()`），所以这一屏不可能漏出别人的 webhook。"""
    require_admin(request)
    ctx = request.app.state.web.contexts.current()
    names = _names(ctx.users.list_users())
    rows = [
        # 库里若躺一条属于已被删掉的用户的老通道（今天没有删用户这条路），那一格退回 id：
        # 宁可显示一串没人认得的东西，也不替它编一个名字
        (channel, names.get(channel.user_id, channel.user_id))
        for channel in ctx.channels_store.list_all()
    ]
    return request.app.state.web.render(request, "admin_channels.html", rows=rows)


def _health_ctx(web, ctx) -> dict:
    """健康页要的那些只读快照：HTTP 线程不碰库（`healthz()` 用它自己的探针连接）。"""
    health = web.runtime.healthz()
    tz = ctx.settings.server.display_tz
    scheduler = health.get("scheduler") or {}
    beat = scheduler.get("beat_age_s")
    if scheduler.get("error"):
        # 读不出调度器要说读不出：报一个空心跳会被读成「没任务所以不跳」，
        # 而真正的情况是那条线程已经死了
        scheduler_note = f"读不到：{scheduler['error']}"
    else:
        scheduler_note = f"心跳 {'—' if beat is None else beat}s 前"
        if scheduler.get("stale"):
            scheduler_note += "（陈旧）"
    db = health.get("db") or {}
    intraday = []
    for market, reading in (health.get("intraday") or {}).items():
        raw = reading.get("last_poll_at")
        age = reading.get("age_s")
        intraday.append(
            {
                "market": market,
                # 那一串是 `_iso()`（tz-aware UTC）自己写的，解析不会失败
                "poll_at": (
                    moment_label(datetime.fromisoformat(raw), tz, seconds=True)
                    if raw
                    else "未轮询"
                ),
                "age": "—" if age is None else f"{age}s",
                "source": reading.get("source") or "—",
                # 从未轮询（休市/刚启动）不是故障，轮过且报过错才是：漏掉这个 `ok`
                # 就会把一个正在报错的市场印成一行的正常读数
                "note": "" if reading.get("ok", True) else "那一次轮询是失败的",
            }
        )
    prune_at = web.runtime.last_prune_at
    return dict(
        healthz=health,
        db_note="ok" if db.get("ok") else (db.get("error") or "读不到"),
        scheduler_note=scheduler_note,
        intraday=intraday,
        local_status=ctx.notify.local_status(),
        markets=sorted(ctx.settings.markets),
        prune_summary=web.runtime.last_prune,
        prune_at=moment_label(prune_at, tz, seconds=True) if prune_at else "",
        prune_error=web.runtime.last_prune_error,
    )


@router.get("/admin/health")
def health_page(request: Request):
    require_admin(request)
    web = request.app.state.web
    return web.render(request, "admin_health.html", **_health_ctx(web, web.contexts.current()))


@router.post("/admin/sync")
def admin_sync(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    market = one(data, "market")
    if market not in ctx.settings.markets:
        return web.render(
            request,
            "admin_health.html",
            status_code=400,
            error=f"市场 {market!r} 没有配置：只在 settings.markets 里触发同步",
            **_health_ctx(web, ctx),
        )
    # 只排队，不在这条线程上采：同步是调度线程的活，HTTP 线程替它起一遍会抢同一把写锁
    queued = ctx.scheduler.request_run(market=market)
    audit(request, action="admin.sync", target=market, ok=True, detail=queued.id)
    return RedirectResponse("/admin/health?sync=queued", status_code=303)


@router.post("/admin/prune")
def admin_prune(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    require_admin(request)
    web = request.app.state.web
    if one(data, "confirm") != "yes":
        # 清理会真删行：第一下只把人挡在确认那一屏，一次 POST 不做两件不可逆的事
        return RedirectResponse("/admin/health?prune=confirm", status_code=303)
    accepted = web.runtime.request_prune()
    audit(request, action="admin.prune", target="daily", ok=accepted)
    return RedirectResponse(
        f"/admin/health?prune={'accepted' if accepted else 'busy'}", status_code=303
    )
