"""/settings：资料、密码、免打扰、通道维护、测试告警（spec §6.3）。

这一页管的是「这个人怎么收到东西」，不是「今天收到了什么」。三件事的形状只在别处
定义过一次，页面转述而不重写：密码长度与 bcrypt 那 72 字节的闸在 `UserRepo`，
免打扰窗口的形状与那个时区名能不能解析在 `UserRepo._validate_dnd`，
「哪种 kind 存在、缺了 url 发不出去」在 `monitor/notifiers` 的工厂（它先跑再落库）。
所以被拒时屏幕上念的就是它们那几句话——两处各写一套措辞，迟早分叉。

配置明文永远不上屏幕：列表与编辑页读的都是掩码视图，编辑表单只收「要覆盖的键」，
空值=保持不变（`ChannelRepo.update_config` 的合并语义就是为这张表单设计的）：
人看不见旧值，就无法把它重打一遍，所以「没填」不可能是「删掉」。要删一个键就删通道重建。
配置本身也不在这里编辑成 TOML：`Settings.load` 只读不写（`core/config.py`），
页面上没有任何一格能存进配置文件，于是也没有那样一格。

两道闸的形状值得单说：

* 归属闸写在每一次写之前（`_own_channel`）。`ChannelRepo.update_config`/`set_enabled`
  都不收 `user_id`，少了这一道就是拿着别人的 id 改别人的行；改配置因此走
  `NotifyService.update_config`——它认 `user_id`，而且与 `add` 同一道工厂闸，
  拼错的值进不了库（进了就是「配了却不发」）。
* 改密码走 `authenticate` + `set_password` + `revoke_others`，**不**走
  `AccountService.reset_password`：那个服务只会 `revoke_all`，会把人刚站着改密码的
  这台机器一起踢下线。

时刻全部在 Python 里折到 `display_tz` 才上屏（`display.moment_label`）：模板不碰
datetime，页面模块也不 `strftime`——连回填 `<input type=time>` 那一格都住在
`display.hhmm`，一个屏幕上认得出的时钟写法只有一个出处。仓库调用全在端点体内走
`web.contexts.current()`（D113）。
"""

from __future__ import annotations

from datetime import time as dtime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.core.errors import DatabaseLocked, SamsError, ValidationError
from sams.core.notify import KINDS, MID, SYSTEM_MARKET, AlertMessage
from sams.storage.repos.channels import Channel
from sams.web.auth import audit, csrf_protect, require_user
from sams.web.display import hhmm, moment_label
from sams.web.forms import form_data, one
from sams.web.pages.alerts import INBOX_TEST_ALERT_NOTICE

router = APIRouter()

#: 表单原文的依赖，提到模块级一份（lint 不许在参数默认值里调用 `Depends`，见 `pages/rules.py`）
_form_body = Depends(form_data)

#: 测试告警那条链的正文。它要说清自己是测试：收件箱里带「测试」徽标（D131），
#: 而这句话是「为什么这条也算一条告警」的唯一现场答案
_TEST_BODY = "这是一条测试告警：选路与免打扰照常走一遍，收不到就看投递记录。"


