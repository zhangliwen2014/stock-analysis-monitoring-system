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

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import DatabaseLocked, SamsError, StorageError, ValidationError
from sams.core.notify import SEVERITIES
from sams.core.rules import FILTER_KEYS, RULE_KINDS, Template, template_for
from sams.services.rules.engine import RunReport
from sams.storage.repos.rules import Rule
from sams.storage.repos.symbol_backfills import BARS
from sams.web.auth import csrf_protect, require_user
from sams.web.display import (
    delivery_label,
    filter_label,
    moment_label,
    price,
    state_label,
    target_type_label,
)
from sams.web.forms import filter_flags, form_data, many, one, param_fields, raw_params
from sams.web.quotes import QuoteView, quote_reader

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

#: 列表顶部那一句只可能是应用自己发的 303：`?notice=` 的原文不进屏幕
_NOTICES = {
    "created": "规则已保存。",
    "updated": "规则已更新。",
    "on": "规则已启用。",
    "off": "规则已停用。",
    "removed": "规则已删除。",
}

#: 阶段的中文名：`STAGES` 那三个词是引擎的词汇，屏幕上要让不看代码的人读得懂
_STAGE_LABELS = {"pre": "临近", "hit": "命中", "urgent": "紧急"}

#: 示例表单没有自选可借时的兜底代码。只当默认值：库里有没有一根 K 线由结果页说
_DEMO_CODES = {"A": "600519", "HK": "00700", "US": "AAPL"}

#: 选 kind 那一屏的分组（专家验收 P2-2）：十四项裸枚举按用途分成五段。
#: 前缀是 kind 的点号前那一段，不在表里的前缀归「其他」——新加一组时忘了登记，
#: 它至少还会出现在屏幕上，而不是静默消失。
_KIND_GROUPS = (
    ("stop", "止损"),
    ("tp", "止盈"),
    ("price", "价格"),
    ("thesis", "论点"),
    ("scheduled", "定时"),
)


