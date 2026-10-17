"""对抗性测试：storage 层（M1 评审改动轮 74c881c..8fe4ca6）。

写法约定：
  * 每个用例断言一条**已成立的**契约：`test_should_*` 是「违反即事故」的那类，
    其余刻画当前行为（含刻意宽松的边界，用例文档里写明归属与后果）。
  * 每个用例只证明一件事，最小复现。

全部离线：内存库 / tmp_path 里的临时迁移目录，绝不改动仓库内的 migrations 文件。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import StorageError, ValidationError
from sams.core.sources import SourceRecord
from sams.core.timeutil import CalendarDay
from sams.storage import migrate
from sams.storage.db import Database
from sams.storage.migrate import (
    MIGRATIONS_DIR,
    applied,
    apply_migrations,
    checksum_of,
    discover,
    pending,
)
from sams.storage.repos.bars import ADJUST, BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.corp_actions import CorpAction, CorpActionRepo
from sams.storage.repos.sessions import SessionRepo
from sams.storage.repos.source_health import SourceHealthRepo
from sams.storage.repos.symbols import SymbolRepo

D1, D2 = date(2024, 1, 2), date(2024, 1, 3)
ALL_MIGRATIONS = tuple(p.name for p in discover())
#: 仓库层的失败必须统一成 StorageError（`errors.py` 的承诺，采集/服务层按它兜仓库故障）：
#: DuckDB 与 polars 的原始异常都不许漏到调用方，否则 CLI 给出的是一坨栈而不是「哪张表哪条约束」
WRITE_FAILURE = StorageError
FRAME_FAILURE = StorageError


def one_bar(
    *, code: str = "600519", day: date = D1, close: float = 1810.0, market: str = "A", **extra
) -> pl.DataFrame:
    rows = {"market": [market], "code": [code], "date": [day], "close": [close], "source": ["ak"]}
    return pl.DataFrame(rows | extra)


def copy_migrations(tmp_path: Path, names: tuple[str, ...], subdir: str) -> Path:
    """把指定迁移复制到临时目录，模拟「某一年的那一套迁移文件」。绝不改仓库内文件。"""
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text((MIGRATIONS_DIR / n).read_text(encoding="utf-8"), encoding="utf-8")
    return d


def legacy_db_with_tampered_0001(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(老迁移集, 当前迁移集, 库文件)：老库只跑 0001+0002，然后 0001 被人追加了一张表。"""
    legacy = copy_migrations(tmp_path, ("0001_init.sql", "0002_source_health.sql"), "legacy")
    current = copy_migrations(tmp_path, ALL_MIGRATIONS, "current")
    (current / "0001_init.sql").write_text(
        (current / "0001_init.sql").read_text(encoding="utf-8")
        + "\nCREATE TABLE IF NOT EXISTS sneaky (x INT);\n",
        encoding="utf-8",
    )
    return legacy, current, tmp_path / "prod.duckdb"


@pytest.fixture
def bars(db) -> BarRepo:
    return BarRepo(db)


@pytest.fixture
def actions(db) -> CorpActionRepo:
    return CorpActionRepo(db)


@pytest.fixture
def legacy_only(tmp_path) -> Database:
    """一个「0004 之前」的库：只应用 0001+0002，因此 schema_migrations 没有 checksum 列。"""
    d = copy_migrations(tmp_path, ("0001_init.sql", "0002_source_health.sql"), "legacy")
    database = Database(duckdb.connect(":memory:"))
    apply_migrations(database, d)
    yield database
    database.close()


# ==================== 1. 迁移链 ====================