def _own_channel(ctx, channel_id: str, user_id: str) -> Channel:
    """归属闸：不存在与不是你的都答 404（不暴露存在性），且**在任何一次写之前**。

    `DatabaseLocked` 必须先冒出去（它是 `SamsError` 的子类）：库被调度线程握着的时候
    答「没有这条通道」是一句自信的错误话，它会让人去重建一条本来还在的通道。
    """
    try:
        return ctx.notify.show(channel_id, user_id=user_id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        raise HTTPException(status_code=404, detail="没有这条通道") from exc


def _parse_config(text: str) -> dict[str, str]:
    """多行 `key=value` → dict。空行跳过；缺等号或缺键名整行拒。

    值原样交出（只 strip 两端）：webhook 地址里带空格是配错了，但把它折掉是页面的臆想。
    """
    config: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise ValidationError("配置一行一个 key=value：这一行缺了等号或键名")
        config[key.strip()] = value.strip()
    return config


def _clock_time(text: str, label: str) -> dtime | None:
    """那一格 → `datetime.time`；空串=没给（清除与半给的分别是仓库的判据）。

    判据与 `sams channel dnd` 同一形状（`time.fromisoformat`）：一个窗口怎么读只有一个说法。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        return dtime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValidationError(f"{label} 要的是 HH:MM（24 小时），收到 {cleaned!r}") from exc


def _channel_rows(ctx, channels: list[Channel], tz: str) -> list[dict[str, object]]:
    """通道那一表的行。`last_ok_at`/`last_error_at` 在这里折成文字，模板不碰 datetime。"""
    return [
        {
            "id": row.id,
            "name": row.name,
            "kind": row.kind,
            "enabled": row.enabled,
            "is_default": row.is_default,
            "ok_at": moment_label(row.last_ok_at, tz),
            "error": row.last_error,
            "error_at": moment_label(row.last_error_at, tz),
        }
        for row in channels
    ]


def _page(
    request: Request,
    *,
    status_code: int = 200,
    error: str = "",
    probe=None,
    probe_label: str = "",
    dispatch=None,
):
    """列表那一屏。`probe`/`dispatch` 不带注解：它们的类住在 `sams.monitor`，
    而 web 不许 import 那一层（同一折法见 `pages/alerts.py` 的 `dispatch`）。

    `probe_label` 从端点传进来：`DeliveryReport` 没有 `name` 字段，而屏幕上要能看出
    「刚才探的是哪一条」——那条通道端点已经读过了，不必再读一遍。
    """
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    tz = ctx.settings.server.display_tz
    return web.render(
        request,
        "settings.html",
        status_code=status_code,
        channels=_channel_rows(ctx, ctx.channels_store.list_for(user.id), tz),
        kinds=KINDS,
        # 框里的值是库里那一双折出来的文字：`time` 对象直接渲染会带着秒（见 `display.hhmm`）
        dnd={
            "start": hhmm(user.dnd_start),
            "end": hhmm(user.dnd_end),
            "tz": user.dnd_tz or "",
        },
        error=error,
        saved=request.query_params.get("saved") or "",
        probe=probe,
        probe_label=probe_label,
        dispatch=dispatch,
    )


@router.get("/settings")
def settings_page(request: Request):
    require_user(request)
    return _page(request)


@router.post("/settings/profile")
def save_profile(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    try:
        ctx.users.set_display_name(user.id, one(data, "display_name"))
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _page(request, status_code=400, error=str(exc))
    audit(request, action="settings.profile", target=user.id, ok=True)
    return RedirectResponse("/settings?saved=profile", status_code=303)


@router.post("/settings/password")
def save_password(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    # 当前密码要验：这一页开着不等于人不在旁边（离开座位两分钟就是一次改密机会）。
    # `authenticate` 顺带刷了 `last_login_at`，可接受：那是登录审计，不是这里的判据
    if ctx.users.authenticate(user.username, one(data, "current")) is None:
        # 失败也留一行：「有人试过这台账号的密码」是几天后要回头看的那件事。
        # 只留日志，不上锁也不限速——这个会话已经改得动显示名、清得掉免打扰、
        # 停得了通道，多一道闸挡不住真正的攻击者，却可能把人锁在自己的改密页外面
        audit(request, action="settings.password", target=user.id, ok=False, detail="current")
        return _page(request, status_code=400, error="当前密码不对")
    try:
        # 长度与那 72 字节的闸都在仓库（`MIN_PASSWORD_LEN`），这里不复写一遍
        ctx.users.set_password(user.id, one(data, "new"))
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _page(request, status_code=400, error=str(exc))
    # 别的设备全部下线、当前这枚保留：改密码的人不该被踢回登录页
    ctx.sessions.revoke_others(user.id, keep=request.state.sams_token)
    audit(request, action="settings.password", target=user.id, ok=True)
    return RedirectResponse("/settings?saved=password", status_code=303)


@router.post("/settings/dnd")
def save_dnd(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    try:
        # 时区名解析不了、三个字段只给了一半，都是仓库那一刀挡下（`_validate_dnd`）：
        # 半开的窗口在库里与一个跨午夜窗口长得一样，而它永远不匹配
        ctx.users.set_dnd(
            user.id,
            start=_clock_time(one(data, "dnd_start"), "开始时间"),
            end=_clock_time(one(data, "dnd_end"), "结束时间"),
            tz=one(data, "dnd_tz").strip() or None,
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _page(request, status_code=400, error=str(exc))
    audit(request, action="settings.dnd", target=user.id, ok=True)
    return RedirectResponse("/settings?saved=dnd", status_code=303)


@router.post("/settings/channels")
def add_channel(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    name = one(data, "name")
    try:
        config = _parse_config(one(data, "config"))
        # 工厂在这一步先跑、再落库：拼错的 kind 与缺了 url 的那条都不会躺在库里
        ctx.notify.add(user.id, one(data, "kind"), name, config)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _page(request, status_code=400, error=str(exc))
    audit(request, action="settings.channel.add", target=name, ok=True)
    return RedirectResponse("/settings?saved=channel", status_code=303)


def _edit_page(
    request: Request,
    channel: Channel,
    *,
    status_code: int = 200,
    error: str = "",
    config: str = "",
):
    """编辑那一屏：`channel` 是刚过完归属闸读回来的那一条（掩码视图，见 `_page` 那段）。

    框里默认空着起步——库里那份是掩码值，人没法照着重打一遍（`update_config` 的合并）。
    被拒的重渲染交回的是人刚敲的那份原文（同 `pages/rules.py` 的 `_values`）：
    那一行语法错了，改的就是那一行，不该把整框丢掉。
    """
    keys = "、".join(channel.config) or "（还没有配任何键）"
    return request.app.state.web.render(
        request,
        "channel_edit.html",
        status_code=status_code,
        channel=channel,
        keys=keys,
        error=error,
        config=config,
    )


@router.get("/settings/channels/{channel_id}/edit")
def edit_channel_page(request: Request, channel_id: str):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    return _edit_page(request, _own_channel(ctx, channel_id, user.id))


@router.post("/settings/channels/{channel_id}/edit")
def save_channel(
    request: Request,
    channel_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    channel = _own_channel(ctx, channel_id, user.id)
    try:
        # 只写填了的那些键：空值=不动（见 `ChannelRepo.update_config` 的那段为什么）。
        # 走 `NotifyService.update_config` 而不是仓库：它跟 `add` 共用同一道闸——
        # 工厂先跑，跑不过就在落库之前被拒，库里不会躺一条「配了却不发」的通道
        ctx.notify.update_config(
            channel.id, _parse_config(one(data, "config")), user_id=user.id
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _edit_page(
            request, channel, status_code=400, error=str(exc), config=one(data, "config")
        )
    audit(request, action="settings.channel.edit", target=channel.id, ok=True)
    return RedirectResponse("/settings?saved=channel", status_code=303)


def _toggle(request: Request, channel_id: str, enabled: bool):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    channel = _own_channel(ctx, channel_id, user.id)
    try:
        ctx.notify.set_enabled(channel.id, enabled, user_id=user.id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        # 典型原因：这是默认通道，得先把默认移走再停用——那句话要回到列表页说，
        # 按钮就长在那一行的操作格里
        return _page(request, status_code=400, error=str(exc))
    audit(
        request,
        action="settings.channel.toggle",
        target=channel.id,
        ok=True,
        detail=f"enabled={enabled}",
    )
    return RedirectResponse("/settings?saved=channel", status_code=303)


@router.post("/settings/channels/{channel_id}/enable")
def enable_channel(request: Request, channel_id: str, _csrf: None = Depends(csrf_protect)):
    return _toggle(request, channel_id, True)


@router.post("/settings/channels/{channel_id}/disable")
def disable_channel(request: Request, channel_id: str, _csrf: None = Depends(csrf_protect)):
    return _toggle(request, channel_id, False)


@router.post("/settings/channels/{channel_id}/probe")
def probe_channel(request: Request, channel_id: str, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    channel = _own_channel(ctx, channel_id, user.id)
    wait = web.probes.try_take(user.id)
    if wait:
        # 每用户一张卡（`WebState.probes`）：这一条也会真发到手机上，而它跑的
        # 是三次带退避的尝试，就压在这条线程池线程上
        raise HTTPException(status_code=429, detail=f"探针每 30 秒一次：请再等 {wait} 秒")
    try:
        # 一次真发送（带退避重试）就发生在这个网页线程里，而且不写 `notify_log`：
        # 探针不是告警，但它会改变那条通道在健康页上的状态
        report = ctx.notify.probe(channel.id, user_id=user.id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _page(request, status_code=400, error=str(exc))
    audit(request, action="settings.channel.probe", target=channel.id, ok=report.ok)
    return _page(request, probe=report, probe_label=channel.name)


@router.post("/settings/test-alert")
def test_alert(request: Request, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    wait = web.test_alerts.try_take(user.id)
    if wait:
        # 每用户一张卡（`WebState.test_alerts`）：这一条会真的发到手机上
        raise HTTPException(
            status_code=429, detail=f"测试告警每 5 分钟一条：请再等 {wait} 秒"
        )
    message = AlertMessage(
        market=SYSTEM_MARKET,
        code="test",
        severity=MID,
        triggered_at=ctx.clock.now_utc(),
        name="测试告警",
        body=_TEST_BODY,
    )
    try:
        # 与真告警共用 `NotifyService.dispatch` 那一条链：记一行 `alerts`、再走
        # `deliver_existing` 的选路、免打扰与 `notify_log`，所以「测试」这两个字
        # 不换任何口径。**不**经过 `AlertQueue`：这个 id 没有交给队列，那个 worker
        # 看不见它，所以不存在第二次发送（`queue.py` 那条链是调度线程的）。
        # `is_test=True` 让它不进 `no_open_alert` 与健康积压（D131）
        dispatch = ctx.notify.dispatch(message, user_id=user.id, is_test=True)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _page(request, status_code=400, error=str(exc))
    audit(
        request,
        action="settings.test_alert",
        target=dispatch.alert_id,
        ok=dispatch.any_sent,
        detail=f"deliveries={len(dispatch.deliveries)}",
    )
    # 成功不再整页重渲染（200 会被刷新重提交），303 回收件箱带那一句白名单提醒——
    # 「告警建了没有」由收件箱里的行与徽标回答，不由这一屏的 HTML
    return RedirectResponse(
        f"/alerts?{urlencode({'notice': INBOX_TEST_ALERT_NOTICE})}", status_code=303
    )