def _param_value(value: object, *, percent: bool = False) -> str:
    """参数在屏上的写法：`1200.0` 印成 `1200`——`params_json` 读回来永远是 float。

    `percent` 参数在库里是小数（0.08），屏上按百分数写（8）：两处口径只有这一处换算。
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value * 100:g}" if percent else f"{value:g}"
    return str(value)


def _condition(rule: Rule) -> str:
    """这一条在等什么：模板名 + 库里那几个参数。列表原先连阈值都不印。"""
    template = template_for(rule.kind)
    parts: list[str] = []
    for param in template.params:
        value = rule.params.get(param.name)
        if value is None:
            continue
        text = (
            ",".join(_param_value(piece, percent=param.percent) for piece in value)
            if isinstance(value, list)
            else _param_value(value, percent=param.percent)
        )
        unit = "%" if param.percent else (f" {param.unit}" if param.unit else "")
        parts.append(f"{param.label} {text}{unit}")
    return template.label + ("（" + "，".join(parts) + "）" if parts else "")


def _target_text(ctx, rule: Rule) -> str:
    """对象那一列：单只标的说得出名字，持仓/论据说得出是谁的。"""
    if rule.target_type == "symbol":
        market, _, code = rule.target_ref.partition("/")
        master = ctx.symbols.get(market, code) or {}
        name = str(master.get("name") or "")
        return f"{name} {rule.target_ref}" if name else rule.target_ref
    if rule.target_type == "watchlist":
        return "自选清单（全部）"
    if rule.target_type == "position":
        account = ctx.sim_accounts.get(rule.user_id, rule.account_id or "")
        whose = account.name if account is not None else rule.account_id or "全部"
        return f"持仓（账户 {whose}）"
    if rule.target_type == "thesis":
        # 仓库带 `_store`：`ctx.theses` 是那个读取用例，没有按 id 取一条的接口
        thesis = ctx.theses_store.get(rule.user_id, rule.thesis_id or rule.target_ref)
        if thesis is not None:
            return f"论点 {thesis.market}/{thesis.code}"
        return f"论点 {rule.target_ref[:12]}"
    return f"{target_type_label(rule.target_type)} {rule.target_ref}"


def _target_url(rule: Rule) -> str:
    """对象那一格通往标的页：专家原话「至少能链接到标的」。写坏的引用不给链接。"""
    if rule.target_type != "symbol":
        return ""
    market, sep, code = rule.target_ref.partition("/")
    if not sep:
        return ""
    try:
        market = canonical_market(market)
        return f"/symbol/{market}/{canonical_code(market, code)}"
    except SamsError:
        return ""


def _symbol_target(rule: Rule) -> tuple[str, str] | None:
    """这条规则挂在哪只票上（归一后的一对）；写坏的引用与不挂标的的规则都交 `None`。"""
    if rule.target_type != "symbol":
        return None
    try:
        market, sep, code = rule.target_ref.partition("/")
        if not sep:
            return None
        market = canonical_market(market)
        return market, canonical_code(market, code)
    except SamsError:
        return None


def _quote(rule: Rule, quotes: dict[tuple[str, str], QuoteView]) -> dict:
    """列表里那一格价：与看板、个股页同一套读模型（S0-E）。

    盘中是过闸的报价，休市回落库内最后一根不复权收盘，并把「10-30 收盘」写在屏幕上。
    一行写坏的引用（旧行、手改过的库）不该把整张列表变成 500：那一格留空，规则照常显示。
    """
    target = _symbol_target(rule)
    view = quotes.get(target) if target is not None else None
    if view is None:
        return {"price": "", "at": "", "state": ""}
    if view.price is None:
        # 没有价时说市场此刻是什么状态（休市/无行情），比一整格空白更能解释自己
        return {"price": "", "at": "", "state": state_label(view.state)}
    return {
        "price": price(view.price, view.unit),
        "at": view.label,
        "state": state_label(view.state),
    }


def _draft_symbol(target_type: str, target_ref: str) -> tuple[str, str] | None:
    """这一屏正在填的引用指着哪只票（归一后的一对）；集合、论与写坏的引用交 `None`。"""
    if target_type != "symbol":
        return None
    market, sep, code = target_ref.strip().partition("/")
    if not sep:
        return None
    try:
        return canonical_market(market), canonical_code(market, code)
    except SamsError:
        return None


def _line_notes(template: Template, values: dict[str, str], view: QuoteView) -> dict[str, str]:
    """把填着的那几个**绝对价**换算成「这条线在现价哪一侧、差多少」。

    只换算 `unit == "元"` 的参数，百分比那一格一律不动——`stop.pct` 的 8% 说的是**成本**
    下浮 8%，`stop.trailing` 的基准是持仓期最高价，`tp.tiered` 的分档是成本到目标价的进度。
    拿现价乘一下印在旁边，说的是「你的线在这儿」而引擎算的是另一个数，这类错要到除权日
    才露出来。那几格的相对关系由试算给（引擎成对交出 `value_text` 与 `threshold_text`）。

    说的是位置，不是结论：「已经破了」是这条线的算术，而这一趟真跑会出哪一档（pre/hit/
    urgent）还要过除权换算、过滤器、冷却与状态跃迁四道闸——那句话归试算。
    """
    current = view.price
    if not current or current <= 0:
        return {}
    out: dict[str, str] = {}
    for spec in template.params:
        if spec.unit != "元":
            continue
        raw = (values.get(f"param_{spec.name}") or "").strip()
        try:
            line = float(raw)
        except ValueError:
            continue
        if line <= 0:
            continue
        gap = (line - current) / current * 100
        if abs(gap) < 0.005:
            where = "这条线就是现价"
        elif gap > 0:
            where = f"这条线在现价上方 {gap:.2f}%"
        else:
            where = f"这条线在现价下方 {abs(gap):.2f}%"
        out[spec.name] = f"{spec.label} {price(line, view.unit)}：{where}"
    return out


def _queue_bars(ctx, market: str, code: str) -> bool:
    """缺日线就排进同一条补采队列（D340，与保存即排同一条 D144 规矩）。

    准入闸与个股页同一道（D314）：代码表不认识的票根本不排，返回 False 让文案别说
    「已排进」那句假话。`enqueue` 对已有开放行返回 False，但这里仍报 True——行已经在
    队列里，「已排进」依然是真话。未配置的市场也挡在外面：`admits` 对整表未同步的市场
    返回 True，不先挡一道就会给 `ZZ/600519` 这种永远读不到行情的市场排一条死队。
    """
    if market not in ctx.settings.markets:
        return False
    if not ctx.symbol_list_store.admits(market, code):
        return False
    ctx.backfills_store.enqueue(market, code, BARS)
    return True


def _price_panel(ctx, reader, template: Template, values: dict[str, str]) -> dict:
    """表单上方那一格：这一屏挂的那只票此刻是多少钱，或者为什么说不清（R-22）。

    读的是 `web/quotes.py` 那一份统一模型——盘中是过闸的报价，休市回落库内最后一根
    不复权收盘。列表页早就在印这一格（`_quote`），而「建规则」那一屏没有：人对着一个
    空框敲止损价，敲完保存才在列表里看见「现价 11.64、线 11.50」——那条线是不是他想画的
    那个位置，只有事后才知道。

    挂在集合或论上没有「这一只的现价」，那句话也说得出：它解释的是这一格为什么是空的。
    """
    target_type = values.get("target_type", "")
    target_ref = values.get("target_ref", "")
    if not target_type:
        return {"text": "", "lines": {}}
    pair = _draft_symbol(target_type, target_ref)
    if pair is None:
        if target_type == "symbol":
            return {
                "text": (
                    f"对象引用要写成 市场/代码（例如 A/600519）——"
                    f"「{target_ref.strip()}」读不出一只票"
                ),
                "lines": {},
            }
        return {
            "text": (
                f"这条挂在{target_type_label(target_type)}上：没有单一现价，"
                "试算会逐只给出现值"
            ),
            "lines": {},
        }
    market, code = pair
    view = reader.view(market, code)
    if view.price is None:
        # 没有价的时候说清此刻是什么状态，并把这只票排进补采队列（D340，与保存即排同一条
        # D144 规矩）：网页用户没有终端，印命令只会让他对着 serve 占着的库敲出 DatabaseLocked
        if _queue_bars(ctx, market, code):
            gap = "已排进后台补采队列：补完刷新这一屏。"
        else:
            gap = "代码表里没有这只票，补采排不上——先等代码表同步。"
        return {
            "text": (
                f"{market}/{code} 库里还没有日线：{gap}"
                f"（{state_label(view.state)}）"
            ),
            "lines": {},
        }
    master = ctx.symbols.get(market, code) or {}
    name = str(master.get("name") or "")
    head = f"{name} {market}/{code}" if name else f"{market}/{code}"
    return {
        "text": f"{head} 现价 {price(view.price, view.unit)}｜{view.label}｜{view.detail}",
        "lines": _line_notes(template, values, view),
    }


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


def _posted_params(template: Template, data: dict[str, list[str]]) -> dict[str, object]:
    """表单里的参数原文 → 交给服务的值：百分数换算成小数只在这一个口。

    错在百分数上就在这里说百分数：`validate_params` 收的是小数，它的
    「不能高于 0.9」对着一个敲了 99 的人说，读起来像在讲另一条规则。
    """
    raw = raw_params(template, data)
    specs = {param.name: param for param in template.params}
    out: dict[str, object] = {}
    for name, value in raw.items():
        spec = specs[name]
        if not spec.percent:
            out[name] = value
            continue
        try:
            number = float(value)
        except ValueError:
            raise ValidationError(f"{spec.label}要的是百分数，收到 {value!r}") from None
        if spec.maximum is not None and number > spec.maximum * 100:
            raise ValidationError(
                f"{spec.label}不能高于 {spec.maximum * 100:g}%（收到 {number:g}）"
            )
        if spec.minimum is not None and number < spec.minimum * 100:
            raise ValidationError(
                f"{spec.label}不能低于 {spec.minimum * 100:g}%（收到 {number:g}）"
            )
        out[name] = number / 100
    return out


def _param_text(value: object, *, percent: bool = False) -> str:
    """一个参数在提示里的写法。列表折回逗号串（`0.5,1.0`）：`str([0.5, 1.0])` 带着空格
    和方括号，人照着它再敲一遍会被 `_as_tiers` 拒——提示得是表单自己读得懂的东西。"""
    if isinstance(value, list):
        return ",".join(_param_value(piece) for piece in value)
    return "" if value is None else _param_value(value, percent=percent)


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
    specs = {param.name: param for param in template_for(rule.kind).params}
    out: dict[str, str] = {}
    for key, value in rule.params.items():
        spec = specs.get(key)
        text = _param_text(value, percent=bool(spec and spec.percent))
        if text:
            out[key] = f"{text}%" if spec is not None and spec.percent else text
    return out


def _mount(rule: Rule) -> str:
    """编辑页那一行「挂在哪里」。

    是文字而不是下拉：`RuleService.edit` 与 `RuleRepo.update` 都不收 `account_id`
    与 `thesis_id`（合并那一段永远取旧值），换成两个改不动的框等于请用户填一个
    存不下去的选择。要换挂载对象就回列表另建一条。
    """
    parts = [f"{target_type_label(rule.target_type)} {rule.target_ref}"]
    if rule.account_id:
        parts.append(f"账户 {rule.account_id}")
    if rule.thesis_id:
        parts.append(f"论 {rule.thesis_id}")
    return "｜".join(parts)


def _demo_symbol(ctx, user) -> tuple[str, str]:
    """示例表单的默认标的：自选里第一只在配置市场中的票，其次 A 股那只最常被问到的。

    只负责默认值，不保证库里有日线——没有时结果页会说该跑哪条同步命令；替用户
    挑一只「一定有数据」的票需要一次全表扫描，而这个表单本来就可以手改。
    """
    markets = set(ctx.settings.markets)
    for item in ctx.watchlists.list(user.id):
        if item.market in markets:
            return item.market, item.code
    market = "A" if "A" in markets else (sorted(markets)[0] if markets else "A")
    return market, _DEMO_CODES.get(market, "")


def _list_page(
    request: Request,
    *,
    error: str | None = None,
    status: int = 200,
    notice: str = "",
    demo_market: str | None = None,
    demo_code: str | None = None,
):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    now = web.core.clock.now_utc()
    tz = ctx.settings.server.display_tz
    # 走服务层而不是仓库那份：`targets` 与 `notes` 说的是「这条规则现在响不响」，
    # 覆盖 0 个对象是一条静默的规则，而这一屏是唯一能看见它的地方
    views = ctx.rule_service.list(user.id)
    # 一趟聚合：每条规则最近一次响在哪天、响过几次。求值时刻没有落库（见设计文档），
    # 这一格回答的是它的一半：从没响过与昨天响过是两句不同的话
    last_by_rule = ctx.alerts_store.last_by_rule(user.id)
    # 报价也一趟取完：逐条规则各查一次日线，同一只票会被读很多遍
    reader = quote_reader(ctx, web.core, now)
    batches: dict[str, set[str]] = {}
    for view in views:
        target = _symbol_target(view.rule)
        if target is not None and target[0] in ctx.settings.markets:
            batches.setdefault(target[0], set()).add(target[1])
    quotes: dict[tuple[str, str], QuoteView] = {}
    for market, codes in batches.items():
        for code, quote_view in reader.views(market, sorted(codes)).items():
            quotes[(market, code)] = quote_view
    rows = []
    for view in views:
        rule = view.rule
        last = last_by_rule.get(rule.id)
        rows.append(
            {
                "view": view,
                "rule": rule,
                "condition": _condition(rule),
                "target": _target_text(ctx, rule),
                "target_url": _target_url(rule),
                "quote": _quote(rule, quotes),
                "last_at": moment_label(last[0], tz) if last else "从未",
                "last_count": last[1] if last else 0,
            }
        )
    groups = [
        (heading, [row for row in rows if row["rule"].enabled is wanted])
        for heading, wanted in (("启用", True), ("已停用", False))
    ]
    if demo_market is None or demo_code is None:
        demo_market, demo_code = _demo_symbol(ctx, user)
    return web.render(
        request,
        "rules.html",
        status_code=status,
        title="规则",
        groups=[(heading, items) for heading, items in groups if items],
        notice=notice,
        error=error,
        demo_market=demo_market,
        demo_code=demo_code,
        demo_markets=sorted(ctx.settings.markets),
    )


def _outcome_rows(ctx, user, report: RunReport, *, lead: str = "") -> dict:
    """一次求值的五摞，压成能进任何一屏的那一份（`partials/rule_outcome.html` 的入参）。

    列表页的「判一次」和表单页的「试算（不保存）」问的是同一件事——这一趟判出了什么，
    差别只在**从哪儿发起**：一处只需要那一格结果，另一处要在同一屏继续改那几个数。
    五摞的排法、投递那句、空结果那句写两遍，改一摞的措辞就要记得改两处。

    `lead` 是这一趟的来路说明（试算那句「按最新收盘判，没读盘中报价」）：检查页留空，
    因为它上面已经有「试运行 / 真跑」那一段了。
    """
    written = [hit.alert_id for hit in report.raised if hit.alert_id]
    outcomes = ctx.alerts_store.latest_outcomes(written)
    return {
        "lead": lead,
        "dry_run": report.dry_run,
        "as_of": report.as_of.isoformat(),
        "judged": report.judged,
        "quiet": report.quiet,
        "raised": [
            {
                "name": hit.name,
                "market": hit.market,
                "code": hit.code,
                "stage": _STAGE_LABELS.get(hit.stage, hit.stage),
                "value": hit.value_text,
                "threshold": hit.threshold_text,
                "action": hit.action_note,
                "data_on": hit.data_on.isoformat() if hit.data_on else "",
                "alert_url": f"/alerts/{hit.alert_id}" if hit.alert_id else "",
                "delivery": (
                    delivery_label(outcomes.get(hit.alert_id, "")) if hit.alert_id else ""
                ),
            }
            for hit in report.raised
        ],
        "suppressed": [
            {
                "market": item.market,
                "code": item.code,
                "stage": _STAGE_LABELS.get(item.stage, item.stage),
                "reason": item.reason,
            }
            for item in report.suppressed
        ],
        "skipped": [
            {"market": item.market, "code": item.code, "reason": item.reason}
            for item in report.skipped
        ],
        "blocked": [{"name": item.rule_name, "reason": item.reason} for item in report.blocked],
        # 没有通道时告警只落在收件箱：这一句在两个出口都是要紧话（试算命中了却没人推）
        "has_channels": bool(ctx.channels_store.list_for(user.id)),
    }


def _check_page(
    request: Request,
    *,
    rule: Rule,
    report: RunReport,
    demo: dict | None = None,
    status: int = 200,
    error: str = "",
):
    """一次求值的出口（D231）。五摞与 `sams rule check` 同源，只是换成人话上屏。

    真跑的那几行要再读一次投递结果：告警进收件箱与消息离开进程是两件事，
    「写了」与「发出去了」在这一页必须分得开（`/alerts` 那一列读的也是这份聚合）。
    """
    web = request.app.state.web
    ctx = web.contexts.current()
    outcome = _outcome_rows(ctx, request.state.sams_user, report)
    return web.render(
        request,
        "rule_check.html",
        status_code=status,
        title="规则求值",
        rule=rule,
        condition=_condition(rule),
        target=_target_text(ctx, rule),
        outcome=outcome,
        demo=demo or {},
        error=error,
    )


def _kind_groups() -> list[dict]:
    """按前缀分组；没有登记的前缀落到最后一段，不静默丢掉。键名不用 `items`：
    在 Jinja 里 `group.items` 先命中 dict 自己的方法，循环拿到的是那个函数。"""
    out = []
    seen: set[str] = set()
    for prefix, label in _KIND_GROUPS:
        kinds = [template_for(kind) for kind in RULE_KINDS if kind.startswith(prefix + ".")]
        seen.update(item.kind for item in kinds)
        if kinds:
            out.append({"label": label, "kinds": kinds})
    rest = [template_for(kind) for kind in RULE_KINDS if kind not in seen]
    if rest:
        out.append({"label": "其他", "kinds": rest})
    return out


def _picker_page(
    request: Request, *, values: dict[str, str], error: str | None = None, status: int = 200
):
    return request.app.state.web.render(
        request,
        "rule_form.html",
        status_code=status,
        title="规则",
        template=None,
        kind_groups=_kind_groups(),
        values=values,
        filters={},
        # 选 kind 那一屏还没有参数，也没有可报价的标的：那一格由 `_form_page` 填
        panel={},
        outcome={},
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
    outcome: dict | None = None,
):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    # 现价那一格：与列表、看板、个股页同一份读模型，进这一屏读一次（R-22）
    reader = quote_reader(ctx, web.core, web.core.clock.now_utc())
    # 只敲代码没带市场前缀时不猜：那一格解释的是为什么这一屏给不出价
    panel = _price_panel(ctx, reader, template, values)
    # 模板不收的那一格连名单都不读：`stop.fixed` 那一屏既不要账户也不要论，
    # 那两次查询读回来没人看
    given: dict[str, object] = {
        # 通道这一份每次都要读：有没有那一格由它判（见 `_CHANNELS_OFFERED`）
        "channels": ctx.channels_store.list_for(user.id),
        "panel": panel,
        # 「试算（不保存）」的那五摞：从 `_preview` 来，其余出口都是空的
        "outcome": outcome or {},
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
        # 名单从契约来，不从模板里那份硬编码来；中文名只为屏幕上认得，
        # 交回去的值仍是契约里那个键（`symbol`/`watchlist`/…）
        targets=[
            (item, target_type_label(item))
            for item in template.targets
            if item not in _UNWRITABLE_TARGETS
        ],
        filter_keys=[(key, filter_label(key)) for key in FILTER_KEYS],
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


def _preview(request: Request, data: dict[str, list[str]], rule_id: str | None = None):
    """保存前试算（R-22 的另一半）：拿刚填的那几个数判一次，表单留在同一屏。

    一趟都不写。引擎那一份 `preview()` 把 `dry_run` 写死成 `True`（不是参数），所以这里
    没有「忘了传只读开关」这种错法；草稿的 id 是 `DRAFT_ID` 那个哨兵，昨天响过什么查不到，
    于是这一趟回答的正是「这条线第一次响会怎样」。

    校验与保存同一套：新建走 `RuleService.draft`（`add` 的四道归属闸 + `RuleRepo.build`
    走的就是 `create` 那一份 `_validate`），编辑走「库里那份 + 刚敲的那几格」的合并，
    与 `rule_edit` 同一口径。两处各判一次，被拒时都是原样回到表单那 400 而不是 500。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    values = _values(data)
    filters = filter_flags(data)
    view = _own_view(ctx, user, rule_id) if rule_id is not None else None
    stored = view.rule if view is not None else None
    if stored is not None:
        # 编辑那一张表不渲染挂载对象（那一格改不动，见 `_mount`），交回来的这两项是空的。
        # 按库里那份补上，否则点一次试算反而把 GET 那一屏的现价弄丢：现价那一格读的
        # 正是 `values` 里的引用
        values["target_type"] = stored.target_type
        values["target_ref"] = stored.target_ref
    kind = stored.kind if stored is not None else one(data, "kind")
    try:
        template = template_for(kind)
    except SamsError as exc:
        return _picker_page(request, values=values, error=str(exc), status=400)
    raw_name = one(data, "name")
    severity = one(data, "severity") or None
    channels = many(data, "channel_ids")
    try:
        # 冷却那一格在这一趟里也要能敲错（`_cooldown` 折的是 `ValidationError`）：
        # 与 `rule_create` / `rule_edit` 同一处兜底，回到表单而不是一屏 500
        cooldown = _cooldown(one(data, "cooldown_min"))
        if stored is None:
            target_type = one(data, "target_type").strip()
            target_ref = one(data, "target_ref").strip()
            thesis_id = one(data, "thesis_id") or None
            if target_type == "thesis" and thesis_id:
                # 与 `rule_create` 同一句：挂在论上时下拉选中的那条就是挂载对象
                target_ref = thesis_id
            draft = ctx.rule_service.draft(
                user.id,
                kind=kind,
                name=raw_name,
                target_type=target_type,
                target_ref=target_ref,
                params=_posted_params(template, data),
                filters=filters,
                account_id=one(data, "account_id") or None,
                thesis_id=thesis_id,
                severity=severity,
                channel_ids=channels,
                cooldown_min=cooldown,
            )
        else:
            touched = _posted_params(template, data)
            draft = ctx.rule_service.draft(
                user.id,
                kind=stored.kind,
                name=raw_name,
                # 挂载对象、账户、论在编辑态都改不动（`_mount` 说的那句话），照旧交回去
                target_type=stored.target_type,
                target_ref=stored.target_ref,
                # 整包合并而不是「没答过就交 None」：试算要的是一份能判的草稿，
                # 空框说的是「这一项不改」= 库里那份还生效，而不是「按模板默认值判」
                params={**dict(stored.params), **touched},
                filters=filters,
                account_id=stored.account_id,
                thesis_id=stored.thesis_id,
                severity=severity or stored.severity,
                channel_ids=channels,
                cooldown_min=stored.cooldown_min if cooldown is None else cooldown,
            )
        report = ctx.rule_engine.preview(user.id, draft)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _form_page(
            request,
            template=template,
            values=values,
            filters=filters,
            error=str(exc),
            status=400,
            rule_id=rule_id,
            bound=_mount(stored) if stored is not None else None,
            hints=_stored_hints(stored) if stored is not None else None,
            ticks=channels,
        )
    return _form_page(
        request,
        template=template,
        values=values,
        filters=filters,
        error=None,
        status=200,
        rule_id=rule_id,
        bound=_mount(stored) if stored is not None else None,
        hints=_stored_hints(stored) if stored is not None else None,
        ticks=channels,
        outcome=_outcome_rows(
            ctx,
            user,
            report,
            lead=(
                "这次试算读的是最新一根日线收盘，没读盘中报价："
                "保存后调度器会按实时报价继续看。"
            ),
        ),
    )


