"""规则的词汇表：模板名单、参数表、阶段、过滤开关、判定形状（spec §10）。

和 `core/notify.py` 同一个理由：**不含 IO**。仓库层要按它校验写入、求值层按它取默认值、
命令行按它解释参数与打印帮助，三层都能引用而互不 import。名单一旦写进 `services`，
`storage` 就够不着它——够不着校验的名单等于没有名单，垃圾能直接进库。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sams.core.errors import ValidationError

#: 三级触发（§10.2）。`pre` 是「快到了」，`hit` 是「破了」，`urgent` 是「跳空破的」。
STAGES = ("pre", "hit", "urgent")
PRE, HIT, URGENT = STAGES
#: 升级次序。它是「这次命中该不该被冷却吞掉」的判据（D94），所以只能有一处。
#: 不要拿字符串比：`"pre" < "hit"` 是字典序，跟语义正好相反。
STAGE_RANK = {PRE: 0, HIT: 1, URGENT: 2}

#: 规则挂在什么对象上。`target_ref` 的形状按类型定死，见 `storage/repos/rules.py`。
TARGET_TYPES = ("symbol", "watchlist", "thesis", "position", "portfolio")

#: 周复检「掉一档」的那一档有多大（§9.3 说的是「下滑超过一档」，而分是 0~100）。
#: 20 分切成五档是这一层的决定，不是分析层的：分项分的刻度只有 M2-D 那一张表知道，
#: 而「掉一档」必须是一个能复算的数，不能是排版上的形容词。
DRIFT_TIER = 20.0

#: `time_window` 那个开关的窗口：开盘前后各 5 分钟内不推价格类（集合竞价噪音，§10.1）
FILTER_MINUTES = 5
#: `data_freshness` 认的行情日落后天数。3 个日历日盖住一个周末 + 一天的数据延迟，
#: 再长就该由 `exclude_halt` 或健康页说话，而不是让规则拿三天前的价报「跌破止损」
STALE_PRICE_DAYS = 3

#: 四个可组合开关。`market_rel` **不在这里**：它要指数序列，而这一层没有那个输入（U27）。
FILTER_KEYS = ("time_window", "data_freshness", "exclude_halt", "no_open_alert")


@dataclass(frozen=True)
class Param:
    """模板要的一个参数。`shape` 只有三种：数、枚举、分档串。"""

    name: str
    label: str
    shape: str = "number"
    default: Any = None
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""

    def describe(self) -> str:
        tail = f"，默认 {self.default}" if not self.required else "，必填"
        bounds = ""
        if self.minimum is not None and self.maximum is not None:
            bounds = f"（{self.minimum:g}~{self.maximum:g}{self.unit}）"
        elif self.minimum is not None:
            bounds = f"（> {self.minimum:g}{self.unit}）"
        if self.choices:
            return f"{self.name}={self.label}{'/'.join(self.choices)}{tail}"
        return f"{self.name}={self.label}{bounds}{tail}"


@dataclass(frozen=True)
class Template:
    """一个 kind 的形状：要哪些参数、默认什么级别、会不会出三档、挂在什么对象上。"""

    kind: str
    label: str
    params: tuple[Param, ...] = ()
    severity: str = "mid"
    #: 会产出多个档位（止损类出 pre/hit/urgent；`tp.tiered` 出 pre/hit——
    #: 中间档算「快到了」，到目标价才是那件事。这一条不只是措辞：同一去重键上
    #: 只有更响的一档能穿冷却（D94），两档都记 `hit` 会让 100% 那一档被 50% 吞掉）
    stages: bool = False
    #: 必须挂账户（去重键含 `account_id` 的那一类，§10.1）
    needs_position: bool = False
    #: 必须挂一条论（读它的价位/赔率/分项）
    needs_thesis: bool = False
    #: 价格类：开盘前后那 5 分钟窗口只作用于它们
    price_class: bool = True
    #: 允许的 target_type（写成元组而不是全放开：一条 `stop.pct` 挂在整个自选股清单上
    #: 会读成「每个账户的每笔持仓」，而它得先知道自己挂的是谁的仓）
    targets: tuple[str, ...] = TARGET_TYPES
    #: 这一条拿快照里**哪几列人填的价**做判断（§10.2）。`anchor.py` 靠它定「搬不动时
    #: 的后果」：拿它做判断 ⇒ 整条不判，只用于显示 ⇒ 屏幕上注明那一列可能偏高。
    #: 与 `needs_position` 不是一件事：`stop.thesis_drift` 要账户，看的是打分掉档
    judged: frozenset[str] = frozenset()

    def money_params(self) -> tuple[Param, ...]:
        """声明为「元」的那几个参数——只有它们是价，需要按除权搬（§10.2）。

        认单位而不是认参数名：新加一个 `--param line=…` 的模板不必去 `anchor.py` 里登记名字，
        而 `unit="元"` 本来就是创建时那句「这是多少钱」的声明（§5.4 规则 3）。
        """
        return tuple(param for param in self.params if param.unit == "元")

    def describe(self) -> str:
        parts = [p.describe() for p in self.params]
        return f"{self.kind}（{self.label}）：" + ("；".join(parts) if parts else "无参数")


def _number(name: str, label: str, *, default=None, required=False, minimum=None,
            maximum=None, unit: str = "") -> Param:
    return Param(name=name, label=label, shape="number", default=default, required=required,
                 minimum=minimum, maximum=maximum, unit=unit)


def _choice(name: str, label: str, choices: tuple[str, ...], default: str) -> Param:
    return Param(name=name, label=label, shape="choice", default=default, choices=choices)


#: 每个 kind 一行。`stop.*` 的严重级别都是 strong：止损是「现在就要动」的事，
#: 把它配成 mid 就等于让默认路由绕开语音通道（§11 的 DEFAULT_ROUTE）。
TEMPLATES: dict[str, Template] = {
    "stop.fixed": Template(
        "stop.fixed", "跌破绝对价即报",
        (_number("price", "止损价", required=True, minimum=0.0, unit="元"),),
        severity="strong", stages=True, targets=("symbol", "watchlist", "thesis", "position"),
    ),
    "stop.pct": Template(
        "stop.pct", "成本下浮 x%",
        (_number("pct", "回撤比例", required=True, minimum=0.0, maximum=0.9),),
        severity="strong", stages=True, needs_position=True,
        judged=frozenset({"avg_cost"}),
        targets=("position", "thesis"),
    ),
    "stop.trailing": Template(
        "stop.trailing", "持仓期最高价回撤 x%",
        (_number("pct", "回撤比例", required=True, minimum=0.0, maximum=0.9),),
        severity="strong", stages=True, needs_position=True, targets=("position", "thesis"),
    ),
    "stop.atr": Template(
        "stop.atr", "成本 − 倍数×ATR14",
        (_number("multiple", "ATR 倍数", default=2.0, minimum=0.1, maximum=10.0, unit="倍"),),
        severity="strong", stages=True, needs_position=True,
        judged=frozenset({"avg_cost"}),
        targets=("position", "thesis"),
    ),
    "stop.ma": Template(
        "stop.ma", "跌破均线",
        (_choice("basis", "哪条线", ("weekly_ma20", "daily_ma60"), "weekly_ma20"),),
        severity="strong", stages=True, targets=("symbol", "watchlist", "position", "thesis"),
    ),
    "stop.breakeven": Template(
        "stop.breakeven", "浮盈超 x% 后把线抬到成本",
        (_number("trigger_pct", "触发浮盈", required=True, minimum=0.01, maximum=2.0),),
        severity="strong", stages=True, needs_position=True,
        judged=frozenset({"avg_cost"}),
        targets=("position", "thesis"),
    ),
    "stop.time": Template(
        "stop.time", "持仓 N 天未达预期 → 复核",
        (_number("days", "持有天数", required=True, minimum=1.0, maximum=3000.0, unit="天"),
         _number("max_gain_pct", "低于此涨幅算未达预期", default=0.0, minimum=-0.9,
                 maximum=1.0)),
        severity="mid", stages=False, needs_position=True,
        judged=frozenset({"avg_cost"}),
        targets=("position", "thesis"),
    ),
    "stop.thesis_drift": Template(
        "stop.thesis_drift", "价格没跌但理由已失效",
        (), severity="mid", stages=False, needs_position=True, needs_thesis=True,
        targets=("position", "thesis"), price_class=False,
    ),
    "tp.tiered": Template(
        "tp.tiered", "目标价分批提醒",
        (Param(name="tiers", label="分档（递增的比例，英文逗号分隔）", shape="tiers",
               default="0.5,1.0"),),
        severity="mid", stages=True, needs_position=True,
        judged=frozenset({"avg_cost", "target_price"}),
        targets=("position", "thesis"),
    ),
    "price.cross_below": Template(
        "price.cross_below", "下穿指定价",
        (_number("price", "价位", required=True, minimum=0.0, unit="元"),),
        severity="mid", stages=False,
    ),
    "price.cross_above": Template(
        "price.cross_above", "上穿指定价",
        (_number("price", "价位", required=True, minimum=0.0, unit="元"),),
        severity="mid", stages=False,
    ),
    "thesis.odds_below": Template(
        "thesis.odds_below", "赔率跌破阈值",
        (_number("threshold", "赔率下限", default=1.5, minimum=0.1, maximum=20.0, unit="倍"),),
        severity="mid", stages=False, needs_thesis=True,
        targets=("thesis", "position"), price_class=False,
    ),
    "thesis.drift": Template(
        "thesis.drift", "周复检掉档",
        (), severity="mid", stages=False, needs_thesis=True, targets=("thesis",),
        price_class=False,
    ),
    "scheduled.reevaluate": Template(
        "scheduled.reevaluate", "披露日后该复核",
        (_number("within_days", "几天内", default=5.0, minimum=1.0, maximum=60.0, unit="天"),),
        severity="weak", stages=False, needs_thesis=True, targets=("thesis",),
        price_class=False,
    ),
}

RULE_KINDS = tuple(TEMPLATES)


def template_for(kind: str) -> Template:
    try:
        return TEMPLATES[kind]
    except KeyError:
        raise ValidationError(
            f"未知规则类型 kind={kind!r}：只有 {', '.join(RULE_KINDS)}。"
            "名单的唯一出处是 `core/rules.py`，加一种就是在这里加一行"
        ) from None


def _as_number(param: Param, raw: object) -> float:
    try:
        value = float(raw)  # 接受 "8"，不接受 "八"：后者抛 ValueError
    except (TypeError, ValueError):
        raise ValidationError(
            f"{param.name} 要的是数字（{param.label}），收到 {raw!r}"
        ) from None
    if not math.isfinite(value):
        # nan 与任何阈值比较都是 False ⇒ 那条规则永远不会响，而屏幕上写着「启用」
        raise ValidationError(f"{param.name} 要的是有限数（{param.label}），收到 {raw!r}")
    if param.minimum is not None and value < param.minimum:
        raise ValidationError(
            f"{param.name} 不能低于 {param.minimum:g}{param.unit}（收到 {value:g}）"
        )
    if param.maximum is not None and value > param.maximum:
        raise ValidationError(
            f"{param.name} 不能高于 {param.maximum:g}{param.unit}（收到 {value:g}）"
        )
    return value


def _as_tiers(param: Param, raw: object) -> list[float]:
    if isinstance(raw, (list, tuple)):
        # 库里的 `params_json` 读回来就是 list[float]：`update` 改名时会把旧参数
        # 原样再校验一遍，这里必须接受自己的输出（否则改个名字都过不去）
        pieces: list[object] = list(raw)
    else:
        text = param.default if raw is None else raw
        if isinstance(text, (int, float)):
            raise ValidationError(f"{param.name} 要的是比例串（例如 0.5,1.0），收到 {raw!r}")
        pieces = [
            piece.strip()
            for piece in str(text).replace("，", ",").split(",")
            if piece.strip()
        ]
    if not pieces:
        raise ValidationError(f"{param.name} 不能是空的：至少给一档")
    steps: list[float] = []
    for piece in pieces:
        try:
            steps.append(float(piece))
        except (TypeError, ValueError):
            raise ValidationError(
                f"{param.name} 里 {piece!r} 不是数字：要的是 0.5,1.0 这种比例串"
            ) from None
    if any(not math.isfinite(step) for step in steps):
        # nan/inf 过得了下面的 >0 与递增检查（比较恒为 False），然后那条规则永远不响
        raise ValidationError(f"{param.name} 的每一档都要是有限数（收到 {steps}）")
    if any(step <= 0 for step in steps):
        raise ValidationError(f"{param.name} 的每一档都要大于 0（收到 {steps}）")
    if any(b <= a for a, b in zip(steps, steps[1:], strict=False)):
        raise ValidationError(
            f"{param.name} 必须递增（收到 {steps}）：1.0 那一档就是「到了目标价」，"
            "排在 0.5 前面会让第一档永远不报"
        )
    return steps


def validate_params(kind: str, raw: Mapping[str, object] | None) -> dict[str, object]:
    """补默认值 + 校形状 + 拒收野键。返回的字典可以直接进 `params_json`。

    「静默丢掉一个不认识的键」是这一层最便宜也最坏的错：用户以为写了 `percent=8`，
    库里那条规则用的还是默认的 `pct`，屏幕上两条都显示「止损比例」——没有任何一处会报错，
    直到那一次没跌破他以为的那条线。
    """
    built = template_for(kind)
    given = dict(raw or {})
    out: dict[str, object] = {}
    for param in built.params:
        if param.name not in given:
            if param.required:
                raise ValidationError(
                    f"{kind} 少了必填参数 {param.name}（{param.label}）：{built.describe()}"
                )
            out[param.name] = _default_for(param)
            continue
        value = given.pop(param.name)
        if value is None and param.required:
            raise ValidationError(f"{kind} 的参数 {param.name} 是必填的（{param.label}）")
        out[param.name] = _default_for(param) if value is None else _coerce(param, value)
    if given:
        raise ValidationError(
            f"{kind} 不认这些参数：{', '.join(sorted(given))}。"
            f"这一种要的是：{built.describe()}"
        )
    return out


def _coerce(param: Param, value: object) -> object:
    if param.shape == "choice":
        text = str(value).strip()
        if text not in param.choices:
            raise ValidationError(
                f"{param.name} 只能是 {'/'.join(param.choices)}（收到 {text!r}）"
            )
        return text
    if param.shape == "tiers":
        return _as_tiers(param, value)
    return _as_number(param, value)


def _default_for(param: Param) -> object:
    return _coerce(param, param.default) if param.shape == "tiers" else param.default


def validate_filters(raw: Mapping[str, object] | None) -> dict[str, bool]:
    """四个开关全给默认值 `False`：默认不降噪，因为「一条静默的规则」比「一条吵的规则」难查。"""
    given = dict(raw or {})
    if "market_rel" in given:
        raise ValidationError(
            "过滤器 market_rel 这一层没有输入：它要比对的指数（沪深300 等）不在库里，"
            "采集链也没有指数端点。给了也不会生效，所以这里直接拒收（记 U27）。"
        )
    out: dict[str, bool] = {}
    for key in FILTER_KEYS:
        value = given.pop(key, False)
        if value is True or value is False:
            out[key] = value
        else:
            raise ValidationError(f"过滤器 {key} 要的是真或假，收到 {value!r}")
    if given:
        raise ValidationError(
            f"不认这些过滤器：{', '.join(sorted(given))}。可用的只有 {', '.join(FILTER_KEYS)}"
        )
    return out


@dataclass(frozen=True)
class Verdict:
    """一次判定的结果。文本与数值**同时带**：文本给人，数值给 `--json` 与复算。

    只给文本的话，机器读者只能去 parse「9.80 元」；只给数值的话，屏幕上那句话就得由
    每个显示层各自拼一遍——§5.4 规则 3（币种口径）会在某一个「各自」里丢掉。
    """

    stage: str
    value: float | None
    threshold: float | None
    value_text: str
    threshold_text: str
    #: 线是怎么算出来的那句话（折回因子、基准是哪天、缺了哪一路），进告警正文
    why: str = ""

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValidationError(
                f"未知阶段 stage={self.stage!r}：只有 {list(STAGES)}，"
                "冷却放行按这三个值的次序判（D94）"
            )

    @property
    def ok(self) -> bool:
        return self.value is not None and self.threshold is not None


def tier_of(score: float | None) -> int | None:
    """分项分落在第几档（0~5）。None 进 None：「没这个数」不能排进任何一档。

    §9.3 说的是「下滑超过一档」，而这里的「一档」由 `DRIFT_TIER` 定死成 20 分。
    写成档位而不是分数差，是为了让「78 → 61」这种没跨档的波动**不**报警：
    分项分本身带着窗口噪声，跨档才是能被说清的变化。
    """
    if score is None:
        return None
    return int(max(0.0, min(DRIFT_TIER * 5.0, score)) // DRIFT_TIER)


def escalates(from_stage: str | None, to_stage: str) -> bool:
    """这一次命中该不该穿过冷却（D94）。

    没有上一次 ⇒ 放行。有上一次 ⇒ 只有**更响**的那一档才放行：`pre` 报了 30 分钟内
    再来一条 `pre` 是噪音，而 `hit` 是另一件事——止损真破了。
    """
    if from_stage is None:
        return True
    try:
        return STAGE_RANK[to_stage] > STAGE_RANK[from_stage]
    except KeyError as exc:
        raise ValidationError(f"未知阶段 {exc.args[0]!r}") from None
