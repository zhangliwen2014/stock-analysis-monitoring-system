"""对抗性口径审计（文档 vs 代码 vs 实际落库）。

命名约定：
- `test_proof_*` / `test_would_go_red_if_*`  锁住当前口径，实现退化即变红；
- 刻意宽松或已知缺口的地方（停牌与数据洞同形、配股缺价字段）在用例文档里写明后果与归属。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest
import support

from sams.core.errors import EmptyResponse, ValidationError
from sams.datasource.akshare_cn import AkshareAdapter
from sams.datasource.eastmoney import EastmoneyAdapter, parse_eastmoney_daily
from sams.datasource.registry import AdapterRegistry
from sams.datasource.yfinance_us import YfinanceAdapter, normalize_history
from sams.services.sync import SyncService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import KINDS, CorpAction, CorpActionRepo
from sams.storage.repos.symbols import SymbolRepo

D1, D2 = date(2024, 1, 2), date(2024, 1, 3)


# ---------------------------------------------------------------- 工具

def akshare_raw(volume: float = 1.0) -> pl.DataFrame:
    """akshare.stock_zh_a_hist 录制形态：成交量列「成交量」单位为手。"""
    return pl.DataFrame(
        {
            "日期": ["2024-01-02"],
            "开盘": [1.0],
            "收盘": [1.2],
            "最高": [1.5],
            "最低": [0.5],
            "成交量": [volume],
            "成交额": [120.0],  # 元
        }
    )


def eastmoney_payload(volume: str = "1") -> dict:
    """东财 kline 录制 payload：fields2=f51..f57，第 6 段是手、第 7 段是元。"""
    return {
        "data": {
            "klines": [f"2024-01-02,1.00,1.20,1.50,0.50,{volume},120.0"],
        }
    }


def yfinance_history(volume: float = 123.0):
    import pandas as pd

    frame = pd.DataFrame(
        {
            "Open": [1.0],
            "High": [1.5],
            "Low": [0.5],
            "Close": [1.2],
            "Volume": [volume],
        },
        index=pd.DatetimeIndex(["2024-01-02"], name="Date"),
    )
    return frame


class _Registry:
    """只回答路由结果的最小 registry，供 SyncService 用例。"""

    def __init__(self, frame: pl.DataFrame, source: str) -> None:
        self.frame = frame
        self.source = source

    def fetch_daily(self, market, code, start, end):
        from sams.datasource.registry import Attempt, FetchResult

        return FetchResult(
            market, code, self.frame, self.source, (Attempt(self.source, True),)
        )


def us_shaped_frame() -> pl.DataFrame:
    """yfinance 真实产出形状：没有成交额列，normalize 后为 null。"""
    return normalize_history(yfinance_history(), market="US", code="AAPL")


# ---------------------------------------------------------------- 1. 成交量单位

def test_would_go_red_if_akshare_dropped_lots_scaling():
    out = AkshareAdapter(fetch=lambda *a: akshare_raw(1.0)).daily_bars(
        "A", "600519", D1, D2
    )
    assert out["volume"].to_list() == [100.0], (
        "akshare 成交量给「手」，适配层必须 ×100 归一为股（0003 头注 / base.py 注释）"
    )


def test_would_go_red_if_eastmoney_dropped_lots_scaling():
    out = parse_eastmoney_daily(eastmoney_payload("1"), market="A", code="600519")
    assert out["volume"].to_list() == [100.0]


def test_proof_eastmoney_does_not_scale_non_a_market():
    # 适配器只服务 A 股，但纯解析函数给了 HK 分支：若将来把 eastmoney 配给 HK，
    # 116.* 的成交量已是股，不能被 ×100。
    out = parse_eastmoney_daily(eastmoney_payload("1"), market="HK", code="00700")
    assert out["volume"].to_list() == [1.0]
    assert EastmoneyAdapter.markets == frozenset({"A"})


def test_proof_yfinance_volume_is_shares_and_not_rescaled():
    out = YfinanceAdapter(fetch=lambda *a: yfinance_history(123.0)).daily_bars(
        "US", "AAPL", D1, D2
    )
    assert out["volume"].to_list() == [123.0]


def test_proof_scaling_does_not_leak_into_amount():
    """成交额本来就是元：只应缩放 volume，不应误伤 amount。"""
    out = AkshareAdapter(fetch=lambda *a: akshare_raw(1.0)).daily_bars(
        "A", "600519", D1, D2
    )
    assert out["amount"].to_list() == [120.0]


def test_proof_full_path_lands_shares_in_db(db):
    bars = BarRepo(db)
    frame = AkshareAdapter(fetch=lambda *a: akshare_raw(2.0)).daily_bars(
        "A", "600519", D1, D2
    )
    bars.upsert(frame)
    assert db.scalar("SELECT volume FROM bars_daily") == 200.0, "落库必须是股"


# ---------------------------------------------------------------- 2. 成交额 amount

def test_proof_us_amount_lands_null_not_zero(db):
    bars = BarRepo(db)
    bars.upsert(us_shaped_frame())
    row = db.query("SELECT amount FROM bars_daily").to_dicts()[0]
    assert row["amount"] is None, (
        "yfinance 无成交额列，假映射删除后 US 落库应是 NULL；"
        "落成 0.0 会让下游换手/流动性过滤把「不可得」当「零成交」"
    )


def test_proof_hk_amount_lands_null_too(db):
    bars = BarRepo(db)
    bars.upsert(normalize_history(yfinance_history(), market="HK", code="00700"))
    assert db.query("SELECT amount FROM bars_daily")["amount"].to_list() == [None]


def test_proof_load_exposes_null_amount_without_crash(db):
    bars = BarRepo(db)
    bars.upsert(us_shaped_frame())
    loaded = bars.load("US", ["AAPL"])
    assert loaded.height == 1 and loaded["amount"][0] is None


# ---------------------------------------------------------------- 3. 币种

def test_b_share_currency_is_taken_from_the_code_segment(db, settings):
    """B 股按市场级币种入库是错数据：900xxx 沪B以美元计价、200xxx 深B以港币计价。
    币种默认值仍来自市场配置，代码段命中时覆盖——这是「报价币种」的最小正确形态。
    """
    for market, code, expect in (("A", "900901", "USD"), ("A", "200011", "HKD")):
        frame = us_shaped_frame().with_columns(
            pl.lit(market).alias("market"), pl.lit(code).alias("code")
        )
        service = SyncService(
            _Registry(frame, "akshare"), BarRepo(db), SymbolRepo(db), settings.markets
        )
        service.sync_daily(market, code, D1, D2)
        assert SymbolRepo(db).get(market, code)["currency"] == expect


def test_hk_rmb_dual_counter_currency_is_taken_from_the_code_segment(db, settings):
    """80 字头是人民币双柜台（80700 = 腾讯-R），币种 CNY；00700 仍是 HKD。"""
    for code, expect in (("80700", "CNY"), ("00700", "HKD")):
        frame = us_shaped_frame().with_columns(
            pl.lit("HK").alias("market"), pl.lit(code).alias("code")
        )
        service = SyncService(
            _Registry(frame, "yfinance"), BarRepo(db), SymbolRepo(db), settings.markets
        )
        service.sync_daily("HK", code, D1, D2)
        assert SymbolRepo(db).get("HK", code)["currency"] == expect


# ---------------------------------------------------------------- 4. 复权口径与 PIT

def test_proof_adj_factor_is_gone_from_live_schema(db):
    assert "adj_factor" not in db.query("SELECT * FROM bars_daily").columns


def test_proof_upsert_always_writes_adjust_raw(db):
    BarRepo(db).upsert(support.bars_frame())
    adjusts = db.query("SELECT DISTINCT adjust FROM bars_daily")["adjust"].to_list()
    assert adjusts == ["raw"], "入库唯一口径由 DDL DEFAULT 'raw' 兜住"


def test_proof_load_and_latest_date_hide_non_raw_rows(db):
    """读层口径锁：人为塞进库的 hfq 序列绝不能从 BarRepo 流出去。"""
    bars = BarRepo(db)
    bars.upsert(support.bars_frame())  # 3 天 raw，最后 2024-01-04
    db.execute(
        "INSERT INTO bars_daily (market, code, date, adjust, close, source) "
        "VALUES ('A', '600519', DATE '2024-06-01', 'hfq', 9.9, 'derived')"
    )
    loaded = bars.load("A", ["600519"])
    assert "hfq" not in loaded["adjust"].to_list()
    assert loaded.height == 3
    assert bars.latest_date("A", "600519") == date(2024, 1, 4), (
        "latest_date 若不过滤 adjust，会被 hfq 的 2024-06-01 冒领，增量同步漏段"
    )


def test_proof_adjust_column_is_free_text(db):
    """DDL 漂移证据：adjust 无 CHECK，'RAW' 能写进去但对读层永久隐身。"""
    db.execute(
        "INSERT INTO bars_daily (market, code, date, adjust, close, source) "
        "VALUES ('A', '000001', DATE '2024-01-02', 'RAW', 1.0, 'typo')"
    )
    assert db.scalar(
        "SELECT count(*) FROM bars_daily WHERE market='A' AND code='000001'"
    ) == 1
    assert BarRepo(db).load("A", ["000001"]).height == 0, "写进去了，没人读得到"


def test_proof_corp_actions_pit_query_excludes_future_ex_date(db):
    repo = CorpActionRepo(db)
    repo.upsert(CorpAction(market="A", code="600519", ex_date=date(2024, 6, 10),
                           kind="dividend", cash_per_share=30.0, src="akshare"))
    repo.upsert(CorpAction(market="A", code="600519", ex_date=date(2025, 6, 10),
                           kind="dividend", cash_per_share=35.0, src="akshare"))
    as_of_2024 = repo.list_for("A", "600519", until=date(2024, 12, 31))
    assert as_of_2024.height == 1, "2025 年的除权不得泄漏进 2024-12-31 的 PIT 读"
    assert as_of_2024["ex_date"].to_list() == [date(2024, 6, 10)]
    # 边界语义：until 含当天（ex_date 当日开盘价已除权，含当天才是正确口径）
    on_the_day = repo.list_for("A", "600519", until=date(2024, 6, 10))
    assert on_the_day.height == 1, "until 为闭区间（ex_date <= until）"
    day_before = repo.list_for("A", "600519", until=date(2024, 6, 9))
    assert day_before.height == 0


# ---------------------------------------------------------------- 5. 公司行为完备性

def test_proof_kind_is_validated_in_python_only(db):
    repo = CorpActionRepo(db)
    with pytest.raises(ValidationError):
        repo.upsert(CorpAction(market="A", code="600519", ex_date=D1,
                               kind="divident", cash_per_share=1.0, src="x"))
    # 但 SQL 层不设防：拼错的 kind 直接裸写就能进库
    db.execute(
        "INSERT INTO corp_actions (market, code, ex_date, kind, cash_per_share, src) "
        "VALUES ('A', '600519', DATE '2024-01-03', 'divident', 1.0, 'raw-sql')"
    )
    assert repo.count() == 1, "kind 无 CHECK 约束，Python 校验挡不住第二条写入路径"


def test_proof_right_issue_has_no_price_field(db):
    """配股需要 (每股配比 ratio, 配股价 price)，schema 只有 cash_per_share/ratio。"""
    with pytest.raises(TypeError):
        CorpAction(market="A", code="600519", ex_date=D1, kind="rights",
                   ratio=0.3, price=8.7, src="akshare")  # 配股价无处安放
    columns = db.query("SELECT * FROM corp_actions LIMIT 0").columns
    assert "price" not in columns
    expected = {"dividend", "split", "bonus", "transfer", "rights"}
    assert set(KINDS) == expected


def test_proof_upsert_requires_cash_or_ratio(db):
    repo = CorpActionRepo(db)
    with pytest.raises(ValidationError):
        repo.upsert(CorpAction(market="A", code="600519", ex_date=D1,
                               kind="dividend", src="akshare"))


# ---------------------------------------------------------------- 6. 停牌 / 休市

def test_proof_zero_volume_bar_is_stored_indistinguishably(db):
    """已知缺口（M1 刻意不补）：停牌日（volume=0）与数据源漏灌（volume=0）在库里长得一模一样。

    区分它们需要日历与逐日缺口比对（spec §6.3 的 covered/unknown 语义），
    加一个 `is_suspended` 列只会多一个没人维护的谎。M1 只保证不把它当成失败。
    """
    bars = BarRepo(db)
    frame = support.bars_frame().with_row_index("r")
    frame = frame.with_columns(pl.when(pl.col("r") == 1).then(0.0)
                               .otherwise(pl.col("volume")).alias("volume"))
    bars.upsert(frame.drop("r"))
    stored = db.query("SELECT date, volume FROM bars_daily ORDER BY date")
    assert stored.height == 3
    assert stored["volume"][1] == 0.0
    cols = db.query("SELECT * FROM bars_daily LIMIT 0").columns
    assert not {"halted", "is_suspended", "is_stale"} & set(cols)


def test_all_empty_response_is_neither_a_failure_nor_a_success(db):
    """口径一致：所有源都答「没有数据」（停牌、新股、区间过短）时，
    给调用方是 `empty_everywhere`，给健康表是「这一趟没结论」——
    既不累计 consecutive_failures（三只停牌票不该把源标红、不该发 SystemAlarm），
    也不标成功（全空本身可疑，不该抹掉之前的故障记录）。缺口检测才是说这话的地方。
    """
    from sams.core.events import EventBus, SystemAlarm

    alarms: list[SystemAlarm] = []
    bus = EventBus()
    bus.subscribe(SystemAlarm, alarms.append)
    registry = AdapterRegistry(
        adapters={"stub": support.StubAdapter("stub", ["A"],
                                              EmptyResponse("stub", "无数据"))},
        market_sources={"A": ("stub",)},
        bucket=None,
        bus=bus,
    )
    for _ in range(3):
        result = registry.fetch_daily("A", "600519", D1, D2)
        assert result.empty_everywhere is True
    record = registry.records["stub"]
    assert (record.ok, record.consecutive_failures) == (None, 0)
    assert alarms == []


# ---------------------------------------------------------------- 7. 无主列 / 漂移

def test_bars_has_no_ownerless_turnover_column(db, settings):
    """换手率曾经躺在 DDL 里却没有任何采集路径产出它（0006 已删列）。
    留着它，M2 的「换手率>x%」模板会把这个恒 NULL 的列当成现成指标读；
    换手率该由 成交量 / 流通股本 在规则层现算，输入两边都有出处才算数。
    """
    frame = us_shaped_frame()
    assert "turnover" not in frame.columns, "标准列集根本不产出 turnover"
    BarRepo(db).upsert(frame)
    assert "turnover" not in db.query("SELECT * FROM bars_daily LIMIT 0").columns


def test_proof_symbol_placeholders_from_sync(db, settings):
    """同步链路只保证「报价币种 + 每手股数」这两件能确定的事，其余留 NULL 占位。

    lot_size 由市场规则给：A 股一手 100 股、美股 1 股、港股每手由联交所按票指定 → NULL
    （给个猜的默认值就是错数据，M3 的整手校验会照着它算错可买股数）。
    """
    frame = us_shaped_frame()
    service = SyncService(_Registry(frame, "yfinance"), BarRepo(db), SymbolRepo(db),
                          settings.markets)
    service.sync_daily("US", "AAPL", D1, D2)
    row = SymbolRepo(db).get("US", "AAPL")
    assert row["name"] is None and row["sector"] is None and row["exchange"] is None
    assert row["lot_size"] == 1


def test_lot_size_follows_market_rules_not_a_single_default(db, settings):
    """每手股数是市场规则：A 股 100、美股 1、港股由联交所按票指定 → 只能留 NULL。"""
    for market, code, expect in (("A", "600519", 100), ("US", "AAPL", 1), ("HK", "00700", None)):
        frame = us_shaped_frame().with_columns(
            pl.lit(market).alias("market"), pl.lit(code).alias("code")
        )
        service = SyncService(
            _Registry(frame, "yfinance"), BarRepo(db), SymbolRepo(db), settings.markets
        )
        service.sync_daily(market, code, D1, D2)
        assert SymbolRepo(db).get(market, code)["lot_size"] == expect


# ---------------------------------------------------------------- 8. 幂等重跑

def _snapshot(db, cols):
    return db.query(
        f"SELECT {', '.join(cols)} FROM bars_daily ORDER BY market, code, date, adjust"
    ).to_dicts()


ALL_COLS = [
    "market", "code", "date", "adjust", "open", "high", "low", "close",
    "volume", "amount", "source",
]


def test_proof_double_ingest_is_byte_stable(db):
    """幂等重跑的质量线：同帧重灌，除 updated_at 外逐字段一致且不产生第二行。

    updated_at 被排除是因为它现在由仓库层显式写 now()——
    「最后修改」必然跟着每一趟写入走，一致的是数据本身。
    """
    bars = BarRepo(db)
    frame = support.bars_frame()
    bars.upsert(frame)
    first = _snapshot(db, ALL_COLS)
    bars.upsert(frame)
    second = _snapshot(db, ALL_COLS)
    assert first == second, "同帧重灌：11 个数据列逐字段一致"
    assert db.row_count("bars_daily") == 3, "重灌不得产生第二行"


def test_upsert_refreshes_updated_at_so_it_means_last_change(db):
    """仓库层显式写 now()：`updated_at` 的含义必须是「最后修改」。

    DDL 的 `DEFAULT now()` 只在插入时生效（见下一条），走默认值的列在覆盖写后
    仍是「首次入库」时间——任何按它做增量/新鲜度判断的逻辑都会被误导。
    """
    bars = BarRepo(db)
    frame = support.bars_frame()
    bars.upsert(frame)
    before = db.scalar("SELECT max(updated_at) FROM bars_daily")
    changed = frame.with_columns(pl.lit(999.0).alias("close"))
    bars.upsert(changed)
    after = db.scalar("SELECT max(updated_at) FROM bars_daily")
    assert after > before, "内容改了时间戳不变，新鲜度判断就无从谈起"
    assert db.scalar("SELECT max(close) FROM bars_daily") == 999.0


def test_ddl_default_timestamp_alone_would_not_refresh(db):
    """DuckDB 语义：INSERT OR REPLACE 未列出的列沿用旧行值，不重打 DEFAULT。

    这条是上一条存在的原因——绕过仓库层裸写 SQL 时，updated_at 真的不会动。
    """
    c = db.conn
    c.execute(
        "INSERT INTO bars_daily (market, code, date, close, source) "
        "VALUES ('A','x','2024-01-02',1.0,'s')"
    )
    before = db.scalar("SELECT updated_at FROM bars_daily WHERE code='x'")
    c.execute(
        "INSERT OR REPLACE INTO bars_daily (market, code, date, close, source) "
        "VALUES ('A','x','2024-01-02',2.0,'s2')"
    )
    row = db.query(
        "SELECT close, source, updated_at FROM bars_daily WHERE code='x'"
    ).to_dicts()[0]
    assert row["close"] == 2.0 and row["source"] == "s2"
    assert row["updated_at"] == before, "裸 SQL 覆盖写不重打 DEFAULT"


def test_proof_corp_action_rerun_no_duplicate(db):
    repo = CorpActionRepo(db)
    action = CorpAction(market="A", code="600519", ex_date=date(2024, 6, 10),
                        kind="dividend", cash_per_share=30.0, src="akshare")
    repo.upsert(action)
    repo.upsert(action)
    assert repo.count() == 1
    row = repo.list_for("A", "600519").to_dicts()[0]
    assert row["cash_per_share"] == 30.0
