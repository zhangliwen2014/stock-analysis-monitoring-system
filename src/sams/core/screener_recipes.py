"""推荐选股组合：把知名投资者的公开原则翻成词表里的条件（D221）。

它们**不落库**：所有用户看到同一份，不需要种子数据，也不会随某个人的编辑而变。
「存为我的方案」走 D220 的 per-user 方案，从此与这份目录分开——目录升级不该
悄悄改掉用户已经存下的那句话。

每条都必须在测试里过 `validate`：配方是代码，拼错一个字，用户点下去才炸就太晚了。
依据与局限跟条件写在一起：只给条件不给出处，等于把一个人的判断冒充成系统结论。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.core.screener_fields import Condition


@dataclass(frozen=True)
class Recipe:
    key: str
    name: str
    investor: str
    market: str
    conditions: tuple[Condition, ...]
    #: 一句话：筛什么
    description: str
    #: 依据：这条原则从哪来、怎么转译
    basis: str
    #: 局限：哪里不适用、哪里是近似
    caveat: str


RECIPES: tuple[Recipe, ...] = (
    Recipe(
        key="buffett",
        name="巴菲特：高 ROE 少负债",
        investor="沃伦·巴菲特",
        market="A",
        conditions=(
            Condition("roe", ">=", 0.15),
            Condition("gross_margin", ">=", 0.35),
            Condition("debt_ratio", "<=", 0.55),
        ),
        description=(
            "年报 ROE ≥ 15%、毛利率 ≥ 35%、资产负债率 ≤ 55%："
            "赚钱能力强、有定价权、不靠高杠杆。"
        ),
        basis=(
            "巴菲特反复讲的「高股东权益回报率 + 低负债 + 持久护城河」；"
            "毛利率是定价权的粗代理，不是护城河的证明。"
        ),
        caveat="不看估值。高 ROE 也可能来自长期高杠杆或一次性收益，要读财报确认。",
    ),
    Recipe(
        key="graham",
        name="格雷厄姆：安全边际",
        investor="本杰明·格雷厄姆",
        market="A",
        conditions=(
            Condition("pe_ttm", "between", (0.01, 15.0)),
            Condition("pb", "between", (0.01, 1.5)),
            Condition("debt_ratio", "<=", 0.50),
            Condition("current_ratio", ">=", 1.5),
        ),
        description=(
            "PE(TTM) ≤ 15、PB ≤ 1.5、资产负债率 ≤ 50%、流动比率 ≥ 1.5："
            "便宜，且有资产与流动性安全垫。"
        ),
        basis=(
            "《聪明的投资者》防御型投资者的量化门槛：PE×PB 不超过 22.5，"
            "再要求流动比率与负债约束。"
        ),
        caveat=(
            "银行、地产、公用事业等以负债为经营方式的行业不适用；"
            "负 PE/PB 的票记「无数」出局。"
        ),
    ),
    Recipe(
        key="lynch",
        name="林奇：成长与价格匹配",
        investor="彼得·林奇",
        market="A",
        conditions=(
            Condition("peg", "<=", 1.0),
            Condition("profit_yoy", ">=", 0.15),
            Condition("debt_ratio", "<=", 0.60),
        ),
        description="PEG ≤ 1 且净利同比 ≥ 15%、资产负债率 ≤ 60%：增长要快，价格别跑到增长前面。",
        basis=(
            "林奇用「市盈率 ÷ 盈利增速」给成长股定价；他偏好增长可理解、能持续的公司，"
            "而不是一次性爆发。"
        ),
        caveat=(
            "PEG 用最近一期年报同比，不预测未来；周期股的高增速常常不可持续，"
            "PEG 会显得过好。"
        ),
    ),
    Recipe(
        key="neff",
        name="涅夫：低市盈率高股息",
        investor="约翰·涅夫",
        market="A",
        conditions=(
            Condition("pe_ttm", "between", (0.01, 12.0)),
            Condition("dividend_yield", ">=", 0.03),
            Condition("profit_yoy", ">=", 0.05),
        ),
        description="PE(TTM) ≤ 12、股息率 ≥ 3%、净利同比 ≥ 5%：便宜、有现金回报，盈利还在增长。",
        basis=(
            "涅夫的低市盈率策略：买 PE 低于市场、股息率高于平均、盈利仍在增长的公司。"
        ),
        caveat="高股息可能只是股价下跌抬出来的；分红能否持续要看现金流，本站暂未把分红率入选股词表。",
    ),
    Recipe(
        key="greenblatt",
        name="格林布拉特：便宜的好生意",
        investor="乔尔·格林布拉特",
        market="A",
        conditions=(
            Condition("roic", ">=", 0.15),
            Condition("pe_ttm", "between", (0.01, 15.0)),
        ),
        description="ROIC ≥ 15%、PE(TTM) ≤ 15：高资本回报 + 低估值，神奇公式的两端。",
        basis=(
            "格林布拉特《股市稳赚》按资本回报率与盈利收益率双排序；"
            "这里用 ROIC 与 PE 倒数做两条门槛。"
        ),
        caveat="神奇公式用的是 EBIT/EV，本系统的 ROIC/PE 只是近似，也没有做行业中性。",
    ),
    Recipe(
        key="pr",
        name="市赚率：PR ≤ 0.5",
        investor="本站市赚率口径",
        market="A",
        conditions=(
            Condition("pr_now", "<=", 0.5),
            Condition("roe", ">=", 0.10),
        ),
        description="现值 PR ≤ 0.5 且年报 ROE ≥ 10%：以五折的市赚率买会赚钱的公司。",
        basis=(
            "本站既有的市赚率口径（D177）：PR = PE(动)² ÷ (100×PB)，"
            "小于 1 表示价格低于按 ROE 估的价值。"
        ),
        caveat=(
            "PR 分母的 ROE 是价格推出来的现值口径，与年报 ROE 不同口径；"
            "亏损或净资产为负的票没有 PR。"
        ),
    ),
)


def recipes_for(market: str) -> tuple[Recipe, ...]:
    return tuple(item for item in RECIPES if item.market == market)