def test_edit_of_applied_migration_is_caught_on_a_current_database(tmp_path, db):
    """0004 之后建的库：改动已应用的迁移会启动即失败，且没有半成品。"""
    d = copy_migrations(tmp_path, ALL_MIGRATIONS, "all")
    apply_migrations(db, d)
    before = set(db.table_names())
    (d / "0002_source_health.sql").write_text(
        (d / "0002_source_health.sql").read_text(encoding="utf-8") + "\n-- tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(StorageError, match="已被修改"):
        apply_migrations(db, d)
    assert set(db.table_names()) == before, "报错之前不应已经改动 schema"
    stored = db.query(
        "SELECT checksum FROM schema_migrations WHERE name='0002_source_health.sql'"
    )["checksum"][0]
    assert stored != checksum_of(d / "0002_source_health.sql"), "库内指纹不应被篡改洗白"


def test_should_refuse_tampering_on_a_pre_checksum_database(tmp_path):
    """0004 之前建的库没有指纹：篡改不但不会被发现，还会被「回填」洗白成合法值——
    同一份迁移目录，老库和新库跑出两套 schema，而 `pending()` 说「已是最新」。
    无指纹时改走可证伪的那条路：把库里记为已应用的文件在内存库重放，
    重放产出的表/列而真库没有 → StorageError。
    """
    legacy, current, path = legacy_db_with_tampered_0001(tmp_path)
    db = Database.open(path)
    try:
        apply_migrations(db, legacy)
        db.close()
        up = Database.open(path)
        try:
            with pytest.raises(StorageError, match="校验和|无法"):
                apply_migrations(up, current)
        finally:
            up.close()
    finally:
        if path.exists():
            path.unlink()


def test_should_rollback_a_migration_that_fails_halfway(tmp_path):
    """一个迁移文件包在一个事务里（DuckDB 的 DDL 是事务性的）：
    逐语句 autocommit 时中途失败会留下半个 schema 且再也重试不回去——0003 的
    `CREATE TABLE bars_daily_new` 没有 IF NOT EXISTS，第二次执行必撞 already exists，
    只能人工删表。回滚之后改好文件就能直接重来。
    """
    d = copy_migrations(tmp_path, ALL_MIGRATIONS, "broken")
    third = d / "0003_bar_semantics.sql"
    third.write_text(
        third.read_text(encoding="utf-8").replace(
            "INSERT OR REPLACE INTO bars_daily_new",
            "CREATE TABLE zork (a INT NOT NULL);\nINSERT INTO zork VALUES (NULL);\n"
            "INSERT OR REPLACE INTO bars_daily_new",
        ),
        encoding="utf-8",
    )
    path = tmp_path / "stalled.duckdb"
    db = Database.open(path)
    try:
        with pytest.raises(StorageError, match="失败"):
            apply_migrations(db, d)
        assert "zork" not in db.table_names(), "失败的迁移留下了残骸"
        assert "bars_daily_new" not in db.table_names()
        assert "adj_factor" in db.query("SELECT * FROM bars_daily LIMIT 0").columns, (
            "bars_daily 应还是 0001 的形状"
        )
        shutil.copy(MIGRATIONS_DIR / "0003_bar_semantics.sql", third)
        apply_migrations(db, d)  # 期望：修好文件即可直接重试
        assert pending(db, d) == []
    finally:
        db.close()
        if path.exists():
            path.unlink()


def test_migration_failing_at_the_very_end_is_retriable(tmp_path):
    """反例：失败点在 0003 全部关键语句之后时重试是安全的（前半段恰好可重入）。
    与上一条合起来说明「能否恢复」取决于失败落点，而不是机制保证。
    """
    d = copy_migrations(tmp_path, ALL_MIGRATIONS, "tailfail")
    third = d / "0003_bar_semantics.sql"
    third.write_text(
        third.read_text(encoding="utf-8")
        + "\nCREATE TABLE zork2 (a INT NOT NULL);\nINSERT INTO zork2 VALUES (NULL);\n",
        encoding="utf-8",
    )
    path = tmp_path / "retriable.duckdb"
    db = Database.open(path)
    try:
        with pytest.raises(StorageError, match="失败"):
            apply_migrations(db, d)
        assert "0003_bar_semantics.sql" in pending(db, d), "失败的迁移不该被记录为已应用"
        shutil.copy(MIGRATIONS_DIR / "0003_bar_semantics.sql", third)
        apply_migrations(db, d)
        assert pending(db, d) == []
    finally:
        db.close()
        if path.exists():
            path.unlink()


def test_should_raise_storage_error_when_an_applied_migration_file_is_deleted(tmp_path, db):
    """BUG(P2)：记录里有、文件没了 → 全程静默。
    pending() 不会要求重跑（文件名还在 schema_migrations 里），applied() 照旧，
    `sams health` 说「已是最新」；而哪天把这个文件补回来，里面的语句永远不会执行。
    期望：迁移集与库记录不一致时报 StorageError。
    """
    d = copy_migrations(tmp_path, ALL_MIGRATIONS, "pruned")
    apply_migrations(db, d)
    (d / "0002_source_health.sql").unlink()
    with pytest.raises(StorageError, match="缺|丢失|missing"):
        apply_migrations(db, d)


def test_unpadded_migration_names_are_refused_at_discovery(tmp_path):
    """`sorted(glob)` 按字符串排序，`10_` 排在 `1_` 之前：依赖关系就此颠倒，
    而且只在某个特定的库上才炸。名字不合格等于顺序不可知 → 启动即报错。
    """
    d = tmp_path / "m"
    d.mkdir()
    (d / "0_base.sql").write_text("CREATE TABLE t0 (x INT);", encoding="utf-8")
    (d / "1_provides.sql").write_text("CREATE TABLE t3 (x INT);", encoding="utf-8")
    (d / "10_depends.sql").write_text("CREATE TABLE t2 (x INT);", encoding="utf-8")
    with pytest.raises(StorageError, match="四位数字前缀"):
        migrate.discover(d)


def test_duplicate_migration_numbers_are_refused(tmp_path):
    """两条 0001：它们会共用一条 schema_migrations 记录，第二条永远不会被执行。"""
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_a.sql").write_text("CREATE TABLE a (x INT);", encoding="utf-8")
    (d / "0001_b.sql").write_text("CREATE TABLE b (x INT);", encoding="utf-8")
    with pytest.raises(StorageError, match="序号重复"):
        migrate.discover(d)


def test_recording_a_migration_leaves_no_half_applied_schema(tmp_path):
    """迁移目录指错（第一个文件不建 schema_migrations）时：整个文件回滚，
    报错是 StorageError 且点名缺哪张表——DDL 半成品不再留在库里。
    """
    d = tmp_path / "m2"
    d.mkdir()
    (d / "0009_stray.sql").write_text("CREATE TABLE stray (x INT);", encoding="utf-8")
    db = Database(duckdb.connect(":memory:"))
    with pytest.raises(StorageError) as excinfo:
        apply_migrations(db, d)
    assert "stray" not in db.table_names(), "失败的迁移不该留下残骸"
    assert "schema_migrations" in str(excinfo.value), f"报错没指出缺哪张表: {excinfo.value}"
    db.close()


def test_comment_line_containing_a_semicolon_splits_the_statement(tmp_path):
    """`_split_statements` 先按 ';' 劈、再剔除整行注释：行尾注释里的半角分号会把一条语句切成两半。
    当前 4 个迁移文件都没踩到（已逐行核对），踩到时是显式报错而不是静默错建——
    但报错内容（Parser Error: syntax error at end of input）不会提示「是注释里的分号」。
    """
    d = copy_migrations(tmp_path, ("0001_init.sql",), "semicolon")
    (d / "0002_comment.sql").write_text(
        "CREATE TABLE note_check (\n"
        "    a INT,  -- 说明: 这里有分号; 而且很长\n"
        "    b INT\n"
        ");\n",
        encoding="utf-8",
    )
    db = Database(duckdb.connect(":memory:"))
    with pytest.raises(StorageError, match="Parser Error"):
        apply_migrations(db, d)
    assert "note_check" not in db.table_names()
    db.close()


def test_statement_splitter_drops_comment_only_chunks():
    assert migrate._split_statements("-- 只有注释\n;-- 又一段\n") == []
    assert migrate._split_statements("SELECT 1;\n-- tail\n") == ["SELECT 1"]


def test_should_not_be_fooled_by_a_same_named_table_in_another_schema(legacy_only, tmp_path):
    """`_has_checksum_column` 查 information_schema 必须限定 `table_schema = 'main'`：
    别的 schema 里有一张同名带 checksum 列的表时，老库会被误判成「已有指纹」，
    于是 0004 的 ADD COLUMN 被跳过、回填 SQL 撞在缺失列上。限定 main 之后升级干净通过。
    """
    legacy_only.execute(
        "CREATE SCHEMA audit;"
        "CREATE TABLE audit.schema_migrations (name VARCHAR, checksum VARCHAR);"
    )
    current = copy_migrations(tmp_path, ALL_MIGRATIONS, "current2")
    newly = apply_migrations(legacy_only, current)
    assert newly == list(ALL_MIGRATIONS[2:])
    rows = legacy_only.query("SELECT name, checksum FROM schema_migrations").to_dicts()
    assert len(rows) == len(ALL_MIGRATIONS) and all(r["checksum"] for r in rows)


def test_upgrade_from_a_data_filled_legacy_database_is_lossless(tmp_path):
    """带数据升级（0001+0002 → 其余全部）：行不丢、口径落成 raw、占位列被删。"""
    legacy = copy_migrations(tmp_path, ("0001_init.sql", "0002_source_health.sql"), "legacy")
    path = tmp_path / "prod.duckdb"
    db = Database.open(path)
    try:
        apply_migrations(db, legacy)
        db.execute(
            "INSERT INTO bars_daily (market, code, date, close, volume, source)"
            " VALUES ('A','600519',DATE '2024-01-02',1810.0,30000,'akshare'),"
            "        ('HK','00700',DATE '2024-01-03',2.5,NULL,'yfinance')"
        )
        db.close()
        up = Database.open(path)
        try:
            assert apply_migrations(up) == list(ALL_MIGRATIONS[2:])
            rows = up.query("SELECT * FROM bars_daily ORDER BY close").to_dicts()
            assert [r["adjust"] for r in rows] == ["raw", "raw"]
            assert [r["close"] for r in rows] == [2.5, 1810.0]
            assert rows[0]["volume"] is None
            assert "adj_factor" not in rows[0]
            assert BarRepo(up).latest_date("HK", "00700") == date(2024, 1, 3)
            assert "corp_actions" in up.table_names()
            assert all(
                r["checksum"]
                for r in up.query("SELECT name, checksum FROM schema_migrations").to_dicts()
            )
        finally:
            up.close()
    finally:
        if path.exists():
            path.unlink()


# ==================== 2. bars_daily ====================


def test_should_not_let_a_nan_close_overwrite_a_good_bar(bars):
    """BUG(P0 静默数据损坏)：`_prepare` 只过滤 `close IS NULL`，不管 NaN。
    数据源把缺数据表示成 NaN（pandas 侧的常态）时，这行不但入库，
    还把上一次写好的正确收盘价覆盖掉，而 upsert 返回 1 表示成功。
    期望：NaN 视同缺失 → 与 close=None 走同一条「告警 + 丢弃」路径。
    """
    bars.upsert(one_bar(close=1810.0))
    bars.upsert(one_bar(close=float("nan")))
    stored = bars.load("A", ["600519"])["close"].to_list()
    assert stored == [1810.0], f"NaN 覆盖了正确的收盘价：{stored}"


def test_should_reject_a_caller_supplied_adjust_column(bars, db):
    """`adjust` 是主键维度却不在 upsert 的列集里：带 `adjust='hfq'` 的推导序列会被
    静默改名成 `raw` 并覆盖真正的不复权 K 线——正是 0003 引入 adjust 维度要防的
    「哪条序列是哪种口径无从考证」。后复权请在读取层由 corp_actions 现算。
    """
    bars.upsert(one_bar(close=1810.0))
    with pytest.raises(StorageError, match="adjust"):
        bars.upsert(one_bar(close=9999.0, source=["derived"], adjust=["hfq"]))
    rows = db.query("SELECT adjust, close FROM bars_daily").to_dicts()
    assert rows == [{"adjust": "raw", "close": 1810.0}]


def test_last_write_wins_for_the_same_key(bars):
    """当前语义（docstring 只写了 INSERT OR REPLACE）：后写覆盖前写。"""
    bars.upsert(one_bar(close=1.0))
    bars.upsert(one_bar(close=2.0))
    assert bars.count() == 1
    assert bars.load("A", ["600519"])["close"][0] == 2.0


def test_same_key_twice_in_one_batch_keeps_the_last_row(bars):
    """单批内重复键靠 polars 去重避免「同一语句内主键冲突」，keep='last'。"""
    assert bars.upsert(pl.concat([one_bar(close=1.0), one_bar(close=2.0)])) == 1
    assert bars.load("A", ["600519"])["close"][0] == 2.0


def test_second_smaller_batch_does_not_read_the_first_batch(bars, db):
    """唯一视图名 + finally 注销：不会读到上一批残留。"""
    big = pl.DataFrame(
        {
            "market": ["A"] * 3,
            "code": ["1", "2", "3"],
            "date": [D1] * 3,
            "close": [1.0, 2.0, 3.0],
            "source": ["ak"] * 3,
        }
    )
    assert bars.upsert(big) == 3
    assert bars.upsert(one_bar(code="9", close=9.0)) == 1
    assert sorted(db.query("SELECT code FROM bars_daily")["code"].to_list()) == [
        "1",
        "2",
        "3",
        "9",
    ]
    assert db.query("SELECT view_name FROM duckdb_views() WHERE internal=false").height == 0, (
        "调用之间不应留有注册视图"
    )


def test_empty_and_all_dirty_batches_return_zero(bars, db):
    """空批不会去碰 DuckDB（不会在 0 行帧上跑 INSERT ... FROM view）。"""
    assert bars.upsert(pl.DataFrame({})) == 0
    assert bars.upsert(pl.DataFrame(schema={"market": pl.Utf8, "code": pl.Utf8})) == 0
    dirty = pl.DataFrame(
        {"market": ["A"], "code": ["7"], "date": [D1], "close": [None], "source": ["ak"]}
    )
    assert bars.upsert(dirty) == 0
    assert bars.count() == 0
    assert db.query("SELECT view_name FROM duckdb_views() WHERE internal=false").height == 0


def test_null_volume_and_amount_roundtrip_as_null(bars, db):
    bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["00700"],
                "date": [D1],
                "close": [2.5],
                "volume": [None],
                "amount": [None],
                "source": ["yfinance"],
            }
        )
    )
    row = db.query("SELECT volume, amount FROM bars_daily").to_dicts()[0]
    assert row == {"volume": None, "amount": None}


