"""市赚率（PR）：把「多少倍市盈率」换算成「为一块钱利润付几块钱」。

`PR = PE ÷ ROE ÷ 100`，在 ROE 是小数口径（D58）时，PE=20、ROE=20% 得 1.00——「1 上下」
就是这条公式的刻度：低于 1 是「付不到一块钱买一块钱利润」，高于 1 是溢价。

PE 不为正时（亏损、困境反转、周期股在周期底部）市盈率本身失效，改用现值 PB 配
**预估** ROE：`PR = PB ÷ ROE ÷ ROE ÷ 100`。它不是另一条独立公式，而是把 `PE ≈ PB ÷ ROE`
代回原式——所以两支的口径必须一致（同一个 ROE 定义、同一个小数口径），否则两条支
算出来的数不可比，而「PR ≤ 0.4 选股」正是要把它们放在一起排序的。

这个模块只吃参数、只出结论，不碰数据库：谁给它 PE、什么时候给、给哪一期的 ROE，
是 `services` 层的事（分层守卫：analysis 不许自己去库里看一眼）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: `MarketProfitRatio.basis` 的三个取值。调用方比字符串，不比文案
BASIS_PE = "pe"
BASIS_PB_ESTIMATE = "pb_estimate"
BASIS_REFUSED = "refused"


@dataclass(frozen=True)
class MarketProfitRatio:
    """一次 PR 计算的结果。`value` 为 None 时 `reason` 一定非空——没有「算不出且不知道为什么」。"""

    value: float | None
    #: 这个数来自哪条公式（`BASIS_*`）；跨 basis 的 PR 不可直接排序（D59）
    basis: str
    #: 实际用进去的那个 ROE（小数口径）。打印时跟着数走：读的人要能验算
    roe_used: float | None = None
    #: True ⇒ ROE 是人给的预估，不是已披露的那一期
    estimated: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None

    def explain(self) -> str:
        """跟着数出门的那句话（D54：造成这个数的事，得和数一起被看见）。"""
        if self.value is None:
            return self.reason
        if self.basis == BASIS_PE:
            return f"PE ÷ ROE ÷ 100，ROE={self.roe_used:.4f}（已披露，小数口径）"
        tail = (
            "（预估，非已披露）"
            if self.estimated
            else "（最近一期已披露，用于替代失效的 PE）"
        )
        return f"PB ÷ ROE ÷ ROE ÷ 100，ROE={self.roe_used:.4f}{tail}"


def _refused(reason: str) -> MarketProfitRatio:
    return MarketProfitRatio(None, BASIS_REFUSED, reason=reason)


def pr(
    *,
    pe_ttm: float | None = None,
    pb: float | None = None,
    roe: float | None = None,
    roe_estimate: float | None = None,
) -> MarketProfitRatio:
    """市赚率。两条分支、四种算不出，全在这里，别处不再算第二遍。"""
    if pe_ttm is not None and pe_ttm > 0:
        if roe is None or roe <= 0:
            return _refused(
                f"PE(TTM)={pe_ttm:g} 可用，但 ROE {'缺失' if roe is None else f'为 {roe:g}'}："
                "分母没有，PR 无从谈起——先补这只票的财务指标（`sams sync financials`）"
            )
        return MarketProfitRatio(pe_ttm / roe / 100.0, BASIS_PE, roe_used=roe)

    # 到这儿是 PE 为负、为零，或源压根没给。三种情形的下一步不一样，所以说清是哪一种
    missing = "PE(TTM) 缺失（源没给这一列）" if pe_ttm is None else f"PE(TTM)={pe_ttm:g} 不为正"
    if pb is None or pb <= 0:
        return _refused(
            f"{missing}，而 PB {'缺失' if pb is None else f'为 {pb:g}'}："
            "这一支要现值 PB 才能算（先同步估值数据：`sams sync valuation`）"
        )
    used = roe_estimate if roe_estimate is not None else roe
    if used is None or used <= 0:
        named = (
            f"预估 ROE={roe_estimate:g}" if roe_estimate is not None else "没有可用 ROE 预估"
        )
        return _refused(
            f"{missing}，PB={pb:g}，{named}：这一支算的是「扭亏后能赚多少」，"
            "缺一个为正的 ROE 就没有 PR 可给（传 `--roe-estimate 0.15` 这种小数）"
        )
    return MarketProfitRatio(
        pb / used / used / 100.0,
        BASIS_PB_ESTIMATE,
        roe_used=used,
        # 只有真给了预估值才算「预估」：回落到已披露 ROE 时，那个数是有出处的
        estimated=roe_estimate is not None,
    )
