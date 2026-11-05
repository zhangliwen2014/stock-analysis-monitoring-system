"""巨潮历史分红（`p_sysapi1139`）→ 标准公司行为列（D318）。

用例里的每一行都是 11-05 实网探测录下的原始 payload 字段（`tmp/probe_383_cninfo_dump.py`，
输出留在 `tmp/cninfo_dump.txt`）：

| 代码   | 实施方案分红说明        | 送股 | 转增   | 派息    | 除权日     |
| ------ | ----------------------- | ---- | ------ | ------- | ---------- |
| 920289 | 「10转增12.5397股」     | —    | 12.5397| —       | 2024-07-17 |
| 920238 | 「10派5.2元(含税)」     | —    | —      | 5.2     | 2025-05-30 |
| 600519 | 「10派280.2423元(含税)」| —    | —      | 280.2423| 2026-06-26 |
| 600519 | 「10转增3股派3元(含税)」| —    | 3.0    | 3.0     | 2004-07-01 |
| 600519 | 「10送1股派2元(含税)」  | 1.0  | —      | 2.0     | 2003-07-14 |

引进它只为一件事：同花顺对北交所不写 A 股除权日（D316），而交易所自己的披露平台写了
920289 那一次 10 转 12.5397。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.akshare_cninfo import (
    AkshareCninfoAdapter,
    akshare_cninfo_plans,
    cninfo_says_no_records,
    expand_cninfo,
)

_EX_DATE = "除权日"
_CASH = "派息比例"
_BONUS = "送股比例"
_TRANSFER = "转增比例"
_SCHEME = "实施方案分红说明"


def _row(**kw) -> dict:
    """920238 那一行的真实形状，按需改字段。"""
    base = {
        "实施方案公告日期": date(2025, 5, 22),
        "分红类型": "年度分红",
        _BONUS: None,
        _TRANSFER: None,
        _CASH: 5.2,
        "股权登记日": date(2025, 5, 29),
        _EX_DATE: date(2025, 5, 30),
        "派息日": date(2025, 5, 30),
        "股份到账日": None,
        _SCHEME: "10派5.2元(含税)",
        "报告时间": "2024年报",
    }
    base.update(kw)
    return base


def _raw(*rows) -> pl.DataFrame:
    schema = {
        "实施方案公告日期": pl.Date,
        "分红类型": pl.Utf8,
        _BONUS: pl.Float64,
        _TRANSFER: pl.Float64,
        _CASH: pl.Float64,
        "股权登记日": pl.Date,
        _EX_DATE: pl.Date,
        "派息日": pl.Date,
        "股份到账日": pl.Utf8,
        _SCHEME: pl.Utf8,
        "报告时间": pl.Utf8,
    }
    return pl.DataFrame(list(rows), schema_overrides=schema)


def _maotai_2026() -> dict:
    """实测 600519 最新一行（公告 2026-06-22、登记 06-25、除权与派息 06-26）。"""
    return _row(
        **{
            "实施方案公告日期": date(2026, 6, 22),
            _CASH: 280.2423,
            "股权登记日": date(2026, 6, 25),
            _EX_DATE: date(2026, 6, 26),
            "派息日": date(2026, 6, 26),
            _SCHEME: "10派280.2423元(含税)",
            "报告时间": "2025年报",
        }
    )


# --- 单位：每 10 股，数值列与说明文本互查（D34） ----------------------------------


def test_a_beijing_transfer_becomes_an_ex_rights_row():
    """实测 920289：`转增比例 12.5397` + 除权日 2024-07-17 ⇒ 乘数 2.25397。

    这只票的同花顺表里躺着同一句方案却不写日期（D316），巨潮写了——引进第三个源就是为了这一行。
    """
    out = expand_cninfo(
        _raw(
            _row(
                **{
                    "实施方案公告日期": date(2024, 7, 9),
                    _TRANSFER: 12.5397,
                    _CASH: None,
                    "股权登记日": date(2024, 7, 16),
                    _EX_DATE: date(2024, 7, 17),
                    "派息日": None,
                    "股份到账日": "2024-07-17",
                    _SCHEME: "10转增12.5397股",
                    "报告时间": "2023年报",
                }
            )
        )
    )
    assert out.select(["ex_date", "kind", "cash_per_share", "ratio"]).to_dicts() == [
        {
            "ex_date": date(2024, 7, 17),
            "kind": "transfer",
            "cash_per_share": None,
            "ratio": pytest.approx(2.25397),
        }
    ]


def test_a_cash_row_is_converted_from_per_ten_to_per_share():
    """实测 920238：`派息比例 5.2` 配「10派5.2元」⇒ 每股 0.52，不是 5.2。

    照抄数值列会把因子大十倍，而价格序列照样单调、图上看不出问题（东财那族同一个坑）。
    """
    out = expand_cninfo(_raw(_row()))
    assert out["cash_per_share"][0] == pytest.approx(0.52)


def test_cninfo_ratios_are_per_ten_shares():
    """实测 600519 最新一行：`派息比例 280.2423` 配「10派280.2423元(含税)」⇒ 每股 28.02423。

    这条钉的是**大数**上的换算：茅台一次派息就够把「每 10 股」这个口径错成「每股」而看不出破绽。
    """
    out = expand_cninfo(_raw(_maotai_2026()))
    assert out["cash_per_share"][0] == pytest.approx(28.02423)


def test_the_ex_rights_date_is_the_anchor_not_the_registration_date():
    """三个日期里只有除权日能当序列上的锚：登记日和派息日都在别的日子（D40 的落点）。

    乘错一天，那天的涨跌就少乘/多乘一次因子，而整条曲线看着仍然连续。
    """
    out = expand_cninfo(_raw(_maotai_2026()))
    assert out["ex_date"].to_list() == [date(2026, 6, 26)]


def test_a_transfer_and_a_dividend_on_one_row_are_two_facts():
    """实测 600519「10转增3股派3元」：一行两个事实 ⇒ 两行。

    主键是 (market, code, ex_date, kind)，塞成一行只能选一种语义，另一种就少乘一次（D34）。
    """
    out = expand_cninfo(
        _raw(
            _row(
                **{
                    "实施方案公告日期": date(2004, 6, 25),
                    _TRANSFER: 3.0,
                    _CASH: 3.0,
                    "股权登记日": date(2004, 6, 30),
                    _EX_DATE: date(2004, 7, 1),
                    "派息日": date(2004, 7, 7),
                    _SCHEME: "10转增3股派3元(含税)",
                    "报告时间": "2003年报",
                }
            )
        )
    )
    assert out.select(["kind", "cash_per_share", "ratio"]).to_dicts() == [
        {"kind": "dividend", "cash_per_share": 0.3, "ratio": None},
        {"kind": "transfer", "cash_per_share": None, "ratio": 1.3},
    ]


def test_bonus_shares_are_a_different_fact_from_transferred_ones():
    """实测 600519「10送1股派2元」的 `送股比例 1.0` ⇒ kind=bonus，不与 transfer 混记。

    两者对价格因子的作用相同，但 `corp_actions.kind` 是给人核对公告用的：把送股写成转增，
    对账时会以为少了一笔公积金转增。
    """
    out = expand_cninfo(
        _raw(
            _row(
                **{
                    "实施方案公告日期": date(2003, 7, 8),
                    _BONUS: 1.0,
                    _CASH: 2.0,
                    "股权登记日": date(2003, 7, 11),
                    _EX_DATE: date(2003, 7, 14),
                    _SCHEME: "10送1股派2元(含税)",
                    "报告时间": "2002年报",
                }
            )
        )
    )
    assert sorted(out["kind"].to_list()) == ["bonus", "dividend"]
    assert out.filter(pl.col("kind") == "bonus")["ratio"][0] == pytest.approx(1.1)


# --- 数值与文本反查：对不上就当场响（D34） ----------------------------------------


def test_a_number_that_disagrees_with_the_written_scheme_fails_loudly():
    """`派息比例 30` 配「10派3元」：两个读数差十倍，猜哪一个都是猜。"""
    with pytest.raises(FieldDrift, match="派息"):
        expand_cninfo(_raw(_row(**{_CASH: 30.0})))


def test_a_scheme_written_per_share_is_a_unit_drift():
    """说明里出现「每股」⇒ 每 10 股这个换算依据本身变了，整批失败而不是继续除 10。"""
    with pytest.raises(FieldDrift, match="每股"):
        expand_cninfo(_raw(_row(**{_SCHEME: "每股派0.52元(含税)"})))


# --- 三态：有、没有、读不出（D316/D318） ------------------------------------------


def test_an_empty_response_frame_answers_that_there_was_nothing():
    """`records: []` 经适配器变成空帧：这是源自己说的一句「这里没有」。"""
    assert expand_cninfo(pl.DataFrame()).is_empty()


def test_an_undated_row_does_not_discard_the_dated_ones():
    """一行没日期，其余行照样能用：整批判「读不出」会把 30 条除权换成一条都不入库。

    实测 11-05 探到的 7 只票（600519/600000/000001/300750/920238/920289/920116）共 101 行，
    除权日无一为空——所以这一支是防御性的，走的就是同花顺那侧已审过的口径（D316）。
    """
    out = expand_cninfo(_raw(_row(**{_EX_DATE: None}), _maotai_2026()))
    assert out["ex_date"].to_list() == [date(2026, 6, 26)]


def test_a_table_where_no_row_can_be_dated_answers_nothing():
    """一句都定不了位 ⇒ `EmptyResponse`（读不出），不是「查无」（D316）。

    当成「没有」会给这只票写下假覆盖确认——而它明明有分红记录，只是我们读不出日子；
    当成源故障又会让全市场换不到这个源（D279）。
    """
    with pytest.raises(EmptyResponse, match="除权日"):
        expand_cninfo(_raw(_row(**{_EX_DATE: None})))


def test_a_dated_row_with_no_numbers_is_kept_as_unknown():
    """有除权日、三个数值全空：这一天确实动了东西，只是我们折不出数 ⇒ 记 `unknown`（D39）。

    丢掉它等于替这只票签一句「这些日子没发生过除权」，而读取层见到 unknown 是拒绝出数。
    """
    out = expand_cninfo(_raw(_row(**{_CASH: None, _SCHEME: "分红方案待披露"})))
    assert out.select(["ex_date", "kind"]).to_dicts() == [
        {"ex_date": date(2025, 5, 30), "kind": "unknown"}
    ]


def test_a_scheme_that_says_no_distribution_is_a_complete_answer():
    """「不分配不转增」不是折不出数的事件：它不该把整只票拖成读不出，也不该留一行 unknown。"""
    out = expand_cninfo(_raw(_row(**{_CASH: None, _SCHEME: "不分配不转增"})))
    assert out.is_empty()


def test_a_frame_without_the_ratio_columns_is_a_redesign():
    """这三列是这条链唯一的数据来源，整列没了就是改版。"""
    drift = pl.DataFrame([{"实施方案公告日期": date(2025, 5, 22), _EX_DATE: date(2025, 5, 30)}])
    with pytest.raises(FieldDrift, match="缺少必需列"):
        expand_cninfo(drift)


# --- 「查无记录」与「崩了」的区分（照东财那一族的做法） ----------------------------


def test_empty_records_from_cninfo_answer_nothing_not_a_crash(monkeypatch):
    """akshare 的重命名对空帧不生效，随后 `temp_df["实施方案公告日期"]` 抛 KeyError。

    实测 920162 / 001246 / 301666：HTTP 200、resultcode 200、`records: []`——那是一句「没有」。
    """
    import akshare

    def empty(symbol: str) -> pl.DataFrame:
        raise KeyError("实施方案公告日期")

    monkeypatch.setattr(akshare, "stock_dividend_cninfo", empty, raising=False)
    out = akshare_cninfo_plans("920162", is_no_records=lambda symbol: True)
    assert out.is_empty()


def test_a_crash_that_is_not_a_no_records_answer_still_raises(monkeypatch):
    """分不清就照故障处理：静默当成「没有记录」会把一只分过红的票抹平。"""
    import akshare

    def empty(symbol: str) -> pl.DataFrame:
        raise KeyError("实施方案公告日期")

    monkeypatch.setattr(akshare, "stock_dividend_cninfo", empty, raising=False)
    with pytest.raises(KeyError):
        akshare_cninfo_plans("920162", is_no_records=lambda symbol: False)


# --- 「查无记录」那一句要问得出来（#360 同一族） -----------------------------------


def test_the_no_records_verdict_comes_from_the_envelope_not_the_crash():
    """实测 920162 / 001246 / 301666：HTTP 200、`resultcode: 200`、`records: []` ⇒ 是一句「没有」。

    这一句决定了覆盖标记能不能写：`KeyError` 本身两种情况都长一样（#360 在东财那侧记过一次）。
    """

    def empty_envelope(symbol: str) -> dict:
        return {"resultcode": 200, "resultmsg": "success", "records": []}

    assert cninfo_says_no_records("920162", post=empty_envelope) is True


def test_records_present_means_the_crash_was_not_a_no_records_answer():
    """表里有行却走到这条路上，那是解析崩了：不许被读成「这家公司没分过红」。"""

    def non_empty(symbol: str) -> dict:
        return {"resultcode": 200, "records": [{"F020D": "2024-07-17"}]}

    assert cninfo_says_no_records("920289", post=non_empty) is False


def test_an_unreadable_envelope_is_not_a_no_records_answer():
    """问不出结论（非 200、被限流、JSON 坏了）时返回 False，让原异常说话。"""

    def http_500(symbol: str) -> dict:
        return {"resultcode": 500, "records": []}

    def quota(symbol: str) -> dict:
        return {"error": "quota exceeded"}

    assert cninfo_says_no_records("920162", post=http_500) is False
    assert cninfo_says_no_records("920162", post=quota) is False


def test_a_probe_that_raises_is_not_a_no_records_answer():
    """探针自己崩了（拿不到 cninfo.js、连不上网）⇒ False：分不清就照故障处理。"""

    def boom(symbol: str) -> dict:
        raise OSError("connection reset")

    assert cninfo_says_no_records("920162", post=boom) is False


# --- 适配器：只有公司行为、给全史（D38/D40） ---------------------------------------


def _adapter(*rows) -> AkshareCninfoAdapter:
    return AkshareCninfoAdapter(plans_fetch=lambda symbol: _raw(*rows))


def test_the_adapter_is_a_full_history_actions_only_source():
    adapter = _adapter(_row())
    assert adapter.markets == frozenset({"A"})
    assert adapter.corp_actions_full_history is True
    assert not hasattr(adapter, "daily_bars")


def test_the_adapter_gives_back_the_whole_history_not_the_window():
    """切窗口会让库与覆盖标记说反话（D40）：2003 那次除权也要交回去。"""
    rows = _row(
        **{
            "实施方案公告日期": date(2003, 7, 8),
            _BONUS: 1.0,
            _CASH: 2.0,
            _EX_DATE: date(2003, 7, 14),
            _SCHEME: "10送1股派2元(含税)",
        }
    )
    out = _adapter(rows).corp_actions("A", "600519", date(2024, 1, 1), date(2024, 3, 1))
    assert [(row["ex_date"], row["kind"]) for row in out.to_dicts()] == [
        (date(2003, 7, 14), "bonus"),
        (date(2003, 7, 14), "dividend"),
    ]
    assert out["market"].to_list() == ["A", "A"]
    assert set(out["src"].to_list()) == {"akshare_cninfo"}


def test_an_unknown_row_survives_the_adapter_rather_than_becoming_nothing():
    """折不出数的那一行要一路活到库里：交回空帧会被读成「这只票没除权过」并写下确认。"""
    out = _adapter(_row(**{_CASH: None, _SCHEME: "分红方案待披露"})).corp_actions(
        "A", "920238", date(2025, 1, 1), date(2025, 12, 31)
    )
    assert out["kind"].to_list() == ["unknown"]
    assert out["src"].to_list() == ["akshare_cninfo"]


def test_the_adapter_names_the_code_it_was_asked_about():
    """源不回写代码：代码只能取自请求参数，所以要归一后传入、再原样标在行上。"""
    out = _adapter(_row()).corp_actions("A", "920238", date(2025, 1, 1), date(2025, 12, 31))
    assert out["code"].to_list() == ["920238"]


def test_an_unsupported_market_is_refused_before_the_network():
    """这个源只吃 A 股：挂到 HK 链上是路由的事，不是让它去猜一只港股的分红表。"""
    with pytest.raises(ValueError, match="不支持市场"):
        _adapter(_row()).corp_actions("HK", "00700", date(2024, 1, 1), date(2024, 3, 1))
