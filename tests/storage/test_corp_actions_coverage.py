"""覆盖区间仓库（spec D36）。

这块的唯一职责是让「没查到除权」与「没去查」在库里长得不一样。
用例围绕一件事：区间不能虚报。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo

D1 = date(2024, 6, 14)
D2 = date(2025, 6, 13)


@pytest.fixture
def repo(db) -> CorpActionsCoverageRepo:
    return CorpActionsCoverageRepo(db)


def record(repo, market="A", code="601088", *, start=D1, end=D2, full=True, src="akshare"):
    repo.record(market, code, covered_from=start, covered_to=end, full_history=full, src=src)


def test_an_unrecorded_code_has_no_coverage(repo):
    assert repo.get("A", "601088", src="akshare") is None
    assert repo.for_code("A", "601088") == []


def test_recording_is_idempotent_and_last_write_wins(repo):
    record(repo, src="akshare")
    record(repo, src="akshare")
    assert repo.count() == 1, "重复确认是同一条记录的刷新，不是第二条覆盖面"


def test_touching_windows_merge_into_one_span(repo):
    record(repo, start=date(2024, 1, 1), end=date(2024, 6, 14), src="akshare")
    record(repo, start=date(2024, 6, 10), end=date(2025, 6, 13), src="akshare")
    got = repo.get("A", "601088", src="akshare")
    assert (got.covered_from, got.covered_to) == (date(2024, 1, 1), date(2025, 6, 13))


def test_a_disjoint_window_narrows_rather_than_claiming_the_gap(repo):
    """两段互不相干的窗口拼起来像一个大窗口，中间那段其实没人看过。"""
    record(repo, start=date(2015, 1, 1), end=date(2015, 6, 14), src="akshare")
    record(repo, start=date(2024, 1, 1), end=date(2025, 6, 13), src="akshare")
    got = repo.get("A", "601088", src="akshare")
    assert got.covered_from == date(2024, 1, 1), "虚报覆盖面比漏报更糟：守卫会据此放行错数"
    assert got.covered_to == date(2025, 6, 13)


def test_full_history_survives_a_later_narrow_fetch(repo):
    """「拿到过全历史」是源说过的话，不该因为后来只拉了一段就被忘掉。"""
    record(repo, start=date(2010, 5, 1), end=date(2025, 6, 13), full=True, src="akshare")
    record(
        repo, start=date(2024, 1, 1), end=date(2024, 6, 14), full=False, src="akshare"
    )
    assert repo.get("A", "601088", src="akshare").full_history is True


def test_each_source_keeps_its_own_span(repo):
    record(repo, src="akshare", start=date(2010, 1, 1), end=date(2025, 6, 13))
    record(repo, src="eastmoney", start=date(2020, 1, 1), end=date(2024, 1, 1))
    spans = {row.src: row.covered_from for row in repo.for_code("A", "601088")}
    assert spans == {"akshare": date(2010, 1, 1), "eastmoney": date(2020, 1, 1)}, (
        "两个源确认过的区间不同，合成一段等于造出谁都没看过的覆盖面"
    )


def test_codes_are_canonicalised_on_the_way_in(repo):
    """和 bars 同一个形态，否则 `HK00700` 确认过、`00700` 查不到。"""
    record(repo, market="HK", code="HK00700", src="yfinance")
    assert repo.get("HK", "00700", src="yfinance") is not None


def test_reversed_span_is_rejected(repo):
    with pytest.raises(ValidationError, match="覆盖区间反了"):
        record(repo, start=D2, end=D1, src="akshare")


def test_sourceless_coverage_is_useless_and_rejected(repo):
    with pytest.raises(ValidationError, match="src"):
        record(repo, src="")