def test_negative_volume_and_amount_are_stored_as_source_gave_them(bars, db):
    """刻意宽松（bars.py 模块注释写明归属）：负成交量原样入库，
    这样「源给了脏数」不会在库里伪装成「这一天没数据」。清洗是采集层的职责。
    """
    bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["1"],
                "date": [D1],
                "close": [1.0],
                "volume": [-5000.0],
                "amount": [-1.0],
                "source": ["ak"],
            }
        )
    )
    assert db.query("SELECT volume, amount FROM bars_daily").to_dicts() == [
        {"volume": -5000.0, "amount": -1.0}
    ]


def tz_frame(aware: datetime) -> pl.DataFrame:
    return (
        pl.DataFrame({"ts": [aware]})
        .with_columns(pl.col("ts").dt.convert_time_zone("Asia/Shanghai").alias("date"))
        .select(
            pl.lit("A").alias("market"),
            pl.lit("600519").alias("code"),
            "date",
            pl.lit(1810.0).alias("close"),
            pl.lit("ak").alias("source"),
        )
    )


def test_should_reject_non_date_column_types(bars):
    """`cast(pl.Date)` 对带时区的 Datetime 一律取 **UTC 日历日**：
    A 股 2024-01-01 00:30+08:00 会写成 2023-12-31。采集层 `base._to_date_expr`
    先转字符串再解析，所以经过适配器的数据是对的；但直接调用 BarRepo 的写入方
    （M2 的推导序列、任何未来的第二写入路径）会静默错一天。
    仓库层无从知道市场时区，因此只接受 pl.Date，其它 dtype 报 StorageError。
    """
    aware = datetime(2024, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(StorageError, match="pl.Date"):
        bars.upsert(tz_frame(aware))
    assert bars.count() == 0


def test_plain_datetime_column_is_also_rejected(bars):
    """不带时区的 Datetime 同样会被静默截断成日期，一并挡住。"""
    frame = (
        pl.DataFrame({"market": ["A"], "code": ["600519"], "close": [1.0], "source": ["ak"]})
        .with_columns(pl.lit(datetime(2024, 1, 2, 15, 30)).alias("date"))
    )
    with pytest.raises(StorageError, match="pl.Date"):
        bars.upsert(frame)


def test_bad_column_types_are_reported_with_an_actionable_message(bars):
    """P2：字符串日期列抛 polars InvalidOperationError 而不是 StorageError——
    `errors.py` 承诺「仓库层错误」统一是 StorageError，采集侧 `except StorageError` 抓不到它。
    """
    with pytest.raises(FRAME_FAILURE) as excinfo:
        bars.upsert(one_bar(date=["2024-01-02"]))
    assert "date" in str(excinfo.value).lower()


def test_constraint_violations_are_reported_with_an_actionable_message(bars, db):
    """P2：`Database.execute` 不包装异常。source 为 NULL 撞 NOT NULL 时上层拿到
    `_duckdb.ConstraintException`；errors.py 写着「仓库层错误：迁移失败、约束冲突等」，
    只有迁移路径真的做到了包装。
    """
    with pytest.raises(WRITE_FAILURE) as excinfo:
        bars.upsert(
            pl.DataFrame(
                {"market": ["A"], "code": ["1"], "date": [D1], "close": [1.0], "source": [None]}
            )
        )
    assert "bars_daily.source" in str(excinfo.value), "至少要说清是哪一列的约束"
    assert db.query("SELECT view_name FROM duckdb_views() WHERE internal=false").height == 0, (
        "bars 用了 try/finally，失败路径不泄漏视图"
    )


def test_count_load_and_latest_date_share_one_adjust_caliber(bars, db):
    """health 报的行数必须是读取路径拿得到的那个数：
    `count()` 与 `load/latest_date` 恒带同一个 `adjust = 'raw'`。
    """
    bars.upsert(one_bar())
    db.execute(
        "INSERT INTO bars_daily (market, code, date, adjust, close, source)"
        " VALUES ('A', '600519', ?, 'hfq', 9.9, 'derived')",
        [D2],
    )
    assert bars.count("A", "600519") == 1
    assert bars.load("A", ["600519"]).height == 1
    assert bars.latest_date("A", "600519") == D1


def test_load_range_bounds_are_inclusive_on_both_ends(bars):
    days = [D1, D2, date(2024, 1, 4)]
    bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * 3,
                "code": ["1"] * 3,
                "date": days,
                "close": [1.0, 2.0, 3.0],
                "source": ["ak"] * 3,
            }
        )
    )
    assert bars.load("A", ["1"])["date"].to_list() == days
    assert bars.load("A", ["1"], start=D2, end=D2)["date"].to_list() == [D2]
    assert ADJUST == "raw"


