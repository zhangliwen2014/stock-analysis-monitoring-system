# SP1 实施计划：代码表 + 搜索加票 + 自动补数 + 个股页可点 + 大盘条

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「加一只票」成为一条走得通的路：搜索（代码/拼音首字母/名称片段）→ 建主档 → 自动排拍补数 → 个股页可点可看 → 看板顶部有大盘条。

**Architecture:** 三张新表（`symbol_master` 代码表、`index_daily` 指数日线、`symbol_backfills` 补数队列）落在 storage 层；两个新源能力（`symbol_list`、`index_daily`）走既有 registry 降级链；同步与消费在 services 层（周同步代码表、每拍至多消费 3 条补数）；web 层加搜索 partial、SymbolInterest 四入口挂钩、个股页兜底建行与大盘条。规格见 `docs/superpowers/specs/2026-10-26-sp1-symbol-master-search-autosync-design.md`（D140–D148）。

**Tech Stack:** DuckDB 2.0（迁移 0021）、polars、FastAPI + Jinja + htmx、pypinyin（新增依赖）、pytest（全离线假源）。

---

## 房规（每个任务都必须遵守，逐条来自既有约定）

- **git 一律用 Bash 原生命令**，在仓库根 `/Users/liwen.zhang/AI-Coding/stocks` 执行；不用任何封装的 git 工具。
- **只新建提交，不 amend**；`git add` 只列本任务改动的具体文件名，**永不 `git add -A` / `git add .`**；`qodercli_resume.sh` 与 `tmp/` 保持未跟踪，绝不提交。
- **子代理不 push、不跑全量测试**。跑测试只跑本任务相关文件的 path（`uv run pytest <path> -q`）；全量与 `uv run ruff check --no-cache src tests` 由控制者收尾统一跑。
- **包管理只用 uv**（装依赖 `uv add ...`）；**绝不 `ruff format`**。
- 注释默认不写；要写就写**中文一行 WHY**（为什么，不是做什么）。
- **TIMESTAMPTZ 不回读**：不把时间戳列 SELECT 回 Python 比较/展示，排序可以 `ORDER BY created_at`；插入时刻用 `DEFAULT now()` / SQL 内 `now()`。
- **读完必须 drain**：`db.query` 返回 polars（已 drain）；`db.execute` 后不留结果集。搜索**永不 LIKE**，用 `contains(lower(col), ?)` / `starts_with`。
- **TDD**：每个仓库/服务/端点先写失败测试、亲眼看它失败（fail，不是 error），再写最小实现让它通过。
- 直接提交在 `main`（本仓库既有的执行约定）。
- 分层：web 不 import datasource；duckdb 只在 `storage/db.py`；pandas 禁入（akshare 返回的 pandas 帧只能在适配器内经 `_as_polars` 转）。

## 文件地图

| 任务 | 新建 | 修改 |
| --- | --- | --- |
| 1 | `storage/migrations/0021_symbols_and_indices.sql`、`repos/symbol_list.py`、`repos/index_daily.py`、`repos/symbol_backfills.py` + 三个测试 | — |
| 2 | `core/codes.py` 加函数、`datasource/symbol_list.py`、`datasource/index_daily.py` | `capabilities.py`、`registry.py`、`akshare_cn.py`、`akshare_hk.py`、`tencent.py`、`sina.py` |
| 3 | `services/symbol_master.py` | `pyproject`（uv add pypinyin）、`core/config.py`、`services/sync.py`、`jobs.py`、`types.py`、`bootstrap.py`、`settings.example.toml`、spec 两处修订 |
| 4 | `services/symbol_interest.py`、`services/scheduler/backfill.py` | `services/sync.py`、`scheduler/service.py`、`bootstrap.py`、web 四端点 |
| 5 | `services/symbol_search.py`、`web/templates/partials/symbol_search.html` | `repos/bars.py`（last_closes）、`web/pages/dashboard.py`、`watchlist.html` |
| 6 | — | `web/pages/symbol.py`、`symbol.html`、`watchlist.html`、`partials/quotes.html`、`web/pages/dashboard.py` |
| 7 | `web/templates/partials/indices.html` | `universe.py`、`services/intraday.py`、`datasource/tencent.py`+`sina_quote.py`（指数报价）、`dashboard.py`、`overview.html` |

---

## Task 1: 迁移 0021 与三张表的仓库

**Files:**
- Create: `src/sams/storage/migrations/0021_symbols_and_indices.sql`
- Create: `src/sams/storage/repos/symbol_list.py`
- Create: `src/sams/storage/repos/index_daily.py`
- Create: `src/sams/storage/repos/symbol_backfills.py`
- Test: `tests/storage/test_symbol_list.py`、`tests/storage/test_index_daily.py`、`tests/storage/test_symbol_backfills.py`

背景知识：`tests/conftest.py` 的 `db` fixture 给的是**已应用全部迁移的内存库**（`Database(duckdb.connect(":memory:"))` + `apply_migrations`）。`Database` 有三个方法：`execute(sql, params)`、`query(sql, params) -> pl.DataFrame`、`scalar(sql, params)`，批量写用 `with db.transaction():`。迁移文件名必须 `\d{4}_名字.sql`，下一号就是 0021（0020 已被 checksum 冻结，不许改）。

**DuckDB 2.0 实测约束（不要再试）：** 不支持 partial unique index（报 `Not implemented Error: Creating partial indexes is not supported`）。`symbol_backfills` 的「同一只票开放队列只许一条」因此用 `open_key` 列表达：排队期 `open_key = '{market}|{code}'`，结案时置 NULL——唯一索引里 NULL 互不冲突，结案的老行不再占位，同一票可重新排队。

- [ ] **Step 1: 写迁移文件**

```sql
-- src/sams/storage/migrations/0021_symbols_and_indices.sql
-- 0021：全市场代码表、指数日线、个股补数队列（SP1 D140/D144/D145）

CREATE TABLE symbol_master (
    market VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    name VARCHAR,
    py_initials VARCHAR NOT NULL,
    py_full VARCHAR NOT NULL,
    currency VARCHAR,
    is_active BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code)
);
CREATE INDEX symbol_master_search ON symbol_master (market, py_initials, code);

CREATE TABLE index_daily (
    market VARCHAR NOT NULL,
    index_code VARCHAR NOT NULL,
    date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE NOT NULL,
    volume DOUBLE,
    amount DOUBLE,
    prev_close DOUBLE,
    change_pct DOUBLE,
    source VARCHAR,
    PRIMARY KEY (market, index_code, date)
);

CREATE TABLE symbol_backfills (
    id VARCHAR PRIMARY KEY,
    market VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0,
    last_error VARCHAR,
    status VARCHAR NOT NULL,
    open_key VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at TIMESTAMPTZ
);
-- DuckDB 没有 partial unique index：开放期非空、结案置 NULL，NULL 不互相冲突
CREATE UNIQUE INDEX symbol_backfills_open ON symbol_backfills (open_key);
```

跑 `uv run pytest tests/storage/test_migrate.py -q` 确认迁移集仍能整卷应用（该文件已存在，用例逐个跑迁移）。

- [ ] **Step 2: 写 `tests/storage/test_symbol_list.py`（先失败）**

```python
"""代码表仓库：入库幂等、退市关停、五档排序一次 SQL 定序（D142）。"""

import pytest

from sams.storage.repos.symbol_list import SymbolListRepo

ROWS = [
    {
        "code": "000001",
        "name": "平安银行",
        "py_initials": "payh",
        "py_full": "pinganyinhang",
        "currency": "CNY",
    },
    {
        "code": "600519",
        "name": "贵州茅台",
        "py_initials": "gzym",
        "py_full": "guizhoumaotai",
        "currency": "CNY",
    },
    {
        "code": "00700",
        "name": "腾讯控股",
        "py_initials": "txkg",
        "py_full": "tengxunkonggu",
        "currency": "HKD",
    },
]


@pytest.fixture
def repo(db):
    r = SymbolListRepo(db)
    r.upsert_batch("A", ROWS[:2])
    r.upsert_batch("HK", ROWS[2:])
    return r


def test_get_and_replace(db, repo):
    assert repo.get("A", "000001")["name"] == "平安银行"
    assert repo.get("A", "000001")["currency"] == "CNY"
    repo.upsert_batch("A", [{**ROWS[0], "name": "平安银行A"}])
    assert repo.get("A", "000001")["name"] == "平安银行A"
    assert repo.get("A", "不存在") is None


def test_search_tiers(repo):
    # 第 1 档：精确代码
    assert repo.search("000001")[0]["code"] == "000001"
    # 第 2 档：首字母精确，输入大小写无关
    assert [row["code"] for row in repo.search("PAYH")] == ["000001"]
    # 第 3 档：全拼精确
    assert [row["code"] for row in repo.search("guizhoumaotai")] == ["600519"]
    # 第 4 档：首字母前缀
    assert [row["code"] for row in repo.search("gzy")] == ["600519"]
    # 第 5 档：名称片段（中文原样进 lower 不受伤）
    assert [row["code"] for row in repo.search("银行")] == ["000001"]
    # 跨市同码不相撞：007 前缀命中 HK/00700
    assert repo.search("007")[0]["market"] == "HK"


def test_search_skips_inactive_and_caps(repo):
    repo.deactivate_missing("A", ["000001"])
    assert [row["code"] for row in repo.search("gzy")] == []
    assert repo.search("000001")[0]["market"] == "A"  # 关停不删行，精确代码仍在
    assert len(repo.search("0", limit=10)) <= 10


def test_deactivate_missing_counts(repo):
    assert repo.deactivate_missing("A", ["000001"]) == 1
    assert repo.count("A") == 1
    assert repo.count() == 2
    assert repo.deactivate_missing("A", ["000001", "600519"]) == 0
```

- [ ] **Step 3: 跑它看失败**

Run: `uv run pytest tests/storage/test_symbol_list.py -q`
Expected: FAIL / collection error，原因都是 `ModuleNotFoundError: No module named 'sams.storage.repos.symbol_list'`。

- [ ] **Step 4: 实现 `src/sams/storage/repos/symbol_list.py`**

```python
"""`symbol_master` 的读写：全市场代码表，搜索加票的唯一入口。

五档排序（精确代码 > 首字母精确 > 全拼精确 > 前缀 > 片段）在一条 SQL 里定序：
拆成五段查询再 Python 合并，症状是每一档各自对、全局序错乱，而 LIMIT 10 截错边。
搜索永不 LIKE：`contains(lower(x), ?)` 是房规（见 `repos/annotations.py`）。
"""

from __future__ import annotations

from sams.storage.db import Database


class SymbolListRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_batch(self, market: str, rows: list[dict]) -> None:
        """一行一 INSERT OR REPLACE：整表周更本来就是全列覆盖。"""
        with self.db.transaction():
            for row in rows:
                self.db.execute(
                    "INSERT OR REPLACE INTO symbol_master"
                    " (market, code, name, py_initials, py_full, currency, is_active, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, true, now())",
                    [
                        market,
                        row["code"],
                        row["name"],
                        row["py_initials"],
                        row["py_full"],
                        row["currency"],
                    ],
                )

    def deactivate_missing(self, market: str, codes: list[str]) -> int:
        """把该市场当前活跃、却不在这份清单里的码关停；返回关停数。"""
        if not codes:
            return 0
        marks = ", ".join("?" for _ in codes)
        stale = self.db.scalar(
            f"SELECT count(*) FROM symbol_master WHERE market = ? AND is_active"
            f" AND code NOT IN ({marks})",
            [market, *codes],
        )
        self.db.execute(
            f"UPDATE symbol_master SET is_active = false, updated_at = now()"
            f" WHERE market = ? AND is_active AND code NOT IN ({marks})",
            [market, *codes],
        )
        return int(stale or 0)

    def get(self, market: str, code: str) -> dict | None:
        rows = self.db.query(
            "SELECT market, code, name, py_initials, py_full, currency, is_active"
            " FROM symbol_master WHERE market = ? AND code = ?",
            [market, code],
        )
        return None if rows.is_empty() else rows.to_dicts()[0]

    def search(self, q: str, limit: int = 10) -> list[dict]:
        """`q` 是归一好的检索词（大写片段）：py 列按小写比，code 按大写比。

        CTE `p` 只为了让 `?` 各出现一次——十来个重复 `?` 的实参表错位都错得安静。
        """
        rows = self.db.query(
            "WITH p AS (SELECT ? AS q_up, ? AS q_low)"
            " SELECT market, code, name, py_initials, py_full FROM ("
            "   SELECT s.market, s.code, s.name, s.py_initials, s.py_full,"
            "     CASE"
            "       WHEN s.code = p.q_up THEN 1"
            "       WHEN s.py_initials = p.q_low THEN 2"
            "       WHEN s.py_full = p.q_low THEN 3"
            "       WHEN starts_with(s.py_initials, p.q_low) OR starts_with(s.code, p.q_up) THEN 4"
            "       ELSE 5"
            "     END AS tier"
            "   FROM symbol_master s, p"
            "   WHERE s.is_active AND ("
            "     s.code = p.q_up OR s.py_initials = p.q_low OR s.py_full = p.q_low"
            "     OR starts_with(s.code, p.q_up) OR starts_with(s.py_initials, p.q_low)"
            "     OR contains(lower(s.name), p.q_low) OR contains(s.py_full, p.q_low))"
            ") ORDER BY tier, market, code LIMIT ?",
            [q.upper(), q.lower(), limit],
        )
        return rows.to_dicts()

    def count(self, market: str | None = None) -> int:
        if market is None:
            return int(self.db.scalar("SELECT count(*) FROM symbol_master WHERE is_active", []))
        return int(
            self.db.scalar(
                "SELECT count(*) FROM symbol_master WHERE is_active AND market = ?", [market]
            )
        )
```

