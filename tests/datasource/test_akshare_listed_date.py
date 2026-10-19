"""上市日期（U18）：D37 那条上市豁免终于有了机器来源。

A 股这一格取自东财 F10 的公司概况。为什么不用 akshare 封装好的
`stock_individual_info_em`：它打的是 `push2.eastmoney.com` 的行情快照，本环境实测
不可达（三次 `ConnectionError: Remote end closed connection without response`），
而同一份「上市时间」在 `datacenter.eastmoney.com/securities` 的报告接口上有
（实测 601088 → 2007-10-09，与交易所公告一致）。形态与本模块
`eastmoney_says_no_records` 一致：直接问一次报告接口，只认那一列。

下面喂的是录制 payload，联网那一半打了 `live` 标记：接口改版由那一半出声。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError, FieldDrift
from sams.datasource.akshare_cn import AkshareAdapter, parse_listing_response
from sams.datasource.capabilities import provided_by

LISTED = date(2007, 10, 9)


def profile(**over) -> pl.DataFrame:
    """`RPT_F10_BASIC_ORGINFO` 里本系统要的那一列（源给的日期带 00:00:00）。"""
    row = {"SECUCODE": "601088.SH", "LISTING_DATE": "2007-10-09 00:00:00"}
    row.update(over)
    return pl.DataFrame([row])


def adapter(frame=None, *, raises=None):
    calls: list[str] = []

    def fetch(symbol: str) -> pl.DataFrame:
        calls.append(symbol)
        if raises is not None:
            raise raises
        return profile() if frame is None else frame

    return AkshareAdapter(listing_fetch=fetch), calls


def test_the_listing_date_comes_from_the_f10_profile():
    made, calls = adapter()
    assert made.listed_date("A", "601088") == LISTED
    # F10 那一族要 `601088.SH`：给它裸代码回的是空表，而空在这条路上是「查无记录」
    assert calls == ["601088.SH"]


def test_no_record_is_a_none_not_an_error():
    """「这个源没有这只票的上市日期」是一句答复，不是故障：下一个源还可以有。"""
    made, _ = adapter(frame=pl.DataFrame())
    assert made.listed_date("A", "601088") is None


def test_a_profile_without_the_listing_column_is_field_drift():
    """整列消失 = 接口改版。填个 null 继续走，等于把「我们没这一列」说成「这只票没日期」。"""
    made, _ = adapter(frame=pl.DataFrame([{"SECUCODE": "601088.SH"}]))
    with pytest.raises(FieldDrift, match="上市日期"):
        made.listed_date("A", "601088")


def test_an_unparseable_listing_date_is_not_a_listing_date():
    made, _ = adapter(frame=profile(LISTING_DATE="待定"))
    assert made.listed_date("A", "601088") is None


def test_other_markets_are_refused():
    """A 股源的号段规则不该被拿去问港股：那边要的是 `00700.HK` 形态。"""
    made, _ = adapter()
    with pytest.raises(ValueError, match="不支持"):
        made.listed_date("HK", "00700")


def test_a_transport_failure_is_classified():
    made, _ = adapter(raises=ConnectionError("Remote end closed connection"))
    with pytest.raises(DatasourceError):
        made.listed_date("A", "601088")


def test_akshare_declares_the_capability_and_tencent_does_not():
    """能力探针按方法存在与否判断（D38）：这一格进了 `CAPABILITIES`，健康页才说得出谁给。"""
    from sams.datasource.tencent import TencentAdapter

    assert "listed_date" in provided_by(AkshareAdapter)
    assert "listed_date" not in provided_by(TencentAdapter)


def test_the_report_says_no_record_for_this_code():
    """`code=9201 返回数据为空` 是一句答复：这只票在这张表里没有记录（实测 999999.SH）。"""
    body = {
        "version": None,
        "result": None,
        "success": False,
        "message": "返回数据为空",
        "code": 9201,
    }
    assert parse_listing_response(body, symbol="999999.SH").is_empty()


def test_a_report_that_does_not_exist_is_not_a_missing_listing_date():
    """`9501 报表配置不存在` 是接口在抱怨，不是这只票没有日子：填 null 继续走会把改版
    说成「这只票查无上市日期」，而屏幕上这两句话长得一模一样。"""
    body = {
        "version": None,
        "result": None,
        "success": False,
        "message": "报表配置不存在,RPT_F10_BASIC_ORGINFO",
        "code": 9501,
    }
    with pytest.raises(FieldDrift, match="报表|拒绝"):
        parse_listing_response(body, symbol="601088.SH")


def test_the_rows_are_handed_over_untouched():
    row = {"SECUCODE": "601088.SH", "LISTING_DATE": "2007-10-09 00:00:00"}
    frame = parse_listing_response({"success": True, "result": {"data": [row]}}, symbol="601088.SH")
    assert frame["LISTING_DATE"][0] == "2007-10-09 00:00:00"


@pytest.mark.live
def test_the_live_profile_agrees_with_the_exchange():
    """601088 的上市日是 2007-10-09（上交所公告）。这一条只在联网时跑。"""
    assert AkshareAdapter().listed_date("A", "601088") == LISTED
