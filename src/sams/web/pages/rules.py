"""规则：列表、契约驱动的表单、启停。

表单形状只有一个出处：参数按 `Param.shape` 那三种排、过滤开关按 `FILTER_KEYS` 排、
可选的挂载对象按 `Template.targets` 排。加一种参数模板不用改这层，也不用改模板。
校验也不在这里第二遍：`validate_params` 与 `validate_filters` 是 CLI 和仓库共用的那一对，
页面只交原文（`web/forms.py`），被拒时念的就是它们那几句话——两处各写一套措辞，
迟早会分叉成「命令行说能存、页面说不行」。

两处与直觉相反的地方都在「没填」上：

* 编辑那一路，没填的那一格交出去的是 `None`（=不动），填了的那一格交的才是**整份**新值。
  为什么非得这样，写在 `rule_edit` 那个合并处（那也正是命令行 `rule edit` 的做法）。
  而「没填」在页面上真的为空，靠的是编辑页的参数框空着起步、库里那份只当提示——
  预填会让每一次保存都成了「把那些数又填了一遍」。
* 冷却留空不等于 0。0 是「没有冷却」，那条规则命中一次就响一遍；空着说的是
  「按配置的来」，交给服务层的是 `None`（`add` 拿它落 `settings.cooldown_min`，
  `edit` 拿它表示这一项不动）。

启停只有 `set_enabled` 一个出口（服务层没有 toggle/pause），页面上的「翻转」读的是
当前那一行。版本 bump 全在服务层发生，这层不碰闸。仓库调用全在端点体内走
`web.contexts.current()`（D113）。
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.core.errors import DatabaseLocked, SamsError, StorageError, ValidationError
from sams.core.notify import SEVERITIES
from sams.core.rules import FILTER_KEYS, RULE_KINDS, Template, template_for
from sams.storage.repos.rules import Rule
from sams.web.auth import csrf_protect, require_user
from sams.web.forms import filter_flags, form_data, many, one, param_fields, raw_params

router = APIRouter()

#: 表单原文的依赖，提到模块级一份：lint 不许在参数默认值里调用 `Depends`，
#: 而 FastAPI 认的就是这个对象本身，不认调用写法。
#: 它与 `csrf_protect` 读到的是同一份 body：Starlette 缓存 `request.form()`，
#: 那个依赖先解析一遍，这里取回的是同一份，不存在解析两次或把 body 读空。
_form_body = Depends(form_data)

#: 表单里那几个单值字段（`param_*` 与它们同一条路，见 `_values`）
_SCALARS = (
    "name", "target_type", "target_ref", "severity", "cooldown_min",
    "account_id", "thesis_id",
)

#: 存不上的挂载对象：`_validate` 见 `portfolio` 就拒（组合级这一层还没有输入，U28），
#: 而模板的 `targets` 里那五种是「形状」不是「能写」。给一个必然被拒的选项是坏掉的控件。
_UNWRITABLE_TARGETS = ("portfolio",)

#: 通道那一格渲染出来时插在里面的暗记（`rule_form.html` 那个 fieldset）。
#: 保存那一路靠它分清两种「一个都没勾」：这一屏压根没有那一格（没人答过，交 `None`
#: =不动），还是满屏的框被人全不勾（那是一句答案：改回默认路由）。
_CHANNELS_OFFERED = "channels_offered"


def _cooldown(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        # `int("一千")` 抛的是 ValueError，不是 SamsError：不自己折一下就越过兜底，
        # 换回一屏 500（而用户手里那张表单明明只是这一格填错了）
        raise ValidationError(
            f"冷却（分钟）要的是整数分钟，收到 {text!r}（留空是按配置的默认值）"
        ) from exc


def _values(data: dict[str, list[str]]) -> dict[str, str]:
    """被拒之后要原样回到输入框里的东西：单值字段 + 全部 `param_*`。"""
    values = {key: one(data, key) for key in _SCALARS}
    values.update(param_fields(data))
    return values


def _param_text(value: object) -> str:
    """一个参数在提示里的写法。列表折回逗号串（`0.5,1.0`）：`str([0.5, 1.0])` 带着空格
    和方括号，人照着它再敲一遍会被 `_as_tiers` 拒——提示得是表单自己读得懂的东西。"""
    if isinstance(value, list):
        return ",".join(str(piece) for piece in value)
    return "" if value is None else str(value)


def _stored_values(rule: Rule) -> dict[str, str]:
    """编辑页预填的那几个单值字段。**参数不在里面**：为什么参数框空着起步，
    写在 `rule_edit_page` 的那段注释里（`params_on` 与除权换算）。"""
    return {
        "name": rule.name,
        "target_type": rule.target_type,
        "target_ref": rule.target_ref,
        "severity": rule.severity,
        "cooldown_min": str(rule.cooldown_min),
        "account_id": rule.account_id or "",
        "thesis_id": rule.thesis_id or "",
    }


def _stored_hints(rule: Rule) -> dict[str, str]:
    """库里那份参数：只当提示渲染（键是参数名），不落进任何一个输入框。"""
    return {key: text for key, value in rule.params.items() if (text := _param_text(value))}


def _mount(rule: Rule) -> str:
    """编辑页那一行「挂在哪里」。

    是文字而不是下拉：`RuleService.edit` 与 `RuleRepo.update` 都不收 `account_id`
    与 `thesis_id`（合并那一段永远取旧值），换成两个改不动的框等于请用户填一个
    存不下去的选择。要换挂载对象就回列表另建一条。
    """
    parts = [f"{rule.target_type}:{rule.target_ref}"]
    if rule.account_id:
        parts.append(f"账户 {rule.account_id}")
    if rule.thesis_id:
        parts.append(f"论 {rule.thesis_id}")
    return "｜".join(parts)


def _list_page(request: Request, *, error: str | None = None, status: int = 200):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    return web.render(
        request,
        "rules.html",
        status_code=status,
        title="规则",
        # 走服务层而不是仓库那份：`targets` 与 `notes` 说的是「这条规则现在响不响」，
        # 覆盖 0 个对象是一条静默的规则，而这一屏是唯一能看见它的地方
        views=ctx.rule_service.list(user.id),
        error=error,
    )


def _picker_page(
    request: Request, *, values: dict[str, str], error: str | None = None, status: int = 200
):
    return request.app.state.web.render(
        request,
        "rule_form.html",
        status_code=status,
        title="规则",
        template=None,
        kinds=RULE_KINDS,
        values=values,
        filters={},
        error=error,
        rule_id=None,
    )


def _form_page(
    request: Request,
    *,
    template: Template,
    values: dict[str, str],
    filters: dict[str, bool],
    error: str | None,
    status: int,
    rule_id: str | None = None,
    bound: str | None = None,
    ticks: Sequence[str] = (),
    hints: dict[str, str] | None = None,
):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    # 模板不收的那一格连名单都不读：`stop.fixed` 那一屏既不要账户也不要论，
    # 那两次查询读回来没人看
    given: dict[str, object] = {
        # 通道这一份每次都要读：有没有那一格由它判（见 `_CHANNELS_OFFERED`）
        "channels": ctx.channels_store.list_for(user.id),
    }
    if template.needs_position:
        given["accounts"] = ctx.sim_accounts.list_for(user.id)
    if template.needs_thesis:
        given["theses"] = ctx.theses.list(user.username)
    return web.render(
        request,
        "rule_form.html",
        status_code=status,
        template=template,
        # 名单从契约来，不从模板里那份硬编码来
        targets=[item for item in template.targets if item not in _UNWRITABLE_TARGETS],
        filter_keys=FILTER_KEYS,
        severities=SEVERITIES,
        values=values,
        filters=filters,
        error=error,
        rule_id=rule_id,
        bound=bound,
        ticks=list(ticks),
        hints=dict(hints or {}),
        **given,
    )


def _own_view(ctx, user, rule_id: str):
    """不存在与不是你的都是 404：服务层那句 `StorageError` 本来就不区分这两种。

    但 `DatabaseLocked` 也是 `StorageError` 的子类（`core/errors.py`）：库被调度线程握着的
    时候答「这条规则不存在」是一句自信的错误话——它会让人去重建一条本来就还在的规则。
    """
    try:
        return ctx.rule_service.show(user.id, rule_id)
    except DatabaseLocked:
        raise
    except StorageError:
        raise HTTPException(status_code=404) from None


@router.get("/rules")
def rules_page(request: Request):
    require_user(request)
    return _list_page(request)


@router.get("/rules/new")
def rule_new_page(
    request: Request, kind: str = "", target_type: str = "symbol", target_ref: str = ""
):
    require_user(request)
    values = {"target_type": target_type, "target_ref": target_ref}
    if not kind:
        return _picker_page(request, values=values)
    try:
        template = template_for(kind)
    except SamsError as exc:
        # 拿着一个错链接的人要的是「一共有哪几种」，而不是一屏拒绝：名单就在这句话里
        return _picker_page(request, values=values, error=str(exc), status=400)
    return _form_page(
        request,
        template=template,
        values=values,
        # 新建那张表一个开关都不预勾：core 的默认是不降噪（一条静默的规则比一条吵的难查）
        filters={},
        error=None,
        status=200,
    )


@router.post("/rules/new")
def rule_create(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    kind = one(data, "kind")
    values = _values(data)
    filters = filter_flags(data)
    try:
        template = template_for(kind)
    except SamsError as exc:
        return _picker_page(request, values=values, error=str(exc), status=400)
    target_type = one(data, "target_type").strip()
    target_ref = one(data, "target_ref").strip()
    thesis_id = one(data, "thesis_id") or None
    if target_type == "thesis" and thesis_id:
        # 挂在论上时，下拉里选中的那一条就是挂载对象：`_validate` 在归一之前先按形状查
        # `target_ref`，要人再把那串 uuid 敲进引用框等于让这个下拉只剩一种用法——产 400
        target_ref = thesis_id
    try:
        ctx.rule_service.add(
            user.id,
            kind=kind,
            name=one(data, "name"),
            # 引用原样交出去（只 strip）：`rules.target_ref` 存的就是人敲的那串，
            # 归一发生在求值时的 `snapshot.expand`——这里再排一遍会留下两套规则（见 symbol.py）
            target_type=target_type,
            target_ref=target_ref,
            # 只交填了的那几项：缺的由 `validate_params` 补默认值，必填的那一个由它拒
            params=raw_params(template, data),
            filters=filters,
            account_id=one(data, "account_id") or None,
            thesis_id=thesis_id,
            # 空选择 = 不指定 = 模板自己那个级别，不是空串进仓库
            severity=one(data, "severity") or None,
            channel_ids=many(data, "channel_ids"),
            cooldown_min=_cooldown(one(data, "cooldown_min")),
        )
    except DatabaseLocked:
        # 库被调度线程握着：那是「过一会儿再建」，不是「你填错了」，交给全局 handler 答 503
        raise
    except SamsError as exc:
        # 业务拒绝（少了必填参数、别人的账户、野过滤器）留在这里说：表单还在屏上，
        # 改那一格就能重交，而 error.html 给的是一屏没有表单的话
        return _form_page(
            request,
            template=template,
            values=values,
            filters=filters,
            error=str(exc),
            status=400,
            ticks=many(data, "channel_ids"),
        )
    # 规则挂在票上也算「想看这只票」：主档 + 补数拍（D144）；引用带市场前缀才拆得出两截
    if target_type == "symbol" and "/" in target_ref:
        ref_market, _, ref_code = target_ref.partition("/")
        ctx.symbol_interest.ensure(ref_market, ref_code, actor=user.username)
    return RedirectResponse("/rules", status_code=303)


@router.get("/rules/{rule_id}/edit")
def rule_edit_page(request: Request, rule_id: str):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    view = _own_view(ctx, user, rule_id)
    rule = view.rule
    return _form_page(
        request,
        template=template_for(rule.kind),
        # 参数框一律空着起步，库里那份只当提示（`hints`）：预填会让「打开编辑页只为
        # 改个名字」那一趟把每个框都填着旧数交回来——`touched` 就永远不为空，
        # `params` 永远进 patch、`params_on` 永远盖成今天（`repos/rules.py` 的判据是
        # 「交没交」），那条线于是在除权日免掉一次该做的搬家。留空 = 不动，
        # 与级别、冷却那两格同一份合同；被拒的重渲染交回的是人刚敲的那份，不是这里。
        values=_stored_values(rule),
        hints=_stored_hints(rule),
        # 四个键都给：库里少一个键（旧行）也只是没勾，不是把渲染交给巧合
        filters={key: bool(rule.filters.get(key)) for key in FILTER_KEYS},
        error=None,
        status=200,
        rule_id=rule_id,
        bound=_mount(rule),
        ticks=rule.channel_ids,
    )


@router.post("/rules/{rule_id}/edit")
def rule_edit(
    request: Request,
    rule_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    view = _own_view(ctx, user, rule_id)
    template = template_for(view.rule.kind)
    values = _values(data)
    filters = filter_flags(data)
    try:
        # 整包替换就写在这一处：`RuleRepo.update` 只把**不是 `None`** 的 kwarg 当成一次写，
        # 交了的那个键则是整份生效——缺的键由 `validate_params` 按模板默认值补，而不是按
        # 库里那一份（少交一个就是「把止损线搬回默认价」）。`params` 在不在 `patch` 里还决定
        # `params_on` 盖不盖今天（`repos/rules.py`），而那是除权时要不要搬那条线的依据。
        # 所以没答过的那一格交 `None`，答过的那一格交完整的合并；判据是「表单交没交」，
        # 不是合出来的那份等不等：人把同一个数再敲一遍，说的还是「今天这里是这个数」。
        # 这一判据要接得上人，靠的是编辑页的参数框空着起步（见 `rule_edit_page`）：
        # 预填之下每个框都「交了」，这一规矩在真实路径上永远不为空。
        # 命令行 `rule edit` 合的就是这一份规矩（`cli/rule.py`）。
        touched = raw_params(template, data)
        params = {**dict(view.rule.params), **touched} if touched else None
        # 没渲染过那一格 = 没人答过 = 不动；渲染了而一个不勾 = 一句答案 = 改回默认路由
        channels = many(data, "channel_ids") if _CHANNELS_OFFERED in data else None
        ctx.rule_service.edit(
            user.id,
            rule_id,
            name=one(data, "name"),
            params=params,
            # 四个开关每次都整套交出：那一张表永远渲染，没勾的那个就是「关掉」这句答案
            filters=filters,
            severity=one(data, "severity") or None,
            channel_ids=channels,
            cooldown_min=_cooldown(one(data, "cooldown_min")),
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        # 重渲染时框里交回的是人刚敲的那份（`_values`），库里那份仍只当提示（`hints`）：
        # 提交过的原文与没碰过的预填是两件事
        return _form_page(
            request,
            template=template,
            values=values,
            hints=_stored_hints(view.rule),
            filters=filters,
            error=str(exc),
            status=400,
            rule_id=rule_id,
            bound=_mount(view.rule),
            ticks=many(data, "channel_ids"),
        )
    return RedirectResponse("/rules", status_code=303)


@router.post("/rules/{rule_id}/toggle")
def rule_toggle(request: Request, rule_id: str, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    view = _own_view(ctx, user, rule_id)
    try:
        # 服务层只有 `set_enabled`，没有 toggle：翻转读的是刚取出来的那一行
        ctx.rule_service.set_enabled(user.id, rule_id, not view.rule.enabled)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        # 这个按钮长在列表上，话就得回到列表说（重新渲染表单会把用户从哪来的都改掉）
        return _list_page(request, status=400, error=str(exc))
    return RedirectResponse("/rules", status_code=303)
