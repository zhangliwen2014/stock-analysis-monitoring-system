"""检索词归一与候选装配（D141/D142）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.services.symbol_search import (
    Candidate,
    SymbolSearchService,
    hinted_market,
    normalize_query,
)
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbol_list import SymbolListRepo


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("payh", "PAYH"),
        (" 00700 ", "00700"),
        ("hk00700", "00700"),
        ("SH600519", "600519"),
        ("平安", "平安"),
        ("%", None),
        ("", None),
        ("   ", None),
        ("HK", None),  # 前缀吃掉后没剩下东西
        ("HK7", "HK7"),  # 只剩一位不是市场前缀：那是代码本体，不许剥
        ("x" * 100, "X" * 64),
    ],
)
def test_normalize(raw, want):
    assert normalize_query(raw) == want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("hk00700", "HK"),
        ("HK00700", "HK"),
        ("sh600519", "A"),
        ("sz000001", "A"),
        ("bj430047", "A"),
        ("usAAPL", "US"),
        ("00700", None),  # 裸码没有前缀可认
        ("PAYH", None),  # 拼音不是市场字母打头
        ("HK7", None),  # 剥不动的前缀不算提示（与 normalize 同一条「至少剩两位」）
        ("HK", None),
        ("", None),
    ],
)
def test_hinted_market(raw, want):
    assert hinted_market(raw) == want


def _listed(db) -> SymbolListRepo:
    listed = SymbolListRepo(db)
    listed.upsert_batch(
        "A",
        [
            {
                "code": "600519",
                "name": "贵州茅台",
                "py_initials": "gzym",
                "py_full": "guizhoumaotai",
                "currency": "CNY",
            }
        ],
    )
    return listed


def test_search_assembles_close(db):
    svc = SymbolSearchService(_listed(db), BarRepo(db))
    hits = svc.search("gzym")
    assert hits == [Candidate("A", "600519", "贵州茅台", None)]


def test_search_attaches_last_close_from_bars(db):
    bars = BarRepo(db)
    bars.upsert(
        pl.DataFrame(
            {
                "market": ["A", "A"],
                "code": ["600519", "600519"],
                "date": [date(2024, 1, 1), date(2024, 1, 2)],
                "close": [1.0, 1499.5],
                "source": ["akshare", "akshare"],
            }
        )
    )
    svc = SymbolSearchService(_listed(db), bars)
    assert svc.search("600519")[0].close == 1499.5


def test_blank_junk_or_symbol_only_query_does_not_hit_db(db):
    class Boom:
        def search(self, *a, **k):
            raise AssertionError("空检索词不该打库")

    svc = SymbolSearchService(Boom(), BarRepo(db))
    assert svc.search("") == []
    assert svc.search("%%%") == []
    assert svc.search("HK") == []