- [ ] **Step 5: 跑它看通过**

Run: `uv run pytest tests/storage/test_symbol_list.py -q`
Expected: 4 passed。若 `WITH ... , p` 语法被 DuckDB 拒，改成 `FROM symbol_master s CROSS JOIN p`，语义不变。

- [ ] **Step 6: 写 `tests/storage/test_index_daily.py`（先失败）**

```python
"""指数日线：prev_close/change_pct 入库即算，增量行能接上存量行（D145）。"""

from datetime import date

import polars as pl

from sams.storage.repos.index_daily import IndexDailyRepo


def _frame(dates: list[date], closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A"] * len(dates),
            "index_code": ["sh000001"] * len(dates),
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * len(dates),
            "amount": [1.0] * len(dates),
            "source": ["tencent"] * len(dates),
        }
    )


def test_prev_close_chain_within_one_upsert(db):
    repo = IndexDailyRepo(db)
    written = repo.upsert(_frame([date(2026, 10, 20), date(2026, 10, 21)], [100.0, 101.0]))
    assert written == 2
    latest = repo.latest_with_change("A", "sh000001")
    assert latest.close == 101.0
    assert latest.change_pct == 1.0
    assert latest.day == date(2026, 10, 21)
    # 首日没有前收：prev_close 与 change_pct 都是 NULL，不是 0
    first = db.query(
        "SELECT prev_close, change_pct FROM index_daily WHERE date = ?",
        [date(2026, 10, 20)],
    ).to_dicts()[0]
    assert first["prev_close"] is None and first["change_pct"] is None


def test_incremental_upsert_sees_stored_rows(db):
    repo = IndexDailyRepo(db)
    repo.upsert(_frame([date(2026, 10, 20)], [100.0]))
    repo.upsert(_frame([date(2026, 10, 21)], [102.0]))
    assert repo.latest_with_change("A", "sh000001").change_pct == 2.0
    assert repo.latest_date("A", "sh000001") == date(2026, 10, 21)


def test_latest_missing_is_none(db):
    repo = IndexDailyRepo(db)
    assert repo.latest_with_change("A", "sh000001") is None
    assert repo.latest_date("A", "sh000001") is None
```

- [ ] **Step 7: 跑它看失败**

Run: `uv run pytest tests/storage/test_index_daily.py -q`
Expected: FAIL，`ModuleNotFoundError: ...repos.index_daily`。

- [ ] **Step 8: 实现 `src/sams/storage/repos/index_daily.py`**

```python
"""`index_daily` 的读写：大盘指数的日线，与个股日线分表分键。

000001 在 A 市是平安银行、在上证是指数——同表同键必撞，所以这里用源形态的
`index_code`（sh000001/hkHSI/usSPX）。prev_close/change_pct 在写入时算：休市看板
要能直接报「昨收 + 涨跌幅」，不该在读取侧再翻一遍历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from sams.storage.db import Database


@dataclass(frozen=True)
class IndexLatest:
    market: str
    index_code: str
    day: date
    close: float
    change_pct: float | None


class IndexDailyRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, frame: pl.DataFrame) -> int:
        """按 (票, 日期) 升序逐行写：事务内的顺序插入让下一行读到刚写入的前收。"""
        written = 0
        with self.db.transaction():
            for row in frame.sort(["market", "index_code", "date"]).to_dicts():
                prev = self.db.scalar(
                    "SELECT close FROM index_daily"
                    " WHERE market = ? AND index_code = ? AND date < ?"
                    " ORDER BY date DESC LIMIT 1",
                    [row["market"], row["index_code"], row["date"]],
                )
                # `if prev` 同时挡掉 None 和 0.0：除以前收为零是编出一个百分比
                change = (
                    round((row["close"] - prev) / prev * 100, 4)
                    if prev and row["close"] is not None
                    else None
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO index_daily"
                    " (market, index_code, date, open, high, low, close, volume, amount,"
                    "  prev_close, change_pct, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        row["market"],
                        row["index_code"],
                        row["date"],
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row["close"],
                        row.get("volume"),
                        row.get("amount"),
                        prev,
                        change,
                        row.get("source"),
                    ],
                )
                written += 1
        return written

    def latest_with_change(self, market: str, index_code: str) -> IndexLatest | None:
        rows = self.db.query(
            "SELECT market, index_code, date, close, change_pct FROM index_daily"
            " WHERE market = ? AND index_code = ? ORDER BY date DESC LIMIT 1",
            [market, index_code],
        )
        if rows.is_empty():
            return None
        row = rows.to_dicts()[0]
        return IndexLatest(
            market=row["market"],
            index_code=row["index_code"],
            day=row["date"],
            close=float(row["close"]),
            change_pct=None if row["change_pct"] is None else float(row["change_pct"]),
        )

    def latest_date(self, market: str, index_code: str) -> date | None:
        value = self.db.scalar(
            "SELECT max(date) FROM index_daily WHERE market = ? AND index_code = ?",
            [market, index_code],
        )
        if value is None:
            return None
        return value.date() if isinstance(value, datetime) else value
```

- [ ] **Step 9: 跑它看通过**

Run: `uv run pytest tests/storage/test_index_daily.py -q`
Expected: 3 passed。

- [ ] **Step 10: 写 `tests/storage/test_symbol_backfills.py`（先失败）**

```python
"""补数队列：open_key 去重、结案后可重开、三次失败转 deferred（D144）。"""

from sams.storage.repos.symbol_backfills import SymbolBackfillRepo


def test_enqueue_dedupe_and_reopen(db):
    repo = SymbolBackfillRepo(db)
    assert repo.enqueue("A", "600519") is True
    assert repo.enqueue("A", "600519") is False
    row = repo.open_for("A", "600519")
    repo.mark_done(row.id)
    assert repo.open_for("A", "600519") is None
    # 结案的老行 open_key 已置 NULL，不再占唯一键：同一只票可以再排
    assert repo.enqueue("A", "600519") is True


def test_note_failure_defers(db):
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519")
    row = repo.take_pending()[0]
    assert repo.note_failure(row.id, "boom\nstack") == "pending"
    assert repo.note_failure(row.id, "boom") == "pending"
    assert repo.note_failure(row.id, "boom") == "deferred"
    row = repo.open_for("A", "600519")
    # deferred 仍占着开放键；错误只存第一行
    assert (row.status, row.tries, row.last_error) == ("deferred", 3, "boom")


def test_take_pending_respects_limit(db):
    repo = SymbolBackfillRepo(db)
    for i in range(5):
        repo.enqueue("A", f"60000{i}")
    assert len(repo.take_pending(limit=3)) == 3


def test_mark_done_by_code(db):
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519")
    repo.mark_done_by_code("A", "600519")
    assert repo.open_for("A", "600519") is None
    assert repo.enqueue("A", "600519") is True
```

- [ ] **Step 11: 跑它看失败**

Run: `uv run pytest tests/storage/test_symbol_backfills.py -q`
Expected: FAIL，`ModuleNotFoundError: ...symbol_backfills`。

- [ ] **Step 12: 实现 `src/sams/storage/repos/symbol_backfills.py`**

```python
"""`symbol_backfills` 的读写：新票的补数排队簿。

`open_key`（`'{market}|{code}'`，结案置 NULL）是去重键：DuckDB 没有 partial
unique index，唯一索引里 NULL 互不冲突，于是「开放期只许一条」就靠非空那一份。
状态只有三个：pending（可试）、deferred（今日试满 3 次，让位盘后任务）、done。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sams.storage.db import Database


@dataclass(frozen=True)
class BackfillRow:
    id: str
    market: str
    code: str
    tries: int
    last_error: str | None
    status: str


class SymbolBackfillRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(self, market: str, code: str) -> bool:
        """排上返回 True；同一票已在开放队列里返回 False（不重复排队）。"""
        key = f"{market}|{code}"
        with self.db.transaction():
            # 查后插放在一个事务里：两个并发都看见 None 时，唯一索引是最后的兜底
            existing = self.db.scalar(
                "SELECT open_key FROM symbol_backfills WHERE open_key = ?", [key]
            )
            if existing is not None:
                return False
            self.db.execute(
                "INSERT INTO symbol_backfills (id, market, code, tries, status, open_key, created_at)"
                " VALUES (?, ?, ?, 0, 'pending', ?, now())",
                [uuid4().hex, market, code, key],
            )
        return True

    def take_pending(self, limit: int = 3) -> list[BackfillRow]:
        # 不回读 created_at：TIMESTAMPTZ 取回不稳（房规），这里只需要它排序
        rows = self.db.query(
            "SELECT id, market, code, tries, last_error, status FROM symbol_backfills"
            " WHERE status = 'pending' ORDER BY created_at, id LIMIT ?",
            [limit],
        )
        return [BackfillRow(**row) for row in rows.to_dicts()]

    def mark_done(self, row_id: str) -> None:
        self.db.execute(
            "UPDATE symbol_backfills SET status = 'done', open_key = NULL, done_at = now()"
            " WHERE id = ?",
            [row_id],
        )

    def mark_done_by_code(self, market: str, code: str) -> None:
        """盘后日线任务替补数队列收尾：只要成功写进行，开放的那条就算结清。"""
        self.db.execute(
            "UPDATE symbol_backfills SET status = 'done', open_key = NULL, done_at = now()"
            " WHERE market = ? AND code = ? AND open_key IS NOT NULL",
            [market, code],
        )

    def note_failure(self, row_id: str, error: str, *, max_tries: int = 3) -> str:
        """记一次失败，返回新状态：pending 或 deferred。"""
        first_line = (error.splitlines() or [""])[0][:200]
        with self.db.transaction():
            self.db.execute(
                "UPDATE symbol_backfills SET tries = tries + 1, last_error = ?,"
                " status = CASE WHEN tries + 1 >= ? THEN 'deferred' ELSE status END"
                " WHERE id = ?",
                [first_line, max_tries, row_id],
            )
            status = self.db.scalar(
                "SELECT status FROM symbol_backfills WHERE id = ?", [row_id]
            )
        return str(status)

    def open_for(self, market: str, code: str) -> BackfillRow | None:
        rows = self.db.query(
            "SELECT id, market, code, tries, last_error, status FROM symbol_backfills"
            " WHERE open_key = ?",
            [f"{market}|{code}"],
        )
        return None if rows.is_empty() else BackfillRow(**rows.to_dicts()[0])
```

- [ ] **Step 13: 跑全部三个测试看通过**

Run: `uv run pytest tests/storage/test_symbol_list.py tests/storage/test_index_daily.py tests/storage/test_symbol_backfills.py tests/storage/test_migrate.py -q`
Expected: 全 passed。

- [ ] **Step 14: Commit**

