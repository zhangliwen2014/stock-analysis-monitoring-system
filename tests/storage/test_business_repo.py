"""核心业务表的读写：一票 × 一字段，来源与日期跟着每个字段走（D240）。

这里钉住三件事：

1. **键归一**：`a/600519` 与 `A/600519` 是同一只票，读写共用 `core.codes` 的规则；
2. **幂等**：同一字段重写是覆盖，不是第二行历史；
3. **空值不落行**：清除走 `delete`，读不到行就回落草稿/缺失——写入空串会让
   读取侧以为「用户确认了一个空答案」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sams.core.business import BUSINESS_FIELD_KEYS
from sams.core.errors import ValidationError
from sams.storage.repos.business import CompanyBusinessRepo

ON = date(2026, 10, 31)
AT = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


def save(repo: CompanyBusinessRepo, field: str = "business", value: str = "卖酒", **over):
    kwargs = {
        "source": "user",
        "as_of": ON,
        "confirmed_by": "alice",
        "confirmed_at": AT,
    }
    kwargs.update(over)
    return repo.upsert("A", "600519", field, value, **kwargs)


def test_upsert_is_idempotent_and_reads_back(db):
    repo = CompanyBusinessRepo(db)
    save(repo)
    save(repo, value="卖酒与系列酒")

    rows = repo.all_for("A", "600519")
    assert set(rows) == {"business"}
    assert rows["business"]["value"] == "卖酒与系列酒"
    assert rows["business"]["source"] == "user"
    assert rows["business"]["as_of"] == ON
    assert rows["business"]["confirmed_by"] == "alice"
    assert repo.count() == 1


def test_the_key_is_normalized_on_both_sides(db):
    repo = CompanyBusinessRepo(db)
    repo.upsert(
        "a",
        "sh600519",
        "business",
        "卖酒",
        source="user",
        as_of=ON,
        confirmed_by="alice",
        confirmed_at=AT,
    )
    assert set(repo.all_for("A", "600519")) == {"business"}
    assert repo.all_for("A", "600519")["business"]["market"] == "A"
    assert repo.all_for("A", "600519")["business"]["code"] == "600519"


def test_each_field_carries_its_own_source_and_date(db):
    repo = CompanyBusinessRepo(db)
    save(repo, field="business", source="company_profiles.summary", as_of=date(2026, 10, 20))
    save(repo, field="trend", source="user", as_of=date(2026, 10, 31))

    rows = repo.all_for("A", "600519")
    assert rows["business"]["as_of"] == date(2026, 10, 20)
    assert rows["trend"]["as_of"] == date(2026, 10, 31)


def test_delete_clears_only_that_field(db):
    repo = CompanyBusinessRepo(db)
    save(repo, field="business")
    save(repo, field="trend")

    assert repo.delete("A", "600519", "business") is True
    assert repo.delete("A", "600519", "business") is False
    assert set(repo.all_for("A", "600519")) == {"trend"}


def test_an_unknown_field_is_refused(db):
    with pytest.raises(ValidationError):
        save(repo=CompanyBusinessRepo(db), field="market_share", value="20%")


def test_an_empty_value_is_refused_instead_of_stored(db):
    with pytest.raises(ValidationError):
        save(repo=CompanyBusinessRepo(db), value="   ")


def test_a_value_over_the_limit_is_refused_with_the_field_name(db):
    repo = CompanyBusinessRepo(db)
    with pytest.raises(ValidationError) as caught:
        save(repo, field="customers", value="客" * 2001)
    assert "主要客户" in str(caught.value)
    assert "2000" in str(caught.value)


def test_every_declared_field_can_be_written(db):
    repo = CompanyBusinessRepo(db)
    for key in BUSINESS_FIELD_KEYS:
        save(repo, field=key, value=f"{key} 的值")
    assert set(repo.all_for("A", "600519")) == set(BUSINESS_FIELD_KEYS)
