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
    #: `derived` = 引擎在同一张帧上算出来的列；`technical` = 引擎按快照日读复权日线现算
    source: str
    #: 工作帧里的列名（引擎负责把三种来源拼成同一张帧）
    column: str
    markets: frozenset[str]
    #: 排序方向（D224）：`high` = 越大越靠前，`low` = 越小越靠前，空 = 按升序。
    #: 首批条件决定整个结果的顺序，所以它必须是字段自己的语义，不能让页面猜。
    prefer: str = ""
    note: str = ""
    #: 不可同篮的字段共用一个非空值（D59/D177）
    basket: str = ""
    #: 推导字段依赖的工作帧列（D222）：引擎据此决定还要 join 哪些财务列
    needs: tuple[str, ...] = ()
    #: 这一列无数时的固定理由；比「快照缺列」更准确，且不逐票展开
    missing_note: str = ""
    #: 只有正值才有「比门槛高低」的意义（R-09）：负 PE/PB 是亏损或负净资产，
    #: 默认记「无数」，不参与命中
    positive: bool = False


_SNAPSHOT_MARKETS = frozenset({"A"})
#: 技术面 v0 只开 A 股：港美股的公司行为覆盖与日线都还撑不起一条能过闸的序列（D251）
_TECHNICAL_MARKETS = frozenset({"A"})

#: A 股、港股与美股共有的列。三个市场各用一张字段位置表（D188/D226）：
#: 美股只放与新浪逐项对过的 7 格；PB 没有可核对的位置，不跟着开。
#: 年报字段三个市场都能从各自的源拿到（D228）；PEG 的 PE 来自快照、净利同比来自年报。
_A_HK_US = frozenset({"A", "HK", "US"})
#: A 股与港股共有、美股没有核对过的列：PB。
_A_HK = frozenset({"A", "HK"})

#: 年报字段的同一句口径：A 股有披露日可挡，港股/美股两条源都没有（D215/D228）。
#: 逐个字段抄一遍这句，改口径时就会有一处继续宣布旧规矩。
_ANNUAL_NOTE = (
    "截至快照日能取到的最近一期年报：A 股按披露日挡，港股/美股没有披露日、按报告期"
    "（D215/D228）；没有就记「无数」"
)