```bash
git add src/sams/storage/migrations/0021_symbols_and_indices.sql src/sams/storage/repos/symbol_list.py src/sams/storage/repos/index_daily.py src/sams/storage/repos/symbol_backfills.py tests/storage/test_symbol_list.py tests/storage/test_index_daily.py tests/storage/test_symbol_backfills.py
git commit -m "feat(storage): 0021 三表与仓库——代码表、指数日线与补数队列（D140/D144/D145）"
```

---

## Task 2: 两个新源能力——`symbol_list` 与 `index_daily`

**Files:**
- Modify: `src/sams/core/codes.py`（加 `looks_like_index_code`）
- Create: `src/sams/datasource/symbol_list.py`、`src/sams/datasource/index_daily.py`
- Modify: `src/sams/datasource/capabilities.py`、`src/sams/datasource/registry.py`、`src/sams/datasource/akshare_cn.py`、`src/sams/datasource/akshare_hk.py`、`src/sams/datasource/tencent.py`、`src/sams/datasource/sina.py`
- Test: `tests/core/test_codes.py`（追加）、`tests/datasource/test_symbol_list.py`（新建）、`tests/datasource/test_registry.py`（追加）、`tests/datasource/test_akshare_listed_date.py` 同风格新建 `tests/datasource/test_akshare_symbol_list.py`、`tests/datasource/test_tencent.py`/`test_sina.py` 追加

先读这几处再动手（形状照抄，别发明）：`registry.py` 的 `FetchResult`（128-141，`degraded` 就是 `len(attempts) > 1`）、`fetch_daily`（376-416）、`fetch_fx`（598-663）、`_call_fx`（665-709）、`_call`（711-756，已带 `*, method`）；`capabilities.py` 的 `fx_rate` 条目（48）；`akshare_cn.py` 的取数注入与 `_as_polars`；`tests/datasource/test_registry.py` 的 FakeAdapter。

- [ ] **Step 1: `looks_like_index_code` 先测试后实现**

追加到 `tests/core/test_codes.py`：

```python
def test_looks_like_index_code():
    from sams.core.codes import looks_like_index_code

    assert looks_like_index_code("A", "sh000001")
    assert looks_like_index_code("A", "sz399006")
    assert not looks_like_index_code("A", "000001")  # 个股归一形态
    assert not looks_like_index_code("A", "sh1000002")  # 位数不对
    assert looks_like_index_code("HK", "hkHSI")
    assert looks_like_index_code("HK", "hkHSTECH")
    assert not looks_like_index_code("HK", "00700")
    assert looks_like_index_code("US", "usSPX")
    assert not looks_like_index_code("US", "AAPL")
    assert not looks_like_index_code("", "sh000001")  # 市场不认就当不是
```

`core/codes.py` 实现（放文件末尾）：

```python
def looks_like_index_code(market: str, code: str) -> bool:
    """源形态的指数码（sh000001/hkHSI/usSPX）。

    000001 是平安银行也是上证指数：区分靠这层市场前缀，轮询与入库据此分叉，
    否则个股表和指数表会互相冒名。HK/US 后缀 2~6 位大写字母（HSCEI 5 位、HSTECH 6 位）。
    """
    c = code.strip()
    tail = c[2:]
    if not tail:
        return False
    if market == "A":
        return c[:2].lower() in ("sh", "sz", "bj") and tail.isdigit() and len(tail) == 6
    if market == "HK":
        return c[:2].lower() == "hk" and tail.isalpha() and tail.isupper() and len(tail) <= 6
    if market == "US":
        return c[:2].lower() == "us" and tail.isalpha() and tail.isupper() and len(tail) <= 6
    return False
```

Run: `uv run pytest tests/core/test_codes.py -q` 先失败（ImportError）后通过。

- [ ] **Step 2: 新建 `datasource/symbol_list.py` 与 `datasource/index_daily.py` + 测试**

`tests/datasource/test_symbol_list.py`:

```python
"""代码表归一：只留 (code, name) 两列，代码进规范形态，空答复就是 EmptyResponse。"""

import polars as pl
import pytest

from sams.core.errors import EmptyResponse
from sams.datasource.symbol_list import has_symbol_list, normalize_symbol_list


def test_normalize_two_column_shapes():
    en = normalize_symbol_list("A", pl.DataFrame({"code": ["600519"], "name": [" 贵州茅台 "]}))
    assert en.to_dicts() == [{"code": "600519", "name": "贵州茅台"}]
    cn = normalize_symbol_list("HK", pl.DataFrame({"代码": ["00700"], "名称": ["腾讯控股"]}))
    assert cn.to_dicts() == [{"code": "00700", "name": "腾讯控股"}]


def test_normalize_canonicalizes_and_dedupes():
    out = normalize_symbol_list("HK", pl.DataFrame({"代码": ["700", "hk0700", ""], "名称": ["腾讯", "腾讯", "空"]}))
    assert out.height == 1  # 别名归到 00700 只留一条，空码丢弃
    assert out["code"][0] == "00700"


def test_empty_is_empty_response():
    with pytest.raises(EmptyResponse):
        normalize_symbol_list("A", pl.DataFrame({"code": [], "name": []}))


def test_missing_columns_is_empty_response():
    with pytest.raises(EmptyResponse):
        normalize_symbol_list("A", pl.DataFrame({"id": ["1"]}))


def test_has_symbol_list():
    class With:
        def symbol_list(self, market): ...

    class Without:
        pass

    assert has_symbol_list(With())
    assert not has_symbol_list(Without())
```

`src/sams/datasource/symbol_list.py`:

```python
"""代码表取数的公共形状：源给什么列，出口只留 (code, name) 两列。

`symbol_list(market)` 与 `daily_bars` 的差别是没有时间窗：一次答全表。
空表与缺列都归 `EmptyResponse`——「这源没这张表」与「这张表是空的」对降级链
是同一句话（继续问下一个源），不该攒成 `empty_everywhere` 之外的失败。
"""

from __future__ import annotations

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import EmptyResponse


def has_symbol_list(obj) -> bool:
    return callable(getattr(obj, "symbol_list", None))


def empty_symbol_list() -> pl.DataFrame:
    return pl.DataFrame(schema={"code": pl.Utf8, "name": pl.Utf8})


def normalize_symbol_list(market: str, frame: pl.DataFrame) -> pl.DataFrame:
    lowered = {c.lower(): c for c in frame.columns}
    code_col = lowered.get("code") or lowered.get("代码")
    name_col = lowered.get("name") or lowered.get("名称")
    if code_col is None or name_col is None:
        raise EmptyResponse("symbol_list", f"代码表缺 code/name 列：{frame.columns}")
    rows: list[dict] = []
    seen: set[str] = set()
    for row in frame.select(code_col, name_col).iter_rows(named=True):
        raw_code = str(row[code_col] or "").strip()
        raw_name = str(row[name_col] or "").strip()
        if not raw_code:
            continue
        code = canonical_code(market, raw_code)
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": raw_name or None})
    if not rows:
        raise EmptyResponse("symbol_list", f"{market} 代码表归一后为空")
    return pl.DataFrame(rows)
```

`src/sams/datasource/index_daily.py`:

```python
"""`index_daily` 能力的探针：形状与 `has_bars` 一路，方法名不同而已。"""

from __future__ import annotations


def has_index_daily(obj) -> bool:
    return callable(getattr(obj, "index_daily", None))
```

Run 上面测试文件，先失败后通过（实现照给）。

- [ ] **Step 3: capabilities 注册两条**

`capabilities.py` 的 `CAPABILITIES` 里、`fx_rate` 条目之后加（probe 用步骤 2 的函数，`from sams.datasource.index_daily import has_index_daily` 等同文件既有 import 风格）：

```python
    "symbol_list": Capability(label="代码表", lacking="不提供代码表", probe=has_symbol_list),
    "index_daily": Capability(label="指数日线", lacking="不提供指数日线", probe=has_index_daily),
```

追加断言到 `tests/datasource/test_capabilities.py`：`capability("symbol_list").label == "代码表"`、`capability("index_daily")` 可取到且 probe 对无该方法对象为 False。

- [ ] **Step 4: registry——`fetch_index_daily`（先测后码）**

追加到 `tests/datasource/test_registry.py`（FakeAdapter 用该文件既有那个，给它加 `index_daily` / `symbol_list` 方法的能力时按现有 FakeAdapter 的构造方式来）：

```python
def test_fetch_index_daily_walks_chain(...):
    # 第一个源 _SourceFailed（抛 DatasourceError），第二个返回帧：结果 source 是第二个、degraded=True
    # 全部 EmptyResponse：empty_everywhere=True、frame 空
    # 全链没有 index_daily 能力：抛 DatasourceError，消息含「指数日线」

def test_fetch_symbol_list_market_level(...):
    # A 链：akshare 样子的源返回 (code,name) 帧 → result.source/frame 对
    # 全部无能力 → DatasourceError；能力有但 0 行 → empty_everywhere=True
```

测试写法完全仿照该文件里 `fetch_daily` 与 `fetch_fx` 的既有用例（如何注入 adapters、如何断言 attempts），只是方法名换成新的两个。

`registry.py` 实现三步：

1. **抽 `_fetch_frame`**：把 `fetch_daily`（376-416）的函数体原样搬进新方法 `_fetch_frame(self, market: str, code: str, start: date, end: date, *, method: str) -> FetchResult`；体内 381 行的 `_walk(..., method="daily_bars")` 改成 `_walk(..., method=method)`；413 行异常消息里的「日线」改成 `capability(method).label`。`fetch_daily` 变成：

```python
    def fetch_daily(self, market: str, code: str, start: date, end: date) -> FetchResult:
        return self._fetch_frame(market, code, start, end, method="daily_bars")

    def fetch_index_daily(self, market: str, index_code: str, start: date, end: date) -> FetchResult:
        """指数日线链：与日线走同一条 `_walk`——「没有」「挂了」「没这能力」
        在两张表上必须是同一套语义，否则休市看板的空与错分不清。"""
        return self._fetch_frame(market, index_code, start, end, method="index_daily")
```

2. **`SymbolListResult`**：仿 `FetchResult`（同一区域文件里）：

```python
@dataclass(frozen=True)
class SymbolListResult:
    market: str
    source: str
    frame: pl.DataFrame
    attempts: tuple[Attempt, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    empty_everywhere: bool = False

    @property
    def degraded(self) -> bool:
        return len(self.attempts) > 1
```

3. **`fetch_symbol_list` + `_call_symbol_list`**：抄 `fetch_fx`/`_call_fx`（598-709）的形状，差别只有：按 `self.sources_for(market)` 路由（代码表有市场维度，汇率没有）、调 `adapter.symbol_list(market)`、空帧常量 `empty_symbol_list()`、无日期裁剪。`_call_symbol_list` 逐行照抄 `_call_fx` 的限速/退避/`_mark_ok`/`_mark_failure`/`_SourceFailed` 归类，把 `adapter.fx_rate(base, quote, start, end)` 换成 `adapter.symbol_list(market)`、label 换成 `f"{name}:symbol_list {market}"`、0 行抛 `EmptyResponse(name, f"{market} 代码表返回 0 行")`、删去日期裁剪段。`fetch_symbol_list` 的终错消息：`f"{market} 没有能取代码表的源：链 {list(self.sources_for(market))}"`（无 attempts 时）与 `f"{market} 所有源均取代码表失败 → {detail}"`（全失败时，用 `_terminal_error`）。

Run: `uv run pytest tests/datasource/test_registry.py -q` 通过，且既有用例零破坏。

- [ ] **Step 5: 四个适配器方法（先测后码）**

`tests/datasource/test_akshare_symbol_list.py`（注入写法参照 `tests/datasource/test_akshare_listed_date.py`）：

```python
"""A 股代码表 = ak.stock_info_a_code_name()，HK = ak.stock_hk_spot_em() 的 代码/名称 列。"""

import polars as pl

from sams.datasource.akshare_cn import AkshareAdapter


def test_a_symbol_list_injected():
    fake = lambda: pl.DataFrame({"code": ["000001", "600519 "], "name": ["平安银行", "贵州茅台"]})
    adapter = AkshareAdapter(...既有构造参数..., list_fetch=fake)
    out = adapter.symbol_list("A")
    assert out.to_dicts() == [
        {"code": "000001", "name": "平安银行"},
        {"code": "600519", "name": "贵州茅台"},
    ]


def test_a_symbol_list_other_market_empty():
    ...  # adapter.symbol_list("HK") 抛 EmptyResponse
```