def test_should_unregister_the_view_when_the_insert_fails(db):
    """注册视图必须 try/finally 注销：`Database.unregister` 的注释写着
    「不注销下一批可能读到上一批残留」。三个写入路径（bars/symbols/calendar）一视同仁。
    """
    with pytest.raises(WRITE_FAILURE):
        SymbolRepo(db).upsert(pl.DataFrame([{"market": "A", "code": "1", "currency": None}]))
    leaked = db.query("SELECT view_name FROM duckdb_views() WHERE internal=false")[
        "view_name"
    ].to_list()
    assert "incoming_symbols" not in leaked


def test_calendar_unregisters_its_view_when_the_insert_fails(db):
    """日历写入失败（tz 违反 NOT NULL）后连接上不该挂着 incoming_calendar。"""
    with pytest.raises(WRITE_FAILURE):
        CalendarRepo(db).save(
            [CalendarDay(market="A", day=D1, is_trading=True, is_half_day=False, tz=None)],
            source="fake",
        )
    leaked = db.query("SELECT view_name FROM duckdb_views() WHERE internal=false")[
        "view_name"
    ].to_list()
    assert "incoming_calendar" not in leaked


# ==================== 3. corp_actions ====================


def _action(**kwargs) -> CorpAction:
    base = {
        "market": "A",
        "code": "600519",
        "ex_date": D1,
        "kind": "dividend",
        "src": "eastmoney",
        "cash_per_share": 30.0,
    }
    return CorpAction(**(base | kwargs))


