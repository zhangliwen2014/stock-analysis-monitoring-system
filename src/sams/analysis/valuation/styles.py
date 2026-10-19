"""打分模板：`value` / `trend` 两套内置权重与参数（M2-D，spec §8.3）。

这个模块**不算任何东西**。它的全部职责是把「按什么打分」写成一份可核对的词表：
分项的名字、每个分项属于哪一面、每个模板给谁多少权重、以及 `compose` / `scoring`
唯一能接受的参数取值。词表是这一层唯一的知识，写错的方向也不是崩溃而是沉默：

* 权重表里多一个不在 `ITEM_KEYS` 上的名字 ⇒ 那一项永远拿不到数据，而合成分照旧出；
* 权重和不是 1 ⇒ 合成分不再落在 0~100，屏幕上的「82 分」与另一只票的「82 分」不同刻度；
* 参数表上少一个键 ⇒ 合成那侧读到 `None`，然后按「没有止损」出赔率而不是按「没配置」拒绝。

「用户可复制改参数」（spec §8.3）落在 `with_overrides()`：它产出一个**新的** Style，
内置那两份永远是只读的。配置里写的是「pr 权重 5」这种人的数，不是概率，所以合并后
重新归一，而不是假设它们加起来正好是 1。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from sams.core.errors import ValidationError

#: 技术面（spec §8.3 那一行的前四项）
TECHNICAL_ITEMS = ("ma_position", "weekly_trend", "boll_position", "volume_follow")
#: 基本面（PR、历史分位、ROE、增速）
FUNDAMENTAL_ITEMS = (
    "pr",
    "pr_percentile",
    "pe_percentile",
    "pb_percentile",
    "roe",
    "growth",
)
#: 资金面（主力净流入方向、北向增减持）
FUNDING_ITEMS = ("main_inflow", "north_bound")

#: 分项名字的唯一词表。`scoring` 按它产出分项，`compose` 按它合成，命令行按它校验 --items。
ITEM_KEYS = TECHNICAL_ITEMS + FUNDAMENTAL_ITEMS + FUNDING_ITEMS

#: `stop` 的四种取值 = spec §8.3 给「策略可选」点名的那四种。
#: §10.2 那张九种的表是 **M3 规则引擎**的目录（trailing / breakeven / time / thesis_drift
#: 都要持仓成本或打分历史才能算），写进这里等于让 `--stop` 接受一个 compose 算不出的取值。
STOP_KINDS = ("fixed", "pct", "atr2", "weekly_ma20")

#: 买入区的两条规则，与两套内置模板一一对应（spec §8.3 那一句括号里的两种）
ENTRY_RULES = ("near_fair_low", "weekly_ma20_pullback")

#: 参数表。合成那一侧只读这些名字，多一个就没人读，少一个就会读到空。
PARAM_KEYS = (
    "stop_kind",
    "stop_pct",
    "atr_multiplier",
    "entry_rule",
    "entry_band_pct",
    "percentile_entry_max",
    "percentile_window_years",
    "fair_pr_low",
    "fair_pr_high",
    "target_lookback_days",
    "min_covered_weight",
)

#: 只有这两个参数是「从一组名字里挑一个」，其余是数
_ENUM_PARAMS = {"stop_kind": STOP_KINDS, "entry_rule": ENTRY_RULES}


@dataclass(frozen=True)
class Style:
    """一套打分模板：每个分项的权重 + 合成用的参数。"""

    name: str
    label: str
    weights: Mapping[str, float]
    params: Mapping[str, Any]

    def weight(self, key: str) -> float | None:
        """这个模板给 `key` 多少权重；None 意思是**没问这一项**（不是「问了而打 0 分」）。"""
        return self.weights.get(key)

    def param(self, key: str) -> Any:
        return self.params.get(key)

    def asked(self) -> tuple[str, ...]:
        """这个模板问了哪几项，按 `ITEM_KEYS` 的顺序 —— 打印那张表时要用同一个顺序。"""
        return tuple(key for key in ITEM_KEYS if key in self.weights)

    def with_overrides(
        self,
        *,
        weights: Mapping[str, float] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Style:
        """产出一份改过的副本（spec §8.3「用户可复制改参数」那条的实现处）。"""
        merged_w = dict(self.weights)
        for key, value in (weights or {}).items():
            if key not in ITEM_KEYS:
                raise ValidationError(
                    f"不认识的分项 {key!r}：能打分的分项只有 {', '.join(ITEM_KEYS)}。"
                    "拼错的名字不会报错，它只会让那一项永远没有数据"
                )
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValidationError(
                    f"分项 {key} 的权重必须是大于 0 的有限数，收到 {value!r}："
                    "不想问这一项就把它从权重表里删掉，写 0 会留下一个「问了而恒零」的分项"
                )
            merged_w[key] = float(value)
        total = sum(merged_w.values())
        # 重新归一：配置里写的是相对轻重，不是概率。除以和之后合成分才仍在 0~100 那一格上。
        weights_now = {key: value / total for key, value in merged_w.items()}

        merged_p = dict(self.params)
        for key, value in (params or {}).items():
            if key not in PARAM_KEYS:
                raise ValidationError(
                    f"不认识的风格参数 {key!r}：能改的参数只有 {', '.join(PARAM_KEYS)}"
                )
            allowed = _ENUM_PARAMS.get(key)
            if allowed is not None:
                if value not in allowed:
                    raise ValidationError(
                        f"{key} 只认 {' / '.join(allowed)}，收到 {value!r}："
                        "这个取值由合成那一侧实现，没实现的名字给了也算不出"
                    )
            elif not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValidationError(f"参数 {key} 必须是大于 0 的有限数，收到 {value!r}")
            merged_p[key] = value
        if float(merged_p["fair_pr_low"]) >= float(merged_p["fair_pr_high"]):
            raise ValidationError(
                f"合理区间的两端反了：fair_pr_low={merged_p['fair_pr_low']} 不小于 "
                f"fair_pr_high={merged_p['fair_pr_high']}"
            )
        if float(merged_p["min_covered_weight"]) > 1.0:
            raise ValidationError(
                f"min_covered_weight={merged_p['min_covered_weight']} 超过 1："
                "它说的是「有多少权重的分项拿到了数据」，超过 1 就是任何一票都合成不出分"
            )
        return replace(self, name=f"{self.name}.copy", weights=weights_now, params=merged_p)

    def note(self) -> str:
        """跟着合成分出门的那句权重话（D54：造成这个数的事得和数一起被看见）。"""
        body = "、".join(f"{key} {self.weights[key]:.0%}" for key in self.asked())
        return f"模板 {self.name}（{self.label}）权重：{body}"


#: 价值型：基本面 67%、技术面 23%、资金面 10%。PR 那一项的权重最高，
#: 因为「PR ≤ 0.4 选股」是这套系统被要求做的第一件事。
VALUE = Style(
    name="value",
    label="价值型",
    weights={
        "pr": 0.18,
        "pr_percentile": 0.14,
        "pe_percentile": 0.05,
        "pb_percentile": 0.05,
        "roe": 0.15,
        "growth": 0.10,
        "ma_position": 0.05,
        "weekly_trend": 0.05,
        "boll_position": 0.08,
        "volume_follow": 0.05,
        "main_inflow": 0.05,
        "north_bound": 0.05,
    },
    params={
        "stop_kind": "pct",
        "stop_pct": 0.08,
        "atr_multiplier": 2.0,
        "entry_rule": "near_fair_low",
        "entry_band_pct": 0.05,
        "percentile_entry_max": 20.0,
        "percentile_window_years": 5,
        "fair_pr_low": 0.8,
        "fair_pr_high": 1.2,
        "target_lookback_days": 250,
        "min_covered_weight": 0.5,
    },
)

#: 趋势型：技术面 58%。它**不问**两样东西：
#: - PE/PB 的历史分位 —— 那是「贵不贵」的问题，不是「动不动」的问题；
#: - 布林位置 —— `scoring.boll_position` 读的是「回调到位没有」（越靠近下轨越高分），
#:   而趋势型要的是「突破上轨没有」，那一条属于 M3 的规则模板（spec §10.3 的
#:   「布林上下轨突破」），不是一个能被同一个权重两面使用的分数。硬塞进来的话，
#:   屏幕上那句「布林位置 15 分」在同一份报告里既是「便宜」又是「太弱」。
TREND = Style(
    name="trend",
    label="趋势型",
    weights={
        "ma_position": 0.18,
        "weekly_trend": 0.24,
        "volume_follow": 0.16,
        "pr": 0.05,
        "pr_percentile": 0.05,
        "roe": 0.05,
        "growth": 0.07,
        "main_inflow": 0.12,
        "north_bound": 0.08,
    },
    params={
        "stop_kind": "weekly_ma20",
        "stop_pct": 0.08,
        "atr_multiplier": 2.0,
        "entry_rule": "weekly_ma20_pullback",
        "entry_band_pct": 0.03,
        "percentile_entry_max": 20.0,
        "percentile_window_years": 5,
        "fair_pr_low": 0.8,
        "fair_pr_high": 1.2,
        "target_lookback_days": 250,
        "min_covered_weight": 0.5,
    },
)

_BUILTIN = (VALUE, TREND)


def style_names() -> tuple[str, ...]:
    return tuple(style.name for style in _BUILTIN)


def get_style(name: str) -> Style:
    """按名字取内置模板。`cli` 不许 import 分析层，所以这份名单也从这个函数出口转一次。"""
    for style in _BUILTIN:
        if style.name == name:
            return style
    raise ValidationError(
        f"不认识的打分模板 {name!r}：内置的是 {', '.join(style_names())}；"
        "复制一份改参数请写在配置里（`valuation.style.<名字>`），不要塞进模板名"
    )