HK 那份同理：注入 `list_fetch` 返回 `pl.DataFrame({"代码": ["00700"], "名称": ["腾讯控股"]})`。

实现：`AkshareAdapter.__init__` 增 `list_fetch` 注入参数（默认 None，缺省实现里 lazy `import akshare as ak` 后 `ak.stock_info_a_code_name()`，**照抄该文件既有 fetch 注入的默认写法**）；方法体：市场不是 "A" 抛 `EmptyResponse("akshare", ...)`，否则 `normalize_symbol_list("A", self._as_polars(self._list_fetch()))`。`AkshareHkAdapter` 同样加 `list_fetch`（默认 `ak.stock_hk_spot_em`），市场不是 "HK" 抛空，取 代码/名称 列后 `normalize_symbol_list("HK", ...)`（HK 适配器里若无 `_as_polars`，复制 cn 那份的三行实现）。

腾讯/新浪 `index_daily` 的测试参照各自文件里 `daily_bars` 的既有测法（录制 payload / 注入 fetch）：

```python
# tests/datasource/test_tencent.py 追加
def test_tencent_index_daily_a():
    # 注入 http fetch 录好的日 K payload；adapter.index_daily("A", "sh000001", start, end)
    # 断言返回列就是 BAR_COLUMNS 规范集、code 列 == "sh000001"（源形态原样，没过 tencent_symbol）

def test_tencent_index_daily_us_empty():
    # adapter.index_daily("US", "usSPX", ...) 抛 EmptyResponse
```

新浪同理且 `index_daily("HK", ...)` 抛 EmptyResponse（新浪适配器只有 A）。

实现：
- `TencentAdapter.index_daily(self, market, code, start, end)`：开头 `if market not in ("A", "HK"): raise EmptyResponse("tencent", f"腾讯指数日线没有 {market} 市场")`；其余**照抄本文件 `daily_bars`（328 起）的函数体**，仅把构造腾讯符号那一步（`tencent_symbol(market, code)` 调用）替换为直接用 `code`——指数码已带源前缀，`tencent_symbol` 会把 sh000001 折成错的符号；KLINE_PATHS、`kline_param`、`parse_tencent_daily`、`normalize_bars` 全部原样。
- `SinaAdapter.index_daily`：开头 `if market != "A": raise EmptyResponse("sina", f"新浪指数日线只有 A 股，收到 {market}")`；其余照抄本文件 `daily_bars` 的函数体，把 `a_share_symbol(...)` 构造换成 `code` 原样。MAX_BARS 窗口逻辑保留。

Run: `uv run pytest tests/datasource -q` 全绿。

- [ ] **Step 6: Commit**

```bash
git add src/sams/core/codes.py src/sams/datasource/symbol_list.py src/sams/datasource/index_daily.py src/sams/datasource/capabilities.py src/sams/datasource/registry.py src/sams/datasource/akshare_cn.py src/sams/datasource/akshare_hk.py src/sams/datasource/tencent.py src/sams/datasource/sina.py tests/core/test_codes.py tests/datasource/test_symbol_list.py tests/datasource/test_akshare_symbol_list.py tests/datasource/test_registry.py tests/datasource/test_capabilities.py tests/datasource/test_tencent.py tests/datasource/test_sina.py
git commit -m "feat(datasource): 代码表与指数日线两个新能力——akshare 清单、腾讯/新浪指数（D148）"
```

---

## Task 3: 代码表周同步、指数日线入库与 `[indices]` 配置

**Files:**
- Create: `src/sams/services/symbol_master.py`
- Modify: `src/sams/core/config.py`、`src/sams/services/sync.py`、`src/sams/services/scheduler/jobs.py`、`src/sams/services/scheduler/types.py`、`src/sams/bootstrap.py`、`config/settings.example.toml`
- Modify（文档）: `docs/superpowers/specs/2026-10-26-sp1-symbol-master-search-autosync-design.md` 两处修订（见 Step 6）
- Test: `tests/services/test_symbol_master.py`（新建）、`tests/core/test_config.py`（追加）、`tests/services/test_sync.py`（追加）、jobs 测试追加到 `tests/services/scheduler/` 下既有的 jobs 测试文件（先看该文件如何拼 stub `JobDeps`，照它的样子加）
- 依赖: `uv add pypinyin`（pypinyin 不在 `test_layering.py` 的 GATED 名单里，新三方库直接放行；若要登记卫生项，可在 GATED 加 `"pypinyin": ("services/symbol_master.py",)`，非必须）

- [ ] **Step 1: 装依赖**

```bash
uv add pypinyin
```

- [ ] **Step 2: `pinyin_columns` + `SymbolMasterSyncService`（先测后码）**

`tests/services/test_symbol_master.py`:

```python
"""代码表周同步：拼音两列写时算、全链空答复不动表（D141/D147）。"""

import polars as pl
import pytest

from sams.datasource.registry import SymbolListResult
from sams.services.symbol_master import SymbolMasterSyncService, pinyin_columns
from sams.storage.repos.symbol_list import SymbolListRepo


def test_pinyin_columns():
    assert pinyin_columns("平安银行") == ("payh", "pinganyinhang")  # 行在银行里读 háng
    assert pinyin_columns(" 贵州 茅台 ") == ("gzym", "guizhoumaotai")
    assert pinyin_columns("AB Corp") == ("abcorp", "abcorp")  # 非中文原样小写


class StubRegistry:
    def __init__(self, result):
        self._result = result

    def fetch_symbol_list(self, market):
        return self._result


def _result(rows):
    return SymbolListResult(
        market="A",
        source="akshare",
        frame=pl.DataFrame(rows) if rows else pl.DataFrame(schema={"code": pl.Utf8, "name": pl.Utf8}),
        empty_everywhere=not rows,
    )


@pytest.fixture
def repo(db):
    return SymbolListRepo(db)


def test_sync_writes_py_and_currency(db, repo, settings):
    svc = SymbolMasterSyncService(StubRegistry(_result([{"code": "000001", "name": "平安银行"}])), repo, settings.markets)
    summary = svc.sync("A")
    assert (summary.rows, summary.deactivated, summary.source) == (1, 0, "akshare")
    row = repo.get("A", "000001")
    assert row["py_initials"] == "payh" and row["py_full"] == "pinganyinhang"
    assert row["currency"] == "CNY"


def test_sync_deactivates_gone_codes(db, repo, settings):
    repo.upsert_batch("A", [{"code": "600000", "name": "老票", "py_initials": "lp", "py_full": "lao", "currency": "CNY"}])
    svc = SymbolMasterSyncService(StubRegistry(_result([{"code": "000001", "name": "平安银行"}])), repo, settings.markets)
    summary = svc.sync("A")
    assert summary.deactivated == 1
    assert repo.get("A", "600000")["is_active"] is False


def test_empty_everywhere_keeps_old_table(db, repo, settings):
    repo.upsert_batch("A", [{"code": "000001", "name": "平安银行", "py_initials": "payh", "py_full": "pinganyinhang", "currency": "CNY"}])
    svc = SymbolMasterSyncService(StubRegistry(_result([])), repo, settings.markets)
    summary = svc.sync("A")
    assert (summary.rows, summary.deactivated, summary.kept_old) == (0, 0, True)
    assert repo.get("A", "000001")["is_active"] is True  # 网络抖动一次不许把全市场标成退市
```

Run: `uv run pytest tests/services/test_symbol_master.py -q` 先失败。

`src/sams/services/symbol_master.py`:

```python
"""symbol_master 的周同步：整张 A/HK 清单拉回来，算好拼音两列再入库。

py_initials/py_full 在写时算（D141）：整词交给 pypinyin 才有多音字（银行的行读 háng），
但每次检索再算是把活放错了地方。降级链的「空」与「挂」分叉在 registry，这里只处理
一个决定：全链空答复 = 整表原样保留——一周一次的同步源同时挂掉与代码真没了，
对旧表是两种处置。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pypinyin import Style, lazy_pinyin

from sams.core.codes import currency_override
from sams.core.config import MarketCfg
from sams.storage.repos.symbol_list import SymbolListRepo

logger = logging.getLogger("sams.services.symbol_master")


def pinyin_columns(name: str) -> tuple[str, str]:
    cleaned = "".join(name.split())
    initials = "".join(lazy_pinyin(cleaned, style=Style.FIRST_LETTER)).lower()
    full = "".join(lazy_pinyin(cleaned, style=Style.NORMAL)).lower()
    return initials, full


@dataclass(frozen=True)
class SymbolMasterSummary:
    market: str
    source: str | None
    rows: int
    deactivated: int
    degraded: bool
    #: 全链空答复，旧表原样保留（D147）
    kept_old: bool = False


class SymbolMasterSyncService:
    def __init__(
        self,
        registry,
        symbol_list: SymbolListRepo,
        markets: dict[str, MarketCfg],
    ) -> None:
        self.registry = registry
        self.symbol_list = symbol_list
        self.markets = markets

    def sync(self, market: str) -> SymbolMasterSummary:
        result = self.registry.fetch_symbol_list(market)
        if result.empty_everywhere or result.frame.is_empty():
            logger.warning("%s 代码表所有源都答空，保留现有表", market)
            return SymbolMasterSummary(market, None, 0, 0, result.degraded, kept_old=True)
        rows = []
        for item in result.frame.to_dicts():
            name = (item["name"] or "").strip()
            initials, full = pinyin_columns(name) if name else ("", "")
            rows.append(
                {
                    "code": item["code"],
                    "name": name or None,
                    "py_initials": initials,
                    "py_full": full,
                    "currency": currency_override(market, item["code"])
                    or self.markets[market].currency,
                }
            )
        self.symbol_list.upsert_batch(market, rows)
        deactivated = self.symbol_list.deactivate_missing(market, [row["code"] for row in rows])
        logger.info("%s 代码表 %s 行（源 %s），关停 %s", market, len(rows), result.source, deactivated)
        return SymbolMasterSummary(market, result.source, len(rows), deactivated, result.degraded)
```

Run: 该文件通过。

- [ ] **Step 3: `[indices]` 配置（先测后码）**

追加到 `tests/core/test_config.py`（写 TOML 再 `Settings.load` 的用例风格照该文件既有的来）：

```python
def test_indices_defaults(tmp_path):
    # 用既有写 toml + load 的路子造一份不含 [indices] 的配置
    s = ...
    assert s.indices.for_market("A") == ("sh000001", "sz399001", "sz399006")
    assert s.indices.for_market("HK") == ("hkHSI", "hkHSTECH", "hkHSCEI")
    assert s.indices.for_market("US") == ("usSPX", "usIXIC", "usDJI")
    assert s.indices.label("sh000001") == "上证指数"


def test_indices_override(tmp_path):
    # toml 里 [indices.A] codes = ["sh000001"] 与 [indices.labels] sh000001 = "沪综指"
    s = ...
    assert s.indices.for_market("A") == ("sh000001",)
    assert s.indices.label("sh000001") == "沪综指"
    assert s.indices.label("unknown") == "unknown"
```

`core/config.py`：在 `MarketCfg` 之后加常量与 `IndicesCfg`（形状抄 `MarketCfg`，同为 frozen dataclass；`Mapping` 从 `collections.abc` 引）：

```python
DEFAULT_INDEX_CODES: dict[str, tuple[str, ...]] = {
    "A": ("sh000001", "sz399001", "sz399006"),
    "HK": ("hkHSI", "hkHSTECH", "hkHSCEI"),
    "US": ("usSPX", "usIXIC", "usDJI"),
}

DEFAULT_INDEX_LABELS: dict[str, str] = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "hkHSI": "恒生指数",
    "hkHSTECH": "恒生科技指数",
    "hkHSCEI": "H股指数",
    "usSPX": "标普500",
    "usIXIC": "纳斯达克",
    "usDJI": "道琼斯",
}


@dataclass(frozen=True)
class IndicesCfg:
    """大盘条问「画哪几只、叫什么」，答案全部在这里（D145）。"""

    codes_by_market: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: {k: tuple(v) for k, v in DEFAULT_INDEX_CODES.items()}
    )
    labels: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_INDEX_LABELS))

    def for_market(self, market: str) -> tuple[str, ...]:
        return tuple(self.codes_by_market.get(market, ()))

    def label(self, index_code: str) -> str:
        return self.labels.get(index_code, index_code)
```

`Settings` 数据体加一行（放在同样带 default 的字段群中间）：