def test_corp_action_upsert_is_idempotent_and_last_write_wins(actions):
    actions.upsert(_action(cash_per_share=30.0))
    actions.upsert(_action(cash_per_share=31.0))
    assert actions.count() == 1
    assert actions.list_for("A", "600519")["cash_per_share"].to_list() == [31.0]


def test_corp_action_validation_matrix(actions):
    with pytest.raises(ValidationError, match="未知公司行为类型"):
        actions.upsert(_action(kind="Dividend"))
    with pytest.raises(ValidationError, match="现金或比例"):
        actions.upsert(_action(cash_per_share=None))
    actions.upsert(_action(ratio=0.5))  # 现金与比例同时给：不报错
    assert actions.count() == 1


def test_should_reject_nan_cash_per_share(actions):
    """BUG(P2)：校验只判 `is None`，NaN 算「给出了现金」。
    被拒绝的那条注释写着「否则复权因子无从计算」——NaN 同样无从计算。
    期望：非有限数值 ValidationError。
    """
    with pytest.raises(ValidationError):
        actions.upsert(_action(cash_per_share=float("nan")))


def test_list_for_until_is_inclusive_and_matches_bars_range_bounds(actions):
    """`until` 是闭区间（<=），与 `bars.load` 的 `end`（<=）一致——但两处都没写明。"""
    actions.upsert(_action(ex_date=D1))
    actions.upsert(_action(ex_date=D2))
    assert actions.list_for("A", "600519", until=D1).height == 1
    assert actions.list_for("A", "600519", until=D2).height == 2
    assert actions.list_for("A", "600519", until=date(2023, 12, 31)).height == 0