@router.get("/rules")
def rules_page(request: Request):
    require_user(request)
    # 只认应用自己发的那几句 303（`_NOTICES`）：query 原文不进屏幕
    notice = _NOTICES.get(request.query_params.get("notice") or "", "")
    return _list_page(request, notice=notice)


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
        created = ctx.rule_service.add(
            user.id,
            kind=kind,
            name=one(data, "name"),
            # 引用原样交出去（只 strip）：`rules.target_ref` 存的就是人敲的那串，
            # 归一发生在求值时的 `snapshot.expand`——这里再排一遍会留下两套规则（见 symbol.py）
            target_type=target_type,
            target_ref=target_ref,
            # 只交填了的那几项：缺的由 `validate_params` 补默认值，必填的那一个由它拒
            params=_posted_params(template, data),
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
        # 先归一再对配置：规则照旧落库是规则服务的答案，补数挂钩不该把成功变 500，
        # 而未配置的市场本就无补数可言（没有日历也没有数据源）——不在配置里就跳过排队
        ref_market = canonical_market(ref_market)
        if ref_market in ctx.settings.markets:
            ctx.symbol_interest.ensure(ref_market, ref_code, actor=user.username)
    # 回到列表并带上这一条：闪一句「已保存」不够，人得看见它落在哪一行
    return RedirectResponse(f"/rules?notice=created#{created.rule.id}", status_code=303)


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
        touched = _posted_params(template, data)
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
    return RedirectResponse(f"/rules?notice=updated#{rule_id}", status_code=303)


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
    # 说清是哪一头：同一个按钮在两种状态下按下去，「已启用」与「已停用」是两句话
    return RedirectResponse(
        f"/rules?notice={'off' if view.rule.enabled else 'on'}#{rule_id}", status_code=303
    )


@router.post("/rules/{rule_id}/check")
def rule_check(
    request: Request,
    rule_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    """试算 / 判一次（D231）。只有明写 `mode=real` 才真跑。

    缺省按试算：回车落在第一个按钮上（试算），拼错一个词也不该替人写出告警。
    点名一条规则会绕过 `enabled`（引擎 `_select` 的既有语义）：停用的规则也看得到
    「如果开着会判出什么」，结果页会另说一句它当前是停用的。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    view = _own_view(ctx, user, rule_id)
    dry = one(data, "mode").strip() != "real"
    try:
        report = ctx.rule_engine.check(user.id, rule_id=rule_id, dry_run=dry)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _list_page(request, status=400, error=str(exc))
    return _check_page(request, rule=view.rule, report=report)


@router.post("/rules/preview")
def rule_preview(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    """新建那一屏的「试算（不保存）」（R-22）。判的是刚敲的那几个数，一条都不写。"""
    return _preview(request, data)


@router.post("/rules/{rule_id}/edit/preview")
def rule_edit_preview(
    request: Request,
    rule_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    """编辑那一屏的「试算（不保存）」：库里那份 + 刚敲的那几格，与保存同一口径。"""
    return _preview(request, data, rule_id=rule_id)


@router.post("/rules/demo")
def rule_demo(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    """一键示例（D231）：建一条必然命中的停用规则，并以演练告警真跑一次。

    止损线设在最新收盘上方 1%：`stop.fixed` 的判据是「现价 < 线」，所以这一趟一定
    命中。用户因此看得到「规则 → 命中 → 收件箱 → 投递」整条链，而不必等下一个
    交易时段。规则停用，调度器不会反复响它；演练告警带 `is_test`（D131）。

    没有日线时一条规则都不建：一条永远判不了的示例比没有示例更难查。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    market_raw = one(data, "market").strip()
    code_raw = one(data, "code").strip()
    try:
        if not market_raw or not code_raw:
            raise ValidationError("示例要一只票：市场和代码都填上")
        market = canonical_market(market_raw)
        code = canonical_code(market, code_raw)
    except SamsError as exc:
        return _list_page(
            request, status=400, error=str(exc), demo_market=market_raw, demo_code=code_raw
        )
    price = ctx.bars.last_closes([(market, code)]).get((market, code))
    if not price or price <= 0:
        # 仍然不建规则（一条永远判不了的示例更难查），但把这只票排进补采队列（D340）
        if _queue_bars(ctx, market, code):
            gap = "已排进后台补采队列：补完再试一次。"
        else:
            gap = "代码表里没有这只票，补采排不上——先等代码表同步。"
        return _list_page(
            request,
            status=400,
            error=f"{market}/{code} 在库里还没有日线：{gap}",
            demo_market=market,
            demo_code=code,
        )
    line = round(price * 1.01, 4)
    if line <= price:
        line = round(price + 0.01, 4)
    symbol = ctx.symbols.get(market, code) or {}
    label = str(symbol.get("name") or code)
    try:
        view = ctx.rule_service.add(
            user.id,
            kind="stop.fixed",
            name=f"示例·{label} 跌破 {_param_value(line)} 即报",
            target_type="symbol",
            target_ref=f"{market}/{code}",
            params={"price": line},
            filters={},
            enabled=False,
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _list_page(
            request, status=400, error=str(exc), demo_market=market, demo_code=code
        )
    try:
        report = ctx.rule_engine.check(
            user.id, rule_id=view.rule.id, dry_run=False, is_test=True
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _list_page(
            request,
            status=400,
            error=f"示例规则已建好，但这一趟没跑成：{exc}",
            demo_market=market,
            demo_code=code,
        )
    return _check_page(
        request, rule=view.rule, report=report, demo={"price": price, "line": line}
    )


@router.post("/rules/{rule_id}/remove")
def rule_remove(request: Request, rule_id: str, _csrf: None = Depends(csrf_protect)):
    """删除（D231）。网页上只从示例结果页调用；普通规则的删除仍走命令行。

    派生规则删不掉（服务层 `remove` 的那句话，§9.2）：它们只能停用，留一条能查到的
    记录。这里不替它降级成「停用」——那会把用户点的一个动作悄悄换成另一个。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    _own_view(ctx, user, rule_id)
    try:
        ctx.rule_service.remove(user.id, rule_id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _list_page(request, status=400, error=str(exc))
    return RedirectResponse("/rules?notice=removed", status_code=303)
