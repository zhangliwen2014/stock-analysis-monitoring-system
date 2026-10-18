from __future__ import annotations

from datetime import date

import polars as pl
import pytest
import support

from sams.core.errors import StorageError
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbols import SymbolRepo

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


@pytest.fixture
def bars(db) -> BarRepo:
    return BarRepo(db)


@pytest.fixture
def symbols(db) -> SymbolRepo:
    return SymbolRepo(db)


def test_upsert_is_idempotent(bars, bars_frame):
    assert bars.upsert(bars_frame) == 3
    assert bars.upsert(bars_frame) == 3
    assert bars.count() == 3, "重跑同一区间不能长出重复行"


def test_same_key_updates_in_place(bars, bars_frame):
    bars.upsert(bars_frame)
    fixed = bars_frame.with_columns(
        pl.when(pl.col("date") == D2).then(9.9).otherwise(pl.col("close")).alias("close")
    )
    bars.upsert(fixed)
    assert bars.count() == 3
    loaded = bars.load("A", ["600519"], start=D2, end=D2)
    assert loaded["close"][0] == 9.9


def test_latest_date_and_empty_market(bars):
    assert bars.latest_date("A", "600519") is None
    bars.upsert(
        pl.DataFrame(
            {
                "market": ["A", "HK"],
                "code": ["600519", "00700"],
                "date": [D1, D3],
                "close": [1.0, 2.0],
                "source": ["akshare", "yfinance"],
            }
        )
    )
    assert bars.latest_date("A", "600519") == D1
    assert bars.latest_date("HK", "00700") == D3
    assert bars.latest_date("US", "AAPL") is None


def test_load_returns_range_ordered(bars, bars_frame):
    bars.upsert(bars_frame)
    rows = bars.load("A", ["600519"], start=D2)
    assert rows["date"].to_list() == [D2, D3]
    assert bars.load("A", []).is_empty()
    assert bars.count("A", "600519") == 3


def test_dirty_rows_are_dropped_not_stored(bars, bars_frame):
    dirty = bars_frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("close"))
    assert bars.upsert(dirty) == 0
    assert bars.count() == 0


def test_missing_required_column_fails_loudly(bars):
    with pytest.raises(StorageError, match="close"):
        bars.upsert(pl.DataFrame({"market": ["A"], "code": ["1"]}))


def test_deduplicates_within_one_batch(bars, bars_frame):
    doubled = pl.concat([bars_frame, bars_frame.with_columns(pl.lit(0.0).alias("close"))])
    bars.upsert(doubled)
    assert bars.count() == 3


def test_symbol_upsert_fills_defaults(symbols):
    symbols.upsert(pl.DataFrame([{"market": "A", "code": "600519", "currency": "CNY"}]))
    row = symbols.get("A", "600519")
    # lot_size 没有默认值：调用方没给就是「不知道」，填 1 会让 M2 的手数换算差一个数量级
    assert row["list_status"] == "L" and row["lot_size"] is None
    assert symbols.upsert(pl.DataFrame([{"market": "A", "code": "600519", "currency": "CNY"}])) == 1
    assert symbols.get("A", "missing") is None


def test_symbol_key_is_market_plus_code(symbols):
    symbols.upsert(
        pl.DataFrame(
            [
                {"market": "US", "code": "1", "currency": "USD", "name": "一品"},
                {"market": "HK", "code": "1", "currency": "HKD", "name": "一品港"},
            ]
        )
    )
    assert symbols.get("US", "1")["currency"] == "USD"
    assert symbols.get("HK", "1")["currency"] == "HKD"


def test_list_market_filters_delisted(symbols):
    symbols.upsert(
        pl.DataFrame(
            [
                {"market": "A", "code": "live", "currency": "CNY"},
                {"market": "A", "code": "gone", "currency": "CNY", "list_status": "D"},
            ]
        )
    )
    # code 的形态在 `_prepare` 里落定（与 bars 同一条规则），所以回来的是大写形态：
    # 断言写成小写等于允许「存进去的形状」和「用来查的形状」分叉，那正是这轮要堵的洞
    assert set(symbols.list_market("A")["code"]) == {"LIVE"}
    assert set(symbols.list_market("A", listed_only=False)["code"]) == {"LIVE", "GONE"}