def test_list_for_without_until_returns_future_ex_dates(actions):
    """P2（易误用）：不传 until 就是无界读，包含未来除权日。
    docstring 称 `until` 为「PIT 读法」，但默认值给的是非 PIT——忘了传就是一次未来函数，
    而 M2 的因子推导正是最可能忘的地方。
    """
    actions.upsert(_action())
    actions.upsert(_action(ex_date=date(2030, 6, 14)))
    assert actions.list_for("A", "600519").height == 2
    assert actions.list_for("A", "600519")["ex_date"].to_list()[-1] == date(2030, 6, 14)
    assert actions.list_for("A", "600519", until=date(2031, 1, 1)).height == 2


def test_corp_action_key_is_scoped_by_market_and_code(actions, db):
    """同 ex_date、同 kind、不同 code 是两行（主键含 code）。"""
    actions.upsert(_action())
    actions.upsert(_action(code="000001"))
    assert actions.count() == 2
    assert db.scalar("SELECT count(*) FROM corp_actions WHERE kind='dividend'") == 2


def test_corp_action_count_is_global(actions):
    """P3：count() 没有 market/code 参数，跨市场断言只能整表数。"""
    actions.upsert(_action())
    actions.upsert(_action(market="HK", code="00700"))
    assert actions.count() == 2


