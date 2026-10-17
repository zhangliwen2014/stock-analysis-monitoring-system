"""对抗性探针：采集层（commits 14796cf..8fe4ca6 及其后续修复）。

约定：
  * 全部离线：不联网、不睡真实钟；适配器一律注入假 fetch / 录制 payload。
  * 每个用例刻画一条**已经成立的**契约：跑绿即「已核实无误」；
    刻意宽松的地方（例如负成交量原样入库）在用例文档里写明是谁的职责。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import logging
import re
import sys
import types
from datetime import UTC, date
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from support import FrozenClock

from sams.core.clock import SYSTEM_CLOCK
from sams.core.config import MarketCfg, Settings
from sams.core.errors import (
    DatasourceError,
    EmptyResponse,
    FieldDrift,
    RateLimited,
    TransientError,
)
from sams.core.events import EventBus, SystemAlarm
from sams.core.sources import SourceRecord
from sams.datasource.base import (
    BAR_COLUMNS,
    LOTS_TO_SHARES,
    classify_error,
    normalize_bars,
    parses_as_date,
)
from sams.datasource.registry import AdapterRegistry
from sams.storage.repos.source_health import SourceHealthRepo

D1, D2 = date(2024, 1, 2), date(2024, 1, 3)

CN_MAP = {
    "date": "日期",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量",
    "amount": "成交额",
}


# ── 本文件自有帮助件（不动 support.py / conftest.py）─────────────────────────


def cn_raw(**overrides) -> pl.DataFrame:
    base = {
        "日期": ["2024-01-02", "2024-01-03", "2024-01-04"],
        "开盘": [1.0, 2.0, 3.0],
        "最高": [1.5, 2.5, 3.5],
        "最低": [0.5, 1.5, 2.5],
        "收盘": [1.2, 2.2, 3.2],
        "成交量": [100, 200, 300],
        "成交额": [120, 440, 960],
    }
    base.update(overrides)
    return pl.DataFrame(base)


def normalize_cn(frame: pl.DataFrame, **kwargs) -> pl.DataFrame:
    return normalize_bars(
        frame, market="A", code="600519", source="akshare", column_map=CN_MAP, **kwargs
    )


def bars_out(source: str = "akshare", market: str = "A", code: str = "600519") -> pl.DataFrame:
    """已经规范好的帧：registry 用例只关心路由，不关心列从哪来。"""
    return pl.DataFrame(
        {
            "market": [market, market],
            "code": [code, code],
            "date": [D1, D2],
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.5, 1.5],
            "close": [1.2, 2.2],
            "volume": [100.0, 200.0],
            "amount": [120.0, 440.0],
            "source": [source, source],
        }
    )


class Scripted:
    """按脚本应答的适配器：outcomes 一项一次，用尽后重复最后一项。

    配合 `attempts_per_source=1`（见 make_registry 默认值），一次 fetch_daily
    正好消耗一项，失败计数才能和 fetch 次数一一对应。
    """

    def __init__(self, name: str, markets, *outcomes) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.outcomes = list(outcomes)
        self.calls = 0
        self.seen: list[tuple[str, str]] = []

    def daily_bars(self, market, code, start, end):
        self.calls += 1
        self.seen.append((market, code))
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_registry(
    *adapters,
    markets=("A",),
    store=None,
    bus=None,
    sleep=None,
    clock=None,
    attempts_per_source=1,
):
    by_name = {a.name: a for a in adapters}
    return AdapterRegistry(
        adapters=by_name,
        market_sources={m: tuple(by_name) for m in markets},
        sleep=sleep if sleep is not None else (lambda seconds: None),
        store=store,
        bus=bus,
        clock=clock if clock is not None else SYSTEM_CLOCK,
        attempts_per_source=attempts_per_source,
    )


def bus_with_alarms() -> tuple[EventBus, list[SystemAlarm]]:
    bus = EventBus()
    seen: list[SystemAlarm] = []
    bus.subscribe(SystemAlarm, seen.append)
    return bus, seen


def pandas_hist(*, index_name: str = "Date", rows: int = 2) -> object:
    import pandas as pd

    days = [date(2024, 1, 2), date(2024, 1, 3)][:rows]
    frame = pd.DataFrame(
        {
            "Open": [1800.0, 1810.0][: len(days)],
            "High": [1820.0, 1830.0][: len(days)],
            "Low": [1790.0, 1800.0][: len(days)],
            "Close": [1810.0, 1815.0][: len(days)],
            "Volume": [30000.0, 28000.0][: len(days)],
        },
        index=pd.to_datetime([d.isoformat() for d in days]),
    )
    frame.index.name = index_name
    return frame


def column_date_frame() -> object:
    """日期是**普通列**、索引是默认 RangeIndex 的帧（源改了返回形态时长这样）。"""
    import pandas as pd

    return pd.DataFrame(
        {
            "Date": ["2024-01-02", "2024-01-03"],
            "Open": [1.0, 2.0],
            "High": [1.0, 2.0],
            "Low": [1.0, 2.0],
            "Close": [1.0, 2.0],
            "Volume": [10.0, 20.0],
        }
    )


def klines_payload(*lines: str, code: str = "600519") -> dict:
    return {"data": {"code": code, "klines": list(lines)}}


def offline_db():
    import duckdb

    from sams.storage.db import Database
    from sams.storage.migrate import apply_migrations

    db = Database(duckdb.connect(":memory:"))
    apply_migrations(db)
    return db


def settings_for(rate_limit_per_s: float = 1) -> Settings:
    return Settings(
        db_path=":memory:",
        secret_key="x",
        report_currency="CNY",
        rate_limit_per_s=rate_limit_per_s,
        polling_interval_s=10,
        cooldown_min=30,
        markets={
            "A": MarketCfg("A", "Asia/Shanghai", "CNY", 30, ("akshare", "eastmoney")),
            "HK": MarketCfg("HK", "Asia/Hong_Kong", "HKD", 30, ("yfinance",)),
            "US": MarketCfg("US", "America/New_York", "USD", 60, ("yfinance",)),
        },
    )


# ── 1. normalize_bars / volume_scale ─────────────────────────────────────────


def test_null_volume_stays_null_under_lot_scaling_not_zero():
    """缺量 ≠ 零量：把 null 变成 0 会让放量阈值在最安静的日子最响。"""
    out = normalize_cn(cn_raw(**{"成交量": [100, None, 0]}), volume_scale=LOTS_TO_SHARES)
    assert out["volume"].to_list() == [10000.0, None, 0.0]


def test_volume_is_cast_to_float_before_scaling_so_int64_does_not_wrap():
    """换算顺序核对：先 cast Float64 再乘 100，int64 上界不会绕成负数或 null。"""
    huge = 9_223_372_036_854_775_807  # int64 上界
    out = normalize_cn(
        cn_raw(**{"成交量": pl.Series([huge, huge - 1, huge - 2], dtype=pl.Int64)}),
        volume_scale=LOTS_TO_SHARES,
    )
    values = out["volume"].to_list()
    assert out["volume"].dtype == pl.Float64
    assert all(v is not None and v > 0 for v in values), values


def test_tz_aware_date_column_with_one_zone_keeps_the_exchange_local_date():
    """时区核对（单一 tz 列）：取的是**本地日期**，美股收盘不会漂到 UTC 次日。

    跨 DST 切换日也在同一列里，逐行按各自的偏移渲染。
    """
    hk = pl.DataFrame(
        {
            "日期": [
                # 港股 2026-12-24 半交易日 09:30 HKT（= 01:30 UTC，同日；12-25 休市）
                dt.datetime(2026, 12, 24, 9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong")),
                dt.datetime(2026, 12, 22, 9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong")),
            ],
            "收盘": [1.0, 2.0],
        }
    )
    assert normalize_cn(hk)["date"].to_list() == [date(2026, 12, 22), date(2026, 12, 24)]

    us = pl.DataFrame(
        {
            "日期": [
                # 美股 2026-03-09 DST 切换日开盘 09:30 EDT = 13:30 UTC
                dt.datetime(2026, 3, 9, 9, 30, tzinfo=ZoneInfo("America/New_York")),
                # 本地 19:00 EST = UTC 次日 00:00 —— 按 UTC 落戳这里就会多一天
                dt.datetime(2026, 1, 5, 19, 0, tzinfo=ZoneInfo("America/New_York")),
            ],
            "收盘": [1.0, 2.0],
        }
    )
    assert normalize_cn(us)["date"].to_list() == [date(2026, 1, 5), date(2026, 3, 9)]


def test_rows_with_different_offsets_are_rezoned_to_the_first_one():
    """已知边界（当前三个适配器都触发不了）：混 tz 的 datetime 列会被整体改判。

    Polars 一列只有一个时区：用逐行 datetime 构造混合偏移的列时，值被按第一行的
    时区重述，日期随之偏移。yfinance/akshare 都给单一 tz 的索引或字符串，所以今天
    够不到；但 M3 的快照/多市场拼帧一旦这么喂，`date` 就会静默错一天。
    """
    mixed = pl.DataFrame(
        {
            "日期": [
                dt.datetime(2026, 12, 24, 9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong")),
                # 纽约本地 2026-01-05 19:00，本应落 1 月 5 日
                dt.datetime(2026, 1, 5, 19, 0, tzinfo=ZoneInfo("America/New_York")),
            ],
            "收盘": [1.0, 2.0],
        }
    )
    assert mixed["日期"].dtype.time_zone == "Asia/Hong_Kong", "第二行被并进了第一行的时区"
    assert normalize_cn(mixed)["date"].to_list() == [date(2026, 1, 6), date(2026, 12, 24)], (
        "纽约那一行被改判成 HKT 后落到了次日的日期"
    )


def test_nan_close_from_a_source_cell_is_rejected_by_the_discard_rule():
    """丢弃规则查的是 `is_finite` 而不是 `is_not_null`：源单元格写作 'nan' 得到的
    NaN 收盘价与 null 一样被丢——它能过 NOT NULL，也能过 `close > 0`（NaN 比较恒真），
    一旦落库，DuckDB 把 NaN 排在所有数之上，max/均值/排序全被无声污染。
    """
    from sams.datasource.eastmoney import parse_eastmoney_daily

    payload = klines_payload(
        "2024-01-02,1800.00,1810.00,1820.00,1790.00,30000,5400000000.00",
        "2024-01-03,1810.00,nan,1830.00,1800.00,28000,5100000000.00",
    )
    out = parse_eastmoney_daily(payload, market="A", code="600519")
    assert out.height == 1, "NaN 收盘价的行该像 null 收盘价一样被丢弃"
    assert out["close"].to_list() == [1810.0]


def test_nan_close_is_rejected_by_the_storage_guard_too():
    """同一缺陷的落库侧兜底：绕过适配器直接写库时，BarRepo 也只收有限收盘价。"""
    from sams.storage.repos.bars import BarRepo

    db = offline_db()
    frame = normalize_cn(cn_raw(**{"收盘": [1.2, float("nan"), 3.2]}))
    assert BarRepo(db).upsert(frame) == 2
    assert db.scalar("SELECT count(*) FROM bars_daily") == 2
    assert db.scalar("SELECT isnan(max(close)) FROM bars_daily") is False


def test_pandas_nan_cells_become_null_so_the_akshare_path_is_safe():
    """边界收窄：pl.from_pandas 已把 NaN 变 null，所以只有「字符串解析型」源暴露。"""
    import pandas as pd

    from sams.datasource.akshare_cn import AkshareAdapter

    frame = pd.DataFrame(
        {
            "日期": ["2024-01-02", "2024-01-03"],
            "收盘": [1810.0, float("nan")],
            "成交量": [30000.0, float("nan")],
        }
    )
    out = AkshareAdapter(fetch=lambda *_: frame).daily_bars("A", "600519", D1, D2)
    assert out.height == 1


def test_amount_is_never_scaled_and_is_absent_for_us_hk():
    from sams.datasource.yfinance_us import normalize_history

    scaled = normalize_cn(cn_raw(), volume_scale=LOTS_TO_SHARES)
    assert scaled["amount"].to_list() == [120.0, 440.0, 960.0]

    us = normalize_history(pandas_hist(), market="US", code="AAPL")
    assert us["amount"].null_count() == 2
    assert us["volume"].to_list() == [30000.0, 28000.0], "美股已是股，不再乘 100"


def test_missing_amount_lands_as_sql_null_not_zero(db):
    """核实：港美股「无成交额」诚实入库为 NULL，下游金额阈值看到的是无值而不是 0。"""
    from sams.datasource.yfinance_us import normalize_history
    from sams.storage.repos.bars import BarRepo

    BarRepo(db).upsert(normalize_history(pandas_hist(), market="US", code="AAPL"))
    assert db.scalar("SELECT count(*) FROM bars_daily WHERE amount IS NULL") == 2
    assert db.scalar("SELECT count(*) FROM bars_daily WHERE amount = 0") == 0
    assert db.scalar("SELECT count(*) FROM bars_daily WHERE adjust = 'raw'") == 2


def test_duplicate_rows_for_one_date_collapse_keeping_the_later_value_under_scaling():
    base = cn_raw()
    revised = cn_raw(**{"成交量": [111, 200, 300]}).head(1)
    out = normalize_cn(pl.concat([base, revised]), volume_scale=LOTS_TO_SHARES)
    assert out.height == 3, "同一天重复行必须折叠成一条"
    assert out["volume"].to_list() == [11100.0, 20000.0, 30000.0], "keep=last，且只乘一次 100"


def test_rows_come_back_sorted_ascending_even_when_the_source_is_descending():
    """东财实际按倒序给 kline：出库必须升序。"""
    from sams.datasource.eastmoney import parse_eastmoney_daily

    out = parse_eastmoney_daily(
        klines_payload(
            "2024-01-04,1.00,1.10,1.20,0.90,10,100.00",
            "2024-01-02,1.00,1.10,1.20,0.90,10,100.00",
            "2024-01-03,1.00,1.10,1.20,0.90,10,100.00",
        ),
        market="A",
        code="600519",
    )
    assert out["date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]


def test_eastmoney_refuses_a_payload_for_a_different_code():
    """东财响应里带着它认为自己的代码：不核对的话，secid 错位或缓存串包
    会把别的票的 K 线存进本票名下，而下游没有任何一处能发现。
    """
    from sams.datasource.eastmoney import parse_eastmoney_daily

    payload = {"data": {"code": "000001", "klines": ["2024-01-02,1.00,1.20,1.50,0.50,1,120.0"]}}
    with pytest.raises(FieldDrift, match="响应代码"):
        parse_eastmoney_daily(payload, market="A", code="600519")


def test_normalize_bars_stamps_the_requested_code():
    """规范化本身不核对源帧里的代码列（各源列名不同、多数根本不给这一列），
    `code` 一律取请求值——所以串包只能在源侧核对，见上一条与 akshare 的录制形态。
    """
    frame = pl.DataFrame({"日期": ["2024-01-02", "2024-01-02"], "收盘": [1.0, 999.0]})
    out = normalize_cn(frame)
    assert out.height == 1, "同一天折叠成一条，keep=last"
    assert out["code"].to_list() == ["600519"]


# ── 2. yfinance 适配器 ───────────────────────────────────────────────────────


def test_history_end_exclusivity_is_compensated_so_the_last_session_survives(monkeypatch):
    """end 开区间核对：`yfinance_fetch` 已 +1 天，闭区间语义没有吃掉最后一个交易日。"""
    import pandas as pd

    from sams.datasource.yfinance_us import YfinanceAdapter

    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]

    class FakeTicker:
        def __init__(self, symbol) -> None:
            self.symbol = symbol

        def history(self, *, start, end, **kwargs):  # 与真接口一样：[start, end)
            days = [
                d
                for d in sessions
                if dt.date.fromisoformat(start) <= d < dt.date.fromisoformat(end)
            ]
            frame = pd.DataFrame(
                {
                    "Open": [1.0] * len(days),
                    "High": [1.0] * len(days),
                    "Low": [1.0] * len(days),
                    "Close": [1.0] * len(days),
                    "Volume": [1.0] * len(days),
                },
                index=pd.to_datetime([d.isoformat() for d in days]),
            )
            frame.index.name = "Date"
            return frame

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))

    # 先自证替身确实是开区间：不 +1 天就拿不到 1-05
    assert FakeTicker("AAPL").history(start="2024-01-02", end="2024-01-05").index[
        -1
    ].date() == date(2024, 1, 4)

    out = YfinanceAdapter().daily_bars("US", "AAPL", date(2024, 1, 2), date(2024, 1, 5))
    assert out["date"].to_list() == sessions, "区间末日必须入库，否则永远缺最近一天"


@pytest.mark.parametrize("index_name", ["Date", "date", "DATETIME", "index", ""])
def test_date_index_name_variants_are_all_recognised(index_name):
    from sams.datasource.yfinance_us import normalize_history

    assert (
        normalize_history(pandas_hist(index_name=index_name), market="US", code="AAPL").height == 2
    )


def test_unknown_index_name_is_field_drift():
    from sams.datasource.yfinance_us import normalize_history

    with pytest.raises(FieldDrift, match="找不到可解析为日期的列"):
        normalize_history(pandas_hist(index_name="timestamp"), market="US", code="AAPL")


def test_real_date_column_beside_an_integer_index_reports_drift_when_nothing_parses():
    """挑日期列不能只按名字：`DATE_INDEX_NAMES` 含 'index'，reset_index 又把它排在第一列，
    装着 0/1/2 的整数索引会顶掉真正的日期列——「结构漂移」被伪装成「这个区间没有数据」，
    方向完全相反的两种结论。名字像日期还必须真的能解析成日期。
    """
    import pandas as pd

    from sams.datasource.yfinance_us import normalize_history

    frame = pd.DataFrame(
        {"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0], "Close": [1.0, 2.0]},
        index=[0, 1],
    )
    frame.index.name = "Date"
    pl_frame = pl.from_pandas(frame.reset_index())
    assert not parses_as_date(pl_frame, "Date"), "前提是这一列确实解析不出日期"
    with pytest.raises(FieldDrift):
        normalize_history(frame, market="US", code="AAPL")


def test_column_date_frame_is_accepted_and_stays_the_right_length():
    """日期作为普通列（RangeIndex 是默认整数索引）是改版后的正常形态，必须能读出来。"""
    from sams.datasource.yfinance_us import normalize_history

    out = normalize_history(column_date_frame(), market="US", code="AAPL")
    assert out.height == 2
    assert out["date"].to_list() == [D1, D2]


def test_structural_drift_is_a_failure_for_caller_and_health_alike():
    """一次取数不能对调用方说「不是失败」、对健康表说「失败」：
    结构漂移是 FieldDrift（故障），只有 EmptyResponse（停牌/退市/新股）才两边都说「没数据」。
    """
    drift = FieldDrift("yfinance", "US AAPL 找不到可解析为日期的列")
    reg = make_registry(Scripted("yfinance", ["US", "HK"], drift), markets=("HK",))
    with pytest.raises(DatasourceError):
        reg.fetch_daily("HK", "00700", D1, D2)
    assert reg.records["yfinance"].ok is False
    assert reg.records["yfinance"].consecutive_failures == 1


def test_multiindex_history_from_one_ticker_is_flattened():
    import pandas as pd

    from sams.datasource.yfinance_us import flatten_columns, normalize_history

    frame = pandas_hist(rows=1)
    frame.columns = pd.MultiIndex.from_product([frame.columns, ["AAPL"]])
    flatten_columns(frame)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert normalize_history(frame, market="US", code="AAPL").columns == BAR_COLUMNS


def test_hk_code_aliases_collapse_to_one_stored_code():
    """入库的 code 必须是规范形态：`code_in_market` 只归一化取数用的 symbol，
    若入库仍用用户原文，同一只票的 00700 / 700 / HK00700 会存成多份主键。
    """
    from sams.datasource.yfinance_us import YfinanceAdapter
    from sams.storage.repos.bars import BarRepo

    seen: list[str] = []

    def spy(symbol, start, end):
        seen.append(symbol)
        return pandas_hist()

    adapter = YfinanceAdapter(fetch=spy)
    aliases = ["700", "00700", "HK00700", "  00700  "]
    frames = [adapter.daily_bars("HK", alias, D1, D2) for alias in aliases]
    assert set(seen) == {"00700.HK"}, "四个别名取的是同一个 symbol —— 确实是同一只票"

    db = offline_db()
    BarRepo(db).upsert(pl.concat(frames))
    assert db.scalar("SELECT count(*) FROM bars_daily") == 2, "同一只票只该有一份日线"
    assert db.scalar("SELECT count(DISTINCT code) FROM bars_daily") == 1


# ── 3. akshare / eastmoney ───────────────────────────────────────────────────


def test_eastmoney_lots_are_scaled_and_the_non_a_branch_is_unreachable():
    """换算分派核对：A 股 ×100；非 A 市场系数为 1（正确），但适配器不受理非 A。"""
    from sams.datasource.eastmoney import EastmoneyAdapter, parse_eastmoney_daily

    line = "2024-01-02,1800.00,1810.00,1820.00,1790.00,30000,5400000000.00"
    assert parse_eastmoney_daily(klines_payload(line), market="A", code="600519")[
        "volume"
    ].to_list() == [3_000_000.0]
    assert parse_eastmoney_daily(klines_payload(line, code="00700"), market="HK", code="00700")[
        "volume"
    ].to_list() == [30000.0]

    with pytest.raises(ValueError, match="不支持市场"):
        EastmoneyAdapter(get=lambda url, params: klines_payload(line)).daily_bars(
            "HK", "00700", D1, D2
        )


def test_hk_never_routes_through_eastmoney(settings):
    """路由核对：HK/US 唯一源是 yfinance；`secid` 的 116/105 分支是死代码。"""
    from sams.datasource.eastmoney import secid

    assert secid("HK", "00700") == "116.00700"  # 有实现、无入口
    stubs = {
        name: Scripted(name, ["A", "US", "HK"], bars_out(name))
        for name in ("akshare", "eastmoney", "yfinance")
    }
    reg = AdapterRegistry.from_settings(settings, adapters=stubs, bucket=None)
    assert reg.sources_for("HK") == ("yfinance",)
    assert reg.sources_for("US") == ("yfinance",)
    assert reg.sources_for("A") == ("akshare", "eastmoney")


def test_eastmoney_suspension_day_with_zero_volume_is_kept():
    """停牌日（量 0、价平）该留在库里：抹掉它会让缺口看起来像缺数据。"""
    from sams.datasource.eastmoney import parse_eastmoney_daily

    out = parse_eastmoney_daily(
        klines_payload(
            "2024-01-02,10.00,10.00,10.00,10.00,0,0.00",
            "2024-01-03,10.00,10.10,10.20,10.00,12345,123456.00",
        ),
        market="A",
        code="600519",
    )
    assert out.height == 2
    assert out["volume"].to_list() == [0.0, 1_234_500.0]


def test_eastmoney_empty_and_drift_payloads_are_distinguishable():
    from sams.datasource.eastmoney import parse_eastmoney_daily

    with pytest.raises(EmptyResponse):
        parse_eastmoney_daily({"data": {"code": "600519", "klines": []}}, market="A", code="600519")
    with pytest.raises(FieldDrift):
        parse_eastmoney_daily({"data": None}, market="A", code="600519")
    with pytest.raises(FieldDrift, match="字段不足"):
        parse_eastmoney_daily(klines_payload("2024-01-02,1.0,2.0"), market="A", code="600519")


def test_one_renamed_optional_column_nulls_the_field_but_says_so(caplog):
    """只有 date/close 缺列才算漂移（取舍：免费源少给一列成交额是常态，不该丢整天数据）；
    但「整列没了」必须留一行警告——成交量整列改名会让放量/换手阈值从此永不触发，
    静默无声比报错危险得多。
    """
    with caplog.at_level(logging.WARNING, logger="sams.datasource"):
        out = normalize_cn(cn_raw().rename({"成交量": "成交量(手)"}), volume_scale=LOTS_TO_SHARES)
    assert out.height == 3
    assert out["volume"].null_count() == 3
    assert any("成交量" in record.getMessage() for record in caplog.records), (
        "可选列整列消失必须吵"
    )


def test_renamed_optional_column_is_not_warned_when_the_map_omits_it(caplog):
    """yfinance 根本不产出成交额：column_map 里没有这一列，不该每次取数都告警。"""
    from sams.datasource.yfinance_us import normalize_history

    with caplog.at_level(logging.WARNING, logger="sams.datasource"):
        out = normalize_history(pandas_hist(), market="US", code="AAPL")
    assert out["amount"].null_count() == out.height
    assert not [r for r in caplog.records if "缺少可选列" in r.getMessage()]


def test_akshare_adapter_classifies_a_renamed_required_column_as_drift():
    from sams.datasource.akshare_cn import AkshareAdapter

    adapter = AkshareAdapter(fetch=lambda *_: cn_raw().rename({"日期": "trade_date"}))
    with pytest.raises(FieldDrift):
        adapter.daily_bars("A", "600519", D1, D2)


# ── 4. registry：降级、计数、告警 ────────────────────────────────────────────


def test_failures_without_a_bus_still_count_and_never_crash():
    """regression 位：bus=None 时 `_mark_failure` 必须照常计数（离线/单测路径）。"""
    reg = make_registry(Scripted("akshare", ["A"], RateLimited("akshare", "429")))
    for _ in range(5):
        with pytest.raises(DatasourceError):
            reg.fetch_daily("A", "600519", D1, D2)
    assert reg.records["akshare"].consecutive_failures == 5
    assert reg.records["akshare"].ok is False


def test_alarm_fires_exactly_once_across_five_consecutive_failures():
    bus, seen = bus_with_alarms()
    reg = make_registry(Scripted("akshare", ["A"], RateLimited("akshare", "429")), bus=bus)
    for _ in range(5):
        with pytest.raises(DatasourceError):
            reg.fetch_daily("A", "600519", D1, D2)
    assert len(seen) == 1, "阈值那一次响一声，之后不该再刷屏"
    assert "连续 3 次失败" in seen[0].message
    assert reg.records["akshare"].consecutive_failures == 5


def test_alarm_rearms_after_an_intervening_success():
    bus, seen = bus_with_alarms()
    good = bars_out()
    failing = RateLimited("akshare", "429")
    reg = make_registry(
        Scripted("akshare", ["A"], failing, failing, failing, good, failing, failing, failing),
        bus=bus,
    )
    for _ in range(7):
        with contextlib.suppress(DatasourceError):
            reg.fetch_daily("A", "600519", D1, D2)
    assert [a.component for a in seen] == ["datasource:akshare", "datasource:akshare"]
    assert reg.records["akshare"].consecutive_failures == 3, "成功把计数清零"


def test_restart_does_not_double_alarm_but_keeps_counting(db):
    """刻画：计数跨进程存活，同一串故障只响一次；恢复后重新累积才会再响。"""
    store = SourceHealthRepo(db)
    failing = RateLimited("akshare", "429")
    bus1, seen1 = bus_with_alarms()
    first = make_registry(Scripted("akshare", ["A"], failing), store=store, bus=bus1)
    for _ in range(4):
        with pytest.raises(DatasourceError):
            first.fetch_daily("A", "600519", D1, D2)
    assert len(seen1) == 1

    bus2, seen2 = bus_with_alarms()
    second = make_registry(Scripted("akshare", ["A"], failing), store=store, bus=bus2)
    assert second.records["akshare"].consecutive_failures == 4
    for _ in range(3):
        with pytest.raises(DatasourceError):
            second.fetch_daily("A", "600519", D1, D2)
    assert seen2 == [], "同一串故障不该再响"
    assert SourceHealthRepo(db).load()["akshare"].consecutive_failures == 7


def test_single_source_market_reports_failure_as_failure_not_empty():
    reg = make_registry(
        Scripted("yfinance", ["US", "HK"], TransientError("yfinance", "timed out")),
        markets=("HK",),
    )
    with pytest.raises(DatasourceError, match="所有源均失败"):
        reg.fetch_daily("HK", "00700", D1, D2)
    assert reg.records["yfinance"].ok is False
    assert reg.records["yfinance"].consecutive_failures == 1


def test_alarm_carries_no_market_information():
    """已知设计：健康按「源」而非「源×市场」计，港美股共用 yfinance 一个计数器。
    告警因此只说得出源名——要区分「美股挂了」与「港股挂了」得先按 源×市场 记账，
    那是 M4 的事，不在 M1 里偷偷加一半。"""
    bus, seen = bus_with_alarms()
    boom = TransientError("yfinance", "timed out")
    reg = make_registry(Scripted("yfinance", ["US", "HK"], boom), markets=("US", "HK"), bus=bus)
    for market, code in (("HK", "00700"), ("US", "AAPL"), ("HK", "09988")):
        with pytest.raises(DatasourceError):
            reg.fetch_daily(market, code, D1, D2)
    assert [a.component for a in seen] == ["datasource:yfinance"]
    assert "HK" not in seen[0].message and "US" not in seen[0].message
    assert reg.records["yfinance"].markets == ("HK", "US")


def test_rate_limiter_sleeps_on_the_injected_clock_and_sleep():
    """限速器与退避重试共用同一套注入：否则常驻采集的真实节流无法由假时钟控制，
    离线用例也断言不了「等了多久」，而「429 之后退避多久」只能靠真等来测。
    """
    waits: list[float] = []
    stubs = {
        name: Scripted(name, ["A", "US", "HK"], bars_out(name))
        for name in ("akshare", "eastmoney", "yfinance")
    }
    clock = FrozenClock(dt.datetime(2024, 1, 2, 9, 0, tzinfo=UTC))
    reg = AdapterRegistry.from_settings(
        settings_for(rate_limit_per_s=1), adapters=stubs, clock=clock, sleep=waits.append
    )
    assert reg.bucket is not None
    reg.fetch_daily("A", "600519", D1, D2)  # 桶满：不该等
    assert waits == []
    clock.advance(seconds=0.5)  # 只补充了半个令牌，第二次必须被按住
    reg.fetch_daily("A", "600519", D1, D2)
    assert waits == [0.5], "等待时长由注入的假时钟算出，而不是真睡 time.sleep"


def test_token_bucket_waits_exactly_the_configured_interval():
    """机制本身可注入：rate=1、capacity=1 时，紧跟着的第二次取数要等满 1 秒。"""
    from sams.datasource.ratelimit import TokenBucket

    waits: list[float] = []
    injected = TokenBucket(rate=1, capacity=1, clock=lambda: 0.0, sleep=waits.append)
    reg = AdapterRegistry(
        adapters={"akshare": Scripted("akshare", ["A"], bars_out())},
        market_sources={"A": ("akshare",)},
        bucket=injected,
        sleep=lambda seconds: None,
    )
    reg.fetch_daily("A", "600519", D1, D2)
    reg.fetch_daily("A", "600519", D1, D2)
    assert waits == [1.0]


def test_permanent_misconfiguration_is_caught_at_construction():
    """源不支持某市场是配置错误，不是运行时状况：当场失败胜过每次采集都「跳过」却不留痕
    ——那样 `sams health` 看到的是一个从没被调用过的源，健康仍是「未知」，采集停摆无人知道。
    """
    bus, seen = bus_with_alarms()
    adapter = Scripted("eastmoney", ["A"], bars_out())
    with pytest.raises(DatasourceError, match="不支持它的源"):
        make_registry(adapter, markets=("HK",), bus=bus)
    assert adapter.calls == 0
    assert seen == []


def test_suspension_response_does_not_burn_the_failure_budget():
    reg = make_registry(
        Scripted("yfinance", ["US", "HK"], EmptyResponse("yfinance", "区间无数据")),
        markets=("HK",),
    )
    result = reg.fetch_daily("HK", "00700", D1, D2)
    assert result.empty_everywhere is True
    record = reg.records["yfinance"]
    assert record.consecutive_failures == 0, "接口正常应答不该消耗失败预算"
    assert record.ok is not False and record.last_error is None


def test_a_batch_of_suspended_codes_does_not_fire_a_system_alarm():
    bus, seen = bus_with_alarms()
    reg = make_registry(
        Scripted("yfinance", ["US", "HK"], EmptyResponse("yfinance", "区间无数据")),
        markets=("HK",),
        bus=bus,
    )
    for code in ("00700", "09988", "03690"):
        result = reg.fetch_daily("HK", code, D1, D2)
        assert result.empty_everywhere is True
    assert seen == [], "停牌不是故障，不该触发系统告警"


def test_a_day_of_suspended_codes_leaves_health_unchanged(db):
    """一次普通补数不该把 yfinance 标成降级、告警并落库——`sams health` 会说它坏了，
    而它每次都正常应答。全空本身可疑，但「可疑」由缺口检测说，不由源健康说。
    """
    bus, seen = bus_with_alarms()
    store = SourceHealthRepo(db)
    reg = make_registry(
        Scripted("yfinance", ["US", "HK"], EmptyResponse("yfinance", "区间无数据")),
        markets=("HK",),
        bus=bus,
        store=store,
    )
    for code in ("00700", "09988", "03690"):
        assert reg.fetch_daily("HK", code, D1, D2).empty_everywhere is True
    assert seen == []
    assert "yfinance" not in store.load(), "没有一次失败/成功被记录，就不该有落库行"
    assert reg.records["yfinance"].ok is None and reg.records["yfinance"].consecutive_failures == 0


def test_primary_empty_then_backup_data_is_reported_as_degraded():
    """刻画：主源「无数据」+ 备源有数据 = degraded，且降级链给空源打勾。"""
    reg = make_registry(
        Scripted("akshare", ["A"], EmptyResponse("akshare", "无")),
        Scripted("eastmoney", ["A"], bars_out("eastmoney")),
    )
    result = reg.fetch_daily("A", "600519", D1, D2)
    assert result.source == "eastmoney" and result.degraded is True
    assert [a.ok for a in result.attempts] == [True, True], "cli 会把没给数据的源显示成 ✓"


def test_failed_attempt_reports_the_real_try_count():
    """`tries` 是实际调用次数，不是配置值：FieldDrift 一次就定性、只调了 1 次，
    报「试了 3 次」等于对调用方说不存在的事（也会让人误判源在抖动而不是改版）。
    """
    reg = make_registry(
        Scripted("akshare", ["A"], FieldDrift("akshare", "少列")),
        Scripted("eastmoney", ["A"], bars_out("eastmoney")),
        attempts_per_source=3,
    )
    result = reg.fetch_daily("A", "600519", D1, D2)
    assert reg.adapters["akshare"].calls == 1
    assert result.attempts[0].tries == 1
    assert result.attempts[1].tries == 1, "成功的源同样按真实次数记"


def test_rate_limited_attempt_reports_all_its_tries():
    """限流是可重试的：3 次配置就该有 3 次真实调用，退避两次。"""
    sleeps: list[float] = []
    reg = make_registry(
        Scripted("akshare", ["A"], RateLimited("akshare", "429 Too Many Requests")),
        attempts_per_source=3,
        sleep=sleeps.append,
    )
    with pytest.raises(DatasourceError):
        reg.fetch_daily("A", "600519", D1, D2)
    assert reg.adapters["akshare"].calls == 3
    assert sleeps == [1.0, 4.0]


def test_message_containing_429_is_not_mistaken_for_rate_limiting():
    """「429」必须是独立一个数才算限流：akshare 里 `float('429元')` 这类 ValueError
    也含 429，按子串匹配会把一个永久性解析错误当成限流，白等 1+4 秒再重试三轮。
    """
    sleeps: list[float] = []

    def broken(symbol, start, end):
        raise ValueError("could not convert string to float: '429元'")

    from sams.datasource.akshare_cn import AkshareAdapter

    adapter = AkshareAdapter(fetch=broken)
    classified = classify_error("akshare", ValueError("could not convert string to float: '429元'"))
    assert not isinstance(classified, RateLimited | TransientError), (
        f"永久性错误被判成可重试：{classified!r}"
    )
    reg = AdapterRegistry(
        adapters={"akshare": adapter},
        market_sources={"A": ("akshare",)},
        attempts_per_source=3,
        sleep=sleeps.append,
    )
    with pytest.raises(DatasourceError):
        reg.fetch_daily("A", "600519", D1, D2)
    assert reg.records["akshare"].consecutive_failures == 1
    assert sleeps == [], "一次就该换源的 bug 不该走退避重试"


def test_transport_errors_and_429_are_classified_distinctly():
    """分类核对：抖动/限流可重试，改版立即换源；"429" 提示优先于 connection。"""
    assert isinstance(
        classify_error("eastmoney", http.client.RemoteDisconnected("closed")), TransientError
    )
    assert isinstance(
        classify_error("eastmoney", ConnectionResetError("Reset by peer")), TransientError
    )
    assert isinstance(classify_error("eastmoney", TimeoutError("read timed out")), TransientError)
    assert isinstance(
        classify_error("eastmoney", RuntimeError("429 Client Error: Too Many Requests")),
        RateLimited,
    )
    assert isinstance(
        classify_error("eastmoney", ConnectionError("connection reset, HTTP 429")), RateLimited
    )
    assert type(classify_error("eastmoney", KeyError("close"))) is DatasourceError


# ── 5. SourceRecord 三态 ok 与 NOT NULL ──────────────────────────────────────


def test_never_attempted_source_is_never_written_to_source_health(db):
    """核实：registry 只落库「真实尝试过」的源；没被调用过的源保持未知，不写 NULL 行。"""
    store = SourceHealthRepo(db)
    reg = AdapterRegistry(
        adapters={
            "akshare": Scripted("akshare", ["A"], bars_out()),
            "yfinance": Scripted("yfinance", ["US", "HK"], bars_out("yfinance", "US", "AAPL")),
        },
        market_sources={"A": ("akshare",), "HK": ("yfinance",)},
        store=store,
        sleep=lambda seconds: None,
    )
    reg.fetch_daily("A", "600519", D1, D2)
    rows = db.query("SELECT name, ok FROM source_health ORDER BY name").to_dicts()
    assert [r["name"] for r in rows] == ["akshare"]
    assert reg.records["yfinance"].ok is None
    assert db.scalar("SELECT count(*) FROM source_health WHERE ok IS NULL") == 0


def test_the_store_persists_all_three_health_states(db):
    """三态存得下来：`ok` 允许 NULL，「从没试过」不会在读写一轮后被伪造成 False。
    曾经的隐患是仓库层没有 None 保护 + 列上写着 NOT NULL，
    于是只有 registry 不写未知态这一条防线；现在库里自己就说得出「未知」。
    """
    store = SourceHealthRepo(db)
    store.save(SourceRecord(name="sina", markets=("A",)))
    assert store.load()["sina"].ok is None
    assert db.scalar("SELECT count(*) FROM source_health WHERE ok IS NULL") == 1

    from sams.storage.repos.source_health import _to_record

    restored = _to_record(
        {
            "name": "x",
            "markets": "",
            "ok": None,
            "last_error": None,
            "last_ok_at": None,
            "consecutive_failures": 0,
        }
    )
    assert (restored.ok, restored.markets) == (None, ())


def test_recovered_source_clears_the_stored_error_and_counter(db):
    store = SourceHealthRepo(db)
    broken = make_registry(Scripted("akshare", ["A"], RateLimited("akshare", "429")), store=store)
    with pytest.raises(DatasourceError):
        broken.fetch_daily("A", "600519", D1, D2)

    healthy = make_registry(Scripted("akshare", ["A"], bars_out()), store=store)
    healthy.fetch_daily("A", "600519", D1, D2)
    loaded = SourceHealthRepo(db).load()["akshare"]
    assert (loaded.ok, loaded.consecutive_failures, loaded.last_error) == (True, 0, None)


def test_last_ok_at_comes_from_the_injected_clock():
    clock = FrozenClock(dt.datetime(2024, 5, 6, tzinfo=UTC))
    reg = make_registry(Scripted("akshare", ["A"], bars_out()), clock=clock)
    reg.fetch_daily("A", "600519", D1, D2)
    assert reg.records["akshare"].last_ok_at == date(2024, 5, 6)
    clock.advance(days=1)
    reg.fetch_daily("A", "600519", D1, D2)
    assert reg.records["akshare"].last_ok_at == date(2024, 5, 7), "标记跟着注入的时钟走"


# ── 6. 时间口径的漏网处 ──────────────────────────────────────────────────────


def test_alarm_timestamp_follows_the_injected_clock():
    """告警时刻必须跟着这次运行的时间走：`SystemAlarm.ts` 默认工厂是 `datetime.now(UTC)`，
    历史重放/M5 回测里「源挂了」若被记成今天发生的事，整条时间线就不可信。
    """
    bus, seen = bus_with_alarms()
    clock = FrozenClock(dt.datetime(2019, 1, 1, tzinfo=UTC))
    reg = make_registry(
        Scripted("akshare", ["A"], RateLimited("akshare", "429 Too Many Requests")),
        bus=bus,
        clock=clock,
    )
    for _ in range(3):
        with pytest.raises(DatasourceError):
            reg.fetch_daily("A", "600519", D1, D2)
    assert len(seen) == 1
    assert seen[0].ts == dt.datetime(2019, 1, 1, tzinfo=UTC)


def test_no_naive_now_leaks_into_the_datasource_layer():
    """核实：采集层不读墙钟日期，唯一的钟是注入的 Clock 与 ratelimit 的 monotonic。"""
    root = Path(__file__).resolve().parents[1] / "src" / "sams" / "datasource"
    offenders = re.compile(r"datetime\.now\(\)|utcnow|date\.today\(\)|time\.time\(")
    hits = [
        f"{path.name}:{lineno}"
        for path in sorted(root.glob("*.py"))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if offenders.search(line)
    ]
    assert hits == []
