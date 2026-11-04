"""核心业务六字段：草稿从哪来、保存后来源怎么变、空值如何清除（D240）。

这一屏最怕的不是「没填」，而是**把草稿冒充事实**：概况摘要与营收代理都是系统猜的，
确认之前必须在屏幕上带「待确认」；用户逐字确认草稿时来源不能摇身变成 `user`，
否则下一个人会以为那句话是人写的。空值也不是「回答了空」——它删除那一行，
让字段回到草稿/缺失。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from support import FrozenClock

from sams.core.business import BUSINESS_FIELD_KEYS, SOURCE_COMPUTED, SOURCE_PROFILE_SUMMARY
from sams.core.errors import ValidationError
from sams.core.missing import NOT_COVERED, NOT_DISCLOSED
from sams.services.business import BusinessProfileService
from sams.storage.repos.business import CompanyBusinessRepo
from sams.storage.repos.f10 import CompanyProfileRepo

DAY = date(2026, 10, 30)
FETCHED = date(2026, 10, 20)
NOW = datetime(2026, 10, 30, 8, 30, tzinfo=UTC)
SUMMARY = "公司主要业务是茅台酒及系列酒的生产与销售。"
POSITION = "营收规模位于三级行业「食品饮料-饮料-白酒」6 家中的第 1 位（超过 83% 的同行）"


@dataclass
class FakePeers:
    """只回答「行业地位那句草稿」：PeerService 自己的行为在 test_peers_service 里钉。"""

    position: str = POSITION
    missing: str = ""

    def compare(self, market: str, code: str, *, as_of: date | None = None):
        return SimpleNamespace(position=self.position, position_missing=self.missing)


def service(db, *, peers: FakePeers | None = None) -> BusinessProfileService:
    return BusinessProfileService(
        CompanyBusinessRepo(db),
        CompanyProfileRepo(db),
        peers if peers is not None else FakePeers(),
        clock=FrozenClock(NOW),
    )


def profile(db, *, summary: str | None = SUMMARY, code: str = "600519") -> None:
    CompanyProfileRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "name": "贵州茅台",
                    "summary": summary,
                    "source": "eastmoney_f10",
                }
            ]
        )
    )
    db.execute(
        "UPDATE company_profiles SET fetched_at = ? WHERE market = ? AND code = ?",
        [datetime(2026, 10, 20, 8, 0, tzinfo=UTC), "A", code],
    )


def six(over: dict[str, str] | None = None) -> dict[str, str]:
    values = {
        "business": "卖酒，靠品牌与渠道赚取毛利",
        "revenue_mix": "茅台酒 84%，系列酒 15%",
        "customers": "前五大客户占比 12%",
        "capacity": "设计产能 4.5 万吨，利用率约 100%",
        "position": "高端白酒市占率第一",
        "trend": "行业增速放缓，集中度继续提升",
    }
    values.update(over or {})
    return values


def test_without_saved_rows_the_six_fields_are_drafts_or_missing(db):
    profile(db)

    read = service(db).read("A", "600519")

    assert [field.spec.key for field in read.fields] == list(BUSINESS_FIELD_KEYS)
    by_key = {field.spec.key: field for field in read.fields}

    business = by_key["business"]
    assert business.value == SUMMARY
    assert business.source == SOURCE_PROFILE_SUMMARY
    assert business.as_of == FETCHED
    assert business.draft is True and business.confirmed is False

    position = by_key["position"]
    assert position.value == POSITION
    assert position.source == SOURCE_COMPUTED
    assert position.as_of == DAY
    assert position.draft is True and position.confirmed is False

    for key in ("revenue_mix", "customers", "capacity", "trend"):
        field = by_key[key]
        assert field.value is None
        assert field.missing_kind == NOT_DISCLOSED
        assert "尚未录入" in field.missing_reason


def test_a_profile_without_a_summary_leaves_business_not_disclosed(db):
    profile(db, summary=None)

    business = service(db).read("A", "600519").fields[0]

    assert business.value is None
    assert business.missing_kind == NOT_DISCLOSED
    assert "公司概况没有公司简介" in business.missing_reason


def test_the_summary_draft_stops_at_a_sentence_and_at_200_chars(db):
    profile(db, summary="甲" * 90 + "。" + "乙" * 300)

    business = service(db).read("A", "600519").fields[0]

    assert business.value == "甲" * 90 + "。"


def test_a_summary_longer_than_200_without_a_sentence_is_cut_at_200(db):
    profile(db, summary="甲" * 500)

    business = service(db).read("A", "600519").fields[0]

    assert business.value == "甲" * 200


def test_a_position_that_cannot_be_computed_is_not_a_draft(db):
    profile(db)
    peers = FakePeers(position="同业对比 v0 只覆盖 A 股", missing=NOT_COVERED)

    position = service(db, peers=peers).read("A", "600519").fields[4]

    assert position.value is None
    assert position.missing_kind == NOT_COVERED
    assert position.draft is False


def test_saving_six_fields_reads_back_source_date_and_confirmer(db):
    profile(db)

    service(db).save("A", "600519", six(), user_id="alice")

    read = service(db).read("A", "600519")
    for field in read.fields:
        assert field.confirmed is True, field.spec.key
        assert field.draft is False, field.spec.key
        assert field.source == "user", field.spec.key
        assert field.as_of == DAY, field.spec.key
        assert field.confirmed_by == "alice", field.spec.key
        assert field.confirmed_at is not None, field.spec.key
        assert field.source_label == "用户录入", field.spec.key
    values = {field.spec.key: field.value for field in read.fields}
    assert values["customers"] == "前五大客户占比 12%"


def test_confirming_the_drafts_verbatim_keeps_their_sources(db):
    profile(db)

    service(db).save(
        "A",
        "600519",
        six({"business": SUMMARY, "position": POSITION}),
        user_id="alice",
    )

    by_key = {field.spec.key: field for field in service(db).read("A", "600519").fields}
    assert by_key["business"].source == SOURCE_PROFILE_SUMMARY
    assert by_key["business"].as_of == FETCHED
    assert by_key["position"].source == SOURCE_COMPUTED
    assert by_key["position"].as_of == DAY
    assert by_key["business"].confirmed is True


def test_a_changed_value_becomes_a_user_answer(db):
    profile(db)

    service(db).save("A", "600519", six({"business": "我改过的说法"}), user_id="alice")

    business = service(db).read("A", "600519").fields[0]
    assert business.value == "我改过的说法"
    assert business.source == "user"
    assert business.as_of == DAY


def test_reconfirming_an_old_user_answer_keeps_its_original_date(db):
    profile(db)
    service(db).save("A", "600519", six(), user_id="alice")
    later = BusinessProfileService(
        CompanyBusinessRepo(db),
        CompanyProfileRepo(db),
        FakePeers(),
        clock=FrozenClock(datetime(2026, 11, 15, 8, 30, tzinfo=UTC)),
    )

    later.save("A", "600519", six(), user_id="bob")

    business = later.read("A", "600519").fields[0]
    assert business.as_of == DAY, "逐字未改就不该把来源日期推后"
    assert business.confirmed_by == "bob", "确认人是最后一次动作的人"


def test_a_blank_value_clears_the_field_back_to_its_draft_or_missing(db):
    profile(db)
    service(db).save("A", "600519", six(), user_id="alice")

    service(db).save("A", "600519", six({"customers": "  ", "business": ""}), user_id="alice")

    by_key = {field.spec.key: field for field in service(db).read("A", "600519").fields}
    assert by_key["customers"].value is None
    assert by_key["customers"].missing_kind == NOT_DISCLOSED
    assert by_key["business"].value == SUMMARY, "清除后回到概况草稿，而不是留一个空回答"
    assert by_key["business"].draft is True


def test_a_value_over_the_limit_is_rejected_with_its_label(db):
    profile(db)

    with pytest.raises(ValidationError, match="『收入构成』最多 2000 字"):
        service(db).save("A", "600519", six({"revenue_mix": "数" * 2001}), user_id="alice")


def test_an_unknown_field_is_rejected_before_anything_is_written(db):
    profile(db)

    with pytest.raises(ValidationError, match="bogus"):
        service(db).save("A", "600519", {"bogus": "x"}, user_id="alice")

    assert CompanyBusinessRepo(db).count() == 0


def test_values_are_stored_trimmed_and_market_code_are_canonical(db):
    profile(db)

    service(db).save("a", "sh600519", six({"trend": "  集中度提升  "}), user_id="alice")

    stored = CompanyBusinessRepo(db).all_for("A", "600519")
    assert stored["trend"]["value"] == "集中度提升"
