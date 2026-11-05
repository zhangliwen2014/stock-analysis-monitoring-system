"""巨潮扩的三条能力：公司概况、公告、上市日期（D324 第二刀）。

payload 形状照 11-06 实网录下的列名写死：

* `stock_profile_cninfo(600519)` → 26 列、1 行，`上市日期` 是日期；
* `stock_zh_a_disclosure_report_cninfo(920289, 20261001~20261106)` → 34 行，
  列 `代码/简称/公告标题/公告时间/公告链接`，链接里带 `announcementId`。

这三条都不能沿用「东财 F10 那套字段名」：巨潮给的是中文列，字段漂移的判据也不同。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError, FieldDrift
from sams.datasource.akshare_cninfo import AkshareCninfoAdapter
from sams.datasource.f10 import empty_announcements

START, END = date(2024, 1, 1), date(2024, 12, 31)

PROFILE_COLUMNS = [
    "公司名称",
    "英文名称",
    "曾用简称",
    "A股代码",
    "A股简称",
    "B股代码",
    "B股简称",
    "H股代码",
    "H股简称",
    "入选指数",
    "所属市场",
    "所属行业",
    "法人代表",
    "注册资金",
    "成立日期",
    "上市日期",
    "官方网站",
    "电子邮箱",
    "联系电话",
    "传真",
    "注册地址",
    "办公地址",
    "邮政编码",
    "主营业务",
    "经营范围",
    "机构简介",
]

ANNOUNCEMENT_COLUMNS = ["代码", "简称", "公告标题", "公告时间", "公告链接"]


def _profile_row(**over) -> dict:
    """600519 的真实一行，按需改字段。"""
    row = {
        "公司名称": "贵州茅台酒股份有限公司",
        "英文名称": "Kweichow Moutai Co., Ltd.",
        "曾用简称": None,
        "A股代码": "600519",
        "A股简称": "贵州茅台",
        "B股代码": None,
        "B股简称": None,
        "H股代码": None,
        "H股简称": None,
        "入选指数": "上证50",
        "所属市场": "上交所",
        "所属行业": "酒、饮料和精制茶制造业",
        "法人代表": "张德芹",
        "注册资金": 125619.78,
        "成立日期": date(1999, 11, 20),
        "上市日期": date(2001, 8, 27),
        "官方网站": "www.moutaichina.com",
        "电子邮箱": "mtdm@moutaichina.com",
        "联系电话": "0851-22386035",
        "传真": "0851-22386035",
        "注册地址": "贵州省仁怀市茅台镇",
        "办公地址": "贵州省仁怀市茅台镇",
        "邮政编码": "564501",
        "主营业务": "茅台酒及系列酒的生产与销售",
        "经营范围": "茅台酒及系列酒的生产与销售",
        "机构简介": "贵州茅台酒股份有限公司主营茅台酒及系列酒。",
    }
    row.update(over)
    return row


def _profile(*rows) -> pl.DataFrame:
    return pl.DataFrame([_profile_row(**r) for r in rows] or [], schema=PROFILE_COLUMNS)


def _announcement(code: str, title: str, at: str, ann_id: str) -> dict:
    return {
        "代码": code,
        "简称": "华汇智能",
        "公告标题": title,
        "公告时间": at,
        "公告链接": (
            f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={code}"
            f"&announcementId={ann_id}&orgId=gfbj0874378&announcementTime={at[:10]}"
        ),
    }


def _announcements(*rows) -> pl.DataFrame:
    return pl.DataFrame(list(rows) or [], schema=ANNOUNCEMENT_COLUMNS)


def test_the_profile_maps_chinese_columns_onto_the_standard_schema():
    adapter = AkshareCninfoAdapter(profile_fetch=lambda _code: _profile({}))

    got = adapter.company_profile("A", "600519", START, END)

    assert got.height == 1
    row = got.row(0, named=True)
    assert (row["market"], row["code"]) == ("A", "600519")
    assert row["name"] == "贵州茅台"
    assert row["full_name"] == "贵州茅台酒股份有限公司"
    assert row["industry"] == "酒、饮料和精制茶制造业"
    assert row["exchange"] == "上交所"
    assert row["listed_on"] == date(2001, 8, 27)
    assert row["founded_on"] == date(1999, 11, 20)
    assert (row["legal_person"], row["chairman"]) == ("张德芹", "张德芹")
    assert row["website"] == "www.moutaichina.com"
    assert row["summary"] == "贵州茅台酒股份有限公司主营茅台酒及系列酒。"
    assert row["source"] == "akshare_cninfo"


def test_a_profile_with_zero_rows_is_an_answer_not_a_failure():
    adapter = AkshareCninfoAdapter(profile_fetch=lambda _code: _profile())

    got = adapter.company_profile("A", "600519", START, END)

    assert got.is_empty()
    assert "name" in got.columns


def test_a_profile_without_the_name_column_is_field_drift():
    adapter = AkshareCninfoAdapter(
        profile_fetch=lambda _code: _profile({}).drop("A股简称", "上市日期")
    )

    with pytest.raises(FieldDrift):
        adapter.company_profile("A", "600519", START, END)


def test_announcements_become_the_standard_columns_with_the_id_from_the_link():
    adapter = AkshareCninfoAdapter(
        announcements_fetch=lambda _code, _start, _end: _announcements(
            _announcement("920289", "2026年三季度报告", "2026-10-29 00:00:00", "1225643653"),
            _announcement(
                "920289", "第二届董事会第四次会议决议公告", "2026-10-28 00:00:00", "1225643652"
            ),
        )
    )

    got = adapter.announcements("A", "920289", date(2026, 10, 1), date(2026, 11, 6))

    assert got.columns == list(empty_announcements().columns)
    assert got.height == 2
    first = got.row(0, named=True)
    assert first["art_code"] == "1225643653"
    assert first["published_on"] == date(2026, 10, 29)
    assert first["title"] == "2026年三季度报告"
    assert first["url"].startswith("http://www.cninfo.com.cn/new/disclosure/detail")
    assert first["source"] == "akshare_cninfo"
    # 新的在前
    assert [str(d) for d in got["published_on"]] == ["2026-10-29", "2026-10-28"]


def test_announcements_outside_the_window_are_dropped():
    adapter = AkshareCninfoAdapter(
        announcements_fetch=lambda _code, _start, _end: _announcements(
            _announcement("600519", "今年的公告", "2024-06-01 00:00:00", "1"),
            _announcement("600519", "去年的公告", "2023-06-01 00:00:00", "2"),
        )
    )

    got = adapter.announcements("A", "600519", START, END)

    assert got["title"].to_list() == ["今年的公告"]


def test_an_empty_announcement_table_is_a_zero_row_answer():
    adapter = AkshareCninfoAdapter(
        announcements_fetch=lambda _code, _start, _end: _announcements()
    )

    got = adapter.announcements("A", "600519", START, END)

    assert got.is_empty()
    assert got.columns == list(empty_announcements().columns)


def test_announcements_that_echo_another_code_are_field_drift():
    adapter = AkshareCninfoAdapter(
        announcements_fetch=lambda _code, _start, _end: _announcements(
            _announcement("000001", "串包的一行", "2024-06-01 00:00:00", "1")
        )
    )

    with pytest.raises(FieldDrift):
        adapter.announcements("A", "600519", START, END)


def test_announcements_without_the_link_column_cannot_yield_an_art_code():
    frame = _announcements(
        _announcement("600519", "今年的公告", "2024-06-01 00:00:00", "1")
    ).drop("公告链接")
    adapter = AkshareCninfoAdapter(announcements_fetch=lambda _c, _s, _e: frame)

    with pytest.raises(FieldDrift):
        adapter.announcements("A", "600519", START, END)


def test_listed_date_comes_from_the_same_profile_answer():
    adapter = AkshareCninfoAdapter(profile_fetch=lambda _code: _profile({}))

    assert adapter.listed_date("A", "600519") == date(2001, 8, 27)


def test_listed_date_is_none_when_the_source_has_no_profile():
    adapter = AkshareCninfoAdapter(profile_fetch=lambda _code: _profile())

    assert adapter.listed_date("A", "600519") is None


def test_listed_date_failure_is_not_mistaken_for_no_listing_date():
    def crash(_code):
        raise TimeoutError("connection timed out")

    adapter = AkshareCninfoAdapter(profile_fetch=crash)

    with pytest.raises(DatasourceError):
        adapter.listed_date("A", "600519")