FIELDS: tuple[Field, ...] = (
    Field("price", "现价", "元", "snapshot", "price", _A_HK_US),
    Field(
        "pct_chg",
        "涨跌幅",
        "百分数（1.85 = 1.85%）",
        "snapshot",
        "pct_chg",
        _A_HK_US,
    ),
    Field(
        "amount",
        "成交额",
        "元",
        "snapshot",
        "amount",
        _A_HK_US,
        prefer="high",
    ),
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
        _A_HK_US,
    ),
    Field(
        "pe_ttm",
        "市盈率(TTM)",
        "倍",
        "snapshot",
        "pe_ttm",
        _A_HK_US,
        prefer="low",
        positive=True,
    ),
    Field(
        "pe_dyn",
        "市盈率(动)",
        "倍",
        "snapshot",
        "pe_dyn",
        _SNAPSHOT_MARKETS,
        prefer="low",
        positive=True,
    ),
    Field(
        "pb",
        "市净率",
        "倍",
        "snapshot",
        "pb",
        _A_HK,
        prefer="low",
        positive=True,
    ),
    Field(
        "market_cap",
        "总市值",
        "亿元",
        "snapshot",
        "market_cap",
        _A_HK_US,
    ),
    Field(
        "float_cap",
        "流通市值",
        "亿元",
        "snapshot",
        "float_cap",
        _A_HK_US,
    ),
    Field(
        "pr_now",
        "现值市赚率 PR",
        "小数比值（0.5 = 50%）",
        "derived",
        "pr_now",
        _SNAPSHOT_MARKETS,
        prefer="low",
        note="PE(动)² ÷ (100×PB)：分母是价推出来的 ROE，随价格变，与年报口径的 pr 不同口径",
        basket="pr",
    ),
    Field(
        "roe",
        "年报 ROE",
        "小数比值（0.15 = 15%）",
        "financials",
        "roe",
        _A_HK_US,
        prefer="high",
        note=(
            _ANNUAL_NOTE
            + "；源没给 ROE 时按同一期年报的 ROA ÷ (1−资产负债率) 估算，"
            "估算值同样过小数口径闸"
        ),
        needs=("roa", "debt_ratio"),
    ),
    Field(
        "revenue_yoy",
        "营收同比",
        "小数比值（0.10 = 10%）",
        "financials",
        "revenue_yoy",
        _A_HK_US,
        prefer="high",
        note=_ANNUAL_NOTE,
    ),
    Field(
        "profit_yoy",
        "净利同比",
        "小数比值（0.10 = 10%）",
        "financials",
        "profit_yoy",
        _A_HK_US,
        prefer="high",
        note=_ANNUAL_NOTE,
    ),
    # D222：0028 的展示列里挑出有稳定年报口径的七列接进选股；其余金额/每股/周转率
    # 仍只做展示——词表是选股的唯一入口，没进来就等于引擎不认
    Field(
        "dividend_yield",
        "股息率",
        "小数比值（0.03 = 3%）",
        "financials",
        "dividend_yield",
        _SNAPSHOT_MARKETS,
        prefer="high",
        note="最近一期年报分红方案对应的股息率（PIT）；没有就记「无数」",
    ),
    Field(
        "gross_margin",
        "毛利率",
        "小数比值（0.35 = 35%）",
        "financials",
        "gross_margin",
        _A_HK_US,
        prefer="high",
        note=_ANNUAL_NOTE,
    ),
    Field(
        "net_margin",
        "净利率",
        "小数比值（0.15 = 15%）",
        "financials",
        "net_margin",
        _A_HK_US,
        prefer="high",
        note=_ANNUAL_NOTE,
    ),
    Field(
        "roa",
        "总资产净利率 ROA",
        "小数比值（0.08 = 8%）",
        "financials",
        "roa",
        _A_HK_US,
        prefer="high",
        note=_ANNUAL_NOTE,
    ),
    Field(
        "roic",
        "投入资本回报率 ROIC",
        "小数比值（0.15 = 15%）",
        "financials",
        "roic",
        _SNAPSHOT_MARKETS,
        prefer="high",
        note="截至快照日已披露的最近一期年报（PIT）；没有就记「无数」",
    ),
    Field(
        "debt_ratio",
        "资产负债率",
        "小数比值（0.60 = 60%）",
        "financials",
        "debt_ratio",
        _A_HK_US,
        prefer="low",
        note=_ANNUAL_NOTE,
    ),
    Field(
        "current_ratio",
        "流动比率",
        "倍",
        "financials",
        "current_ratio",
        _A_HK_US,
        prefer="high",
        note=_ANNUAL_NOTE,
    ),
    Field(
        "peg",
        "PEG（PE÷净利增速）",
        "倍",
        "derived",
        "peg",
        _A_HK_US,
        prefer="low",
        note=(
            "PE(TTM) ÷ 年报净利同比百分数；PE 或同比 ≤ 0 记「无数」。"
            "港股与美股的财务源没有披露日（D215/D228），净利同比按「报告期 ≤ 快照日」"
            "的最新一期年报取"
        ),
        needs=("profit_yoy",),
        missing_note="PE(TTM) 或年报净利同比缺失，或净利同比不大于 0：PEG 没有意义",
    ),
    # S4-A：技术面字段按快照日为止的复权日线现算，不落表（D251/D253）。
    # 缺口由 TechnicalService 逐字段给原因（日线不足/覆盖未确认/量比遇送转），
    # 这里只写口径；缺省文案不在这里重复一遍。
    Field(
        "ma20_gap",
        "现价距 MA20",
        "小数比值（0.03 = 高于均线 3%）",
        "technical",
        "ma20_gap",
        _TECHNICAL_MARKETS,
        note="收盘 ÷ 20 日均线 − 1；按快照日为止的复权收盘现算",
    ),
    Field(
        "ma60_gap",
        "现价距 MA60",
        "小数比值（0.03 = 高于均线 3%）",
        "technical",
        "ma60_gap",
        _TECHNICAL_MARKETS,
        note="收盘 ÷ 60 日均线 − 1；按快照日为止的复权收盘现算",
    ),
    Field(
        "ma250_gap",
        "现价距 MA250",
        "小数比值（0.03 = 高于均线 3%）",
        "technical",
        "ma250_gap",
        _TECHNICAL_MARKETS,
        note="收盘 ÷ 250 日均线 − 1；不足 250 根日线时记「无数」",
    ),
    Field(
        "rsi14",
        "RSI(14)",
        "0–100",
        "technical",
        "rsi14",
        _TECHNICAL_MARKETS,
        note="Wilder RSI(14)；整段没有涨跌时分母为 0，记「无数」",
    ),
    Field(
        "vol_ratio_5d",
        "5 日量比",
        "倍",
        "technical",
        "vol_ratio_5d",
        _TECHNICAL_MARKETS,
        note="当日成交量 ÷ 前 5 日均量；近 6 个交易日有送转/拆股时口径不成立，记「无数」",
    ),
    Field(
        "pos_52w",
        "52 周位置",
        "小数比值（1 = 52 周最高）",
        "technical",
        "pos_52w",
        _TECHNICAL_MARKETS,
        note="(收盘 − 近 250 个交易日最低) ÷ (最高 − 最低)；最高等于最低时记「无数」",
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
                    f"{spec.label} 的 between 要给两个数（低..高），收到 {cond.value!r}"
                )
            low = _number(cond.value[0], where=f"{spec.label} between 的下界")
            high = _number(cond.value[1], where=f"{spec.label} between 的上界")
            if low > high:
                # 不自动交换：屏幕上写的是「10..1」，交换后跑的是另一件事（D58）
                raise ValidationError(
                    f"{spec.label} between 的两个数写反了（{low:g}..{high:g}）："
                    "低的一头放前面，这里不替你交换"
                )
            checked.append(Condition(spec.name, cond.op, (low, high)))
        else:
            value = _number(cond.value, where=f"{spec.label} {cond.op} 的值")
            checked.append(Condition(spec.name, cond.op, value))
        if spec.basket:
            seen = baskets.get(spec.basket)
            if seen is not None and seen != spec.name:
                raise ValidationError(
                    f"「{field(seen, registry=registry).label}」与「{spec.label}」"
                    "是两条不同口径的市赚率，不能在同一篮里混排"
                    "（D59/D177）：请只选一条，范围行会跟着写它的口径名"
                )
            baskets[spec.basket] = spec.name
    return tuple(checked)