```python
    indices: IndicesCfg = field(default_factory=IndicesCfg)
```

`Settings.load`（147-226，仿 `server_raw = raw.get("server") or {}` 那段的形状）在构造 Settings 之前插入：

```python
    idx_raw = raw.get("indices") or {}
    codes_by_market: dict[str, tuple[str, ...]] = {}
    for market_name in markets:
        given = (idx_raw.get(market_name) or {}).get("codes")
        codes_by_market[market_name] = (
            tuple(given) if given else tuple(DEFAULT_INDEX_CODES.get(market_name, ()))
        )
    labels = dict(DEFAULT_INDEX_LABELS)
    labels.update({str(k): str(v) for k, v in (idx_raw.get("labels") or {}).items()})
    indices = IndicesCfg(codes_by_market=codes_by_market, labels=labels)
```

并在 load 末尾构造 `Settings(...)` 处加 `indices=indices`。

`config/settings.example.toml` 末尾加注释样例（`settings.toml` 不动）：

```toml
# 大盘条指数清单：写了的市场整组覆盖默认，不写用内置九只
# [indices.A]
# codes = ["sh000001"]
# [indices.labels]
# sh000001 = "上证指数"
```

- [ ] **Step 4: `SyncService.sync_index` + `indices` 注入（先测后码）**

追加到 `tests/services/test_sync.py`（StubRegistry/make_service 用该文件既有的，扩一个 `index_frames` 出口）：

```python
def test_sync_index_writes_with_prev(db, settings):
    from sams.storage.repos.index_daily import IndexDailyRepo

    # StubRegistry 的 fetch_index_daily 返回三日帧（code 列 = sh000001）
    svc = ...  # SyncService(registry, BarRepo(db), SymbolRepo(db), settings.markets, indices=IndexDailyRepo(db))
    summary = svc.sync_index("A", "sh000001", date(2026, 10, 19), date(2026, 10, 21))
    assert summary.written == 3
    latest = IndexDailyRepo(db).latest_with_change("A", "sh000001")
    assert latest.change_pct is not None
    # 指数不建主档
    assert SymbolRepo(db).get("A", "sh000001") is None


def test_sync_index_empty(db, settings):
    # StubRegistry 返回 empty_everywhere=True 的 FetchResult
    summary = svc.sync_index("A", "sh000001", ...)
    assert summary.empty is True and summary.written == 0
```

`services/sync.py`：`__init__` 加 keyword-only 参数 `indices: IndexDailyRepo | None = None` 并存 `self.indices`；新方法：

```python
    def sync_index(self, market: str, index_code: str, start: date, end: date) -> SyncSummary:
        """指数日线：降级链与个股同一条，但不补主档——指数不是票。

        帧里那列 `code` 换成 `index_code` 再入库：指数表与个股表分键（000001 两边都占）。
        """
        assert self.indices is not None  # job 登记前已查过能力与注入，这里只兜编程错误
        result = self.registry.fetch_index_daily(market, index_code, start, end)
        if result.empty_everywhere:
            return SyncSummary(
                market=market,
                code=index_code,
                empty=True,
                latest_date=self.indices.latest_date(market, index_code),
                attempts=result.attempts,
            )
        frame = result.frame.rename({"code": "index_code"})
        written = self.indices.upsert(frame)
        return SyncSummary(
            market=market,
            code=index_code,
            source=result.source,
            fetched=frame.height,
            written=written,
            degraded=result.degraded,
            latest_date=self.indices.latest_date(market, index_code),
            attempts=result.attempts,
        )
```

- [ ] **Step 5: jobs 与 JobDeps（先测后码）**

追加到 `tests/services/scheduler/` 既有 jobs 测试文件：

```python
def test_run_indices_window(...):
    # 照该文件既有用例拼 deps（sync.indices 给一个记录了调用的 stub repo：latest_date 返回
    # ctx.as_of - 1 → sync_index 被叫一次；返回 ctx.as_of 当天 → 追平，一次都不叫）
    outcome = run_indices(deps, ctx)
    assert outcome.ok

def test_build_jobs_gates_new_specs(...):
    # deps.symbol_master=None 时不登记 symbol_master；给实例且链上有 symbol_list 能力才登记
    # deps.sync.indices=None 或链上没 index_daily 能力时不登记 indices
    names = {(spec.name, spec.market) for spec in build_jobs(deps)}
```

`types.py`：`JobDeps` 末尾加 `symbol_master: SymbolMasterSyncService | None = None`（`from __future__ import annotations` 在，前向引用无需真 import；如该文件已 import services 就直接用真类型）。**带默认值才不破坏既有全部 `JobDeps(...)` 构造点。**

`jobs.py`：

```python
def run_symbol_master(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """代码表一周一次，单元是整个市场：一张表拆不成「按只隔离」。"""
    assert ctx.market is not None
    try:
        summary = deps.symbol_master.sync(ctx.market)
    except SamsError as exc:
        return JobOutcome(note=f"{ctx.market} 代码表失败", errors=(f"{ctx.market}: {exc}",))
    return JobOutcome(
        rows_affected=summary.rows,
        note=f"{ctx.market} 代码表 {summary.rows} 行、关停 {summary.deactivated}",
    )


def run_indices(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    assert ctx.market is not None
    assert deps.sync.indices is not None

    def one(market: str, index_code: str) -> int:
        start = _window(deps.sync.indices.latest_date(market, index_code), ctx.as_of)
        if start is None:
            return 0
        return deps.sync.sync_index(market, index_code, start, ctx.as_of).written

    units = [(ctx.market, ic) for ic in deps.settings.indices.for_market(ctx.market)]
    return _collect(units, one, "指数日线")
```

`build_jobs` 的市场循环改成（注释一行说顺序为什么）：

```python
    for market in sorted(deps.settings.markets):
        # 代码表排市场组最前：当晚新上市的票先进表，后面的搜索与补数才找得到它
        if deps.symbol_master is not None and _has(deps, market, "symbol_list"):
            jobs.append(JobSpec("symbol_master", WEEKLY, market, partial(run_symbol_master, deps)))
        for method, name, run in _DATA_JOBS:
            if _has(deps, market, method):
                jobs.append(JobSpec(name, DAILY, market, partial(run, deps)))
        if deps.sync.indices is not None and _has(deps, market, "index_daily"):
            jobs.append(JobSpec("indices", DAILY, market, partial(run_indices, deps)))
        jobs.append(JobSpec("recheck", WEEKLY, market, partial(run_recheck, deps)))
        jobs.append(JobSpec("rules", DAILY, market, partial(run_rules, deps)))
```

- [ ] **Step 6: bootstrap 接线 + spec 修订**

`bootstrap.py`（构造顺序：仓库在消费方之前）：

```python
    symbol_list_store = SymbolListRepo(database)
    index_daily_store = IndexDailyRepo(database)
```

放在 `sync = SyncService(...)`（约 574 行）之前；`SyncService` 那行加 `indices=index_daily_store`；再构造：

```python
    symbol_master_sync = SymbolMasterSyncService(registry, symbol_list_store, cfg.markets)
```

`JobDeps(...)` 加 `symbol_master=symbol_master_sync`；`ServiceContext` 数据体（157 起那串字段）加 `symbol_list_store`、`index_daily_store`、`symbol_master_sync` 三个字段并在 `return ServiceContext(...)` 里传入（该数据体是无默认字段的构造，所有构造点只有 bootstrap，测试经 `build_core`/`contexts` 走同一条路）。

spec 文档同交（一处措辞 + 一节）：`docs/superpowers/specs/2026-10-26-sp1-symbol-master-search-autosync-design.md`
1. §2.1 的「partial unique index」句改为 open_key 方案，注明「DuckDB 2.0 实测不支持 partial unique index，`open_key` 非空占位、结案置 NULL 放行重排」；
2. §2.4 删去 `symbol_master_sync_weekday` 配置项那半句，注「WEEKLY 周期本身在整周收满时到期（`due.py` 的 weekly_due），不设星期几旋钮」。

- [ ] **Step 7: 全量本任务相关测试 + Commit**

Run: `uv run pytest tests/services/test_symbol_master.py tests/services/test_sync.py tests/core/test_config.py tests/services/scheduler -q`
Expected: 全 passed（scheduler 目录把 jobs/due/tick 的既有用例一起跑一遍，确认 `build_jobs` 改动零破坏）。

```bash
git add pyproject.toml uv.lock src/sams/services/symbol_master.py src/sams/core/config.py src/sams/services/sync.py src/sams/services/scheduler/jobs.py src/sams/services/scheduler/types.py src/sams/bootstrap.py config/settings.example.toml docs/superpowers/specs/2026-10-26-sp1-symbol-master-search-autosync-design.md tests/services/test_symbol_master.py tests/core/test_config.py tests/services/test_sync.py tests/services/scheduler
git commit -m "feat(services): 代码表周同步与指数日线入库——pypinyin 两列、[indices] 配置（D141/D145）"
```

---

## Task 4: 新票自动排拍——`SymbolInterest` 四入口 + 每拍消费

**Files:**
- Create: `src/sams/services/symbol_interest.py`、`src/sams/services/scheduler/backfill.py`
- Modify: `src/sams/services/sync.py`（backfills 注入 + 结案钩子）、`src/sams/services/scheduler/service.py`（tick 接线）、`src/sams/bootstrap.py`、`src/sams/web/pages/dashboard.py`、`src/sams/web/pages/portfolio.py`、`src/sams/web/pages/theses.py`、`src/sams/web/pages/rules.py`
- Test: `tests/services/test_symbol_interest.py`、`tests/services/test_backfill_consumer.py`（新建）、`tests/services/scheduler/test_scheduler.py`（追加 tick 一例）、web 既有成功用例各追加一行断言

- [ ] **Step 1: `SymbolInterest`（先测后码）**

`tests/services/test_symbol_interest.py`:

```python
"""登记兴趣：主档没有就建行（代码表有名字就用名字），日线为零就排队（D144）。"""

import pytest

from sams.services.symbol_interest import SymbolInterest
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbol_backfills import SymbolBackfillRepo
from sams.storage.repos.symbol_list import SymbolListRepo
from sams.storage.repos.symbols import SymbolRepo


@pytest.fixture
def parts(db, settings):
    symbols = SymbolRepo(db)
    listed = SymbolListRepo(db)
    listed.upsert_batch(
        "A",
        [{"code": "000001", "name": "平安银行", "py_initials": "payh", "py_full": "pinganyinhang", "currency": "CNY"}],
    )
    backfills = SymbolBackfillRepo(db)
    interest = SymbolInterest(
        symbols=symbols, bars=BarRepo(db), symbol_list=listed, backfills=backfills, markets=settings.markets
    )
    return symbols, backfills, interest


def test_creates_master_with_name(db, parts):
    symbols, backfills, interest = parts
    report = interest.ensure("A", "000001", actor="alice")
    assert (report.market, report.code) == ("A", "000001")
    assert report.created_master is True
    row = symbols.get("A", "000001")
    assert row["name"] == "平安银行" and row["currency"] == "CNY"
    assert backfills.open_for("A", "000001") is not None  # 无日线 → 排队


def test_unknown_code_still_creates_bare_master(db, parts):
    symbols, _, interest = parts
    report = interest.ensure("A", "601299", actor="alice")
    assert report.created_master is True
    assert symbols.get("A", "601299")["name"] is None  # 代码表查不到也先立行，名字后补


def test_existing_bars_no_queue(db, parts, bars_frame):
    symbols, backfills, interest = parts
    BarRepo(db).upsert(bars_frame)  # support.bars_frame 就是 A/600519 三天
    report = interest.ensure("A", "600519", actor="alice")
    assert report.created_master is True and report.queued is False
    assert backfills.open_for("A", "600519") is None


def test_ensure_twice_is_quiet(db, parts):
    _, backfills, interest = parts
    interest.ensure("A", "000001", actor="alice")
    second = interest.ensure("A", "000001", actor="alice")
    assert (second.created_master, second.queued) == (False, False)  # 开放队列不重插
```

Run 先失败，然后实现 `src/sams/services/symbol_interest.py`:

```python
"""「有人想看这只票」的登记处：主档建行 + 补数排队，一次调用两件事。

四个入口（自选/成交/论点/规则）都汇到这里，市场与代码在这里归一——入口各归一遍
迟早有一处忘，`/symbol/HK/700` 那种「页面在、数据不在」就是这么来的。
actor 只进日志：谁加的票是排查线索，不是数据（D144）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import polars as pl

from sams.core.codes import canonical_code, canonical_market, currency_override, lot_size_of
from sams.core.config import MarketCfg

logger = logging.getLogger("sams.services.symbol_interest")


@dataclass(frozen=True)
class EnsureReport:
    market: str
    code: str
    created_master: bool
    queued: bool


class SymbolInterest:
    def __init__(self, *, symbols, bars, symbol_list, backfills, markets: dict[str, MarketCfg]) -> None:
        self.symbols = symbols
        self.bars = bars
        self.symbol_list = symbol_list
        self.backfills = backfills
        self.markets = markets

    def ensure(self, market: str, code: str, *, actor: str) -> EnsureReport:
        market = canonical_market(market)
        code = canonical_code(market, code)
        created = False
        if self.symbols.get(market, code) is None:
            listed = self.symbol_list.get(market, code) or {}
            self.symbols.upsert(
                pl.DataFrame(
                    [
                        {
                            "market": market,
                            "code": code,
                            "name": listed.get("name"),
                            "currency": listed.get("currency")
                            or currency_override(market, code)
                            or self.markets[market].currency,
                            "lot_size": lot_size_of(market),
                        }
                    ]
                )
            )
            created = True
        queued = False
        if self.bars.count(market, code) == 0:
            queued = self.backfills.enqueue(market, code)
        logger.info(
            "%s %s 登记兴趣 actor=%s 建主档=%s 排队=%s", market, code, actor, created, queued
        )
        return EnsureReport(market, code, created, queued)
```

- [ ] **Step 2: 结案钩子进 `SyncService`（先测后码）**

`tests/services/test_sync.py` 追加：

```python
def test_sync_daily_settles_backfill(db, settings):
    from sams.storage.repos.symbol_backfills import SymbolBackfillRepo

    backfills = SymbolBackfillRepo(db)
    backfills.enqueue("A", "600519")
    svc = ...  # SyncService(..., backfills=backfills)，StubRegistry 返回标准三日帧
    svc.sync_daily("A", "600519", date(2026, 10, 19), date(2026, 10, 21))
    assert backfills.open_for("A", "600519") is None  # 盘后任务补上了，队列结清（deferred 的出路）
```

`sync.py`：`__init__` 再加 `backfills: SymbolBackfillRepo | None = None`；`sync_daily` 成功分支 `written = self.bars.upsert(...)` 之后：

```python
        if self.backfills is not None and written > 0:
            # 盘后日线任务替补数队列收尾：deferred 那三条今晚就是被这里结清的
            self.backfills.mark_done_by_code(market, code)
```

- [ ] **Step 3: `BackfillConsumer`（先测后码）**

`tests/services/test_backfill_consumer.py`:

```python
"""每拍至多消费 limit 条：单票失败记账不炸拍（D144）。"""

from datetime import datetime, timezone

import pytest

from sams.core.errors import DatasourceError
from sams.services.scheduler.backfill import BackfillConsumer
from sams.services.sync import SyncSummary
from sams.storage.repos.symbol_backfills import SymbolBackfillRepo

MOMENT = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)


class StubSync:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def sync_daily(self, market, code, start, end):
        self.calls.append((market, code, start, end))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _summary(**kw):
    base = {"market": "A", "code": "600519", "written": 3}
    base.update(kw)
    return SyncSummary(**base)


@pytest.fixture
def queued(db):
    repo = SymbolBackfillRepo(db)
    for code in ("600519", "000001", "600000", "601299"):
        repo.enqueue("A", code)
    return repo


def test_success_marks_done(settings, queued):
    sync = StubSync([_summary(), _summary(), _summary(), _summary()])
    done = BackfillConsumer(sync, queued, settings).consume(MOMENT)
    assert done == 3  # 一拍只吃 limit=3，第四条留到下拍
    assert queued.open_for("A", "600519") is None
    assert queued.open_for("A", "601299") is not None
    # 窗口 = 回补 730 天到当地今天
    _, _, start, end = sync.calls[0]
    assert (end - start).days == 730


def test_failure_bookkeeping(settings, queued):
    sync = StubSync([DatasourceError("t", "源挂了"), _summary(empty=True), _summary()])
    BackfillConsumer(sync, queued, settings).consume(MOMENT)
    row = queued.open_for("A", "600519")
    assert (row.tries, row.last_error) == (1, "源挂了")
    assert queued.open_for("A", "000001").last_error == "区间内所有源均无数据"


def test_three_fails_defer_and_stop_pending(settings, queued):
    sync = StubSync([DatasourceError("t", "挂")] * 3)
    consumer = BackfillConsumer(sync, queued, settings, limit=1)
    for _ in range(3):
        consumer.consume(MOMENT)
    assert queued.open_for("A", "600519").status == "deferred"
    assert consumer.consume(MOMENT) == 0  # deferred 不是 pending：不再占拍，让位盘后任务
```

实现 `src/sams/services/scheduler/backfill.py`:

```python
"""补数队列的每拍消费者：至多 limit 条，单票的失败记在 `symbol_backfills` 自己头上。

不进 task_runs 是因为那份历史回答「哪个 job 跑没跑」，这里积压的是单票属性——
混进去之后「bars 跑成功了但三只票还欠着」两种真相分不开。失败三次转 deferred：
今天的重试预算用光，等今晚的盘后日线任务收尾（`SyncService.sync_daily` 的结案钩子）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sams.core.config import Settings
from sams.core.errors import SamsError
from sams.services.scheduler.due import local_today
from sams.services.scheduler.jobs import INITIAL_SYNC_DAYS

logger = logging.getLogger("sams.services.scheduler.backfill")


class BackfillConsumer:
    def __init__(self, sync, backfills, settings: Settings, *, limit: int = 3) -> None:
        self.sync = sync
        self.backfills = backfills
        self.settings = settings
        self.limit = limit

    def consume(self, moment: datetime) -> int:
        processed = 0
        for row in self.backfills.take_pending(self.limit):
            end = local_today(self.settings, row.market, moment)
            start = end - timedelta(days=INITIAL_SYNC_DAYS)
            try:
                summary = self.sync.sync_daily(row.market, row.code, start, end)
            except SamsError as exc:
                self.backfills.note_failure(row.id, str(exc))
                processed += 1
                continue
            if summary.empty:
                self.backfills.note_failure(row.id, "区间内所有源均无数据")
            else:
                self.backfills.mark_done(row.id)
            processed += 1
        return processed
```

- [ ] **Step 4: `Scheduler.tick` 接线（先测后码）**

`tests/services/scheduler/test_scheduler.py` 追加一例：仿该文件既有用例构造 Scheduler 的地方，多传 `backfill=` 一个记录调用的 stub（`class RecordingConsumer: def __init__(self): self.moments = []` / `def consume(self, moment): self.moments.append(moment); return 0`），跑一拍后断言 `consumer.moments == [那一拍的 moment]`。

`service.py`：`__init__` 加 `backfill=None` 存 `self.backfill`；`tick()` 里 `manual = self._run_requests(self.requests.drain(), universe, moment)` 之后插：

```python
            # 补数积压排在手动请求之后、周期 job 之前：手动请求是「现在就要」，
            # 而这一口每拍至多三只票、失败只记账——不该让看板那一拍等它
            if self.backfill is not None:
                self.backfill.consume(moment)
```

- [ ] **Step 5: 四个 web 入口 + bootstrap（每处先加断言看失败）**

先在相关 web 测试的既有「POST 成功」用例里加断言（跑一遍看它们失败）：

```python
ctx = app.state.web.contexts.current()
# POST /watchlist/add（市场 A、代码 600519）成功后：
assert ctx.backfills.open_for("A", "600519") is not None
```

改动点（都在成功路径的 redirect 之前一行）：
- `web/pages/dashboard.py` `POST /watchlist/add`：`ctx.symbol_interest.ensure(market, code.strip(), actor=user.username)`（`code` 是表单解析出的变量名，按端点体现状对齐）；
- `web/pages/portfolio.py` `POST /ledger/fill`：`ctx.symbol_interest.ensure(market, code, actor=user.username)`；
- `web/pages/theses.py` `POST /theses/create`：`ctx.symbol_interest.ensure(report.thesis.market, report.thesis.code, actor=user.username)`；
- `web/pages/rules.py` `POST /rules/new`：

```python
    ref = (data.get("target_ref") or "").strip()
    if data.get("target_type") == "symbol" and "/" in ref:
        ref_market, _, ref_code = ref.partition("/")
        ctx.symbol_interest.ensure(ref_market, ref_code, actor=user.username)
```

`bootstrap.py`：`backfills_store = SymbolBackfillRepo(database)` 与其他两仓库并排；`SyncService(...)` 加 `backfills=backfills_store`；构造

```python
    symbol_interest = SymbolInterest(
        symbols=symbols,
        bars=bars,
        symbol_list=symbol_list_store,
        backfills=backfills_store,
        markets=cfg.markets,
    )
```

`Scheduler(...)` 加 `backfill=BackfillConsumer(sync, backfills_store, cfg)`；`ServiceContext` 加 `backfills_store`、`symbol_interest` 两字段并在 return 传入。

- [ ] **Step 6: 全跑本任务相关 + Commit**

Run: `uv run pytest tests/services/test_symbol_interest.py tests/services/test_backfill_consumer.py tests/services/test_sync.py tests/services/scheduler tests/web -q`
Expected: 全 passed。

```bash
git add src/sams/services/symbol_interest.py src/sams/services/scheduler/backfill.py src/sams/services/sync.py src/sams/services/scheduler/service.py src/sams/bootstrap.py src/sams/web/pages/dashboard.py src/sams/web/pages/portfolio.py src/sams/web/pages/theses.py src/sams/web/pages/rules.py tests/services/test_symbol_interest.py tests/services/test_backfill_consumer.py tests/services/test_sync.py tests/services/scheduler/test_scheduler.py tests/web
git commit -m "feat(services): 新票自动排拍——四入口登记兴趣、每拍至多消费 3 条（D144）"
```

---

## Task 5: 搜索加票——`/api/symbol-search` partial 与 htmx 候选面板

**Files:**
- Create: `src/sams/services/symbol_search.py`、`src/sams/web/templates/partials/symbol_search.html`
- Modify: `src/sams/storage/repos/bars.py`（`last_closes`）、`src/sams/web/pages/dashboard.py`（新路由）、`src/sams/web/templates/watchlist.html`、`src/sams/bootstrap.py`（`symbol_search` 接线）
- Test: `tests/services/test_symbol_search.py`、`tests/web/test_symbol_search.py`（新建）、`tests/storage/test_bars.py`（追加）

- [ ] **Step 1: `BarRepo.last_closes`（先测后码）**

追加到 `tests/storage/test_bars.py`（用该文件既有的 `bars_frame`/入库路子）:

```python
def test_last_closes_per_pair(db, bars_frame):
    from datetime import date

    repo = BarRepo(db)
    repo.upsert(bars_frame)  # A/600519 三天，收盘逐日抬高
    got = repo.last_closes([("A", "600519"), ("A", "000001")])
    assert got[("A", "600519")] == bars_frame.sort("date")["close"][-1]
    assert ("A", "000001") not in got
    assert repo.last_closes([]) == {}
```

`repos/bars.py` 实现：

```python
    def last_closes(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
        """一批 (市场, 代码) 的库内最后收盘：搜索面板的「昨收」列（D142）。

        一次 `arg_max` 聚合问完，不按票循环——候选十条就是十次查询的旧路，
        面板是每敲一个字都要重画的。
        """
        if not pairs:
            return {}
        marks = ", ".join("(?, ?)" for _ in pairs)
        params = [v for pair in pairs for v in pair]
        rows = self.db.query(
            f"SELECT market, code, arg_max(close, date) AS close FROM bars_daily"
            f" WHERE adjust = '{ADJUST}' AND (market, code) IN ({marks})"
            f" GROUP BY market, code",
            params,
        )
        return {(r["market"], r["code"]): float(r["close"]) for r in rows.to_dicts()}
```

（`(m,c) IN ((?,?),...)` 元组 IN 是实测可用的写法；`ADJUST` 就是该文件顶部的 `"raw"` 常量。）

