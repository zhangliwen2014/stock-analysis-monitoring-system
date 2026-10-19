"""分项打分与合成（M2-D，spec §8.3）：每个 0~100 的分数都带一句「它是从哪个字段算出来的」。

spec 给这一节的标题里写着**非黑盒**。这里做到的不是靠写说明，而是靠三条结构约束：

1. 每个分项是一个独立函数，输入是已经读出来的**数**（不是数据库句柄、不是整张表）——
   所以它可以被逐一手算核对；
2. 每个 `Item` 出门就带着 `evidence`：来源字段名 + 实际用进去的那几个数 + 窗口；
3. 算不出就是算不出：`value=None` 加一句 `reason`，**不补 50 分「中性」**。
   补一个中间值是让「没有数据」在合成里长得像「数据很平均」，那正是黑盒的入口。

合成（`composite`）只在模板**问到的**分项里加权平均，并按拿到的权重比例决定出不出数：
覆盖不到 `min_covered_weight` 就拒绝合成。权重表里没有的名字不许传进来（会被静默加权成
没人负责的一项），同一个 key 传两次也拒（后一个盖掉前一个，屏幕上看不出来）。

打分的刻度（锚点表）是这个模块里**最像意见**的部分，所以它们全部写成显式的锚点常数：
`PR_SCORE_ANCHORS` 里 `PR=0.4 → 90 分` 直接来自需求原文那条「筛 PR ≤ 0.4 的股票」，
而不是从某个回归里长出来的。换刻度就该改这张表，并让用例当场对上。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sams.analysis.valuation.percentile import Percentile
from sams.analysis.valuation.styles import Style
from sams.core.errors import ValidationError
from sams.core.fundamentals import RATIO_BOUNDS, out_of_caliber

#: 胜率参与排序的最小样本数（spec §8.3 那句约束）。30 是需求写下的数，不是这里选的数。
WIN_RATE_MIN_SAMPLES = 30

#: PR 的打分锚点（x 升序、y 降序）。刻度钉在三个可解释的位置上：
#: 0.4 = 需求要筛的那条线（90 分）、1.0 = 「一块钱利润付一块钱」（60 分，公允）、
#: 4.0 = 付四块钱买一块钱利润（0 分）。地板 0.2 以下不再加分：已经足够便宜，
#: 加下去只是在奖励「源给错了数」。
PR_SCORE_ANCHORS = ((0.2, 100.0), (0.4, 90.0), (1.0, 60.0), (2.0, 30.0), (4.0, 0.0))

#: ROE 锚点（小数口径）。0 及以下 0 分，20% 85 分，30% 起满分：ROE 是这一套里唯一
#: 「越高越好且没有上限」的量，所以上端要封一个顶，否则一只 ROE=90% 的票会把总分拉爆。
ROE_SCORE_ANCHORS = (
    (-1.0, 0.0),
    (0.0, 0.0),
    (0.05, 40.0),
    (0.10, 60.0),
    (0.15, 75.0),
    (0.20, 85.0),
    (0.30, 100.0),
)

#: 增速锚点：取营收与利润增速的平均。-20% 0 分、0 分界 40 分、+50% 满分。
GROWTH_SCORE_ANCHORS = ((-0.20, 0.0), (0.0, 40.0), (0.10, 60.0), (0.30, 80.0), (0.50, 100.0))

#: 资金流锚点：净额的**符号**给方向，规模按当期市值归一。±1% 市值封两端 ——
#: 不做归一时「净流入 1 亿」在大盘股上什么都不是，在小盘股上是极端信号。
FLOW_SCORE_ANCHORS = ((-0.01, 0.0), (0.0, 50.0), (0.01, 100.0))

#: 量能四象限（spec §8.3 的「量能配合」）。放量上涨最强，放量下跌最差；
#: 缩量上涨是无量上涨（弱），缩量下跌是抛压轻（中性偏正）。平盘没有方向可判 ⇒ 50。
VOLUME_QUADRANTS = {
    ("up", "heavy"): 80.0,
    ("up", "light"): 55.0,
    ("down", "heavy"): 30.0,
    ("down", "light"): 45.0,
}

#: 均线位置的三块分：站上 MA20 / 站上 MA60 / 短均在长均上方（多头排列的第一半）
MA_ABOVE_SHORT = 40.0
MA_ABOVE_LONG = 40.0
MA_STACK = 20.0

#: 周线趋势的两块分：价在周 MA20 上方 / 周 MA20 本身在上行
WEEKLY_ABOVE = 50.0
WEEKLY_RISING = 50.0

#: 分位的分项名：`Percentile.metric` 与 `ITEM_KEYS` 是两套名字，这里就是它们唯一的映射处
_PERCENTILE_ITEM = {"pr": "pr_percentile", "pe_ttm": "pe_percentile", "pb": "pb_percentile"}

_ITEM_LABELS = {
    "ma_position": "均线位置",
    "weekly_trend": "周线趋势",
    "boll_position": "布林位置（回调）",
    "volume_follow": "量能配合",
    "pr": "市赚率 PR",
    "pr_percentile": "PR 历史分位",
    "pe_percentile": "PE 历史分位",
    "pb_percentile": "PB 历史分位",
    "roe": "净资产收益率",
    "growth": "营收与利润增速",
    "main_inflow": "主力净流入",
    "north_bound": "北向增减持",
}


@dataclass(frozen=True)
class Item:
    """一个分项的一次读数。`value` 为 None 时 `reason` 一定非空。"""

    key: str
    label: str
    value: float | None
    #: 来源字段 + 实际用进去的数（spec §8.3 那句「每项带 evidence 指明数据来源」）
    evidence: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class Composite:
    """按模板权重合成出来的那一个分，连着它自己的覆盖度。"""

    score: float | None
    #: 拿到数据的分项权重之和（0~1）。它是这个分数值多少的那句话
    covered_weight: float
    missing: tuple[str, ...]
    items: tuple[Item, ...]
    evidence: str
    reason: str = ""
    #: M5 由回测回填；M2/M3 阶段是 None + 「未回测」（spec §8.3）
    win_rate: float | None = None
    win_rate_samples: int | None = None

    @property
    def ok(self) -> bool:
        return self.score is not None

    @property
    def win_rate_note(self) -> str:
        if self.win_rate is None:
            return "未回测"
        if self.win_rate_samples is None or self.win_rate_samples < WIN_RATE_MIN_SAMPLES:
            got = "没有样本数" if self.win_rate_samples is None else f"样本 {self.win_rate_samples}"
            return f"样本过少（{got} < {WIN_RATE_MIN_SAMPLES}），不参与排序"
        return f"样本 {self.win_rate_samples}，可参与排序"

    @property
    def sortable_by_win_rate(self) -> bool:
        """spec §8.3 那条约束的机器形态：样本不够时这个值压根不进排序。"""
        return self.win_rate is not None and (self.win_rate_samples or 0) >= WIN_RATE_MIN_SAMPLES


def _item(key: str, value: float | None, evidence: str, reason: str = "") -> Item:
    return Item(key, _ITEM_LABELS[key], value, evidence, reason)


def _refused(key: str, evidence: str, missing: str) -> Item:
    return _item(key, None, evidence, f"{missing}：这一项没有数据，不给分也不补一个「中性」的 50")


def _scale(value: float, anchors: Sequence[tuple[float, float]]) -> float:
    """锚点之间的线性插值，两端封住（超出不额外奖励，因为那通常是数据错）。"""
    if value <= anchors[0][0]:
        return anchors[0][1]
    if value >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:], strict=True):
        if x0 <= value <= x1:
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    raise ValidationError(f"锚点表在 {value} 上没有落点：这张表不是升序或中间有洞")


def ma_position(*, close: float | None, ma20: float | None, ma60: float | None) -> Item:
    """均线位置：站上 MA20 / 站上 MA60 / 短均在长均上方，三块分各 40 / 40 / 20。"""
    shown = f"close={close} ma20={ma20} ma60={ma60}"
    if close is None or ma20 is None or ma60 is None:
        return _refused("ma_position", "来源：日线收盘（后复权）与 MA20/MA60", "缺收盘价或均线值")
    score = (
        (MA_ABOVE_SHORT if close >= ma20 else 0.0)
        + (MA_ABOVE_LONG if close >= ma60 else 0.0)
        + (MA_STACK if ma20 >= ma60 else 0.0)
    )
    return _item(
        "ma_position",
        score,
        f"来源：日线收盘（后复权）与 MA20/MA60 最后一根；{shown}"
        f" ⇒ 站上短均 {MA_ABOVE_SHORT:g} + 长均 {MA_ABOVE_LONG:g}"
        f" + 多头排列 {MA_STACK:g}",
    )


def weekly_trend(
    weekly_close: float | None, weekly_ma20: float | None, weekly_ma20_prev: float | None
) -> Item:
    """周线趋势：价在周 MA20 上方（50）+ 周 MA20 在上行（50）。

    周线是日线现算的（spec §8.1 / M2-B），库里没有 `bars_weekly`，所以这里的 evidence
    点名的窗口是「按周重采样后的最后两根」。
    """
    shown = f"周收盘={weekly_close} 周MA20={weekly_ma20} 上一根={weekly_ma20_prev}"
    if weekly_close is None or weekly_ma20 is None or weekly_ma20_prev is None:
        return _refused(
            "weekly_trend",
            "来源：日线按周重采样后的收盘与周 MA20（最后两根）",
            "缺周线收盘或均线值",
        )
    score = (WEEKLY_ABOVE if weekly_close >= weekly_ma20 else 0.0) + (
        WEEKLY_RISING if weekly_ma20 > weekly_ma20_prev else 0.0
    )
    return _item(
        "weekly_trend",
        score,
        f"来源：日线按周重采样（不入库）的收盘与周 MA20 最后两根；{shown}"
        f" ⇒ 在线上 {WEEKLY_ABOVE:g} + 均线上行 {WEEKLY_RISING:g}",
    )


def boll_position(close: float | None, upper: float | None, lower: float | None) -> Item:
    """布林位置：`100 - %B×100`，也就是**越靠近下轨越高分**（回调到位）。

    方向是这一项的全部内容，所以名字里带着它：突破上轨那种读数是 M3 的规则模板
    （spec §10.3「布林上下轨突破」），趋势型压根不问这一项（`styles.TREND` 有注释）。
    同一个 %B 在两套模板里被解释成两种好坏，才是这一节要防的那种黑盒。
    """
    shown = f"close={close} 上轨={upper} 下轨={lower}"
    if close is None or upper is None or lower is None:
        return _refused("boll_position", "来源：日线收盘与 BOLL(20,2) 上下轨", "缺收盘或上下轨值")
    if upper <= lower:
        return _item(
            "boll_position",
            None,
            f"来源：日线收盘与 BOLL(20,2) 上下轨；{shown}",
            "上下轨重合或反了（带宽 ≤ 0）：那是指标算坏了，不是「刚好在中轨」",
        )
    band = (close - lower) / (upper - lower)
    return _item(
        "boll_position",
        100.0 - max(0.0, min(1.0, band)) * 100.0,
        f"来源：日线收盘与 BOLL(20,2) 上下轨；{shown} ⇒ %B={band:.3f}，"
        "读数按「靠近下轨=回调到位」取 100-%B×100",
    )


def volume_follow(
    price_change: float | None, volume: float | None, volume_ma: float | None
) -> Item:
    """量能配合：涨跌方向 × 放量/缩量 的四格表（`VOLUME_QUADRANTS`）。"""
    if price_change is None or volume is None or volume_ma is None or volume_ma <= 0:
        return _refused(
            "volume_follow", "来源：当日复权涨跌幅与成交量/均量（5 日）", "缺涨跌、量或均量"
        )
    shown = f"当日涨跌={price_change:.2%} 成交量={volume:,.0f} 均量={volume_ma:,.0f}"
    direction = "up" if price_change > 0 else "down" if price_change < 0 else "flat"
    if direction == "flat":
        score = 50.0
        why = "平盘没有方向可判"
    else:
        heavy = "heavy" if volume >= volume_ma else "light"
        score = VOLUME_QUADRANTS[(direction, heavy)]
        why = ("放量" if heavy == "heavy" else "缩量") + ("上涨" if direction == "up" else "下跌")
    return _item(
        "volume_follow",
        score,
        f"来源：当日复权涨跌幅与成交量/5 日均量；{shown} ⇒ {why} 落在四象限表的 {score:g} 分",
    )


def pr_score(value: float | None, *, reason: str = "") -> Item:
    """市赚率按 `PR_SCORE_ANCHORS` 打分。`value=None` 时把 PR 那侧的拒绝原因原样带出来。"""
    if value is None:
        return _item(
            "pr",
            None,
            "来源：valuation_daily.pe_ttm（或 pb）× financials.roe，见 PR 那一行",
            reason or "没有可用的 PR：这一项没有数据，不给分",
        )
    return _item(
        "pr",
        _scale(value, PR_SCORE_ANCHORS),
        f"来源：市赚率 PR={value:.3f}（PE(TTM) ÷ ROE ÷ 100，小数口径），"
        f"按锚点表 {PR_SCORE_ANCHORS} 换算",
    )


def percentile_score(pct: Percentile) -> Item:
    """历史分位 → 分项：分位越低越便宜 ⇒ 分数越高（`100 - 分位`）。

    样本数、窗口与丢弃计数跟着 evidence 走：一个 260 天样本的 20 分位与一个 1200 天样本的
    20 分位是同一个数，但不是同一句话。
    """
    key = _PERCENTILE_ITEM.get(pct.metric)
    if key is None:
        raise ValidationError(
            f"分位的 metric={pct.metric!r} 没有对应的分项："
            f"这里只映射 {' / '.join(_PERCENTILE_ITEM)}"
        )
    source = f"{pct.metric} 历史分位，窗口 {pct.window_from}~{pct.window_to}，样本 {pct.samples} 天"
    if pct.value is None:
        return _item(key, None, f"来源：{source}；{pct.evidence}", pct.reason)
    return _item(
        key,
        100.0 - pct.value,
        f"来源：{source}；{pct.evidence} ⇒ 现在 {pct.current:.4g} 处在 {pct.value:.1f} 分位，"
        "分数按「低分位=便宜」取 100-分位",
    )


def roe_score(value: float | None, *, period: str = "") -> Item:
    """ROE 水平（小数口径，D58）。口径可疑的那一个直接拒（D66）。"""
    tail = f"，报告期 {period}" if period else ""
    source = f"来源：financials.roe{tail}（小数口径，0.20 = 20%）"
    if value is None:
        return _refused("roe", source, "这一期没有 ROE")
    if out_of_caliber("roe", value):
        return _item(
            "roe",
            None,
            source,
            f"ROE={value:g} 在小数口径里只能读成 {value * 100:g}%，"
            f"达到可信上界（|ROE| ≥ {RATIO_BOUNDS['roe']:g}）：这更像漏了 ÷100，"
            "打分给 100 分是把采集错误奖励成优势",
        )
    return _item(
        "roe",
        _scale(value, ROE_SCORE_ANCHORS),
        f"{source} ⇒ ROE={value:.4f}（{value * 100:.1f}%），按锚点表 {ROE_SCORE_ANCHORS} 换算",
    )


def growth_score(revenue_yoy: float | None, profit_yoy: float | None) -> Item:
    """营收与利润增速：取平均。只有一项时用它，但必须把「只有一项」写进 evidence。"""
    given = [
        (name, value)
        for name, value in (("revenue_yoy", revenue_yoy), ("profit_yoy", profit_yoy))
        if value is not None
    ]
    source = "来源：financials.revenue_yoy / financials.profit_yoy（小数口径，同一报告期）"
    if not given:
        return _refused("growth", source, "两个增速都没有")
    for name, value in given:
        if out_of_caliber(name, value):
            return _item(
                "growth",
                None,
                f"{source}；给的数 {dict(given)}",
                f"{name}={value:g} 落进口径死区（|值| ≥ {RATIO_BOUNDS[name]:g}）："
                "疑似漏 ÷100，不打分",
            )
    mean = sum(value for _, value in given) / len(given)
    only = "" if len(given) == 2 else f"；只有 {given[0][0]} 一项，均值就是它"
    return _item(
        "growth",
        _scale(mean, GROWTH_SCORE_ANCHORS),
        f"{source} ⇒ 均值 {mean:.4f}（{mean * 100:.1f}%）{only}，"
        f"按锚点表 {GROWTH_SCORE_ANCHORS} 换算",
    )


def _flow_score(
    key: str, net: float | None, market_cap: float | None, column: str, reason: str = ""
) -> Item:
    source = f"来源：funding_flow.{column} 区间合计 ÷ valuation_daily.market_cap"
    if net is None:
        # 「这一段没有行」与「有行而这一列全空」是两句不同的话，下一步命令也不同：
        # 前者去 `sams sync flow`，后者是那个源压根不给这一列（U25）。调用方知道是哪种，
        # 所以这个 `reason` 由它带进来，不给时落到下面那句
        return _item(
            key,
            None,
            source,
            reason
            or f"{column} 这一列没有数（该源不给，见 U25）："
            "这一项没有数据，不给分也不补一个「中性」的 50",
        )
    if market_cap is None or market_cap <= 0:
        return _item(
            key,
            None,
            source,
            "没有市值就没法把净额归一：绝对金额在大盘股与小盘股上不是同一件事，"
            "这里不假设一个市值",
        )
    ratio = net / market_cap
    return _item(
        key,
        _scale(ratio, FLOW_SCORE_ANCHORS),
        f"{source} ⇒ {net:.4g} ÷ {market_cap:.4g} = {ratio:.5f}，"
        f"按锚点表 {FLOW_SCORE_ANCHORS} 换算",
    )


def main_inflow_score(net_in: float | None, market_cap: float | None, *, reason: str = "") -> Item:
    """主力净流入方向与规模：`main_net_in` 区间合计按当期市值归一。"""
    return _flow_score("main_inflow", net_in, market_cap, "main_net_in", reason)


def north_score(north_net: float | None, market_cap: float | None, *, reason: str = "") -> Item:
    """北向增减持。现在这一列在库外没有源给得出（U25），所以这里大概率是 None —— 而
    一句「该源没有北向」比补一个 50 分诚实。"""
    return _flow_score("north_bound", north_net, market_cap, "north_net", reason)


def composite(
    items: Sequence[Item],
    style: Style,
    *,
    win_rate: float | None = None,
    win_rate_samples: int | None = None,
) -> Composite:
    """按模板权重合成。缺的那部分权重不摊派给有数的那几项：它写在 `covered_weight` 里。"""
    seen: dict[str, Item] = {}
    for item in items:
        if item.key not in style.weights:
            raise ValidationError(
                f"模板 {style.name} 没有问分项 {item.key!r}："
                "收下它等于让一个没有权重的数进合成，或者把权重和顶出 1"
            )
        if item.key in seen:
            raise ValidationError(
                f"分项 {item.key} 被传了两次（{seen[item.key].evidence} 与 {item.evidence}）："
                "后一个会盖掉前一个，而屏幕上只有一份"
            )
        seen[item.key] = item

    covered = sum(style.weights[key] for key, item in seen.items() if item.value is not None)
    weighted = sum(style.weights[key] * item.value for key, item in seen.items() if item.ok)
    missing = tuple(key for key in style.asked() if seen.get(key) is None or not seen[key].ok)
    floor = float(style.param("min_covered_weight"))
    evidence = (
        f"{style.note()}；覆盖权重 {covered:.0%}（地板 {floor:.0%}），"
        f"有数的分项 {len(style.weights) - len(missing)}/{len(style.weights)}"
    )
    produced = tuple(seen[key] for key in style.asked() if key in seen)
    win = {"win_rate": win_rate, "win_rate_samples": win_rate_samples}

    if covered < floor:
        # 覆盖不到地板时连「缺哪几项」一起说：只报「分不够高」是这一节要防的那种含糊
        return Composite(
            score=None,
            covered_weight=covered,
            missing=missing,
            items=produced,
            evidence=evidence,
            reason=(
                f"只有 {covered:.0%} 的权重拿到了数据，低于地板 {floor:.0%}，不合成："
                + "；".join(_why(seen, style, key) for key in missing)
            ),
            **win,
        )
    tail = "" if not missing else "；缺项：" + "；".join(_why(seen, style, key) for key in missing)
    return Composite(
        score=weighted / covered,
        covered_weight=covered,
        missing=missing,
        items=produced,
        evidence=evidence + tail,
        **win,
    )


def _why(seen: dict[str, Item], style: Style, key: str) -> str:
    """缺的那一项为什么缺：分项函数给出的原因原样传出来，没有就说是调用方没给。"""
    item = seen.get(key)
    given = item.reason if item is not None and item.reason else "调用方没有给出这一项"
    return f"{key}（{style.weights[key]:.0%}）：{given}"
