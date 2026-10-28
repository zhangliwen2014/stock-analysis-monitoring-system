"""选股词表：一个字段一行，页面的下拉、参数校验、覆盖率与列头全从这张表长出来（D179）。

字段只在这里加。写第二份词表的代价不是重复，是「屏幕上能选、服务端不认」——
那类错误在点下按钮之后才出现，而它看起来像一个后端 bug。

两条口径的声明就在这里写完（D58）：`unit` 说清 0.5 与 50 的换算，`basket` 说清哪两条
数不可同篮（D59 的 PR 两支）；校验器与引擎都只读这两格，不再各写一份判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from sams.core.errors import ValidationError

#: 比较符。`=` 配 `低..高` 才是 between——词表里不另造一个 `between` 记号，
#: 否则命令行、表单、JSON 三处各有一份拼法
OPS = (">=", ">", "<=", "<", "=", "between")

#: 界面上比较符的中文写法。摘要与下拉都念这一份，屏幕上不会出现两种 `<=`
OP_LABELS = {">=": "≥", ">": ">", "<=": "≤", "<": "<", "=": "=", "between": "介于"}


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    #: 显示单位。D58 的教训：`0.5` 与 `50` 必须在这里说清哪一个
    unit: str
    #: `snapshot` = snapshot_daily 的列；`financials` = 按快照日 PIT 取的那一期；
    #: `derived` = 引擎在同一张帧上算出来的列
    source: str
    #: 工作帧里的列名（引擎负责把三种来源拼成同一张帧）
    column: str
    markets: frozenset[str]
    note: str = ""
    #: 不可同篮的字段共用一个非空值（D59/D177）
    basket: str = ""


_SNAPSHOT_MARKETS = frozenset({"A"})

FIELDS: tuple[Field, ...] = (
    Field("price", "现价", "元", "snapshot", "price", _SNAPSHOT_MARKETS),
    Field(
        "pct_chg",
        "涨跌幅",
        "百分数（1.85 = 1.85%）",
        "snapshot",
        "pct_chg",
        _SNAPSHOT_MARKETS,
    ),
    Field("amount", "成交额", "元", "snapshot", "amount", _SNAPSHOT_MARKETS),
    Field(
        "turnover_rate",
        "换手率",
        "百分数（0.31 = 0.31%）",
        "snapshot",
        "turnover_rate",
        _SNAPSHOT_MARKETS,
    ),
    Field("volume_ratio", "量比", "倍", "snapshot", "volume_ratio", _SNAPSHOT_MARKETS),
    Field(
        "amplitude",
        "振幅",
        "百分数（2.39 = 2.39%）",
        "snapshot",
        "amplitude",
        _SNAPSHOT_MARKETS,
    ),
    Field("pe_ttm", "市盈率(TTM)", "倍", "snapshot", "pe_ttm", _SNAPSHOT_MARKETS),
    Field("pe_dyn", "市盈率(动)", "倍", "snapshot", "pe_dyn", _SNAPSHOT_MARKETS),
    Field("pb", "市净率", "倍", "snapshot", "pb", _SNAPSHOT_MARKETS),
    Field("market_cap", "总市值", "亿元", "snapshot", "market_cap", _SNAPSHOT_MARKETS),
    Field("float_cap", "流通市值", "亿元", "snapshot", "float_cap", _SNAPSHOT_MARKETS),
    Field(
        "pr_now",
        "现值市赚率 PR",
        "小数比值（0.5 = 50%）",
        "derived",
        "pr_now",
        _SNAPSHOT_MARKETS,
        note="PE(动)² ÷ (100×PB)：分母是价推出来的 ROE，随价格变，与年报口径的 pr 不同口径",
        basket="pr",
    ),
    Field(
        "roe",
        "年报 ROE",
        "小数比值（0.15 = 15%）",
        "financials",
        "roe",
        _SNAPSHOT_MARKETS,
        note="截至快照日已披露的最近一期年报（PIT）；没有就记「无数」",
    ),
    Field(
        "revenue_yoy",
        "营收同比",
        "小数比值（0.10 = 10%）",
        "financials",
        "revenue_yoy",
        _SNAPSHOT_MARKETS,
    ),
    Field(
        "profit_yoy",
        "净利同比",
        "小数比值（0.10 = 10%）",
        "financials",
        "profit_yoy",
        _SNAPSHOT_MARKETS,
    ),
)

_BY_NAME = {item.name: item for item in FIELDS}


def field(name: str, *, registry: tuple[Field, ...] = FIELDS) -> Field:
    by_name = {item.name: item for item in registry}
    try:
        return by_name[name]
    except KeyError:
        raise ValidationError(
            f"不认识的字段 {name!r}：可选的是 {', '.join(by_name)}"
        ) from None


def fields_for(market: str, *, registry: tuple[Field, ...] = FIELDS) -> tuple[Field, ...]:
    return tuple(item for item in registry if market in item.markets)


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: float | tuple[float, float]


def describe(condition: Condition, *, registry: tuple[Field, ...] = FIELDS) -> str:
    """一条条件的可读写法。单位跟在这一句里：只说 `≤ 0.5` 会让人再猜一次小数还是百分数。"""
    spec = field(condition.field, registry=registry)
    if condition.op == "between":
        low, high = condition.value  # type: ignore[misc]
        return f"{spec.label}（{spec.unit}） 介于 {low:g}..{high:g}"
    return (
        f"{spec.label}（{spec.unit}） "
        f"{OP_LABELS.get(condition.op, condition.op)} {float(condition.value):g}"
    )


def describe_all(
    conditions: list[Condition] | tuple[Condition, ...], *, registry: tuple[Field, ...] = FIELDS
) -> str:
    return " 且 ".join(describe(item, registry=registry) for item in conditions)


def _number(text: object, *, where: str) -> float:
    try:
        number = float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationError(f"{where} 要的是数，收到 {text!r}") from None
    if not isfinite(number):
        raise ValidationError(f"{where} 要的是一个有限的数，收到 {text!r}")
    return number


def validate(
    market: str, conditions: list[Condition], *, registry: tuple[Field, ...] = FIELDS
) -> tuple[Condition, ...]:
    """过一遍词表：字段在市场里有、比较符认识、值是有序的两个有限数、篮子不冲突。

    全部通过才返回（返回的是同一批条件，调用方不必再查一遍）。
    """
    if not conditions:
        raise ValidationError("一条条件都没有：空条件等于「全市场都通过」，那不是一次筛选")
    baskets: dict[str, str] = {}
    checked: list[Condition] = []
    for cond in conditions:
        spec = field(cond.field, registry=registry)
        if market not in spec.markets:
            raise ValidationError(
                f"{market} 没有「{spec.label}」这个字段（它只供 {', '.join(sorted(spec.markets))}）"
            )
        if cond.op not in OPS:
            raise ValidationError(f"不认识的比较符 {cond.op!r}：可选的是 {', '.join(OPS)}")
        if cond.op == "between":
            if not isinstance(cond.value, tuple | list) or len(cond.value) != 2:
                raise ValidationError(
                    f"{spec.name} 的 between 要给两个数（低..高），收到 {cond.value!r}"
                )
            low = _number(cond.value[0], where=f"{spec.name} between 的下界")
            high = _number(cond.value[1], where=f"{spec.name} between 的上界")
            if low > high:
                # 不自动交换：屏幕上写的是「10..1」，交换后跑的是另一件事（D58）
                raise ValidationError(
                    f"{spec.name} between 的两个数写反了（{low:g}..{high:g}）："
                    "低的一头放前面，这里不替你交换"
                )
            checked.append(Condition(spec.name, cond.op, (low, high)))
        else:
            value = _number(cond.value, where=f"{spec.name} {cond.op} 的值")
            checked.append(Condition(spec.name, cond.op, value))
        if spec.basket:
            seen = baskets.get(spec.basket)
            if seen is not None and seen != spec.name:
                raise ValidationError(
                    f"「{seen}」与「{spec.name}」是两条不同口径的市赚率，不能在同一篮里混排"
                    "（D59/D177）：请只选一条，范围行会跟着写它的口径名"
                )
            baskets[spec.basket] = spec.name
    return tuple(checked)