- [ ] **Step 2: `symbol_search` 服务（先测后码）**

`tests/services/test_symbol_search.py`:

```python
"""检索词归一与候选装配（D141/D142）。"""

import pytest

from sams.services.symbol_search import SymbolSearchService, normalize_query
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
        ("x" * 100, "X" * 64),
    ],
)
def test_normalize(raw, want):
    assert normalize_query(raw) == want


def test_search_assembles_close(db):
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
    svc = SymbolSearchService(listed, BarRepo(db))
    hits = svc.search("gzym")
    assert [(h.market, h.code, h.name, h.close) for h in hits] == [("A", "600519", "贵州茅台", None)]
```

`src/sams/services/symbol_search.py`:

```python
"""搜索加票的读模型：归一检索词 → 五档候选 → 贴上库内昨收。

昨收取本地 `bars_daily` 不取报价板：报价板上只有已订阅的票，而这一面板要能给
「还没加的票」报数（D142）。取不到就 None，模板写「—」——空位是真话，0 是假话。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbol_list import SymbolListRepo

#: 剥市场前缀只在剩下至少两位时执行：`HK` 自己也要能当检索词（搜 汇控之类会走片段档）
_PREFIXES = ("SH", "SZ", "BJ", "HK", "US")


def normalize_query(raw: str) -> str | None:
    q = (raw or "").strip().upper().replace(" ", "")
    for prefix in _PREFIXES:
        if q.startswith(prefix) and len(q) - len(prefix) >= 2:
            q = q[len(prefix) :]
            break
    q = q[:64]
    return q if q and any(ch.isalnum() for ch in q) else None


@dataclass(frozen=True)
class Candidate:
    market: str
    code: str
    name: str | None
    close: float | None


class SymbolSearchService:
    def __init__(self, symbol_list: SymbolListRepo, bars: BarRepo) -> None:
        self.symbol_list = symbol_list
        self.bars = bars

    def search(self, raw: str) -> list[Candidate]:
        q = normalize_query(raw)
        if q is None:
            return []
        hits = self.symbol_list.search(q)
        closes = self.bars.last_closes([(h["market"], h["code"]) for h in hits])
        return [
            Candidate(h["market"], h["code"], h["name"], closes.get((h["market"], h["code"])))
            for h in hits
        ]
```

- [ ] **Step 3: partial 模板 + 路由 + 自选页接线（先测后码）**

新建 `tests/web/test_symbol_search.py`（fixtures 走 `tests/web/conftest.py` 的 `web_app`/`logged`，种数据参照 `tests/web/test_dashboard.py` 的 `_seed_symbol` 直取 `ctx`）:

```python
"""搜索面板 fragment：命中给可点加的候选，查无给一句话（D143）。"""


def _seed(web):
    ctx = web.contexts.current()
    ctx.symbol_list_store.upsert_batch(
        "A",
        [{"code": "000001", "name": "平安银行", "py_initials": "payh", "py_full": "pinganyinhang", "currency": "CNY"}],
    )


def test_candidate_with_add_form(web_app):
    _seed(web_app)
    r = web_app.get("/api/symbol-search?code=payh&market=A", follow_redirects=False)
    assert r.status_code == 200  # （登录态用例按该目录既有用例拿 logged client 的写法来）
    body = r.text
    assert "平安银行" in body and "加入" in body and 'value="000001"' in body


def test_none_found_says_so(web_app):
    _seed(web_app)
    r = web_app.get("/api/symbol-search?code=不存在的东西")
    assert "查无" in r.text


def test_empty_query_is_empty_panel(web_app):
    r = web_app.get("/api/symbol-search?code=")
    assert "查无" not in r.text and "<table" not in r.text
```

（鉴权/登录写法以 `tests/web/conftest.py` 现成的 `logged` fixture 为准；上面按 `web_app` 写的断言在执行时对齐到既有 fixture 名——保持三条用例的语义不变。）

`web/pages/dashboard.py` 新路由（放在 `/api/partials/quotes` 旁边，鉴权与其同一写法）:

```python
@router.get("/api/symbol-search")
def symbol_search_partial(request: Request, code: str = "", market: str = ""):
    """候选面板：htmx 每敲一个字重来一次，空检索词回空面板，不打扰。"""
    require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    candidates = ctx.symbol_search.search(code)
    rows = [
        {
            "market": c.market,
            "code": c.code,
            "name": c.name,
            "price": c.close,
            "unit": currency_override(c.market, c.code)
            or ctx.settings.markets[c.market].currency,
        }
        for c in candidates
    ]
    # 表单上选中的市场排前面，但不排掉别的：选着 A 搜 00700，答案在 HK
    sel = market.strip().upper()
    rows = [r for r in rows if r["market"] == sel] + [r for r in rows if r["market"] != sel]
    return web.render(
        request,
        "partials/symbol_search.html",
        rows=rows,
        none_found=bool(code.strip()) and not rows,
    )
```

（`currency_override` 从 `sams.core.codes` import。）

新建 `src/sams/web/templates/partials/symbol_search.html`:

```html
{% if rows %}
<table>
  <tr><th>市场</th><th>代码</th><th>名称</th><th class="num">昨收</th><th></th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.market }}</td>
    <td>{{ row.code }}</td>
    <td>{{ row.name or "—" }}</td>
    <td class="num">{{ money(row.price, row.unit) if row.price is not none else "—" }}</td>
    <td>
      <form method="post" action="/watchlist/add" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="market" value="{{ row.market }}">
        <input type="hidden" name="code" value="{{ row.code }}">
        <button type="submit">加入</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
{% elif none_found %}
<p class="muted">查无——代码、拼音首字母、名称都没命中。也可以直接提交试试。</p>
{% endif %}
```

`watchlist.html` 的加票表单整块替换为（新增 id、hx 属性与候选容器）:

```html
  <form method="post" action="/watchlist/add" id="wl-form" class="panel inline-form">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <select name="market">
      {% for code in markets %}<option value="{{ code }}">{{ code }}</option>{% endfor %}
    </select>
    <input name="code" placeholder="代码 / 首字母 / 名称，如 000001、payh、平安"
           hx-get="/api/symbol-search" hx-trigger="keyup changed delay:300ms"
           hx-target="#wl-candidates" hx-include="#wl-form" hx-swap="innerHTML">
    <input name="note" placeholder="备注（可空）">
    <button type="submit">加入</button>
  </form>
  <div id="wl-candidates"></div>
```

（`hx-include="#wl-form"` 让 market 下拉的当前值一起进查询串——`code` 参数就是这条 input 自己的 name。）

`bootstrap.py`：构造 `symbol_search = SymbolSearchService(symbol_list_store, bars)`，`ServiceContext` 加 `symbol_search` 字段并传入。

- [ ] **Step 4: 跑通 + Commit**

Run: `uv run pytest tests/storage/test_bars.py tests/services/test_symbol_search.py tests/web/test_symbol_search.py tests/web/test_dashboard.py -q`
Expected: 全 passed。

```bash
git add src/sams/storage/repos/bars.py src/sams/services/symbol_search.py src/sams/web/pages/dashboard.py src/sams/web/templates/partials/symbol_search.html src/sams/web/templates/watchlist.html src/sams/bootstrap.py tests/storage/test_bars.py tests/services/test_symbol_search.py tests/web/test_symbol_search.py
git commit -m "feat(web): 搜索加票——五档排序、昨收列、查无提示与直登兜底（D142/D143）"
```

---

## Task 6: 个股页可达——代码表兜底、空态排队、链接统一

**Files:**
- Modify: `src/sams/web/pages/symbol.py`、`src/sams/web/templates/symbol.html`、`src/sams/web/templates/watchlist.html`、`src/sams/web/templates/partials/quotes.html`、`src/sams/web/pages/dashboard.py`（`_quote_rows` 去掉 `has_master`）
- Test: 既有 `tests/web/test_symbol.py`（若无此名，grep `"/symbol/` 找到那页个股测试）追加；`tests/web/test_dashboard.py` 里断「主档未同步」span 的用例翻转

- [ ] **Step 1: 写失败测试**

追加到个股页测试文件（fixtures 用法照该文件既有用例）:

```python
def test_code_table_row_renders_pending_state(web_app, logged):
    # 只种代码表（symbol_list_store.upsert_batch），不种主档、无日线
    ctx = web_app.state.web.contexts.current()
    ctx.symbol_list_store.upsert_batch("A", [{"code": "600519", "name": "贵州茅台", "py_initials": "gzym", "py_full": "guizhoumaotai", "currency": "CNY"}])
    r = logged.get("/symbol/A/600519")
    assert r.status_code == 200
    assert "待同步" in r.text
    # 兜底那一脚同时完成了登记兴趣：主档建了、队列排了
    assert ctx.symbols.get("A", "600519")["name"] == "贵州茅台"
    assert ctx.backfills.open_for("A", "600519") is not None


def test_unknown_code_still_404(logged):
    assert logged.get("/symbol/A/999999").status_code == 404


def test_sync_button_requeues(web_app, logged):
    r = logged.post("/symbol/A/600519/sync", data={"csrf": "<从 cookie/既有 POST 用例的拿法>"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/symbol/A/600519"
    assert web_app.state.web.contexts.current().backfills.open_for("A", "600519") is not None


def test_quote_board_code_links(logged, web_app):
    # 种一个只有报价、主档已由代码表兜底的行（或直接种主档+报价），断 partial 里是链接
    r = logged.get("/api/partials/quotes")
    assert '/symbol/A/600519' in r.text
```

再 grep `主档未同步` 与 `has_master` 在 tests/ 的断言处，把「死文字 span」的断言翻成「链接」断言。

- [ ] **Step 2: `_require_symbol` 兜底 + 两个调用点**

`symbol.py` 的 `_require_symbol` 改签名并插兜底（其余保持）：

```python
def _require_symbol(request: Request, user, market: str, code: str):
    """…原 docstring 保留，末尾补一段：

    主档没有但代码表有 = 「全市场认识这只票、本地档案还没追上」：兜底建行走
    `SymbolInterest.ensure`（顺带排补数），不再 404（D146）。两个都没有才 404，
    措辞不变——不给探测者区分「没有」与「不是你的」。
    """
    web = request.app.state.web
    ctx = web.contexts.current()
    market = canonical_market(market)
    code = canonical_code(market, code)
    if market not in ctx.settings.markets:
        raise HTTPException(status_code=404)
    row = ctx.symbols.get(market, code)
    if row is None and ctx.symbol_list_store.get(market, code) is not None:
        ctx.symbol_interest.ensure(market, code, actor=user.username)
        row = ctx.symbols.get(market, code)
    if row is None:
        raise HTTPException(status_code=404)
    return ctx, market, code, row
```

`symbol_page` 里 `user = require_user(request)` 已有，调用改 `_require_symbol(request, user, market, code)`；`bars_api` 里把 `require_user(request)` 接成 `user = require_user(request)` 后同样传入。

- [ ] **Step 3: 空态变量 + 立即排一拍端点 + 模板**

`symbol_page` 渲染前加：

```python
    no_bars = ctx.bars.count(market, code) == 0
    backfill_row = ctx.backfills_store.open_for(market, code)
    backfill_note = None
    if backfill_row is not None:
        if backfill_row.status == "deferred":
            backfill_note = f"今天已试过 {backfill_row.tries} 次都没成（{backfill_row.last_error}），今晚随盘后任务再试。"
        else:
            backfill_note = "已排队，下一拍自动开始（一拍至多三只，前面的队首先走）。"
```

`web.render(...)` 追加 `no_bars=no_bars, backfill_note=backfill_note`。

新端点（`symbol.py` 末尾；CSRF 校验与 `POST /watchlist/add` 现行写法对齐——那边若在 handler 里比对 `request.form` 的 csrf，这里抄那几行）:

```python
@router.post("/symbol/{market}/{code}/sync")
def symbol_sync(request: Request, market: str, code: str):
    """「立即排一拍」：入队下一拍消费，不承诺同步已经发生（D143 直登兜底的按钮）。"""
    user = require_user(request)
    ctx, market, code, _row = _require_symbol(request, user, market, code)
    ctx.symbol_interest.ensure(market, code, actor=user.username)
    return RedirectResponse(f"/symbol/{market}/{code}", status_code=303)
```

（顶部补 `from fastapi.responses import RedirectResponse`。）

