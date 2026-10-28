"""基本面/估值/资金流的采集契约（D38、D43、D58 在采集层的落点）。

这里钉住的是三件事，每一件都对应一种会静默出错的后果：

1. **单位**（D58）。源按百分数给（ROE 18.32 = 18.32%），库里只能是小数（0.1832）。
   不换算的症状不是报错，而是市赚率算出 PR=0.01 并把高 ROE 的票排进「便宜」前列。
   `percent_columns` 因此是**必填**：默认值意味着「忘了声明」也是一种声明，而两种源
   （akshare 百分数、yfinance 小数）都真实存在。
2. **0 行不是一次成功的取数**（D43）。日线的 0 行是「这个区间确实没交易」，基本面的
   0 行只是「这只票在那张表里没记录」——把它当成答复写进覆盖标记，等于宣布一家公司
   没有财务报表，之后所有「查无基本面」的结论都建立在这次沉默上。
3. **能力按方法判断**（D38）。只有日线的源在基本面链上被显式跳过并说明原因，
   而不是静默失败——「这个市场没有任何源能给基本面」必须与「这些票都没数据」区分开。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.core.fundamentals import (
    FINANCIAL_COLUMNS,
    FLOW_COLUMNS,
    RATIO_COLUMNS,
    VALUATION_COLUMNS,
)
from sams.datasource.capabilities import CAPABILITIES
from sams.datasource.fundamentals import (
    empty_financials,
    empty_flow,
    empty_valuation,
    has_financials,
    has_flow,
    has_valuation,
    normalize_financials,
    normalize_flow,
    normalize_valuation,
)
from sams.datasource.registry import AdapterRegistry

D1, D2 = date(2024, 1, 2), date(2024, 3, 31)
Q1 = date(2024, 3, 31)
Q4 = date(2023, 12, 31)

#: 源侧列名 → 标准列名：中文列是 akshare 的形状，故意与标准词不同，重命名才是被测的那一步
FINANCIAL_MAP = {
    "report_date": "报告期",
    "published_date": "披露日",
    "roe": "净资产收益率",
    "revenue_yoy": "营收同比",
    "profit_yoy": "利润同比",
    "dividend_yield": "股息率",
    "market_cap": "总市值",
}


def raw_financials(**over) -> pl.DataFrame:
    """一期 2024Q1 + 一期 2023Q4，比率按**百分数**给。"""
    rows = [
        {
            "报告期": "20240331",
            "披露日": "2024-04-26",
            "净资产收益率": 18.32,
            "营收同比": 12.0,
            "利润同比": 8.0,
            "股息率": 4.2,
            "总市值": 6.6e11,
        },
        {
            "报告期": "2023-12-31",
            "披露日": "2024-03-20",
            "净资产收益率": 24.11,
            "营收同比": 0.0,
            "利润同比": None,
            "股息率": 5.1,
            "总市值": 6.4e11,
        },
    ]
    rows[0].update(over)
    return pl.DataFrame(rows)


def normalized_financials() -> pl.DataFrame:
    return normalize_financials(
        raw_financials(),
        market="A",
        code="601088",
        source="akshare",
        column_map=FINANCIAL_MAP,
        percent_columns=RATIO_COLUMNS,
    )


def by_period(out: pl.DataFrame, column: str) -> dict[date, object]:
    """按报告期取值：归一后是**时间升序**（与日线一致），而用例关心的是哪一期等于多少。"""
    return dict(zip(out["report_date"], out[column], strict=True))


def test_normalize_moves_ratios_to_the_decimal_caliber_and_fills_the_key():
    out = normalized_financials()
    assert out.columns == list(FINANCIAL_COLUMNS)
    assert by_period(out, "roe")[Q1] == pytest.approx(0.1832)
    assert by_period(out, "roe")[Q4] == pytest.approx(0.2411)
    assert by_period(out, "dividend_yield")[Q1] == pytest.approx(0.042)
    # 金额不是比率：÷100 只发生在比率列上，市值动了 100 倍就是一个错到没人会怀疑的数
    assert by_period(out, "market_cap")[Q1] == 6.6e11
    assert out["market"].to_list() == ["A", "A"]
    assert out["source"].to_list() == ["akshare", "akshare"]


def test_both_date_spellings_parse_to_the_same_period_and_publication():
    """'20240331' 与 '2024-04-26' 是同一次报告的两个日期、两种写法，都要认。"""
    out = normalized_financials()
    assert by_period(out, "published_date")[Q1] == date(2024, 4, 26)
    assert by_period(out, "published_date")[Q4] == date(2024, 3, 20)


def test_zero_stays_zero_while_unknown_stays_unknown():
    """0 是「真的没增长」，null 是「源没给」——归一若把 null 折成 0，选股就静默排除掉一批票。"""
    out = normalized_financials()
    assert by_period(out, "revenue_yoy")[Q4] == 0.0
    assert by_period(out, "profit_yoy")[Q4] is None


def test_a_source_that_already_gives_fractions_can_declare_so():
    """yfinance 的 ROE 本来就是小数（0.1832）：再 ÷100 会得到一只「几乎不赚钱」的票。"""
    out = normalize_financials(
        raw_financials(**{"净资产收益率": 0.1832}),
        market="A",
        code="601088",
        source="yfinance",
        column_map=FINANCIAL_MAP,
        percent_columns=(),
    )
    assert by_period(out, "roe")[Q1] == pytest.approx(0.1832)


def test_a_missing_required_column_is_field_drift_naming_the_actual_columns():
    """列名改了要在采集层就炸，而不是入库后变成「这只票的 ROE 一直是空」。"""
    with pytest.raises(FieldDrift, match="报告期"):
        normalize_financials(
            raw_financials().drop("报告期"),
            market="A",
            code="601088",
            source="akshare",
            column_map=FINANCIAL_MAP,
            percent_columns=RATIO_COLUMNS,
        )


def test_an_absent_publication_date_is_null_and_said_aloud(caplog):
    """披露日整列没了不能丢行（那些 ROE 是真数），但也不能静默：PIT 读取会把它们全部挡掉。"""
    with caplog.at_level("WARNING"):
        out = normalize_financials(
            raw_financials().drop("披露日"),
            market="A",
            code="601088",
            source="akshare",
            column_map=FINANCIAL_MAP,
            percent_columns=RATIO_COLUMNS,
        )
    assert out["published_date"].to_list() == [None, None]
    assert out.height == 2, "少一列披露日不该连报告期一起丢"
    assert "披露日" in caplog.text


def test_a_row_without_a_usable_period_is_dropped_not_stored():
    """报告期是主键的一半：空着的那一行存进去就再也读不到，只会涨行数。"""
    out = normalize_financials(
        pl.concat(
            [raw_financials(), raw_financials().with_columns(pl.lit(None).alias("报告期"))]
        ),
        market="A",
        code="601088",
        source="akshare",
        column_map=FINANCIAL_MAP,
        percent_columns=RATIO_COLUMNS,
    )
    assert out.height == 2


def test_the_key_is_canonical_on_the_way_out():
    """`hk`/`700` 出去要变成 `HK`/`00700`：K 线与基本面分两种键形时，
    市赚率配不上对，症状是「这只票算不出 PR」。"""
    out = normalize_financials(
        raw_financials(),
        market="hk",
        code="700",
        source="akshare_hk",
        column_map=FINANCIAL_MAP,
        percent_columns=RATIO_COLUMNS,
    )
    assert (out["market"][0], out["code"][0]) == ("HK", "00700")


def test_valuation_and_flow_normalize_to_their_own_vocabulary():
    val = normalize_valuation(
        pl.DataFrame(
            {
                "日期": ["2024-06-28"],
                "市盈率TTM": [18.5],
                "市净率": [2.1],
                "股息率": [4.2],
                "总市值": [6.6e11],
            }
        ),
        market="A",
        code="601088",
        source="akshare",
        column_map={
            "date": "日期",
            "pe_ttm": "市盈率TTM",
            "pb": "市净率",
            "dividend_yield": "股息率",
            "market_cap": "总市值",
        },
        percent_columns=("dividend_yield",),
    )
    assert val.columns == list(VALUATION_COLUMNS)
    assert val["pe_ttm"].to_list() == [18.5], "倍数不 ÷100"
    assert val["dividend_yield"].to_list() == [pytest.approx(0.042)]

    flow = normalize_flow(
        pl.DataFrame({"日期": ["2024-06-28"], "主力净流入": [1.2e8]}),
        market="A",
        code="601088",
        source="akshare",
        column_map={"date": "日期", "main_net_in": "主力净流入"},
        percent_columns=(),
    )
    assert flow.columns == list(FLOW_COLUMNS)
    assert flow["main_net_in"].to_list() == [1.2e8]
    assert flow["north_net"].to_list() == [None], "源没给的列填 null，不是 0"


@pytest.mark.parametrize(
    ("maker", "columns"),
    [
        (empty_financials, FINANCIAL_COLUMNS),
        (empty_valuation, VALUATION_COLUMNS),
        (empty_flow, FLOW_COLUMNS),
    ],
)
def test_the_empty_answer_has_the_same_shape_as_a_real_one(maker, columns):
    """调用方不该为「这个源一只票都没给」写分支：空帧的列集与真帧一致。"""
    assert maker().columns == list(columns)
    assert maker().height == 0


def test_the_three_new_kinds_are_registered_as_capabilities():
    assert {"financials", "valuation", "funding_flow"} <= set(CAPABILITIES)
    assert [capability.label for capability in CAPABILITIES.values()] == [
        "日线",
        "公司行为",
        "基本面",
        "按日估值",
        "资金流",
        # 主档的一格，不是第四类数据：它不走降级链，但「谁能给」仍只有这一份判据（U18）
        "上市日期",
        # 汇率也没有市场维度，走 `registry.fetch_fx` 那条独立链；登记在这里是为了让
        # 「谁能给汇率」与其余五种共用同一份判据（D38）
        "汇率",
        # 报价没有窗口，一次问一批（U18）
        "报价",
        # SP1 两种（D148）：代码表一次答全市场，指数日线与日线同窗不同表
        "代码表",
        "指数日线",
        # 计划 B 两种：分钟粒度不落库，只有读端（D163）
        "分时",
        "分钟 K",
        # 计划 D：一次答一个市场的横截面，不按票走降级链（D173）
        "全市场快照",
    ]


def test_probes_follow_the_method_not_the_class():
    class OnlyValuation:
        def valuation(self, market, code, start, end):
            ...

    stub = OnlyValuation()
    assert has_valuation(stub) and not has_financials(stub) and not has_flow(stub)


class Scripted:
    """一个按脚本应答的方法，方法名由调用方指定，**其余能力一律没有**。

    能力探针按方法存在与否判断，所以桩必须能只长出某一个方法：给桩加上 `daily_bars`
    就等于把它变成三类数据都能取的源，链的跳过逻辑当场就测不到了。
    """

    def __init__(self, name: str, method: str, *outcomes, markets=("A",)) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.outcomes = list(outcomes)
        self.calls = 0

        def respond(market, code, start, end):
            self.calls += 1
            outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        setattr(self, method, respond)


def registry(*adapters) -> AdapterRegistry:
    by_name = {a.name: a for a in adapters}
    return AdapterRegistry(
        adapters=by_name,
        market_sources={"A": tuple(by_name)},
        sleep=lambda seconds: None,
    )


def test_a_bars_only_source_is_skipped_and_says_why():
    bars_only = Scripted("sina", "daily_bars", pl.DataFrame())
    fin = Scripted("akshare", "financials", normalized_financials())
    result = registry(bars_only, fin).fetch_financials("A", "601088", D1, D2)
    assert result.source == "akshare"
    assert result.skipped == (("sina", "不提供基本面"),)
    assert bars_only.calls == 0, "没有这个能力的源一次都不该被调用"


def test_a_zero_row_financials_answer_keeps_the_chain_asking():
    """0 行不算答复（D43）：sina 说「没记录」时还得问 akshare，而不是就此交付一个空。"""
    quiet = Scripted("sina", "financials", empty_financials())
    full = Scripted("akshare", "financials", normalized_financials())
    result = registry(quiet, full).fetch_financials("A", "601088", D1, D2)
    assert (quiet.calls, full.calls, result.source) == (1, 1, "akshare")
    assert result.confirmed_empty_by == ()
    assert result.degraded


def test_only_a_chain_where_every_source_answered_no_can_confirm_the_absence():
    a = Scripted("sina", "financials", empty_financials())
    b = Scripted("akshare", "financials", empty_financials())
    result = registry(a, b).fetch_financials("A", "601088", D1, D2)
    assert result.confirmed_empty_by == ("sina", "akshare")
    assert result.frame.height == 0


def test_one_silence_voids_every_other_no():
    """一个源抛 EmptyResponse（接口回空/解析不出）时，别个的「查无记录」还不能写成确认。

    否则「sina 挂了 + akshare 说没有」会被记成「确认 601088 没有财务数据」，
    而真相是我们只问到了一个可能同样不可靠的源（D36 的同一条线）。
    """
    silent = Scripted("sina", "financials", EmptyResponse("sina", "接口回空"))
    quiet = Scripted("akshare", "financials", empty_financials())
    with pytest.raises(DatasourceError, match="akshare"):
        registry(silent, quiet).fetch_financials("A", "601088", D1, D2)


def test_all_sources_failing_raises_with_the_chain_and_the_skipped_names():
    broken = Scripted("sina", "financials", DatasourceError("sina", "503"))
    bars_only = Scripted("tencent", "daily_bars", pl.DataFrame())
    with pytest.raises(DatasourceError) as exc:
        registry(broken, bars_only).fetch_financials("A", "601088", D1, D2)
    text = str(exc.value)
    assert "503" in text and "tencent" in text


@pytest.mark.parametrize(
    ("method", "maker"),
    [("valuation", empty_valuation), ("funding_flow", empty_flow)],
)
def test_valuation_and_flow_chains_beat_the_same_way_as_the_financials_one(method, maker):
    """三条新链共用同一条降级逻辑（T-0 收敛的意义）：语义按种类分，走法不分叉。

    方法名 = 能力名 = `fetch_<方法名>`，所以这里不需要任何映射表——加一种数据只要
    在 `CAPABILITIES` 里登记，链、健康检查、这条断言都自动跟上新名字。
    """
    quiet = Scripted("sina", method, maker())
    full = Scripted("akshare", method, maker())
    result = getattr(registry(quiet, full), f"fetch_{method}")("A", "601088", D1, D2)
    assert result.confirmed_empty_by == ("sina", "akshare")