def test_adj_factor_placeholder_column_is_gone(bars, db):
    """没有生产者的列会被下游当成可信口径，所以直接删掉（0003 迁移）。"""
    assert "adj_factor" not in db.query("SELECT * FROM bars_daily").columns


def test_adjust_is_part_of_the_primary_key(bars, db, bars_frame):
    """同一天允许 raw 与推导序列并存：主键没这个维度，M2 就只能重建库。"""
    assert bars.upsert(bars_frame) == 3
    db.execute(
        "INSERT INTO bars_daily (market, code, date, adjust, close, source)"
        " VALUES ('A', '600519', ?, 'hfq', 9.9, 'derived')",
        [D1],
    )
    assert db.scalar("SELECT count(*) FROM bars_daily") == 4
    loaded = bars.load("A", ["600519"])
    assert loaded.height == 3, "读取默认只回不复权口径，混着两种价的序列会算错指标"
    assert set(loaded["adjust"].to_list()) == {"raw"}


# ---------------------------------------------------------------- 脏值守卫


def zero_close_frame(close=0.0) -> pl.DataFrame:
    frame = support.bars_frame()
    return frame.with_columns(
        pl.when(pl.col("date") == D2).then(float(close)).otherwise(pl.col("close")).alias("close")
    )


def test_a_zero_close_row_is_dropped_not_stored(bars, db):
    """收盘 0 会被存成「今天收盘 0 元」：区间跌幅立刻 −100%，止损规则据此打死一只好票。"""
    assert bars.upsert(zero_close_frame()) == 2
    assert db.scalar("SELECT count(*) FROM bars_daily") == 2
    assert bars.load("A", ["600519"])["date"].to_list() == [D1, D3]


def test_a_negative_close_row_is_dropped_too(bars):
    assert bars.upsert(zero_close_frame(-3.0)) == 2


def test_a_dropped_row_is_logged_not_silent(bars, caplog):
    """丢弃必须留痕：否则「源给了脏数」和「这一天没数据」在库里长得一样。"""
    with caplog.at_level("WARNING"):
        bars.upsert(zero_close_frame())
    assert "收盘价缺失或非正" in caplog.text


def test_non_positive_open_high_low_become_null_not_fake_prices(bars):
    """非正的开高低是「这个字段没数据」，置空后 M2 的形态规则看得见缺失。"""
    frame = support.bars_frame().with_columns(
        pl.when(pl.col("date") == D1).then(0.0).otherwise(pl.col("open")).alias("open")
    )
    bars.upsert(frame)
    loaded = bars.load("A", ["600519"]).sort("date")
    assert loaded["open"].to_list()[0] is None
    assert loaded["close"].to_list() == [1.2, 2.2, 3.2], "开盘价坏了不该陪葬整根 K 线"


def test_inverted_extremes_are_nulled_on_both_ends(bars):
    """high < low 说明极值被写反了；high 低于 close 同样不可信（突破类规则会误报）。"""
    frame = pl.DataFrame(
        {
            "market": ["A", "A"],
            "code": ["600519", "600519"],
            "date": [D1, D2],
            "open": [10.0, 10.0],
            "high": [9.0, 9.5],  # 第一行 high<low，第二行 high 连收盘都包不住
            "low": [11.0, 8.0],
            "close": [10.0, 10.0],
            "volume": [100.0, 100.0],
            "amount": [1000.0, 1000.0],
            "source": ["akshare", "akshare"],
        }
    )
    bars.upsert(frame)
    loaded = bars.load("A", ["600519"]).sort("date")
    assert loaded["high"].to_list() == [None, None]
    assert loaded["low"].to_list() == [None, None]
    assert loaded["close"].to_list() == [10.0, 10.0]


def test_negative_volume_is_still_stored_as_a_fact(bars):
    """负量原样存是决定（见模块文件头）：丢弃会把它伪装成「这天没数据」。

    它由 `sams health` 的「N 行成交量为负」露头，所以这条测试锁的是「别改成丢弃」。
    """
    frame = support.bars_frame().with_columns(pl.lit(-5.0).alias("volume"))
    assert bars.upsert(frame) == 3
    assert bars.load("A", ["600519"])["volume"].unique().to_list() == [-5.0]


