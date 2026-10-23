from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sams.datasource.quote import (
    QUOTE_BATCH,
    chunks,
    echo_matches,
    field_float,
    has_quotes,
    parse_moment,
)


class WithQuotes:
    def quotes(self, market, codes):
        ...


class WithoutQuotes:
    pass


def test_the_probe_follows_the_method_not_the_class_name():
    assert has_quotes(WithQuotes())
    assert has_quotes(WithQuotes)  # 注册时只有类，也要能判
    assert not has_quotes(WithoutQuotes())


def test_batches_are_cut_at_the_declared_size():
    items = [str(i) for i in range(QUOTE_BATCH + 3)]
    parts = chunks(items, QUOTE_BATCH)
    assert [len(p) for p in parts] == [QUOTE_BATCH, 3]
    assert sum(parts, []) == items
    with pytest.raises(ValueError, match="size"):
        chunks(items, 0)


def test_echo_must_name_the_code_we_asked_for():
    assert echo_matches("A", "600519", "600519")
    assert echo_matches("A", "600519", "sh600519")
    assert echo_matches("HK", "700", "00700")
    assert echo_matches("US", "AAPL", "AAPL.OQ")
    assert not echo_matches("A", "600519", "600518")
    assert not echo_matches("US", "AAPL", "MSFT.OQ")
    assert not echo_matches("HK", "00700", "")
    # 同类股只差尾部段：裸代码相同而股类不同，回显比对不能把它们折成一只
    assert echo_matches("US", "BRK.A", "BRK.A.N")
    assert not echo_matches("US", "BRK.A", "BRK.B")
    assert not echo_matches("US", "BRK.A", "BRK.B.N")
    assert not echo_matches("US", "BRK.A", "BRK")


def test_field_float_never_turns_garbage_into_zero():
    assert field_float(["", "3.5", "N/A", "nan"], 1) == 3.5
    assert field_float(["", "3.5"], 0) is None
    assert field_float(["", "3.5"], 99) is None
    assert field_float(["", "N/A"], 1) is None
    assert field_float(["", "nan"], 1) is None


def test_parse_moment_tries_the_market_formats_in_order():
    zone = ZoneInfo("Asia/Shanghai")
    moment = parse_moment("20261023103000", ("%Y%m%d%H%M%S",), zone)
    assert moment == datetime(2026, 10, 23, 2, 30, tzinfo=UTC)
    assert parse_moment("not-a-time", ("%Y%m%d%H%M%S",), zone) is None
