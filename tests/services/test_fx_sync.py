"""`FxSyncService`：全链没答上不许落库，答上了才一行行写（D36/D43 的同一条分界）。

这里用**真的** `AdapterRegistry`（只把适配器换成假的），因为这条链最要紧的三条行为
都在注册表里：谁被跳过、谁救了场、谁说「没有」算一句可写的确认而谁的沉默不算。
在服务层假造一个链，这三条就只是我在替实现讲故事。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import (
    DatasourceError,
    EmptyResponse,
    FieldDrift,
    RateLimited,
    TransientError,
)
from sams.datasource.fx import normalize_fx
from sams.datasource.registry import AdapterRegistry
from sams.storage.db import Database
from sams.storage.repos.fx import FxRepo

D1, D2 = date(2024, 10, 24), date(2024, 10, 25)


class _Ok:
    """给一段：`fx_rate` 只被问一次，问的是整段区间，答复也覆盖整段。"""

    name = "fake_fx"
    markets = ("US",)

    def __init__(self, rate: float = 7.1, *, days=(D1, D2)) -> None:
        self._rate = rate
        self._days = days
        self.asked: list[tuple[date, date]] = []

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        self.asked.append((start, end))
        return normalize_fx(
            pl.DataFrame(
                {
                    "date": [row.isoformat() for row in self._days],
                    "base": [base] * len(self._days),
                    "quote": [quote] * len(self._days),
                    "rate": [str(self._rate)] * len(self._days),
                }
            ),
            base=base,
            quote=quote,
            source=self.name,
        )


class _SaysNo:
    """正常应答但没有这一段：与「挂了」是两句话（D43）。"""

    name = "says_no"
    markets = ("US",)

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        raise EmptyResponse(self.name, f"{start}~{end} 没有 {base}→{quote} 的报价")


class _Down:
    name = "broken"
    markets = ("US",)

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        raise TransientError("broken", "503")


class _Limited:
    """429：和抖动一样值得下轮再试，和改版一样不该被当成结论。"""

    name = "limited"
    markets = ("US",)

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        raise RateLimited("limited", "Too Many Requests")


class _Timeouting:
    name = "timeouting"
    markets = ("US",)

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        raise TimeoutError("timed out")


class _Drifted:
    name = "drifted"
    markets = ("US",)

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        raise FieldDrift("drifted", "没有 Close 列")


class _NoFx:
    """注册进来的一个源，但它压根没有 `fx_rate` 这个方法（D38 要出声的那种缺）。"""

    name = "no_fx"
    markets = ("US",)


def _registry(*adapters) -> AdapterRegistry:
    """假源不走市场路由（外汇没有市场维度），但注册表的构造校验要一份合法路由表。"""
    by_name = {a.name: a for a in adapters}
    return AdapterRegistry(
        adapters=by_name,
        market_sources={"US": tuple(by_name)},
        sleep=lambda seconds: None,
    )


def _service(*adapters, db: Database):
    from sams.services.fx import FxSyncService

    return FxSyncService(_registry(*adapters), FxRepo(db))


def test_a_working_source_lands_rows_in_fx_rates(db: Database) -> None:
    source = _Ok()
    summary = _service(source, db=db).sync("USD", "CNY", D1, D2)
    assert summary.written == summary.fetched == 2
    assert FxRepo(db).get(D1, "USD", "CNY").rate == pytest.approx(7.1)
    assert FxRepo(db).get(D2, "USD", "CNY").rate == pytest.approx(7.1)
    # 整段区间问一次，不按日循环：按日循环把一次限流风险变成三十次
    assert source.asked == [(D1, D2)]


def test_rows_outside_the_requested_window_are_not_written(db: Database) -> None:
    """源给的窗口常比我们问的宽：多出来的日子落进库，会让人以为那段也同步过。"""
    wide = _Ok(days=(D1, D2, date(2024, 10, 26)))
    summary = _service(wide, db=db).sync("USD", "CNY", D1, D2)
    assert summary.written == 2
    assert FxRepo(db).get(date(2024, 10, 26), "USD", "CNY") is None


def test_the_source_that_answered_is_recorded_with_the_rows(db: Database) -> None:
    """数的出处要跟着数一起出门（D54）：手工覆盖之前得知道被覆盖的是哪一家的数。"""
    _service(_Ok(), db=db).sync("USD", "CNY", D1, D2)
    assert FxRepo(db).get(D1, "USD", "CNY").source == "fake_fx"


def test_a_backup_that_answers_is_reported_as_degraded(db: Database) -> None:
    summary = _service(_Down(), _Ok(), db=db).sync("USD", "CNY", D1, D2)
    assert summary.degraded is True
    assert summary.source == "fake_fx"
    assert summary.written == 2


def test_a_source_without_the_capability_is_skipped_not_called(db: Database) -> None:
    """链上跳过它并说明原因，而不是抛「没这个方法」；一个能给的时候照样落库。"""
    summary = _service(_NoFx(), _Ok(), db=db).sync("USD", "CNY", D1, D2)
    assert summary.written == 2
    assert summary.skipped == (("no_fx", "不提供汇率"),)


def test_the_sources_behind_the_one_that_answered_are_not_asked(db: Database) -> None:
    """答上了就收敛：排在后面的源一个请求都不发。多打那几次不是「顺便热身」，是行为改变。"""
    behind = _Ok()
    # 注册表按名字建索引：两个假源同名会互相覆盖，那个「没被问到」的必须是另一个名字
    behind.name = "backup_fx"
    _service(_Ok(), behind, db=db).sync("USD", "CNY", D1, D2)
    assert behind.asked == []


def test_a_chain_with_no_fx_source_says_so_loudly(db: Database) -> None:
    """一个能给汇率的源都没有：这是要说出口的缺，不是「0 条」。

    后者会被读成「这段没有汇率」，于是持仓那一屏的 `fx_pnl` 静默变成 0。
    """
    with pytest.raises(Exception, match="汇率") as exc:
        _service(_NoFx(), db=db).sync("USD", "CNY", D1, D2)
    assert "不提供汇率" in str(exc.value)


def test_every_source_down_is_an_error_not_a_zero(db: Database) -> None:
    """全链都没答上：这是「取不到」，不许被写成一行 rate=0 落进库里。"""
    with pytest.raises(TransientError):
        _service(_Down(), db=db).sync("USD", "CNY", D1, D2)
    assert FxRepo(db).get(D1, "USD", "CNY") is None


def test_a_purely_limited_chain_still_says_rerun_later(db: Database) -> None:
    """全是 429 时说成 `TransientError`：调度器看到的是「下轮再试」，不是「采集链坏了」。

    这条是本机实际会撞上的形状（Yahoo 对本机长期 429），把它报成硬失败会让健康页
    一直红着，而红着的东西没人看。
    """
    with pytest.raises(TransientError, match="429|Too Many|限流|limited"):
        _service(_Limited(), db=db).sync("USD", "CNY", D1, D2)


def test_one_drift_in_the_chain_is_not_reported_as_transient(db: Database) -> None:
    """掺一个改版进来就不再是「待会儿重跑就好」：重跑不会让 Yahoo 把列名改回来。"""
    with pytest.raises(DatasourceError) as exc:
        _service(_Limited(), _Drifted(), db=db).sync("USD", "CNY", D1, D2)
    assert not isinstance(exc.value, TransientError)
    assert "drifted" in str(exc.value)


def test_a_bare_timeout_still_says_rerun_later(db: Database) -> None:
    """裸超时被兜底包过一层，也要按原始类型判成抖动，而不是链坏了。"""
    with pytest.raises(TransientError, match="TimeoutError"):
        _service(_Timeouting(), db=db).sync("USD", "CNY", D1, D2)


def test_every_source_saying_no_is_a_confirmed_empty(db: Database) -> None:
    """每个有能力的源都独立答了「没有」：这才可以写成一次空，而且要说有几分。"""
    summary = _service(_SaysNo(), db=db).sync("USD", "CNY", D1, D2)
    assert summary.empty is True
    assert summary.written == 0
    assert summary.confirmed_empty_by == ("says_no",)
    assert FxRepo(db).get(D1, "USD", "CNY") is None


def test_a_silence_does_not_earn_an_empty_answer_the_right(db: Database) -> None:
    """一家说「没有」、另一家挂了：后者没答上，就不许把前者的「没有」当成结论。"""
    with pytest.raises(TransientError):
        _service(_SaysNo(), _Down(), db=db).sync("USD", "CNY", D1, D2)
    assert FxRepo(db).get(D1, "USD", "CNY") is None


def test_running_the_same_range_twice_writes_the_same_rows(db: Database) -> None:
    """重跑不该多出或改数：`fx_rates` 的主键是 (date, base, quote)。"""
    service = _service(_Ok(), db=db)
    service.sync("USD", "CNY", D1, D2)
    second = service.sync("USD", "CNY", D1, D2)
    assert second.written == 2
    assert FxRepo(db).get(D2, "USD", "CNY").rate == pytest.approx(7.1)


def test_the_latest_day_travels_with_the_summary(db: Database) -> None:
    """`sams health` 用它判断汇率新不新：拿不到最后一天就只能报「不知道几天了」。"""
    summary = _service(_Ok(), db=db).sync("USD", "CNY", D1, D2)
    assert summary.latest_date == D2