# --- 键形态由仓库层保证（D46 的下沉）------------------------------------------------
#
# D46 原本只在三个服务入口归一市场名，而仓库层原样收下 `market`：`WatchlistRepo.add`
# 就漏在那里（那一层只归一 code）。这类没经过归一入口的写路径会往主键里种一个小写形态，
# 而读取层此后一律按大写查，那一行就永久隐形——症状是「从没采集过 / 没有行情」，库里明明有。
# 主键定义在哪一层，键的形态就该由哪一层保证，服务边界那层留作第二重。


def test_a_lowercase_market_lands_on_the_canonical_key(bars):
    bars.upsert(support.sample_bars(market="a", code="600519"))
    assert bars.load("A", ["600519"]).height == 2, "写进去的必须是大写形态，否则读取层看不见"
    assert bars.load("a", ["600519"]).height == 2, "读的时候大小写都不是语义"
    assert bars.count("A", "600519") == 2
    assert bars.latest_date("a", "600519") == date(2024, 1, 3)


def test_an_empty_market_is_refused_rather_than_stored_as_the_empty_key(bars, db):
    """空市场名以前会写出一行 `market=''`：它谁也都读不到，却在 `sams health` 的行数里。"""
    bars.upsert(support.sample_bars(market=" ", code="600519"))
    assert int(db.scalar("SELECT count(*) FROM bars_daily") or 0) == 0


def test_symbol_writes_and_reads_share_one_market_spelling(symbols):
    symbols.upsert(
        pl.DataFrame(
            {
                "market": ["hk"],
                "code": ["00700"],
                "name": ["腾讯控股"],
                "currency": ["HKD"],
                "lot_size": [100],
            }
        )
    )
    assert symbols.get("HK", "00700") is not None
    assert symbols.get("hk", "00700") is not None
    assert symbols.list_market("hk").height == 1


def test_hk_bar_aliases_share_the_key_the_actions_use(bars, db):
    """`700` 归一成 `00700`：K 线与除权记录必须落在同一个键上（D50 没做完的那半边）。

    `corp_actions` 与 `watchlists` 用 `canonical_code`，bars 以前只 `upper()`：
    腾讯的 K 线会存成 `700`、除权记录存成 `00700`，于是「这天的除权在 K 线上找不到
    对应记录」那条闸（D37 的镜像）会为一个**存在**的键报警，而 `analysis return` 按
    归一后的 `00700` 查 K 线，报「这只票从没采集过」。两个仓库各自 trim+upper
    看着像同一条规则，其实是两条。
    """
    bars.upsert(support.sample_bars(market="HK", code="700"))
    stored = db.scalar("SELECT code FROM bars_daily LIMIT 1")
    assert stored == "00700", f"落库形态必须是 canonical 的 5 位，收到 {stored!r}"
    assert bars.load("HK", ["00700"]).height == 2
    assert bars.load("HK", ["700"]).height == 2, "读的时候别名也算同一只票"
    assert bars.count("HK", "HK.00700") == 2
    assert bars.latest_date("HK", "00700") == date(2024, 1, 3)


def test_a_share_exchange_prefix_is_not_part_of_the_bar_key(bars, db):
    """`sh600519` 是数据源要的形态，不是本系统的键：剥掉它，否则同一天两条主键。"""
    bars.upsert(support.sample_bars(market="A", code="sh600519"))
    assert db.scalar("SELECT code FROM bars_daily LIMIT 1") == "600519"
    assert bars.load("A", ["600519"]).height == 2


def test_symbol_aliases_share_the_bar_key(symbols, db):
    symbols.upsert(
        pl.DataFrame(
            {
                "market": ["HK"],
                "code": ["HK00700"],
                "name": ["腾讯控股"],
                "currency": ["HKD"],
                "lot_size": [100],
            }
        )
    )
    assert db.scalar("SELECT code FROM symbols") == "00700"
    assert symbols.get("HK", "700") is not None