# ==================== 4. 单写者 / 连接 ====================


def test_second_process_on_the_same_file_gets_a_storage_error(tmp_path):
    """跨进程抢同一个 DuckDB 文件：`Database.open` 包成 StorageError，
    消息带「哪个文件 + 谁持锁 + 官方文档链接」——可处置，不是不透明崩溃。
    """
    path = tmp_path / "single_writer.duckdb"
    holder = Database.open(path)
    apply_migrations(holder)
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
        "from sams.storage.db import Database\n"
        "try:\n"
        f"    Database.open({str(path)!r}).close()\n"
        "    print('NO_ERROR')\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__, exc)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    try:
        assert result.stdout.startswith("StorageError"), result.stdout + result.stderr
        assert "无法打开数据库" in result.stdout
        assert "lock" in result.stdout.lower(), result.stdout
    finally:
        holder.close()


def test_two_handles_on_the_same_path_in_one_process_share_the_database(tmp_path):
    """同进程内没有「单写者」保护：两个 Database 指向同一个 DuckDB 实例，互见对方的写。
    不是 bug（DuckDB 语义如此），但意味着「测试里开两个库对象做隔离」是自欺。
    surprising-but-correct。
    """
    path = tmp_path / "shared.duckdb"
    a = Database.open(path)
    apply_migrations(a)
    b = Database.open(path)
    BarRepo(a).upsert(one_bar(code="1"))
    BarRepo(b).upsert(one_bar(code="2"))
    assert sorted(BarRepo(a).load("A", ["1", "2"])["code"].to_list()) == ["1", "2"]
    assert sorted(BarRepo(b).load("A", ["1", "2"])["code"].to_list()) == ["1", "2"]
    a.close()
    BarRepo(b).upsert(one_bar(code="3"))
    assert b.row_count("bars_daily") == 3
    b.close()