`symbol.html` 在第一个 `</section>` 之后、chart section 之前插：

```html
{% if no_bars %}
<section class="card">
  <p>该股数据待同步：{{ backfill_note or "下一拍会自动补。" }}</p>
  <form method="post" action="/symbol/{{ market }}/{{ code }}/sync" class="inline-form">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit">立即排一拍</button>
  </form>
</section>
{% endif %}
```

- [ ] **Step 4: 链接统一（N4 了结）**

- `watchlist.html` 管理表格 `<td>{{ row.market }}</td><td>{{ row.code }}</td>` 里的 code 单元格换成 `<td><a href="/symbol/{{ row.market }}/{{ row.code }}">{{ row.code }}</a></td>`；
- `partials/quotes.html` 删 `{% if row.has_master %}…{% else %}…{% endif %}` 分支，只留链接那一行；
- `dashboard.py` `_quote_rows` 删 `"has_master"` 那个键（86-87 行附近）及为其服务的查询。

- [ ] **Step 5: 跑通 + Commit**

Run: `uv run pytest tests/web -q`
Expected: 全 passed（含翻转后的旧断言）。

```bash
git add src/sams/web/pages/symbol.py src/sams/web/pages/dashboard.py src/sams/web/templates/symbol.html src/sams/web/templates/watchlist.html src/sams/web/templates/partials/quotes.html tests/web
git commit -m "feat(web): 个股页可达——代码表兜底建行、空态排队、链接统一（D146，N4 了结）"
```

---

## Task 7: 大盘条——指数进轮询与看板顶部

**Files:**
- Modify: `src/sams/services/scheduler/universe.py`、`src/sams/services/intraday.py`、`src/sams/datasource/tencent.py`、`src/sams/datasource/sina_quote.py`、`src/sams/web/pages/dashboard.py`、`src/sams/web/templates/overview.html`
- Create: `src/sams/web/templates/partials/indices.html`
- Test: `tests/services/scheduler/test_universe.py`（或 grep `UniverseBuilder` 找到处）、`tests/services/test_intraday.py`、`tests/datasource/test_tencent.py`、`tests/datasource/test_sina_quote.py`、`tests/web/test_indices.py`（新建）

- [ ] **Step 1: `Universe` 加指数字段（先测后码）**

`universe.py`：先读现状。给 frozen `Universe` 追加带默认的字段与方法（字段名与方法的分别是有意的——`indices_by_market` 存、`indices()` 取）：

```python
@dataclass(frozen=True)
class Universe:
    by_market: Mapping[str, tuple[str, ...]]
    # …既有 fx_pairs 等字段原样……
    #: 大盘条与指数轮询的订阅清单：不随用户变，来自 settings（D145）
    indices_by_market: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def indices(self, market: str) -> tuple[str, ...]:
        return tuple(self.indices_by_market.get(market, ()))
```

**带默认值才能让测试里的 `StubUniverse`/裸构造点零破坏。** `UniverseBuilder.build()` 返回处把 `indices_by_market={m: self.settings.indices.for_market(m) for m in by_market}` 传进 Universe（builder 持有的 settings 字段名按现状对齐）。

测试追加：`build()` 结果的 `universe.indices("A") == settings.indices.for_market("A")`（settings 用 conftest 夹具，A/HK/US 都有默认九只的各自分组）。

- [ ] **Step 2: intraday 把指数并进轮询（先测后码）**

`intraday.py:76-100` 一带，`codes = universe.codes(market)` 改为：

```python
            # 票跟着用户走，指数跟着设置走：休市后的看板也要有大盘数，
            # 一个自选全空的用户也应当看到上证在跳（D145）
            codes = list(universe.codes(market)) + list(universe.indices(market))
```

并确认外层市场循环遍历的是 `cfg.markets`（settings）而不是 universe 的键——若现状按 universe 键循环，改成 settings 键，否则无票市场整个不上板。既有 intraday 测试（fake quotes 记 calls）追加一条断言：poll 的 codes 里含 `sh000001`。

- [ ] **Step 3: 两个报价适配器认指数（先测后码）**

`tests/datasource/test_tencent.py` 追加：录制一个指数行情的 echo payload，断言 `adapter.quotes("A", ["sh000001"])` 返回 `Quote(market="A", code="sh000001", ...)`（code 保持源形态——它与 `index_daily` 的 `index_code`、settings 的键同形）。
`tests/datasource/test_sina_quote.py` 追加：`adapter.quotes("A", ["600519", "sh000001"])` → `sh000001` 进 `batch.skipped`（reason 提「指数」），`600519` 正常。

实现：
- `TencentAdapter.quotes`：拼请求符号处，`looks_like_index_code(market, code)` 的码**原样透传**（`sh000001`/`hkHSI` 本来就是腾讯的符号形态，过 `tencent_symbol` 会被折坏）；解析回包时指数码**跳过 echo 校验**（`quote.py:44-92` 的 `echo_matches` 按请求符号比对，指数行的回显字段与个股不同形），其余流程不变。
- `SinaQuoteAdapter.quotes`：入口先分流，指数码不进请求、直接进 `QuoteBatch.skipped`（附原因），不记 errors。

Run: `uv run pytest tests/datasource/test_tencent.py tests/datasource/test_sina_quote.py -q` 全绿。

- [ ] **Step 4: 看板 partial（先测后码）**

新建 `tests/web/test_indices.py`（fixtures 照 `tests/web/conftest.py`）:

```python
"""大盘条：板上新鲜优先，否则库里最后一天带日期说明（D145/§3）。"""

from datetime import date

import polars as pl


def _seed_index(web, closes: list[float]):
    ctx = web.contexts.current()
    days = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)][: len(closes)]
    ctx.index_daily_store.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(closes),
                "index_code": ["sh000001"] * len(closes),
                "date": days,
                "open": closes, "high": closes, "low": closes, "close": closes,
                "volume": [1.0] * len(closes), "amount": [1.0] * len(closes),
                "source": ["tencent"] * len(closes),
            }
        )
    )


def test_market_closed_shows_last_close(web_app, logged):
    _seed_index(web_app, [100.0, 101.0])
    r = logged.get("/api/partials/indices")
    assert "上证指数" in r.text and "收盘" in r.text and "10-21" in r.text


def test_fresh_quote_wins(web_app, logged):
    from datetime import datetime, timedelta, timezone

    from sams.core.quotes import Quote

    _seed_index(web_app, [100.0, 101.0])
    core = web_app.state.web.core
    now = core.clock.now_utc()
    core.board.update(
        "A",
        [Quote(market="A", code="sh000001", ts=now - timedelta(seconds=5), price=3900.0, pct_chg=0.5)],
        now=now,
        error=None,
    )
    r = logged.get("/api/partials/indices")
    assert "实时" in r.text


def test_missing_everywhere_market_absent(web_app, logged):
    r = logged.get("/api/partials/indices")
    assert "index-group" not in r.text
```

（`Quote(...)` 构造必填字段按 `core/quotes.py` 现状对齐；`tests/web/test_dashboard.py` 的 `_seed_quote` 是现成抄样。）

`dashboard.py` 加（`pct_class` 已在 `web/display.py`，本文件应已 import——没有就补）:

```python
@router.get("/api/partials/indices")
def indices_partial(request: Request):
    """大盘条 fragment：与 quotes partial 同一套「首屏服务端渲染 + htmx 30s 重拉」。"""
    require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    segments = _index_rows(ctx, web.core, ctx.settings)
    return web.render(request, "partials/indices.html", segments=segments)


def _index_rows(ctx, core, settings):
    """报价板新鲜就用板，否则用库内最后一行：休市看不到数，是这一屏最大的旧痛点。"""
    segments = []
    for market in settings.markets:
        rows = []
        for index_code in settings.indices.for_market(market):
            unit = settings.markets[market].currency
            quote = core.board.get(market, index_code)
            if quote is not None and not quote.is_stale and quote.price is not None:
                rows.append(
                    {
                        "code": index_code,
                        "label": settings.indices.label(index_code),
                        "value": quote.price,
                        "pct": quote.pct_chg,
                        "pct_class": pct_class(quote.pct_chg),
                        "at": "实时",
                        "unit": unit,
                    }
                )
                continue
            latest = ctx.index_daily_store.latest_with_change(market, index_code)
            if latest is None:
                continue
            rows.append(
                {
                    "code": index_code,
                    "label": settings.indices.label(index_code),
                    "value": latest.close,
                    "pct": latest.change_pct,
                    "pct_class": pct_class(latest.change_pct),
                    "at": f"{latest.day:%m-%d} 收盘",
                    "unit": unit,
                }
            )
        if rows:
            segments.append({"market": market, "rows": rows})
    return segments
```

新建 `src/sams/web/templates/partials/indices.html`:

```html
{% if segments %}
<div class="index-bar">
  {% for segment in segments %}
  <span class="index-group">
    <strong>{{ segment.market }}</strong>
    {% for row in segment.rows %}
    <span class="badge">
      {{ row.label }}
      <strong class="num">{{ money(row.value, row.unit) }}</strong>
      <span class="{{ row.pct_class }}">{{ pct(row.pct) }}</span>
      <span class="muted">{{ row.at }}</span>
    </span>
    {% endfor %}
  </span>
  {% endfor %}
</div>
{% endif %}
```

`overview.html`：`{% block content %}` 下一行插：

```html
  <div id="index-bar" hx-get="/api/partials/indices" hx-trigger="load, every 30s" hx-swap="innerHTML"></div>
```

- [ ] **Step 5: 跑通 + Commit**

Run: `uv run pytest tests/services tests/datasource/test_tencent.py tests/datasource/test_sina_quote.py tests/web -q`
Expected: 全 passed。

```bash
git add src/sams/services/scheduler/universe.py src/sams/services/intraday.py src/sams/datasource/tencent.py src/sams/datasource/sina_quote.py src/sams/web/pages/dashboard.py src/sams/web/templates/partials/indices.html src/sams/web/templates/overview.html tests/services tests/datasource/test_tencent.py tests/datasource/test_sina_quote.py tests/web/test_indices.py
git commit -m "feat(web): 大盘条——指数进轮询与看板顶部（D145）"
```

---

## 收尾（控制者执行，不进子代理任务）

1. `uv run ruff check --no-cache src tests`（发现格式问题按报错逐条改，**不跑 ruff format**）；
2. `uv run pytest tests -q` 全量（不带管道，看结尾统计行原样）；
3. 控制者 push、重启 serve（`SAMS_SECRET_KEY="$(cat tmp/.sams_secret)"` 内联环境那套）、真源验证：
   - 手动排一拍 `symbol_master`（CLI 或等 weekly 到期前用手动请求）看 A/HK 代码表进 `symbol_master`（`count` 上千行）；
   - `sams sync` 一条指数日线（如 sh000001）后看板顶部出现大盘条；
   - 浏览器里用 `payh` 搜到平安银行并一点加入，观察下一拍补数把日线带回来、个股页从「待同步」变出图。

## Self-review 对照（写完后自查过一遍）

- D140→Task 3（周同步 job）；D141→Task 3（拼音列）+ Task 5（归一）；D142→Task 1（五档 SQL）+ Task 5（服务/路由/昨收）；D143→Task 5（htmx 面板）+ Task 6（兜底按钮/直登）；D144→Task 4（四入口+消费者+deferred）；D145→Task 2/3/7（能力+入库+轮询+看板）；D146→Task 6；D147→Task 3（空答复保旧表）；D148→Task 1/2（迁移与能力注册）。
- 测试七项 rubric（spec §4）全部落在各 Task 的测试步骤里，均为离线假源。
- 类型跨任务一致：`symbol_list_store`/`index_daily_store`/`backfills_store`（ServiceContext 字段名）↔ `SymbolListRepo`/`IndexDailyRepo`/`SymbolBackfillRepo`（类名）；`for_market`/`label` 只在 `IndicesCfg`；`open_for`/`enqueue`/`note_failure`/`mark_done`/`mark_done_by_code`/`take_pending` 只在 `SymbolBackfillRepo`；`ensure → EnsureReport`、`search → list[Candidate]`、`consume → int` 与各调用点一致。
- 无占位符：所有代码步骤给了成块代码；个别处写「照该文件既有 X 的写法对齐」的是本仓库内部形状（fixtures 名、CSRF 校验写法、构造必填参数），执行者读目标文件即可，不是 TBD。