# ==================== 5. 其它仓库 ====================


def test_should_persist_the_never_called_health_state(db):
    """BUG(P2，schema 与值域互相矛盾)：0002 写的是 `ok BOOLEAN NOT NULL`，
    而 `SourceRecord.ok` 是三态 `bool | None`，`registry.__post_init__` 还给每个源都建了一条
    ok=None 的记录。于是 `SourceHealthRepo._to_record` 里「NULL → None」的分支不可能发生，
    而「把从没被路由过这件事留到下一个进程」一旦有人做就当场 ConstraintException。
    目前没有生产调用方写 None（只有 _remember 写 True/False），所以这是潜在而非正在发生的故障。
    期望：新增迁移去掉 NOT NULL，让落库能表达仓库层声明的整个值域。
    """
    repo = SourceHealthRepo(db)
    repo.save(SourceRecord(name="yfinance", markets=("HK",), ok=None))
    assert repo.load()["yfinance"].ok is None


def test_health_roundtrip_for_true_and_false(db):
    repo = SourceHealthRepo(db)
    repo.save(SourceRecord(name="akshare", markets=("A", "HK"), ok=False, last_error="429"))
    repo.save(SourceRecord(name="east", markets=("A",), ok=True, last_ok_at=date(2026, 10, 16)))
    loaded = repo.load()
    assert loaded["akshare"].ok is False
    assert loaded["east"].ok is True
    assert loaded["akshare"].markets == ("A", "HK")
    assert loaded["east"].last_ok_at == date(2026, 10, 16)


def test_market_or_name_with_a_comma_breaks_the_roundtrip(db):
    """P3：markets 用 ',' 拼成 VARCHAR，含逗号的值会还原成错误的元组；
    name 是主键但没有分隔符保护。目前没有这种市场码。
    """
    repo = SourceHealthRepo(db)
    repo.save(SourceRecord(name="x", markets=("A,B",), ok=True))
    assert repo.load()["x"].markets == ("A", "B")


def test_should_count_active_sessions_with_the_injected_clock(db):
    """BUG(P1，与 d99a939「时间不再藏在 SQL 里」冲突)：
    `verify` 用注入时钟判过期，`active_count`/`revoke_all` 却写死 SQL `now()`。
    结果：刚签发、verify 认为有效的会话在 active_count 里是 0，
    `revoke_all` 返回 0（而它的 UPDATE 不看过期，会把早已过期的行一起标成 revoked_at）。
    期望：同一张表所有时间判断只用一个来源。
    """
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    repo = SessionRepo(db, clock=clock)
    token = repo.issue("u1", ttl_days=7)
    assert repo.verify(token) == "u1"
    assert repo.active_count("u1") == 1, "有效会话不该算 0"
    assert repo.revoke_all("u1") == 1


def test_session_expiry_boundary_is_the_injected_moment_itself(db):
    """`expires_at > now` → 到期的那一刻即失效（左闭右开的有效期区间）。
    行为合理，但没有任何一处文档写着「第 N 天 00:00 起失效」。
    """
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    repo = SessionRepo(db, clock=clock)
    token = repo.issue("u1", ttl_days=1)  # expires_at = 2026-01-02T00:00Z
    clock.advance(days=1, seconds=-1)
    assert repo.verify(token) == "u1", "到期前一秒仍有效"
    clock.advance(seconds=1)  # 正好 expires_at == clock.now()
    assert repo.verify(token) is None, "到期时刻本身已失效（闭开区间）"


def test_issued_session_expiry_is_stored_in_the_injected_clock_frame(db):
    """`issue` 用的是注入时钟（正确）：expires_at 相对冻结时刻，而不是墙钟。"""
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    repo = SessionRepo(db, clock=clock)
    repo.issue("u1", ttl_days=7)
    expires_at = db.scalar("SELECT expires_at FROM user_sessions LIMIT 1")
    assert expires_at.astimezone(UTC) == datetime(2026, 1, 8, tzinfo=UTC)


def test_migrations_are_all_applied_on_the_shared_fixture(db):
    assert pending(db) == []
    assert {p.name for p in discover()} == applied(db)
