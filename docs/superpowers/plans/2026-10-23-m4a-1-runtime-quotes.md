# M4-A-1 运行时与报价 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M4-A 的地基落成一个能验证的增量：配置里出现 `[server]`、进程内多线程运行时骨架、A/HK/US 三市场的实时报价链路（腾讯主源 + 新浪备源、独立健康、快照落库、五态行情板）、盘中轮询线程、调度器的手动请求队列、迁移 0019、清理命令与 `/healthz`。

**Architecture:** 报价是继日线/公司行为/基本面/估值/资金流/上市日期/汇率之后的第八种数据能力，注册在 `CAPABILITIES` 这一份判据里，路由仍走 `AdapterRegistry.chain_for(market, "quote")`。`QuoteService` 只做「按市场问链、降级、规范化、落库、更新行情板、发事件、记独立健康」，`IntradayMonitor` 只做「时段判断 + 标的全集 + 调 QuoteService」。进程级共享对象收进 `AppCore`，线程级仓库与服务由 `build_context` 各造一份、各持一个 DuckDB 连接；`Runtime` 只管线程启动/停止与 `/healthz` 的数据，不 import 组装根。盘后规则评估与 Web 页面分别在计划 2、3 接入，本计划只留出它们要用的接缝（`on_quotes`、`RunRequestQueue`、`QuoteBoard`、`VersionGate`）。

**Tech Stack:** Python 3.13、DuckDB、Polars、pytest、FastAPI/uvicorn（本计划只引入最小 `/healthz`，页面在计划 3）。所有命令用 `uv run`。

---

## 0. 执行者必须先读的五条硬约束

1. **只用 `uv run`**：`uv run pytest …`、`uv run ruff check src tests --no-cache`、`uv run sams …`。系统 Python 没有依赖。
2. **lint 只跑 `ruff check`，绝不跑 `ruff format`**。行宽 100，规则集 E,F,I,UP,B,SIM。
3. **绝不修改已应用的迁移**：`schema_migrations.checksum` 会当场拒绝。表结构只改新文件 `0019_quotes_and_prune.sql`。
4. **测试库只写 `tmp/` 或内存库**，绝不碰 `data/sams.duckdb`。每个任务结束时 `git fetch origin main && git merge --ff-only origin/main && git push origin main`；推不上去原样报告，不 `--force`、不 `--amend`。
5. **真实网络用例打 `@pytest.mark.live`**，日常跑 `-m "not live"`。

开工前量水位（必须全绿；红了退回上一个任务，不许跳过）：

```bash
uv run ruff check src tests --no-cache
uv run pytest tests/test_layering.py -q
uv run pytest -m "not live" -q
```

## 1. 这一层要防的错

| 错法 | 屏幕上什么样 | 真实代价 |
|---|---|---|
| 备用报价源把「没回这只票」写成 0 | 自选里一条平直的 0 元线 | 规则按 0 元判跌破，用户被指挥去卖 |
| 报错时清空行情板 | 页面空白，像刚启动 | 用户不知道是断网还是自己没加自选；上一笔报价其实是可用的 |
| 报价健康写进日线健康表 | 健康页显示日线源失败 | 盘中每 10 秒的抖动把日线源染红，盘后采集被误判为故障 |
| 两个线程各造一个 TokenBucket | 每个源看起来都没超限 | 实际请求速率翻倍，免费源限流后整条链一起瞎 |
| 停止事件只停住调度线程 | Ctrl-C 后进程不退 | 盘中线程还在抓库，下一次启动拿不到锁（D111） |
| `Quote.ts` 存本地时间 | 收盘后 age 永远为负或几十小时 | 五态里 `stale` 与 `closed` 永远错一个 |

## 2. 文件结构

```
src/sams/core/config.py                  [改] ServerCfg、quote_stale_s
src/sams/core/version.py                 [新] VersionGate
src/sams/core/quotes.py                  [新] Quote / QuoteBatch / as_utc / quote_state / QuoteSourceRecord
src/sams/core/sources.py                 [改] SourceHealthView 加 chain_for
src/sams/datasource/quote.py             [新] QuoteAdapter 协议、has_quotes、批量/回显/字段辅助
src/sams/datasource/capabilities.py      [改] 登记 quote
src/sams/datasource/sources.py           [改] _NOT_ADAPTERS 加 quote
src/sams/datasource/tencent.py           [改] quotes(...)、腾讯报价解析
src/sams/datasource/sina_quote.py        [新] 新浪仅报价适配器
src/sams/datasource/ratelimit.py         [改] TokenBucket 加锁
src/sams/storage/migrations/0019_quotes_and_prune.sql [新]
src/sams/storage/repos/quotes.py         [新] QuoteRepo（含清理边界）、QuoteHealthRepo
src/sams/storage/repos/task_runs.py      [改] 清理用的 count_before/delete_before 与心跳的 count_beyond/delete_beyond
src/sams/services/quotes.py              [新] QuoteService、QuoteBoard、QuotePollResult
src/sams/services/intraday.py            [新] IntradayMonitor
src/sams/services/prune.py               [新] PruneService
src/sams/services/runtime.py             [新] Runtime
src/sams/services/scheduler/due.py       [改] local_today 公开（手动锚点与到期计算共用）
src/sams/services/scheduler/service.py   [改] RunRequestQueue、request_run、stop
src/sams/services/health.py              [改] 报价源健康
src/sams/bootstrap.py                    [改] AppCore / build_core / build_context / ServeParts / build_serve
src/sams/web/__init__.py                 [新]
src/sams/web/app.py                      [新] create_app + /healthz
src/sams/cli/serve.py                    [新] sams serve
src/sams/cli/__init__.py                 [改] 注册 serve、锁分支提示
src/sams/cli/db.py                       [改] 加 prune 子命令
src/sams/cli/health.py                   [改] [报价源]、锁住时的 /healthz 提示
tests/...                                每个任务各自列出
pyproject.toml                           [改] fastapi/uvicorn/jinja2/python-multipart/httpx
```

---

### Task 1: 配置 `[server]` 与 `quote_stale_s`

**Files:**
- Modify: `src/sams/core/config.py`
- Test: `tests/core/test_config.py`

- [ ] **Step 1: 写失败测试**

在 `tests/core/test_config.py` 末尾追加：

```python
def test_server_defaults_are_loopback_only(tmp_path):
    settings = Settings.load(write(tmp_path))
    assert (settings.server.host, settings.server.port) == ("127.0.0.1", 8765)
    assert settings.server.allow_lan is False
    assert settings.server.session_ttl_days == 7
    assert settings.server.idle_timeout_min == 720
    assert settings.server.display_tz == "Asia/Shanghai"
    assert settings.quote_stale_s == 60


def test_server_section_is_read(tmp_path):
    text = BASE + """
[server]
host = "127.0.0.1"
port = 9000
allow_lan = false
session_ttl_days = 3
idle_timeout_min = 60
login_max_failures = 4
login_window_min = 2
display_tz = "Asia/Hong_Kong"
tls_certfile = "/tmp/cert.pem"
tls_keyfile = "/tmp/key.pem"

[general.quote_stale_s]
"""
    # quote_stale_s 属于 [general]：上面那段只验证 [server] 的读取
    read = write(tmp_path, text.replace("[general.quote_stale_s]\n", ""))
    server = Settings.load(read).server
    assert server.port == 9000 and server.session_ttl_days == 3
    assert server.login_max_failures == 4 and server.display_tz == "Asia/Hong_Kong"
    assert server.tls_certfile == "/tmp/cert.pem" and server.tls_keyfile == "/tmp/key.pem"


def test_quote_stale_seconds_is_read(tmp_path):
    text = BASE.replace("[general]", "[general]\nquote_stale_s = 30")
    assert Settings.load(write(tmp_path, text)).quote_stale_s == 30
    with pytest.raises(ValidationError, match="quote_stale_s"):
        Settings.load(write(tmp_path, BASE.replace("[general]", "[general]\nquote_stale_s = 0")))


def test_a_non_loopback_host_requires_allow_lan(tmp_path):
    text = BASE + '\n[server]\nhost = "0.0.0.0"\n'
    with pytest.raises(ValidationError, match="allow_lan"):
        Settings.load(write(tmp_path, text))
    allowed = write(tmp_path, text + "allow_lan = true\n")
    assert Settings.load(allowed).server.allow_lan is True


def test_tls_pair_must_be_complete(tmp_path):
    half = BASE + '\n[server]\ntls_certfile = "/tmp/cert.pem"\n'
    with pytest.raises(ValidationError, match="tls"):
        Settings.load(write(tmp_path, half))


@pytest.mark.parametrize(
    "line, keyword",
    [
        ("port = 0", "port"),
        ("port = 70000", "port"),
        ("session_ttl_days = 0", "session_ttl_days"),
        ("idle_timeout_min = 0", "idle_timeout_min"),
        ("login_max_failures = 0", "login_max_failures"),
        ("login_window_min = 0", "login_window_min"),
        ('display_tz = "Mars/Olympus"', "display_tz"),
    ],
)
def test_server_numbers_and_timezone_are_validated(tmp_path, line, keyword):
    text = BASE + f"\n[server]\n{line}\n"
    with pytest.raises(ValidationError, match=keyword):
        Settings.load(write(tmp_path, text))
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/core/test_config.py -q
```

Expected: FAIL，`AttributeError: 'Settings' object has no attribute 'server'`（或 `quote_stale_s`）。

- [ ] **Step 3: 实现**

在 `src/sams/core/config.py` 的 `MarketCfg` 之后加：

```python
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class ServerCfg:
    """`sams serve` 的监听与登录策略（spec §7/§8）。

    默认只绑回环地址：这个产品先服务「本机一个人」。`0.0.0.0` 与「允许局域网」
    必须同时成立——单独一个 host 就开门，等于让一次改端口顺手把库暴露给整层楼。
    """

    host: str = "127.0.0.1"
    port: int = 8765
    allow_lan: bool = False
    session_ttl_days: int = 7
    idle_timeout_min: int = 720
    login_max_failures: int = 5
    login_window_min: int = 5
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    display_tz: str = "Asia/Shanghai"

    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    def validate(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValidationError(f"server.port 非法: {self.port}")
        if self.session_ttl_days <= 0:
            raise ValidationError("server.session_ttl_days 必须为正数")
        if self.idle_timeout_min <= 0:
            raise ValidationError("server.idle_timeout_min 必须为正数")
        if self.login_max_failures <= 0:
            raise ValidationError("server.login_max_failures 必须为正数")
        if self.login_window_min <= 0:
            raise ValidationError("server.login_window_min 必须为正数")
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValidationError("server.tls_certfile 与 tls_keyfile 必须成对给出")
        if not self.is_loopback() and not self.allow_lan:
            raise ValidationError(
                f"server.host={self.host!r} 不是回环地址：要监听局域网请同时设置 allow_lan = true"
            )
        try:
            ZoneInfo(self.display_tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError(f"server.display_tz 非法: {self.display_tz}") from exc
```

把 `Settings` 的字段改成（`dataclass` 的默认字段必须排在无默认字段之后）：

```python
    scheduler_tick_s: int = 60
    #: 报价超过这么久没更新 ⇒ `stale`，价格类规则不评估（spec D134）
    quote_stale_s: int = 60
    server: ServerCfg = field(default_factory=ServerCfg)
```

在 `Settings.load` 里、构造 `cls(...)` 之前加：

```python
        server_raw = raw.get("server") or {}
        server = ServerCfg(
            host=str(server_raw.get("host", "127.0.0.1")),
            port=int(server_raw.get("port", 8765)),
            allow_lan=bool(server_raw.get("allow_lan", False)),
            session_ttl_days=int(server_raw.get("session_ttl_days", 7)),
            idle_timeout_min=int(server_raw.get("idle_timeout_min", 720)),
            login_max_failures=int(server_raw.get("login_max_failures", 5)),
            login_window_min=int(server_raw.get("login_window_min", 5)),
            tls_certfile=str(server_raw["tls_certfile"]) if server_raw.get("tls_certfile") else None,
            tls_keyfile=str(server_raw["tls_keyfile"]) if server_raw.get("tls_keyfile") else None,
            display_tz=str(server_raw.get("display_tz", "Asia/Shanghai")),
        )
```

构造参数追加：

```python
            scheduler_tick_s=int(general.get("scheduler_tick_s", 60)),
            quote_stale_s=int(general.get("quote_stale_s", 60)),
            server=server,
```

`validate` 里追加：

```python
        if self.quote_stale_s <= 0:
            raise ValidationError("quote_stale_s 必须为正数")
        self.server.validate()
```

导入行改为 `from dataclasses import dataclass, field`。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/core/test_config.py -q
uv run ruff check src/sams/core/config.py tests/core/test_config.py --no-cache
```

- [ ] **Step 5: 提交**

```bash
git add src/sams/core/config.py tests/core/test_config.py
git commit -m "feat(config): [server] 监听/登录策略与 quote_stale_s"
```

---

### Task 2: `VersionGate`（跨线程缓存失效计数）

**Files:**
- Create: `src/sams/core/version.py`
- Test: `tests/core/test_version.py`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import threading

import pytest

from sams.core.version import DEFAULT_KINDS, VersionGate


def test_a_new_gate_has_a_zero_for_every_declared_kind():
    gate = VersionGate()
    assert gate.kinds == DEFAULT_KINDS
    assert gate.snapshot() == {kind: 0 for kind in DEFAULT_KINDS}


def test_bump_only_moves_its_own_kind():
    gate = VersionGate()
    gate.bump("rules")
    assert gate.current("rules") == 1
    assert gate.current("bars") == 0


def test_a_snapshot_is_a_copy_not_a_live_view():
    gate = VersionGate()
    before = gate.snapshot()
    gate.bump("theses")
    assert before["theses"] == 0 and gate.snapshot()["theses"] == 1


def test_changed_since_answers_the_question_the_cache_asks():
    gate = VersionGate()
    seen = gate.snapshot()
    assert not gate.changed_since(seen)
    gate.bump("ledger")
    assert gate.changed_since(seen)


def test_an_unknown_kind_is_a_programming_error_with_the_known_list():
    gate = VersionGate()
    with pytest.raises(KeyError, match="rules"):
        gate.bump("rulse")
    with pytest.raises(KeyError, match="rules"):
        gate.current("rulse")
    with pytest.raises(KeyError, match="rules"):
        gate.changed_since({"rulse": 0})


def test_concurrent_bumps_do_not_lose_counts():
    gate = VersionGate(kinds=("rules",))
    threads = [threading.Thread(target=lambda: [gate.bump("rules") for _ in range(100)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert gate.current("rules") == 800
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/core/test_version.py -q
```

Expected: FAIL，`ModuleNotFoundError: sams.core.version`。

- [ ] **Step 3: 实现**

```python
"""进程内的版本计数：谁缓存了「规则/观点/账本/日线」，谁就要知道自己过期了没有。

盘中线程不该每 10 秒把规则与日线重读一遍；但用户在网页上改了规则、录了成交、推进了
观点，或盘后 job 刚补了日线——这些都不经过盘中线程。缓存若没有失效判据，最坏的样子是
「用户刚把止损改到 9 元，页面显示已生效，盘中仍按 10 元在判」，而且没有任何报错。
计数只在这个进程里有意义：它答的是「这份缓存」与「当前库」的差，不是数据版本。
"""

from __future__ import annotations

import threading

DEFAULT_KINDS = ("rules", "theses", "ledger", "bars")


class VersionGate:
    def __init__(self, kinds: tuple[str, ...] = DEFAULT_KINDS) -> None:
        self._versions = {kind: 0 for kind in kinds}
        self._lock = threading.Lock()

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self._versions)

    def _check(self, kind: str) -> None:
        if kind not in self._versions:
            raise KeyError(f"未知的缓存种类 {kind!r}：已登记 {list(self._versions)}")

    def bump(self, kind: str) -> int:
        with self._lock:
            self._check(kind)
            self._versions[kind] += 1
            return self._versions[kind]

    def current(self, kind: str) -> int:
        with self._lock:
            self._check(kind)
            return self._versions[kind]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._versions)

    def changed_since(self, snapshot: dict[str, int]) -> bool:
        with self._lock:
            for kind in snapshot:
                self._check(kind)
            return any(self._versions[kind] != value for kind, value in snapshot.items())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/core/test_version.py -q
uv run ruff check src/sams/core/version.py tests/core/test_version.py --no-cache
```

- [ ] **Step 5: 提交**

```bash
git add src/sams/core/version.py tests/core/test_version.py
git commit -m "feat(core): VersionGate 给跨线程缓存一个失效判据"
```

---

### Task 3: `core/quotes.py`（报价值对象与五态）

**Files:**
- Create: `src/sams/core/quotes.py`
- Test: `tests/core/test_quotes.py`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sams.core.errors import ValidationError
from sams.core.quotes import (
    CLOSED,
    DELAYED,
    LIVE,
    NO_DATA,
    STALE,
    Quote,
    QuoteBatch,
    as_utc,
    quote_state,
)


def quote(**overrides) -> Quote:
    base = dict(
        market="A",
        code="600519",
        ts=datetime(2026, 10, 23, 2, 0, tzinfo=UTC),
        price=1500.0,
        prev_close=1490.0,
        source="tencent",
    )
    base.update(overrides)
    return Quote(**base)


def test_a_quote_refuses_a_price_that_is_not_a_traded_price():
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValidationError, match="价格"):
            quote(price=bad)


def test_a_quote_requires_an_aware_timestamp():
    with pytest.raises(ValidationError, match="UTC"):
        quote(ts=datetime(2026, 10, 23, 10, 0))


def test_as_utc_attaches_the_market_zone_to_a_naive_source_time():
    naive = datetime(2026, 10, 23, 10, 0)
    assert as_utc(naive, ZoneInfo("Asia/Shanghai")) == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)


def test_as_utc_respects_daylight_saving():
    naive = datetime(2026, 7, 1, 9, 30)
    assert as_utc(naive, ZoneInfo("America/New_York")) == datetime(2026, 7, 1, 13, 30, tzinfo=UTC)


def test_the_five_states_are_ordered_by_what_the_user_must_do_first():
    now = datetime(2026, 10, 23, 2, 0, 30, tzinfo=UTC)
    fresh = quote()
    assert quote_state(fresh, now=now, market_open=True, stale_s=60) == LIVE
    assert quote_state(None, now=now, market_open=True, stale_s=60) == NO_DATA
    assert quote_state(fresh, now=now, market_open=False, stale_s=60) == CLOSED
    old = quote(ts=datetime(2026, 10, 23, 1, 58, tzinfo=UTC))
    assert quote_state(old, now=now, market_open=True, stale_s=60) == STALE
    flagged = quote(is_stale=True)
    assert quote_state(flagged, now=now, market_open=True, stale_s=60) == STALE
    delayed = quote(delayed=True)
    assert quote_state(delayed, now=now, market_open=True, stale_s=60) == DELAYED


def test_a_quote_from_the_future_is_not_reported_as_negative_age():
    future = quote(ts=datetime(2026, 10, 23, 2, 1, tzinfo=UTC))
    now = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote_state(future, now=now, market_open=True, stale_s=60) == LIVE


def test_an_empty_batch_is_still_a_batch():
    batch = QuoteBatch()
    assert batch.quotes == () and batch.skipped == () and batch.errors == ()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/core/test_quotes.py -q
```

Expected: FAIL，`ModuleNotFoundError: sams.core.quotes`。

- [ ] **Step 3: 实现**

```python
"""报价的域值对象与「这一笔现在算哪种状态」。

`ts` 是**源给的报价时刻**，不是取回时刻：交易所的时钟与这台机器的时钟不是一回事，
用取回时刻算 age 会把一次延迟 5 分钟的源显示成实时。源不给时刻时由适配器用取回时刻
兜底并置 `is_stale=True`——那一句的意思是「这个时间不是源说的」，不是「它旧」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sams.core.errors import ValidationError

#: 交易所所在地的时区。这是市场属性，不是用户展示偏好（后者是 `server.display_tz`）。
MARKET_ZONES: dict[str, ZoneInfo] = {
    "A": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}

LIVE, DELAYED, CLOSED, STALE, NO_DATA = "live", "delayed", "closed", "stale", "no_data"


@dataclass(frozen=True)
class Quote:
    market: str
    code: str
    #: 一律 UTC：展示时才按市场时区折算
    ts: datetime
    price: float
    open: float | None = None
    prev_close: float | None = None
    high: float | None = None
    low: float | None = None
    pct_chg: float | None = None
    volume: float | None = None
    amount: float | None = None
    bid1: float | None = None
    ask1: float | None = None
    #: 源侧交易状态（有就带，不猜）：停牌、盘前之类只有源知道
    status: str = ""
    #: 源明确标了延迟才为 True；`None` 是「没标」，不等于「保证实时」
    delayed: bool | None = None
    source: str = ""
    #: 源没给时刻（或给了读不懂的格式），于是 `ts` 是取回时刻
    is_stale: bool = False

    def __post_init__(self) -> None:
        if not self.market or not self.code:
            raise ValidationError("Quote 的 market/code 不能为空")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValidationError(f"报价价格必须有限且为正: {self.price!r}")
        if self.ts.tzinfo is None:
            raise ValidationError("Quote.ts 必须是带时区的 UTC 时刻")

    def marked_stale(self) -> Quote:
        return replace(self, is_stale=True)


@dataclass(frozen=True)
class QuoteBatch:
    """一个源对一批代码的一次答复。

    `skipped` 与 `errors` 分开：前者是「源明确没有这只票」（不重试），后者是
    「响应里有这只票但读不懂」（值班时要看的那一句）。两者都**不**写 0。
    """

    quotes: tuple[Quote, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuoteSourceRecord:
    """报价源健康：与日线的 `SourceRecord` 分开一张表、分开一个类型。

    `last_ok_at` 是时刻而不是日期：盘中每 10 秒都在问，按天记的话「今天成功过一次」
    与「三小时前成功过一次」长得一样。
    """

    name: str
    markets: tuple[str, ...] = ()
    ok: bool | None = None
    last_error: str | None = None
    last_ok_at: datetime | None = None
    consecutive_failures: int = 0


def as_utc(value: datetime, zone: ZoneInfo) -> datetime:
    """源侧本地时刻 → UTC；已是 aware 的就只换算。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(UTC)


def quote_state(
    quote: Quote | None,
    *,
    now: datetime,
    market_open: bool,
    stale_s: int = 60,
) -> str:
    """五态（D134）。顺序就是修法的顺序：先看市场开没开，再看有没有、旧不旧。

    `closed` 优先于 `stale`：周末看到一条周五的报价不是故障，页面不该把它染红。
    `delayed` 排在 `stale` 之后：源自己说延迟 15 分钟、而这一笔已经 20 分钟没动，
    前者是常态、后者才是要查的事。
    """
    if not market_open:
        return CLOSED
    if quote is None:
        return NO_DATA
    age = max(0.0, (now - quote.ts).total_seconds())
    if quote.is_stale or age > stale_s:
        return STALE
    if quote.delayed is True:
        return DELAYED
    return LIVE
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/core/test_quotes.py -q
uv run ruff check src/sams/core/quotes.py tests/core/test_quotes.py --no-cache
```

- [ ] **Step 5: 提交**

```bash
git add src/sams/core/quotes.py tests/core/test_quotes.py
git commit -m "feat(core): Quote/QuoteBatch 与五态报价判定"
```

---

### Task 4: 报价能力与适配器协议

**Files:**
- Create: `src/sams/datasource/quote.py`
- Modify: `src/sams/datasource/capabilities.py`, `src/sams/datasource/sources.py`
- Test: `tests/datasource/test_quote_helpers.py`, `tests/datasource/test_capabilities.py`, `tests/datasource/test_fundamentals_adapters.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/datasource/test_quote_helpers.py`：

```python
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
```

在 `tests/datasource/test_capabilities.py` 的两处名单里加 `"quote"`：

```python
    assert set(CAPABILITIES) == {
        "daily_bars",
        "corp_actions",
        "financials",
        "valuation",
        "funding_flow",
        "listed_date",
        "fx_rate",
        "quote",
    }
```

在 `tests/datasource/test_fundamentals_adapters.py` 的标签名单末尾加 `"报价"`：

```python
        "汇率",
        "报价",
    ]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/datasource/test_quote_helpers.py tests/datasource/test_capabilities.py tests/datasource/test_fundamentals_adapters.py -q
```

Expected: FAIL，`ModuleNotFoundError: sams.datasource.quote`；名单两项也不等。

- [ ] **Step 3: 实现**

新建 `src/sams/datasource/quote.py`：

```python
"""报价适配器契约与三个源共用的解析件。

报价与日线是两种取数：没有区间、一次问一批、结果以「只」为单位。把它塞进
`BarAdapter.daily_bars` 的形状里，适配器就要假装每只票都有一根 K 线，而失败会以
「这个区间没有数据」的样子出现——那正是 D43 说过的「两种 0 行不能混成一个结论」。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from sams.core.codes import canonical_code
from sams.core.quotes import QuoteBatch, as_utc

#: 一次请求问多少只。免费接口对 URL 长度与 QPS 都有脾气，50 是实测能稳过的量级
QUOTE_BATCH = 50


class QuoteAdapter(Protocol):
    name: str
    markets: frozenset[str]

    def quotes(self, market: str, codes: list[str]) -> QuoteBatch:
        """返回这一批里能读到的报价；缺的代码进 `skipped`，读不懂的进 `errors`。"""
        ...


def has_quotes(obj: object) -> bool:
    """按能力判断，不按类判断（D38 的同一条约定）。"""
    return callable(getattr(obj, "quotes", None))


def chunks(items: list[str], size: int = QUOTE_BATCH) -> list[list[str]]:
    if size <= 0:
        raise ValueError(f"size 必须为正: {size}")
    return [items[i : i + size] for i in range(0, len(items), size)]


def echo_matches(market: str, requested: str, echoed: str) -> bool:
    """源回显的代码是不是我们问的那一只。

    美股回显带交易所后缀（问 `AAPL` 回 `AAPL.OQ`），所以按点前的裸代码比；
    A/HK 用 `canonical_code` 归一（`sh600519` 与 `600519` 同一只）。
    回显不符必须整只拒绝：错一位就是把别家公司的价格写进这只票的键下。
    """
    text = (echoed or "").strip()
    if not text:
        return False
    if market == "US":
        return text.split(".")[0].upper() == requested.split(".")[0].strip().upper()
    try:
        return canonical_code(market, text) == canonical_code(market, requested)
    except ValueError:
        return False


def field_float(fields: Sequence[str], index: int) -> float | None:
    """按位置取一个数：空、缺位、`N/A`、NaN 一律 None，绝不折成 0。"""
    try:
        text = fields[index]
    except IndexError:
        return None
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_moment(raw: str, formats: Sequence[str], zone: ZoneInfo) -> datetime | None:
    """按源给的本地时刻解析并转 UTC；读不懂就返回 None，由调用方决定怎么兜底。"""
    text = (raw or "").strip()
    for fmt in formats:
        try:
            return as_utc(datetime.strptime(text, fmt), zone)
        except ValueError:
            continue
    return None
```

在 `src/sams/datasource/capabilities.py` 的导入区加：

```python
from sams.datasource.quote import has_quotes
```

在 `CAPABILITIES` 末尾加：

```python
    #: 报价没有窗口，一次问一批；`chain_for(market, "quote")` 仍按这份判据过滤
    "quote": Capability(label="报价", lacking="不提供报价", probe=has_quotes),
```

在 `src/sams/datasource/sources.py` 的 `_NOT_ADAPTERS` 集合里加 `"quote"`（它是协议与解析件，不是实现）。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/datasource/test_quote_helpers.py tests/datasource/test_capabilities.py tests/datasource/test_fundamentals_adapters.py tests/datasource/test_sources_pluggable.py -q
```

Expected: PASS（此时还没有适配器实现 `quotes`，所以 `tencent` 的能力仍是 `("daily_bars",)`）。

- [ ] **Step 5: 提交**

```bash
git add src/sams/datasource/quote.py src/sams/datasource/capabilities.py src/sams/datasource/sources.py tests/datasource/test_quote_helpers.py tests/datasource/test_capabilities.py tests/datasource/test_fundamentals_adapters.py
git commit -m "feat(datasource): 登记 quote 能力与解析辅助"
```

---

### Task 5: 腾讯批量报价

**Files:**
- Modify: `src/sams/datasource/tencent.py`
- Test: `tests/datasource/test_tencent_quotes.py`, `tests/datasource/test_capabilities.py`, `tests/datasource/test_sources_pluggable.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/datasource/test_tencent_quotes.py`：

```python
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from support import FrozenClock

from sams.datasource.tencent import (
    QUOTE_URL,
    TencentAdapter,
    parse_tencent_quotes,
    tencent_quote_symbol,
)

NOW = datetime(2026, 10, 23, 14, 0, 5, tzinfo=UTC)


def line(key: str, fields: list[str]) -> str:
    return f'v_{key}="{"~".join(fields)}";'


def fields(*, code: str, price="100.00", prev="99.00", open_="98.50",
           high="101.00", low="97.00", volume="12345", ts="",
           pct="1.01", bid="99.99", ask="100.01", amount="", amount_parts=None):
    cells = [""] * 40
    cells[0] = "1"
    cells[1] = "测试票"
    cells[2] = code
    cells[3] = price
    cells[4] = prev
    cells[5] = open_
    cells[6] = volume
    cells[9] = bid
    cells[19] = ask
    cells[30] = ts
    cells[31] = "1.00"
    cells[32] = pct
    cells[33] = high
    cells[34] = low
    cells[35] = amount_parts or amount
    cells[37] = amount
    return cells


def test_symbol_shapes_for_three_markets():
    assert tencent_quote_symbol("A", "600519") == "sh600519"
    assert tencent_quote_symbol("HK", "700") == "hk00700"
    # 报价接口不要交易所后缀：带 .OQ 会返回空行，看起来像「这只票没有报价」
    assert tencent_quote_symbol("US", "AAPL.OQ") == "usAAPL"


def test_a_share_quote_fields_timezone_and_lot_conversion():
    text = line("sh600519", fields(
        code="600519",
        price="1540.00",
        prev="1530.00",
        open_="1525.00",
        high="1545.00",
        low="1520.00",
        volume="23456",
        ts="20261023100000",
        pct="0.65",
        amount_parts="0/0/3612345678",
    ))
    batch = parse_tencent_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    quote = batch.quotes[0]
    assert (quote.price, quote.prev_close, quote.open) == (1540.0, 1530.0, 1525.0)
    assert quote.high == 1545.0 and quote.low == 1520.0
    # 腾讯 A 股给「手」，入库统一为「股」
    assert quote.volume == 23456 * 100
    assert quote.amount == 3612345678
    assert quote.pct_chg == 0.65
    assert quote.bid1 == 99.99 and quote.ask1 == 100.01
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.source == "tencent" and quote.is_stale is False


def test_hk_quote_uses_hk_timestamp_and_amount_column():
    text = line("hk00700", fields(
        code="00700",
        price="380.00",
        volume="1000000",
        ts="2026/10/23 10:00:00",
        amount="380000000",
    ))
    quote = parse_tencent_quotes(
        text, market="HK", codes=["700"], now=NOW,
        zone=ZoneInfo("Asia/Hong_Kong"),
    ).quotes[0]
    assert quote.code == "00700"
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.volume == 1000000 and quote.amount == 380000000


def test_us_quote_accepts_exchange_suffix_in_the_echo():
    text = line("usAAPL", fields(
        code="AAPL.OQ",
        price="230.50",
        ts="2026-10-23 10:00:00",
        amount="123456789",
    ))
    quote = parse_tencent_quotes(
        text, market="US", codes=["AAPL"], now=NOW,
        zone=ZoneInfo("America/New_York"),
    ).quotes[0]
    assert quote.code == "AAPL"
    assert quote.ts == datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    assert quote.amount == 123456789


def test_missing_and_mismatched_codes_are_skipped_not_zero():
    text = line("sh600519", fields(code="600519", ts="20261023100000"))
    batch = parse_tencent_quotes(
        text, market="A", codes=["600519", "000001"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert [q.code for q in batch.quotes] == ["600519"]
    assert batch.skipped == ("000001",)
    assert batch.errors == ()


def test_a_quote_without_a_tradable_price_is_an_error_not_a_zero():
    text = line("sh600519", fields(code="600519", price="0", ts="20261023100000"))
    batch = parse_tencent_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert batch.quotes == ()
    assert batch.errors and "价格" in batch.errors[0]


def test_an_unreadable_time_falls_back_to_fetch_time_and_is_marked_stale():
    text = line("sh600519", fields(code="600519", ts="not-a-time"))
    quote = parse_tencent_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    ).quotes[0]
    assert quote.ts == NOW and quote.is_stale is True


def test_empty_response_is_an_empty_response_not_an_empty_batch():
    from sams.core.errors import EmptyResponse

    with pytest.raises(EmptyResponse):
        parse_tencent_quotes(
            "", market="A", codes=["600519"], now=NOW,
            zone=ZoneInfo("Asia/Shanghai"),
        )


def test_the_adapter_posts_the_batch_to_the_quote_endpoint():
    seen = {}

    def fetch(url, params, *, timeout):
        seen.update(url=url, params=params, timeout=timeout)
        return line("sh600519", fields(code="600519", ts="20261023100000"))

    adapter = TencentAdapter(
        quote_fetch=fetch,
        timeout=7,
        clock=FrozenClock(NOW),
    )
    batch = adapter.quotes("A", ["600519"])
    assert seen["url"] == QUOTE_URL
    assert seen["params"] == {"q": "sh600519"}
    assert seen["timeout"] == 7
    assert batch.quotes[0].code == "600519"


@pytest.mark.live
def test_live_tencent_quote_smoke():
    batch = TencentAdapter().quotes("A", ["600519"])
    assert batch.quotes and batch.quotes[0].price > 0
    assert batch.quotes[0].ts.tzinfo is not None
```

把 `tests/datasource/test_capabilities.py` 里腾讯那一行改成：

```python
    assert entries["tencent"].capabilities == ("daily_bars", "quote")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/datasource/test_tencent_quotes.py tests/datasource/test_capabilities.py -q
```

Expected: FAIL，`ImportError: cannot import name 'QUOTE_URL'` / `TencentAdapter.quotes` 不存在。

- [ ] **Step 3: 实现**

在 `src/sams/datasource/tencent.py` 顶部补 import：

```python
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.quotes import Quote, QuoteBatch
from sams.datasource.quote import echo_matches, field_float, parse_moment
```

在常量区追加：

```python
QUOTE_URL = "https://qt.gtimg.cn/q"
_TENCENT_LINE = re.compile(r'v_([^=]+)="([^"]*)"')
#: 三个市场的时间格式各不相同：同一列 30 的字符串，A 股没有分隔符，港股是斜杠
_QUOTE_TS_FORMATS = {
    "A": ("%Y%m%d%H%M%S",),
    "HK": ("%Y/%m/%d %H:%M:%S",),
    "US": ("%Y-%m-%d %H:%M:%S",),
}


def tencent_quote_symbol(market: str, code: str) -> str:
    """报价接口的 symbol 与日线不同：美股**不能**带交易所后缀。

    实测 `usAAPL.OQ` 返回空行，而 `usAAPL` 返回正常报价且字段里的代码仍是
    `AAPL.OQ`。所以这里只取裸代码，回显校验由 `echo_matches` 按点前比较。
    """
    if market == "A":
        return a_share_symbol(code)
    if market == "HK":
        return f"hk{canonical_code('HK', code)}"
    if market == "US":
        return f"us{canonical_code('US', code).split('.')[0].upper()}"
    raise ValueError(f"未知市场 {market}")
```

追加 `quote_get`（放在 `smartbox_get` 后面）：

```python
def quote_get(url: str, params: dict[str, str], *, timeout: int = 15) -> str:
    """腾讯报价是 GBK 的 JS 赋值语句，不是 JSON。"""
    import requests

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
    )
    if response.status_code == 429:
        raise RateLimited("tencent", "HTTP 429 被限流")
    response.raise_for_status()
    return response.content.decode("gbk", errors="replace")
```

追加纯解析函数（放在 `parse_tencent_daily` 后面）：

```python
def parse_tencent_quotes(
    text: str,
    *,
    market: str,
    codes: list[str],
    now: datetime,
    zone: ZoneInfo,
) -> QuoteBatch:
    """腾讯 `v_xxx="..."` 批量响应 → QuoteBatch。

    字段位置是接口的硬事实：3 价、4 昨收、5 开、6 量、30 时间、32 涨跌幅、
    33 高、34 低、9 买一、19 卖一。A 股的量是「手」，港美股是「股」；
    A 股的成交额藏在第 35 个复合字段的第三段，港美股直接给在第 37 位。
    """
    if not (text or "").strip():
        raise EmptyResponse("tencent", f"{market} 报价响应为空")
    parsed: list[list[str]] = []
    for raw in text.splitlines():
        match = _TENCENT_LINE.search(raw)
        if match is not None and match.group(2).strip():
            parsed.append(match.group(2).split("~"))
    if not parsed:
        raise EmptyResponse("tencent", f"{market} 报价响应里没有可读的行")

    quotes: list[Quote] = []
    skipped: list[str] = []
    errors: list[str] = []
    for code in codes:
        canonical = canonical_code(market, code)
        row = next(
            (
                fields
                for fields in parsed
                if len(fields) > 2 and echo_matches(market, canonical, fields[2])
            ),
            None,
        )
        if row is None:
            skipped.append(canonical)
            continue
        if len(row) < 35:
            errors.append(f"{canonical}: 字段不足（{len(row)} 个）")
            continue
        price = field_float(row, 3)
        if price is None or price <= 0:
            errors.append(f"{canonical}: 报价价格不可用（{row[3]!r}）")
            continue
        moment = parse_moment(row[30], _QUOTE_TS_FORMATS[market], zone)
        volume = field_float(row, 6)
        if volume is not None and market == "A":
            volume *= LOTS_TO_SHARES
        # 复合字段只在 A 股的第 35 位，且行可能短于它；港股/美股直接读第 37 位。
        # 越界会抛 IndexError，而这里要的是「没有这一格」→ None
        if market == "A":
            amount = field_float(row[35].split("/"), 2) if len(row) > 35 else None
        else:
            amount = field_float(row, 37)
        bid = field_float(row, 9)
        ask = field_float(row, 19)
        quotes.append(
            Quote(
                market=market,
                code=canonical,
                ts=moment or now,
                price=price,
                open=field_float(row, 5),
                prev_close=field_float(row, 4),
                high=field_float(row, 33),
                low=field_float(row, 34),
                pct_chg=field_float(row, 32),
                volume=volume,
                amount=amount,
                bid1=bid if bid and bid > 0 else None,
                ask1=ask if ask and ask > 0 else None,
                source="tencent",
                is_stale=moment is None,
            )
        )
    return QuoteBatch(tuple(quotes), tuple(skipped), tuple(errors))
```

改造 `TencentAdapter.__init__`（只加两个参数，原参数保持）：

```python
    def __init__(
        self,
        *,
        get: Callable[..., dict] = http_get,
        hint_get: Callable[..., str] = smartbox_get,
        resolve: Callable[[str], str] | None = None,
        timeout: int = 15,
        quote_fetch: Callable[..., str] = quote_get,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._get = get
        self._hint_get = hint_get
        self._timeout = timeout
        self._hints: dict[str, str] = {}
        self._resolve = resolve or self._smartbox_resolve
        self._quote_fetch = quote_fetch
        self._clock = clock
```

在类里加：

```python
    def quotes(self, market: str, codes: list[str]) -> QuoteBatch:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        wanted = list(dict.fromkeys(canonical_code(market, code) for code in codes))
        if not wanted:
            return QuoteBatch()
        symbols = [tencent_quote_symbol(market, code) for code in wanted]
        try:
            text = self._quote_fetch(
                QUOTE_URL, {"q": ",".join(symbols)}, timeout=self._timeout
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return parse_tencent_quotes(
            text,
            market=market,
            codes=wanted,
            now=self._clock.now_utc(),
            zone=ZoneInfo(
                {"A": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}[
                    market
                ]
            ),
        )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/datasource/test_tencent_quotes.py tests/datasource/test_capabilities.py tests/datasource/test_sources_pluggable.py -q
uv run ruff check src/sams/datasource/tencent.py tests/datasource/test_tencent_quotes.py --no-cache
```

Expected: PASS（live 用例默认不跑）。

- [ ] **Step 5: 提交**

```bash
git add src/sams/datasource/tencent.py tests/datasource/test_tencent_quotes.py tests/datasource/test_capabilities.py
git commit -m "feat(datasource): 腾讯批量报价"
```

---

### Task 6: `sina_quote` 仅报价适配器

**Files:**
- Create: `src/sams/datasource/sina_quote.py`
- Test: `tests/datasource/test_sina_quote.py`, `tests/datasource/test_sources_pluggable.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/datasource/test_sina_quote.py`：

```python
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from support import FrozenClock

from sams.core.errors import EmptyResponse
from sams.datasource.sina_quote import (
    QUOTE_URL,
    SinaQuoteAdapter,
    parse_sina_quotes,
    sina_quote_symbol,
)

NOW = datetime(2026, 10, 23, 14, 0, 5, tzinfo=UTC)


def line(key: str, cells: list[str]) -> str:
    return f'var hq_str_{key}="' + ",".join(cells) + '";'


def a_cells(**overrides):
    cells = [""] * 33
    cells[0] = "贵州茅台"
    cells[1] = "1525.00"   # open
    cells[2] = "1530.00"   # prev close
    cells[3] = "1540.00"   # price
    cells[4] = "1545.00"   # high
    cells[5] = "1520.00"   # low
    cells[8] = "2400000"   # volume, shares
    cells[9] = "3612345678"
    cells[11] = "1539.90"
    cells[21] = "1540.10"
    cells[30] = "2026-10-23"
    cells[31] = "10:00:00"
    cells.update(overrides)
    return cells


def test_symbols_for_three_markets():
    assert sina_quote_symbol("A", "600519") == "sh600519"
    assert sina_quote_symbol("HK", "700") == "hk00700"
    assert sina_quote_symbol("US", "AAPL.OQ") == "gb_aapl"


def test_a_share_fields_and_derived_pct():
    text = line("sh600519", a_cells())
    quote = parse_sina_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    ).quotes[0]
    assert quote.price == 1540.0 and quote.prev_close == 1530.0
    assert quote.open == 1525.0 and quote.high == 1545.0 and quote.low == 1520.0
    assert quote.volume == 2400000 and quote.amount == 3612345678
    assert quote.bid1 == 1539.9 and quote.ask1 == 1540.1
    assert quote.pct_chg == pytest.approx((1540 / 1530 - 1) * 100)
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.source == "sina_quote"


def test_hk_fields_use_chinese_name_and_hk_time():
    cells = [""] * 19
    cells[0] = "TENCENT"
    cells[1] = "腾讯控股"
    cells[2] = "370.00"
    cells[3] = "368.00"
    cells[4] = "382.00"
    cells[5] = "369.00"
    cells[6] = "380.00"
    cells[7] = "12.00"
    cells[8] = "3.26"
    cells[9] = "379.80"
    cells[10] = "380.20"
    cells[11] = "380000000"
    cells[12] = "1000000"
    cells[17] = "2026/10/23"
    cells[18] = "10:00"
    quote = parse_sina_quotes(
        line("hk00700", cells), market="HK", codes=["00700"], now=NOW,
        zone=ZoneInfo("Asia/Hong_Kong"),
    ).quotes[0]
    assert quote.price == 380.0 and quote.pct_chg == 3.26
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.volume == 1000000 and quote.amount == 380000000


def test_us_fields_use_price_datetime_and_prev_close_columns():
    cells = [""] * 31
    cells[0] = "Apple Inc"
    cells[1] = "230.50"
    cells[2] = "1.25"
    cells[3] = "2026-10-23 10:00:00"
    cells[4] = "2.85"
    cells[5] = "228.00"
    cells[6] = "231.00"
    cells[7] = "227.50"
    cells[10] = "50000000"
    cells[26] = "227.65"
    cells[30] = "11525000000"
    quote = parse_sina_quotes(
        line("gb_aapl", cells), market="US", codes=["AAPL"], now=NOW,
        zone=ZoneInfo("America/New_York"),
    ).quotes[0]
    assert quote.price == 230.5 and quote.prev_close == 227.65
    assert quote.ts == datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    assert quote.volume == 50000000 and quote.amount == 11525000000


def test_an_empty_line_is_skipped_not_a_zero_quote():
    text = 'var hq_str_sh600519="";'
    batch = parse_sina_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert batch.quotes == () and batch.skipped == ("600519",)


def test_echoed_key_must_match_the_requested_code():
    text = line("sh600518", a_cells())
    batch = parse_sina_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert batch.quotes == () and batch.skipped == ("600519",)


def test_a_zero_price_is_an_error():
    text = line("sh600519", a_cells(**{3: "0.00"}))
    batch = parse_sina_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert batch.quotes == () and batch.errors and "价格" in batch.errors[0]


def test_no_assignments_at_all_is_empty_response():
    with pytest.raises(EmptyResponse):
        parse_sina_quotes(
            "<html>403</html>", market="A", codes=["600519"], now=NOW,
            zone=ZoneInfo("Asia/Shanghai"),
        )


def test_adapter_uses_gbk_endpoint_and_its_own_name():
    seen = {}

    def fetch(url, params, *, timeout):
        seen.update(url=url, params=params, timeout=timeout)
        return line("sh600519", a_cells())

    adapter = SinaQuoteAdapter(
        quote_fetch=fetch, timeout=4, clock=FrozenClock(NOW)
    )
    batch = adapter.quotes("A", ["600519"])
    assert seen == {
        "url": QUOTE_URL,
        "params": {"list": "sh600519"},
        "timeout": 4,
    }
    assert batch.quotes[0].source == "sina_quote"


@pytest.mark.live
def test_live_sina_quote_smoke():
    batch = SinaQuoteAdapter().quotes("A", ["600519"])
    assert batch.quotes and batch.quotes[0].price > 0
```

> 上面 `line()` 的写法刻意绕开了一个空 f-string；实现里请直接写成
> `f'var hq_str_{key}="' + ",".join(cells) + '";'`。测试代码如果让 ruff 报
> `F541`，按这个形状改写即可，不要改断言。

把 `tests/datasource/test_sources_pluggable.py` 的 `ADAPTER_MODULES` 加上 `"sina_quote"`：

```python
ADAPTER_MODULES = {
    "akshare_cn",
    "akshare_hk",
    "akshare_ths",
    "eastmoney",
    "sina",
    "sina_quote",
    "tencent",
    "yfinance_us",
}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/datasource/test_sina_quote.py tests/datasource/test_sources_pluggable.py -q
```

Expected: FAIL，`ModuleNotFoundError: sams.datasource.sina_quote`。

- [ ] **Step 3: 实现**

新建 `src/sams/datasource/sina_quote.py`：

```python
"""新浪 `hq.sinajs.cn`：只提供报价的备源（D114）。

只报 `quote` 能力、不报日线：报价的字段与新鲜度语义和日线完全不同，把两者塞进
一个适配器会让「日线不支持这个市场」与「报价暂时没有」共用一个健康格。

三个市场的字段表不是同一张：
  * A 股：0 名、1 开、2 昨收、3 现价、4 高、5 低、8 量（股）、9 额、
    11 买一、21 卖一、30 日期、31 时间；
  * 港股：0 英文名、1 中文名、2 开、3 昨收、4 高、5 低、6 价、8 涨跌幅、
    9 买一、10 卖一、11 额、12 量、17 日期、18 时间；
  * 美股：0 名、1 价、2 涨跌幅、3 时刻、5 开、6 高、7 低、10 量、26 昨收、30 额。

按「看起来差不多」复用一张表，会把港股的昨收读成现价——这个错误不会抛异常。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, EmptyResponse, RateLimited
from sams.core.quotes import Quote, QuoteBatch
from sams.datasource.base import a_share_symbol, classify_error
from sams.datasource.quote import field_float, parse_moment
from sams.datasource.sources import source

QUOTE_URL = "https://hq.sinajs.cn/list="
_LINE = re.compile(r'hq_str_([^=]+)="([^"]*)"')
_MARKET_ZONES = {
    "A": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}


def sina_quote_symbol(market: str, code: str) -> str:
    if market == "A":
        return a_share_symbol(code)
    if market == "HK":
        return f"hk{canonical_code('HK', code)}"
    if market == "US":
        return f"gb_{canonical_code('US', code).split('.')[0].lower()}"
    raise ValueError(f"未知市场 {market}")


def quote_get(url: str, params: dict[str, str], *, timeout: int = 15) -> str:
    import requests

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        },
    )
    if response.status_code == 429:
        raise RateLimited("sina_quote", "HTTP 429 被限流")
    response.raise_for_status()
    return response.content.decode("gbk", errors="replace")


def parse_sina_quotes(
    text: str,
    *,
    market: str,
    codes: list[str],
    now: datetime,
    zone: ZoneInfo,
) -> QuoteBatch:
    payloads: dict[str, list[str]] = {}
    for raw in (text or "").splitlines():
        match = _LINE.search(raw)
        if match is not None:
            payloads[match.group(1).strip().lower()] = match.group(2).split(",")
    if not payloads:
        raise EmptyResponse("sina_quote", f"{market} 报价响应里没有赋值语句")

    quotes: list[Quote] = []
    skipped: list[str] = []
    errors: list[str] = []
    for code in codes:
        canonical = canonical_code(market, code)
        symbol = sina_quote_symbol(market, canonical)
        cells = payloads.get(symbol.lower())
        if cells is None or not any(cell.strip() for cell in cells):
            skipped.append(canonical)
            continue
        # 回显校验就是键名：新浪 A/HK 的字段里没有代码，键是唯一的回显。
        # 键与请求的 symbol 不等时，这是另一个标的的报价——跳过，不能贴到这只票上
        cells = payloads.get(symbol.lower())
        if cells is None or not any(cell.strip() for cell in cells):
            skipped.append(canonical)
            continue
        try:
            quote = _to_quote(market, canonical, cells, now=now, zone=zone)
        except _BadRow as exc:
            errors.append(f"{canonical}: {exc}")
            continue
        quotes.append(quote)
    return QuoteBatch(tuple(quotes), tuple(skipped), tuple(errors))


class _BadRow(ValueError):
    pass


def _to_quote(market: str, code: str, cells: list[str], *, now: datetime, zone):
    if market == "A":
        return _a_quote(code, cells, now=now, zone=zone)
    if market == "HK":
        return _hk_quote(code, cells, now=now, zone=zone)
    if market == "US":
        return _us_quote(code, cells, now=now, zone=zone)
    raise ValueError(f"未知市场 {market}")


def _price(cells: list[str], index: int) -> float:
    value = field_float(cells, index)
    if value is None or value <= 0:
        raise _BadRow(f"报价价格不可用（{(cells[index] if index < len(cells) else '缺位')!r}）")
    return value


def _tap(cells: list[str], index: int) -> float | None:
    value = field_float(cells, index)
    return value if value and value > 0 else None


def _a_quote(code: str, cells: list[str], *, now: datetime, zone) -> Quote:
    if len(cells) < 32:
        raise _BadRow(f"A 股字段不足（{len(cells)} 个）")
    price = _price(cells, 3)
    prev = field_float(cells, 2)
    pct = (price / prev - 1) * 100 if prev and prev > 0 else None
    moment = parse_moment(
        f"{cells[30].strip()} {cells[31].strip()}", ("%Y-%m-%d %H:%M:%S",), zone
    )
    return Quote(
        market="A",
        code=code,
        ts=moment or now,
        price=price,
        open=field_float(cells, 1),
        prev_close=prev,
        high=field_float(cells, 4),
        low=field_float(cells, 5),
        pct_chg=pct,
        volume=field_float(cells, 8),
        amount=field_float(cells, 9),
        bid1=_tap(cells, 11),
        ask1=_tap(cells, 21),
        source="sina_quote",
        is_stale=moment is None,
    )


def _hk_quote(code: str, cells: list[str], *, now: datetime, zone) -> Quote:
    if len(cells) < 19:
        raise _BadRow(f"港股字段不足（{len(cells)} 个）")
    price = _price(cells, 6)
    moment = parse_moment(
        f"{cells[17].strip()} {cells[18].strip()}",
        ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"),
        zone,
    )
    return Quote(
        market="HK",
        code=code,
        ts=moment or now,
        price=price,
        open=field_float(cells, 2),
        prev_close=field_float(cells, 3),
        high=field_float(cells, 4),
        low=field_float(cells, 5),
        pct_chg=field_float(cells, 8),
        volume=field_float(cells, 12),
        amount=field_float(cells, 11),
        bid1=_tap(cells, 9),
        ask1=_tap(cells, 10),
        source="sina_quote",
        is_stale=moment is None,
    )


def _us_quote(code: str, cells: list[str], *, now: datetime, zone) -> Quote:
    if len(cells) < 31:
        raise _BadRow(f"美股字段不足（{len(cells)} 个）")
    price = _price(cells, 1)
    moment = parse_moment(cells[3], ("%Y-%m-%d %H:%M:%S",), zone)
    return Quote(
        market="US",
        code=code,
        ts=moment or now,
        price=price,
        open=field_float(cells, 5),
        prev_close=field_float(cells, 26),
        high=field_float(cells, 6),
        low=field_float(cells, 7),
        pct_chg=field_float(cells, 2),
        volume=field_float(cells, 10),
        amount=field_float(cells, 30),
        source="sina_quote",
        is_stale=moment is None,
    )


class SinaQuoteAdapter:
    name = "sina_quote"
    markets = frozenset({"A", "HK", "US"})

    def __init__(
        self,
        *,
        quote_fetch: Callable[..., str] = quote_get,
        timeout: int = 15,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._quote_fetch = quote_fetch
        self._timeout = timeout
        self._clock = clock

    def quotes(self, market: str, codes: list[str]) -> QuoteBatch:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        wanted = list(dict.fromkeys(canonical_code(market, code) for code in codes))
        if not wanted:
            return QuoteBatch()
        symbols = [sina_quote_symbol(market, code) for code in wanted]
        try:
            text = self._quote_fetch(
                QUOTE_URL, {"list": ",".join(symbols)}, timeout=self._timeout
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return parse_sina_quotes(
            text,
            market=market,
            codes=wanted,
            now=self._clock.now_utc(),
            zone=_MARKET_ZONES[market],
        )


source(
    "sina_quote",
    options=("timeout",),
    note="新浪 hq.sinajs.cn 批量报价，A/HK/US，只提供报价",
)(SinaQuoteAdapter)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/datasource/test_sina_quote.py tests/datasource/test_sources_pluggable.py tests/datasource/test_capabilities.py -q
uv run ruff check src/sams/datasource/sina_quote.py tests/datasource/test_sina_quote.py --no-cache
```

Expected: PASS。`catalog()` 现在能看见 `sina_quote`，能力是 `("quote",)`。

- [ ] **Step 5: 提交**

```bash
git add src/sams/datasource/sina_quote.py tests/datasource/test_sina_quote.py tests/datasource/test_sources_pluggable.py
git commit -m "feat(datasource): 新浪仅报价备源"
```

### Task 7: 迁移 0019 与种子链

**Files:**
- Create: `src/sams/storage/migrations/0019_quotes_and_prune.sql`
- Create: `tests/storage/test_migration_0019.py`
- Modify: `tests/storage/test_data_sources.py:42-53,61-63`
- Modify: `tests/test_bootstrap.py:80-91,106-111`

- [ ] **Step 1: 写失败测试**

新建 `tests/storage/test_migration_0019.py`：

```python
"""0019：盘中报价的形状 + 测试告警 + 清理对象的索引。

这一枚只验形状与默认值：四个价格列可空、`is_test` 在旧行与新行上都读得到 False、
`sina_quote` 排在每个市场链末位。行为契约分别在 `test_quotes_repo.py`（Task 8）
与 `test_prune.py`（Task 13）。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations
from sams.storage.repos.data_sources import DataSourceRepo


def insert_alert(db: Database, alert_id: str) -> None:
    db.execute(
        "INSERT INTO alerts (id, user_id, market, code, stage, payload_json)"
        " VALUES (?, 'u1', 'A', '600519', 'entry', '{}')",
        [alert_id],
    )


def test_0019_adds_quote_columns_and_health_table(db):
    assert {"open", "prev_close", "high", "low"} <= db.columns("quotes_snapshot")
    assert {"display_name"} <= db.columns("users")
    assert {"is_test"} <= db.columns("alerts")
    assert "quote_source_health" in db.table_names()


def test_0019_seeds_sina_quote_at_the_end_of_every_market(db):
    repo = DataSourceRepo(db)
    row = repo.get("sina_quote")
    assert row is not None and row.kind == "sina_quote"
    assert repo.chains() == {
        "A": ("akshare", "eastmoney", "tencent", "sina", "akshare_ths", "sina_quote"),
        "HK": ("yfinance", "tencent", "akshare_hk", "sina_quote"),
        "US": ("yfinance", "tencent", "sina_quote"),
    }


def test_is_test_defaults_to_false_on_new_rows(db):
    insert_alert(db, "a1")
    assert db.scalar("SELECT is_test FROM alerts WHERE id = 'a1'") is False


def test_0019_backfills_is_test_on_an_existing_database(tmp_path):
    """DuckDB 不支持 `ADD COLUMN ... NOT NULL DEFAULT`，所以 0019 走「加列 → 设默认
    → 回填」。这三步少任何一步，旧库上都会出现 NULL 或默认值没生效；只在新库上测
    不出来，必须真的先建一个停在 0018 的库。
    """
    legacy_dir = tmp_path / "migrations"
    legacy_dir.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "0019":
            shutil.copy(path, legacy_dir / path.name)

    database = Database.open(str(tmp_path / "legacy.duckdb"))
    try:
        apply_migrations(database, legacy_dir)
        insert_alert(database, "old")
        assert apply_migrations(database) == ["0019_quotes_and_prune.sql"]
        assert database.scalar("SELECT is_test FROM alerts WHERE id = 'old'") is False
        # 迁移之后由服务层漏传这一列的行也必须拿到 False，而不是 NULL
        insert_alert(database, "new")
        assert database.scalar("SELECT is_test FROM alerts WHERE id = 'new'") is False
    finally:
        database.close()
```

把 `tests/storage/test_data_sources.py` 的种子用例改成：

```python
def test_migration_seeds_eight_sources_and_three_chains(repo):
    """种子的意义：老库跑完迁移就有备源，不需要谁来手工插记录。"""
    assert repo.chains() == {
        # akshare_ths 只报公司行为（D38）：东财那张分红送配表对 002450 这类票回
        # 「返回数据为空」，A 股因此不能只有单源——漏记一次派息是 D37 抓不住的（1~6% < 15%）
        # sina_quote 只报报价（D114）：它在两条数据链上都可见，但按能力过滤后
        # 日线链看不见它，报价链上它是腾讯之后的第二个故障域
        "A": ("akshare", "eastmoney", "tencent", "sina", "akshare_ths", "sina_quote"),
        "HK": ("yfinance", "tencent", "akshare_hk", "sina_quote"),
        "US": ("yfinance", "tencent", "sina_quote"),
    }
    assert len(repo.list()) == 8
```

同一文件里的幂等用例：

```python
def test_migrations_are_idempotent_for_the_seed(repo):
    apply_migrations(repo.db)
    assert len(repo.list()) == 8
```

把 `tests/test_bootstrap.py` 里的两条链断言改成：

```python
        assert built.registry.market_sources == {
            # akshare_ths 只报公司行为（D38）：它在日线链上会被显式跳过，但它确实在库里，
            # 所以 market_sources 这条「链上排了谁」的事实要把它算进来
            "A": ("akshare", "eastmoney", "tencent", "sina", "akshare_ths", "sina_quote"),
            # 链是「按市场的路由」，不是「谁能取日线」：akshare_hk 只报公司行为，
            # 采集时被跳过（D38），备源判据走 registry.chain_for(market, method)
            "HK": ("yfinance", "tencent", "akshare_hk", "sina_quote"),
            "US": ("yfinance", "tencent", "sina_quote"),
        }
        assert built.registry.chain_for("HK", "daily_bars") == ("yfinance", "tencent")
        assert built.registry.chain_for("HK", "corp_actions") == ("yfinance", "akshare_hk")
        assert built.registry.chain_for("A", "corp_actions") == ("akshare", "akshare_ths")
        # 报价链只认 quote 能力：腾讯与新浪报价，日线源一个都不该混进来
        assert built.registry.chain_for("A", "quote") == ("tencent", "sina_quote")
        assert built.registry.chain_for("HK", "quote") == ("tencent", "sina_quote")
        assert built.registry.chain_for("US", "quote") == ("tencent", "sina_quote")
```

禁用 akshare 之后的那条：

```python
        assert second.registry.market_sources["A"] == (
            "eastmoney",
            "tencent",
            "sina",
            "akshare_ths",
            "sina_quote",
        )
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/storage/test_migration_0019.py tests/storage/test_data_sources.py tests/test_bootstrap.py -q
```

Expected: FAIL——`quote_source_health` 不存在、`sina_quote` 没在种子里、链断言少一个名字。

- [ ] **Step 3: 写迁移**

新建 `src/sams/storage/migrations/0019_quotes_and_prune.sql`：

```sql
-- 0019：盘中报价的形状、测试告警与两条查询索引（spec §4.2/§4.3，D114/D116/D131）。
--
-- 1) `quotes_snapshot` 在 0001 里只有价/涨跌幅/量/额/买卖一。盘中判 urgent 要开盘价与
--    前收盘（跳空与当日振幅），页面也要一个不依赖 `bars_daily` 的当日高低。四列都可空：
--    老快照不知道就说不知道，回填 0 会让「开盘价为零」与「没记过」长得一样。
--
-- 2) `quote_source_health` 与 `source_health` 分表。报价与日线是两条独立的故障域：
--    日线源在盘后同步时坏掉，不该让盘中报价显示为降级；反过来也一样。`ok` 沿用三态
--    （NULL = 从没被路由到），`last_ok_at` 是时刻而不是日期——报价的新鲜度按秒算，
--    日期粒度会把「三分钟没报价」显示成「今天成功过」。
--
-- 3) `alerts.is_test`（D131）：测试告警要能在收件箱里被认出来，且不参与 no_open_alert、
--    健康积压与复盘。DuckDB **不支持** `ADD COLUMN ... NOT NULL DEFAULT ...`（实测
--    ParserException），所以分三步：加可空列 → 设默认值 → 回填旧行。只加列不设默认的话，
--    服务层漏传一次就会写进 NULL；只回填不设默认的话，下一行仍是 NULL。
--
-- 4) 两个索引各服务一条查询：`quotes_snapshot` 按 `(market, code)` 取最近一笔填
--    `QuoteBoard`；`alerts` 按 `(user_id, triggered_at)` 取某个用户的收件箱。
--
-- 5) `sina_quote` 追加到每个市场链末位（D114）。它只报 `quote` 能力，日线链按能力过滤后
--    看不见它（D38）；放在同一张路由表里是因为「一个市场有哪些源」只能有一处答案。
--    已经手工加过同名源的库不动它的配置；只在该市场还没有这条路由时追加。

ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS open DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS prev_close DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS high DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS low DOUBLE;

CREATE INDEX IF NOT EXISTS idx_quotes_snapshot_lookup
    ON quotes_snapshot (market, code, ts);

ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR;

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS is_test BOOLEAN;
ALTER TABLE alerts ALTER COLUMN is_test SET DEFAULT FALSE;
UPDATE alerts SET is_test = FALSE WHERE is_test IS NULL;

CREATE INDEX IF NOT EXISTS idx_alerts_user_triggered
    ON alerts (user_id, triggered_at);

CREATE TABLE IF NOT EXISTS quote_source_health (
    name                 VARCHAR PRIMARY KEY,
    markets              VARCHAR NOT NULL DEFAULT '',
    ok                   BOOLEAN,
    last_error           VARCHAR,
    last_ok_at           TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('sina_quote', 'sina_quote', TRUE, '{}', '新浪 hq.sinajs.cn 批量报价（只提供报价，没有日线）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority)
SELECT tail.market, 'sina_quote', tail.next_priority
FROM (
    SELECT market, max(priority) + 1 AS next_priority
    FROM data_source_routes
    WHERE market IN ('A', 'HK', 'US')
    GROUP BY market
) AS tail
WHERE EXISTS (
    SELECT 1 FROM data_sources s
    WHERE s.name = 'sina_quote' AND s.kind = 'sina_quote'
);
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/storage/test_migration_0019.py tests/storage/test_data_sources.py tests/test_bootstrap.py -q
uv run ruff check tests/storage/test_migration_0019.py --no-cache
```

Expected: PASS。特别确认 `test_0019_backfills_is_test_on_an_existing_database` 里的
`new` 行读出来是 `False` 而不是 `None`——那一步验的正是默认值真的挂在了列上。

- [ ] **Step 5: 提交**

```bash
git add src/sams/storage/migrations/0019_quotes_and_prune.sql tests/storage/test_migration_0019.py tests/storage/test_data_sources.py tests/test_bootstrap.py
git commit -m "feat(storage): 0019 报价快照与测试告警形状"
```

---

### Task 8: 报价快照与报价源健康仓库

**Files:**
- Create: `src/sams/storage/repos/quotes.py`
- Test: `tests/storage/test_quotes_repo.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/storage/test_quotes_repo.py`：

```python
"""报价快照与报价源健康：重启回填、同秒覆盖、三态健康。

「同一只票取最近一笔」由窗口查询负责，不在读取侧把整张历史拉进内存——快照一天
就是几万行，而行情板只关心每只一行。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb

from sams.core.quotes import Quote, QuoteSourceRecord
from sams.storage.db import Database
from sams.storage.repos.quotes import QuoteHealthRepo, QuoteRepo

T0 = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=10)


def quote(code="600519", *, ts=T0, price=1540.0, market="A", **extra) -> Quote:
    return Quote(
        market=market, code=code, ts=ts, price=price, source="tencent", **extra
    )


def test_save_roundtrips_every_price_column(db):
    repo = QuoteRepo(db)
    written = repo.save([
        quote(
            open=1525.0,
            prev_close=1530.0,
            high=1545.0,
            low=1520.0,
            pct_chg=0.65,
            volume=2_400_000.0,
            amount=3_612_345_678.0,
            bid1=1539.9,
            ask1=1540.1,
            status="交易中",
        )
    ])
    assert written == 1
    stored = repo.latest()[0]
    assert stored.price == 1540.0
    assert (stored.open, stored.prev_close, stored.high, stored.low) == (
        1525.0, 1530.0, 1545.0, 1520.0,
    )
    assert stored.pct_chg == 0.65
    assert stored.volume == 2_400_000.0 and stored.amount == 3_612_345_678.0
    assert stored.bid1 == 1539.9 and stored.ask1 == 1540.1
    assert stored.status == "交易中"
    assert stored.ts == T0 and stored.source == "tencent"
    assert stored.is_stale is False


def test_latest_keeps_one_row_per_code_at_its_newest_time(db):
    repo = QuoteRepo(db)
    repo.save([
        quote(ts=T0, price=100.0),
        quote(ts=T1, price=101.0),
        quote("000001", ts=T0, price=10.0),
    ])
    rows = repo.latest()
    assert [(q.code, q.price) for q in rows] == [("000001", 10.0), ("600519", 101.0)]


def test_latest_can_be_narrowed_to_one_market(db):
    repo = QuoteRepo(db)
    repo.save([quote(), quote("AAPL", market="US", price=230.0)])
    assert [q.code for q in repo.latest(market="US")] == ["AAPL"]


def test_same_code_and_timestamp_replaces_instead_of_duplicating(db):
    repo = QuoteRepo(db)
    repo.save([quote(price=100.0)])
    repo.save([quote(price=101.0)])
    assert db.row_count("quotes_snapshot") == 1
    assert repo.latest()[0].price == 101.0


def test_empty_batch_is_a_noop(db):
    assert QuoteRepo(db).save([]) == 0
    assert db.row_count("quotes_snapshot") == 0


def test_rows_without_a_usable_price_are_skipped_on_read(db):
    """旧快照可能留下 price 为 0 或 NULL 的行（0001 的列可空）。

    行情板启动时读这张表：一行坏数据不该让整个看板起不来，也不该把 0 当成报价。
    """
    db.execute(
        "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
        " VALUES ('A', 'bad', ?, 0.0, 'tencent')",
        [T0],
    )
    db.execute(
        "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
        " VALUES ('A', 'null', ?, NULL, 'tencent')",
        [T0],
    )
    QuoteRepo(db).save([quote()])
    assert [q.code for q in QuoteRepo(db).latest()] == ["600519"]


def test_quote_health_roundtrips_three_states(db):
    repo = QuoteHealthRepo(db)
    repo.save(
        QuoteSourceRecord(
            name="tencent",
            markets=("A", "HK", "US"),
            ok=True,
            last_ok_at=T1,
            consecutive_failures=0,
        )
    )
    repo.save(QuoteSourceRecord(name="sina_quote", markets=("A",), ok=None))
    repo.save(
        QuoteSourceRecord(
            name="dead",
            markets=("A",),
            ok=False,
            last_error="timeout",
            consecutive_failures=3,
        )
    )
    loaded = repo.load()
    assert loaded["tencent"].ok is True
    assert loaded["tencent"].markets == ("A", "HK", "US")
    assert loaded["tencent"].last_ok_at == T1
    assert loaded["sina_quote"].ok is None, "NULL 是「从没被路由到」，不是 False"
    assert loaded["dead"].ok is False
    assert loaded["dead"].last_error == "timeout"
    assert loaded["dead"].consecutive_failures == 3


def test_health_reads_are_quiet_without_the_table():
    """0019 之前的老库：健康页要能出得来，这一节就是空的。"""
    database = Database(duckdb.connect(":memory:"))
    try:
        assert QuoteHealthRepo(database).load() == {}
    finally:
        database.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/storage/test_quotes_repo.py -q
```

Expected: FAIL，`ModuleNotFoundError: sams.storage.repos.quotes`。

- [ ] **Step 3: 实现**

新建 `src/sams/storage/repos/quotes.py`：

```python
"""报价快照与报价源健康。

两张表各答一个问题：

* `quotes_snapshot` 是历史。每轮每只一行，保留 5 个交易日（`sams db prune` 清）。
  行情板只关心「每只最近一笔」，由 `latest()` 的窗口查询给出——把整张历史拉进
  内存再挑，第一天就会变成几万行的读。
* `quote_source_health` 是报价链路自己的健康（D114）。写进日线的 `source_health`
  会让盘后健康页把日线源标红，而那条链可能完全正常；反过来也一样。

读侧对坏行是宽容的：`price` 为 NULL/0 的快照不构造成 `Quote`。行情板启动时读这张表，
一行历史脏数据不该让整个看板起不来，也不该把 0 当成报价。
"""

from __future__ import annotations

from collections.abc import Iterable

from sams.core.quotes import Quote, QuoteSourceRecord
from sams.storage.db import Database

_QUOTE_COLUMNS = (
    "market, code, ts, price, open, prev_close, high, low, pct_chg,"
    " volume, amount, bid1, ask1, status, is_stale, source"
)


class QuoteRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, quotes: Iterable[Quote]) -> int:
        """一批一个事务：写进去一半的批次比没写更糟——下一轮会把那几只当成已更新。"""
        rows = list(quotes)
        if not rows:
            return 0
        with self.db.transaction():
            for q in rows:
                self.db.execute(
                    f"INSERT OR REPLACE INTO quotes_snapshot ({_QUOTE_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        q.market,
                        q.code,
                        q.ts,
                        q.price,
                        q.open,
                        q.prev_close,
                        q.high,
                        q.low,
                        q.pct_chg,
                        q.volume,
                        q.amount,
                        q.bid1,
                        q.ask1,
                        q.status,
                        q.is_stale,
                        q.source,
                    ],
                )
        return len(rows)

    def latest(self, market: str | None = None) -> tuple[Quote, ...]:
        """每只票最近的一笔；`market` 给了就只看那一个市场。"""
        where, params = ("WHERE market = ?", [market]) if market else ("", [])
        rows = self.db.query(
            f"SELECT {_QUOTE_COLUMNS} FROM quotes_snapshot {where}"
            " QUALIFY row_number() OVER (PARTITION BY market, code ORDER BY ts DESC) = 1"
            " ORDER BY market, code",
            params,
        )
        return tuple(
            quote for quote in (_to_quote(row) for row in rows.to_dicts()) if quote is not None
        )


def _to_quote(row: dict) -> Quote | None:
    value = row["price"]
    if value is None or float(value) <= 0:
        return None
    return Quote(
        market=row["market"],
        code=row["code"],
        ts=row["ts"],
        price=float(value),
        open=row["open"],
        prev_close=row["prev_close"],
        high=row["high"],
        low=row["low"],
        pct_chg=row["pct_chg"],
        volume=row["volume"],
        amount=row["amount"],
        bid1=row["bid1"],
        ask1=row["ask1"],
        status=row["status"] or "",
        source=row["source"],
        is_stale=bool(row["is_stale"]),
    )


class QuoteHealthRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, record: QuoteSourceRecord) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO quote_source_health
                (name, markets, ok, last_error, last_ok_at, consecutive_failures, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, now())
            """,
            [
                record.name,
                ",".join(record.markets),
                record.ok,
                record.last_error,
                record.last_ok_at,
                record.consecutive_failures,
            ],
        )

    def load(self) -> dict[str, QuoteSourceRecord]:
        if "quote_source_health" not in self.db.table_names():
            return {}
        rows = self.db.query("SELECT * FROM quote_source_health").to_dicts()
        return {row["name"]: _to_record(row) for row in rows}


def _to_record(row: dict) -> QuoteSourceRecord:
    markets = row["markets"] or ""
    return QuoteSourceRecord(
        name=row["name"],
        markets=tuple(markets.split(",")) if markets else (),
        ok=None if row["ok"] is None else bool(row["ok"]),
        last_error=row["last_error"],
        last_ok_at=row["last_ok_at"],
        consecutive_failures=int(row["consecutive_failures"] or 0),
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/storage/test_quotes_repo.py -q
uv run ruff check src/sams/storage/repos/quotes.py tests/storage/test_quotes_repo.py --no-cache
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/storage/repos/quotes.py tests/storage/test_quotes_repo.py
git commit -m "feat(storage): 报价快照与报价源健康仓库"
```

---

### Task 9: 取价服务、行情板与线程安全的限速桶

**Files:**
- Create: `src/sams/services/quotes.py`
- Modify: `src/sams/datasource/ratelimit.py`
- Test: `tests/services/test_quote_board.py`, `tests/services/test_quotes.py`, `tests/datasource/test_ratelimit.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_quote_board.py`：

```python
"""行情板：读方拿到的是副本，写方一轮只动一个市场。

盘中的读（页面）与写（轮询线程）在同一个进程里并发，锁不是可选项；
但锁之外还有一条契约：`snapshot()` 返回的字典改了不能影响板子本身。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.quotes import Quote
from sams.services.quotes import QuoteBoard

T0 = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)


def quote(code="600519", *, market="A", price=100.0, ts=T0) -> Quote:
    return Quote(market=market, code=code, ts=ts, price=price, source="tencent")


def test_update_puts_quotes_in_place_and_clears_the_skip_reason():
    board = QuoteBoard()
    board.skip("A", "午休", now=T0)
    board.update("A", [quote()], now=T0)
    view = board.snapshot()["A"]
    assert view.quotes["600519"].price == 100.0
    assert view.last_poll_at == T0
    assert view.skip_reason is None and view.last_error is None


def test_failure_keeps_the_last_quote_but_marks_it_stale():
    board = QuoteBoard()
    board.update("A", [quote()], now=T0)
    board.fail("A", "全部源超时", now=T0)
    view = board.snapshot()["A"]
    assert view.quotes["600519"].is_stale is True
    assert view.quotes["600519"].price == 100.0, "失败不删除上一笔：页面要能显示最后已知价"
    assert view.last_error == "全部源超时"


def test_snapshot_is_a_copy():
    board = QuoteBoard()
    board.update("A", [quote()], now=T0)
    view = board.snapshot()
    view["A"].quotes.clear()
    view["A"].quotes["000001"] = quote("000001")
    assert set(board.snapshot()["A"].quotes) == {"600519"}


def test_a_skip_does_not_touch_the_quotes_even_across_a_session_break():
    """午休期间页面显示上午的最后一笔是正常的：`quote_state` 会把市场判成 closed，
    这里不额外标 stale——把闭市标成陈旧会让每个午休都像一次故障。
    """
    board = QuoteBoard()
    board.update("A", [quote()], now=T0)
    board.skip("A", "非交易时段（午休）", now=T0)
    view = board.snapshot()["A"]
    assert view.quotes["600519"].is_stale is False
    assert view.skip_reason == "非交易时段（午休）"
```

新建 `tests/services/test_quotes.py`：

```python
"""取价：降级、部分返回、批次大小、落库重试与三分钟告警。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sams.core.errors import StorageError, TransientError
from sams.core.events import EventBus, QuoteUpdated, SystemAlarm
from sams.core.quotes import Quote, QuoteBatch
from sams.datasource.registry import AdapterRegistry
from sams.services.quotes import QUOTE_OUTAGE_ALARM_S, QuoteBoard, QuoteService
from sams.storage.repos.quotes import QuoteHealthRepo, QuoteRepo
from support import FrozenClock

T0 = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=10)


class StubQuoteAdapter:
    def __init__(self, name: str, behaviour, *, markets=("A",)) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.behaviour = behaviour
        self.calls: list[list[str]] = []

    def quotes(self, market: str, codes: list[str]) -> QuoteBatch:
        self.calls.append(list(codes))
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour(market, codes)


def quote(code="600519", *, market="A", ts=T0, price=100.0, source="p") -> Quote:
    return Quote(market=market, code=code, ts=ts, price=price, source=source)


def batch_for(market: str, codes: list[str], *, source="p") -> QuoteBatch:
    return QuoteBatch(tuple(quote(code, market=market, source=source) for code in codes))


def make_service(
    db,
    adapters: dict[str, StubQuoteAdapter],
    *,
    market_sources: dict[str, tuple[str, ...]] | None = None,
    bus: EventBus | None = None,
    bucket=None,
    quotes_repo=None,
    sleep=lambda _seconds: None,
):
    registry = AdapterRegistry(
        adapters=adapters,
        market_sources=market_sources or {"A": tuple(adapters)},
    )
    board = QuoteBoard()
    service = QuoteService(
        registry,
        board,
        quotes_repo or QuoteRepo(db),
        QuoteHealthRepo(db),
        clock=FrozenClock(T0),
        bucket=bucket,
        bus=bus,
        sleep=sleep,
    )
    return service, board


def test_poll_persists_updates_board_records_health_and_publishes_events(db):
    events: list[QuoteUpdated] = []
    bus = EventBus()
    bus.subscribe(QuoteUpdated, events.append)
    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, bus=bus)

    result = service.poll("A", ["600519"], now=T0)

    assert [q.code for q in result.quotes] == ["600519"]
    assert result.sources == ("p",)
    assert QuoteRepo(db).latest()[0].price == 100.0
    assert board.get("A", "600519").price == 100.0
    assert [(event.market, event.code, event.ts) for event in events] == [("A", "600519", T0)]
    record = QuoteHealthRepo(db).load()["p"]
    assert record.ok is True and record.last_ok_at == T0
    assert record.consecutive_failures == 0


def test_primary_failure_falls_back_and_both_sources_are_recorded(db):
    primary = StubQuoteAdapter("p", TransientError("p", "timeout"))
    backup = StubQuoteAdapter("s", lambda market, codes: batch_for(market, codes, source="s"))
    service, board = make_service(
        db, {"p": primary, "s": backup}, market_sources={"A": ("p", "s")}
    )

    result = service.poll("A", ["600519"], now=T0)

    assert [q.source for q in result.quotes] == ["s"]
    assert primary.calls == [["600519"]] and backup.calls == [["600519"]]
    health = QuoteHealthRepo(db).load()
    assert health["p"].ok is False and health["p"].consecutive_failures == 1
    assert health["s"].ok is True
    assert "p" in (board.snapshot()["A"].last_error or ""), "降级要留在板上，不能只写日志"


def test_a_second_failure_accumulates_and_success_resets(db):
    primary = StubQuoteAdapter("p", TransientError("p", "timeout"))
    service, _ = make_service(db, {"p": primary}, market_sources={"A": ("p",)})
    service.poll("A", ["600519"], now=T0)
    service.poll("A", ["600519"], now=T1)
    assert QuoteHealthRepo(db).load()["p"].consecutive_failures == 2

    primary.behaviour = batch_for
    service.poll("A", ["600519"], now=T1)
    record = QuoteHealthRepo(db).load()["p"]
    assert record.ok is True and record.consecutive_failures == 0


def test_skipped_codes_do_not_fall_back_and_are_never_written_as_zero(db):
    primary = StubQuoteAdapter(
        "p", lambda market, codes: QuoteBatch((quote(codes[0]),), tuple(codes[1:]))
    )
    backup = StubQuoteAdapter("s", batch_for)
    service, board = make_service(
        db, {"p": primary, "s": backup}, market_sources={"A": ("p", "s")}
    )

    result = service.poll("A", ["600519", "000001"], now=T0)

    assert [q.code for q in result.quotes] == ["600519"]
    assert result.skipped == ("000001",)
    assert backup.calls == [], "源正常回答时，跳过的代码是下一轮的事，不是换源的理由"
    assert [q.code for q in QuoteRepo(db).latest()] == ["600519"]
    assert board.get("A", "000001") is None


def test_a_large_universe_is_split_into_batches_of_fifty(db):
    primary = StubQuoteAdapter("p", batch_for)
    service, _ = make_service(db, {"p": primary})
    codes = [f"{i:06d}" for i in range(120)]

    result = service.poll("A", codes, now=T0)

    assert [len(call) for call in primary.calls] == [50, 50, 20]
    assert len(result.quotes) == 120


def test_an_empty_code_list_touches_nothing(db):
    primary = StubQuoteAdapter("p", batch_for)
    service, _ = make_service(db, {"p": primary})
    result = service.poll("A", [], now=T0)
    assert result.quotes == () and primary.calls == []


def test_a_market_without_a_quote_source_is_an_error_on_the_board(db):
    service, board = make_service(db, {}, market_sources={"A": ()})
    result = service.poll("A", ["600519"], now=T0)
    assert result.quotes == () and result.errors
    assert "报价源" in (board.snapshot()["A"].last_error or "")


def test_outage_marks_the_board_stale_and_alarms_exactly_once_after_three_minutes(db):
    alarms: list[SystemAlarm] = []
    bus = EventBus()
    bus.subscribe(SystemAlarm, alarms.append)
    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, bus=bus)
    service.poll("A", ["600519"], now=T0)

    primary.behaviour = TransientError("p", "down")
    service.poll("A", ["600519"], now=T1)
    assert alarms == [], "第一次失败不告警：一次超时不是事故"

    service.poll("A", ["600519"], now=T1 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S - 1))
    assert alarms == []

    service.poll("A", ["600519"], now=T1 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S))
    service.poll("A", ["600519"], now=T1 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S + 60))

    assert len(alarms) == 1
    assert alarms[0].component == "quote.A"
    assert board.snapshot()["A"].quotes["600519"].is_stale is True


def test_recovery_clears_the_outage_so_a_later_outage_can_alarm_again(db):
    alarms: list[SystemAlarm] = []
    bus = EventBus()
    bus.subscribe(SystemAlarm, alarms.append)
    primary = StubQuoteAdapter("p", TransientError("p", "down"))
    service, board = make_service(db, {"p": primary}, bus=bus)
    service.poll("A", ["600519"], now=T0)
    service.poll("A", ["600519"], now=T0 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S))
    assert len(alarms) == 1

    primary.behaviour = batch_for
    service.poll("A", ["600519"], now=T0 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S + 10))
    assert board.snapshot()["A"].quotes["600519"].is_stale is False

    primary.behaviour = TransientError("p", "down again")
    start = T0 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S + 20)
    service.poll("A", ["600519"], now=start)
    service.poll("A", ["600519"], now=start + timedelta(seconds=QUOTE_OUTAGE_ALARM_S))
    assert len(alarms) == 2


def test_snapshot_write_is_retried_without_losing_the_board(db):
    class FlakyRepo(QuoteRepo):
        def __init__(self, database):
            super().__init__(database)
            self.calls = 0

        def save(self, quotes):
            self.calls += 1
            if self.calls < 3:
                raise StorageError("another connection is writing")
            return super().save(quotes)

    repo = FlakyRepo(db)
    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, quotes_repo=repo)

    result = service.poll("A", ["600519"], now=T0)

    assert repo.calls == 3
    assert result.quotes and board.get("A", "600519") is not None
    assert len(QuoteRepo(db).latest()) == 1


def test_a_permanently_failing_snapshot_write_does_not_kill_the_poll(db):
    class BrokenRepo:
        def save(self, quotes):
            raise StorageError("schema drift")

    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, quotes_repo=BrokenRepo())

    result = service.poll("A", ["600519"], now=T0)

    assert result.quotes, "这一轮的价格仍然是事实"
    assert board.get("A", "600519") is not None, "板子先服务页面，落库下一轮再试"
```

把 `tests/datasource/test_ratelimit.py` 顶部补 import：

```python
import threading
import time
from concurrent.futures import ThreadPoolExecutor
```

追加用例：

```python
def test_reserve_does_not_lose_tokens_under_threads():
    """盘中轮询与盘后同步共用同一个桶：扣减必须串行。

    没有锁时，两边可能读到同一个 `_level`、各自扣一次、后写的覆盖先写的——
    症状不是报错，而是限速被悄悄放宽（或收紧）了一倍。
    """
    start = threading.Barrier(8)

    def slow_clock():
        time.sleep(0.002)
        return 0.0

    bucket = TokenBucket(rate=1.0, capacity=1.0, clock=slow_clock)

    def take(_index):
        start.wait()
        return bucket.reserve(1.0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        waits = list(pool.map(take, range(8)))

    assert sorted(waits) == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/services/test_quote_board.py tests/services/test_quotes.py tests/datasource/test_ratelimit.py -q
```

Expected: FAIL，`ModuleNotFoundError: sams.services.quotes`；线程那条可能侥幸通过（无锁但线程恰好没交错），
实现后必须稳定通过。

- [ ] **Step 3: 实现**

新建 `src/sams/services/quotes.py`：

```python
"""取价、写快照、更新行情板、记报价源健康（spec §4.2/§4.3）。

这一层不认识「现在是不是交易时段」：时段判断在 `IntradayMonitor`，它只在该问的时候
调用 `poll`。也不认识页面：页面读 `QuoteBoard`，不读本类。

顺序是：取价 → 落库（失败重试后仍失败就记日志）→ 更新板 → 发事件。
落库失败不回滚板：这一轮的价格仍然是事实，页面先服务人，下一轮会重写同一只票。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import SamsError, StorageError
from sams.core.events import EventBus, QuoteUpdated, SystemAlarm
from sams.core.quotes import Quote, QuoteSourceRecord
from sams.core.retry import Sleeper, call_with_backoff
from sams.datasource.quote import QUOTE_BATCH, QuoteAdapter
from sams.datasource.ratelimit import TokenBucket
from sams.datasource.registry import AdapterRegistry
from sams.storage.repos.quotes import QuoteHealthRepo, QuoteRepo

logger = logging.getLogger("sams.services.quotes")

#: 整市场连续这么久没有任何报价才告警（spec §4.2）：一次超时不是事故
QUOTE_OUTAGE_ALARM_S = 180


@dataclass(frozen=True)
class MarketBoard:
    """一个市场此刻的读视图。`quotes` 按代码索引，是副本。"""

    market: str
    quotes: dict[str, Quote] = field(default_factory=dict)
    last_poll_at: datetime | None = None
    last_error: str | None = None
    #: 非交易时段/午休被跳过时的那一句；它解释「为什么没有新报价」，不是故障
    skip_reason: str | None = None


@dataclass
class _Mark:
    last_poll_at: datetime | None = None
    last_error: str | None = None
    skip_reason: str | None = None


class QuoteBoard:
    """进程内的「最后已知价」。

    读写都加锁：写方是盘中轮询线程，读方是每个页面的请求线程。`snapshot()` 返回
    深拷贝的字典（`Quote` 本身不可变），调用方改它不会碰到板子。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[tuple[str, str], Quote] = {}
        self._marks: dict[str, _Mark] = {}

    def seed(self, quotes: Iterable[Quote]) -> None:
        with self._lock:
            for quote in quotes:
                self._quotes[(quote.market, quote.code)] = quote

    def update(
        self,
        market: str,
        quotes: Iterable[Quote],
        *,
        now: datetime,
        error: str | None = None,
    ) -> None:
        with self._lock:
            for quote in quotes:
                self._quotes[(quote.market, quote.code)] = quote
            mark = self._marks.setdefault(market, _Mark())
            mark.last_poll_at = now
            mark.last_error = error
            mark.skip_reason = None

    def fail(self, market: str, error: str, *, now: datetime) -> None:
        """整市场取价失败：保留上一笔并显式标 stale。

        `quote_state` 也会按年龄判 stale，但「这一轮全都失败」比「价格十分钟没动」
        更强：源挂了时页面必须马上变色，而不是等 `quote_stale_s` 过去。
        """
        with self._lock:
            for key, quote in list(self._quotes.items()):
                if key[0] == market:
                    self._quotes[key] = quote.marked_stale()
            mark = self._marks.setdefault(market, _Mark())
            mark.last_poll_at = now
            mark.last_error = error
            mark.skip_reason = None

    def skip(self, market: str, reason: str, *, now: datetime) -> None:
        with self._lock:
            mark = self._marks.setdefault(market, _Mark())
            mark.last_poll_at = now
            mark.last_error = None
            mark.skip_reason = reason

    def get(self, market: str, code: str) -> Quote | None:
        with self._lock:
            return self._quotes.get((market, code))

    def snapshot(self) -> dict[str, MarketBoard]:
        with self._lock:
            markets = sorted(set(self._marks) | {market for market, _ in self._quotes})
            result: dict[str, MarketBoard] = {}
            for market in markets:
                mark = self._marks.get(market) or _Mark()
                result[market] = MarketBoard(
                    market=market,
                    quotes={
                        code: quote
                        for (quoted_market, code), quote in self._quotes.items()
                        if quoted_market == market
                    },
                    last_poll_at=mark.last_poll_at,
                    last_error=mark.last_error,
                    skip_reason=mark.skip_reason,
                )
            return result


@dataclass(frozen=True)
class QuotePollResult:
    """一整条报价链跑完后的结论。

    与 `QuoteBatch` 差在「跨源」这一维：`QuoteBatch` 是一个源的答复，
    这里汇总了降级过程中所有尝试。
    """

    quotes: tuple[Quote, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: 这一轮真正给出报价的源（按尝试顺序）；全失败时为空
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Attempt:
    quotes: tuple[Quote, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    failure: str | None = None


class QuoteService:
    def __init__(
        self,
        registry: AdapterRegistry,
        board: QuoteBoard,
        quotes: QuoteRepo,
        health: QuoteHealthRepo,
        *,
        clock: Clock = SYSTEM_CLOCK,
        bucket: TokenBucket | None = None,
        bus: EventBus | None = None,
        batch: int = QUOTE_BATCH,
        alarm_after_s: int = QUOTE_OUTAGE_ALARM_S,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self.registry = registry
        self.board = board
        self.quotes = quotes
        self.health = health
        self.clock = clock
        #: 与盘后同步共用同一个桶：两条线程各自限速等于把 `rate_limit_per_s` 放大一倍
        self.bucket = bucket
        self.bus = bus
        self.batch = batch
        self.alarm_after_s = alarm_after_s
        self.sleep = sleep
        previous = health.load()
        self._failures = {name: record.consecutive_failures for name, record in previous.items()}
        self._last_ok = {name: record.last_ok_at for name, record in previous.items()}
        self._outage_since: dict[str, datetime] = {}
        self._alarmed: set[str] = set()

    def load_board(self) -> int:
        """启动时用库里每只票最近一笔填板：重启后页面不该从空白开始。"""
        quotes = self.quotes.latest()
        self.board.seed(quotes)
        return len(quotes)

    def poll(
        self, market: str, codes: Sequence[str], *, now: datetime | None = None
    ) -> QuotePollResult:
        moment = now or self.clock.now_utc()
        wanted = list(dict.fromkeys(codes))
        if not wanted:
            return QuotePollResult()
        chain = self.registry.chain_for(market, "quote")
        if not chain:
            message = f"{market} 没有任何支持 quote 能力的数据源"
            self.board.fail(market, message, now=moment)
            self._note_outage(market, moment, got=False)
            return QuotePollResult(errors=(message,))

        collected: list[Quote] = []
        skipped: list[str] = []
        errors: list[str] = []
        degraded: list[str] = []
        answered: list[str] = []
        remaining = list(wanted)
        for name in chain:
            if not remaining:
                break
            adapter = self.registry.adapter(name)
            attempt = self._attempt(name, adapter, market, remaining)
            if attempt.failure is None:
                self._record_source(name, adapter, ok=True, error=None, moment=moment)
                collected.extend(attempt.quotes)
                skipped.extend(attempt.skipped)
                errors.extend(attempt.errors)
                answered.append(name)
                # 这个源回答了（哪怕只回了一部分）：跳过的代码下一轮再试，
                # 换成下一个源等于替用户猜「另一家更可能有这只票」
                break
            collected.extend(attempt.quotes)
            degraded.append(f"{name}: {attempt.failure}")
            errors.append(f"{name}: {attempt.failure}")
            self._record_source(name, adapter, ok=False, error=attempt.failure, moment=moment)
            got = {quote.code for quote in attempt.quotes} | set(attempt.skipped)
            remaining = [code for code in remaining if code not in got]

        summary = "；".join(degraded) or None
        if collected:
            self._persist(market, collected, moment)
            self.board.update(market, collected, now=moment, error=summary)
            self._note_outage(market, moment, got=True)
        else:
            self.board.fail(market, summary or f"{market} 本轮没有取得任何报价", now=moment)
            self._note_outage(market, moment, got=False)
            self._persist(market, [], moment)

        if self.bus is not None:
            for quote in collected:
                self.bus.publish(
                    QuoteUpdated(
                        ts=quote.ts,
                        market=quote.market,
                        code=quote.code,
                        price=quote.price,
                        pct_chg=quote.pct_chg,
                        volume=quote.volume,
                        is_stale=quote.is_stale,
                    )
                )
        return QuotePollResult(
            quotes=tuple(collected),
            skipped=tuple(skipped),
            errors=tuple(errors),
            sources=tuple(answered),
        )

    def _attempt(
        self,
        name: str,
        adapter: QuoteAdapter,
        market: str,
        codes: list[str],
    ) -> _Attempt:
        quotes: list[Quote] = []
        skipped: list[str] = []
        errors: list[str] = []
        for start in range(0, len(codes), self.batch):
            chunk = codes[start : start + self.batch]
            if self.bucket is not None:
                self.bucket.acquire()
            try:
                batch = adapter.quotes(market, chunk)
            except Exception as exc:  # noqa: BLE001 — 一个源的坏实现不该带走整条链
                logger.warning("%s 报价失败：%s", name, exc)
                return _Attempt(
                    tuple(quotes),
                    tuple(skipped),
                    tuple(errors),
                    failure=f"{type(exc).__name__}: {exc}",
                )
            quotes.extend(batch.quotes)
            skipped.extend(batch.skipped)
            errors.extend(batch.errors)
        return _Attempt(tuple(quotes), tuple(skipped), tuple(errors))

    def _persist(self, market: str, quotes: list[Quote], moment: datetime) -> None:
        try:
            call_with_backoff(
                lambda: self.quotes.save(quotes),
                attempts=4,
                base=0.01,
                factor=5.0,
                sleep=self.sleep,
                retryable=(StorageError,),
                label=f"写入 {market} 报价快照",
            )
        except SamsError as exc:
            # 落库失败不改变这一轮的结论：下一轮会重写同一只票，板子仍然服务页面
            logger.error("%s 报价快照落库失败：%s", market, exc)

    def _record_source(
        self,
        name: str,
        adapter: QuoteAdapter,
        *,
        ok: bool,
        error: str | None,
        moment: datetime,
    ) -> None:
        if ok:
            self._failures[name] = 0
            self._last_ok[name] = moment
        else:
            self._failures[name] = self._failures.get(name, 0) + 1
        record = QuoteSourceRecord(
            name=name,
            markets=tuple(sorted(getattr(adapter, "markets", ()))),
            ok=ok,
            last_error=None if ok else error,
            last_ok_at=self._last_ok.get(name),
            consecutive_failures=self._failures.get(name, 0),
        )
        try:
            self.health.save(record)
        except SamsError as exc:
            logger.warning("报价源健康落库失败（%s）：%s", name, exc)

    def _note_outage(self, market: str, moment: datetime, *, got: bool) -> None:
        if got:
            self._outage_since.pop(market, None)
            self._alarmed.discard(market)
            return
        since = self._outage_since.setdefault(market, moment)
        if market in self._alarmed:
            return
        if (moment - since).total_seconds() < self.alarm_after_s:
            return
        self._alarmed.add(market)
        if self.bus is not None:
            self.bus.publish(
                SystemAlarm(
                    component=f"quote.{market}",
                    message=(
                        f"{market} 市场连续 {self.alarm_after_s // 60} 分钟没有取得任何报价；"
                        "检查报价源与网络"
                    ),
                )
            )
```

把 `src/sams/datasource/ratelimit.py` 的 `TokenBucket` 改成：

```python
import threading
...

class TokenBucket:
    def __init__(...):
        ...
        self._level = float(self.capacity)
        self._updated = self.clock()
        #: 盘中轮询与盘后同步共用一个桶：没有锁时两边各自 `_refill` 再扣减，
        #: 后写的 `_level` 会覆盖先写的，限速被悄悄放宽
        self._lock = threading.Lock()

    def reserve(self, tokens: float = 1.0) -> float:
        """扣减 tokens，返回调用方应等待的秒数（0 表示可立即执行）。

        `sleep` 留在锁外（见 `acquire`）：持锁等待会把所有调用方串行到一次 HTTP 的
        时长上，那不是限速，是排队超时。
        """
        with self._lock:
            self._refill()
            self._level -= tokens
            return 0.0 if self._level >= 0 else -self._level / self.rate
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/services/test_quote_board.py tests/services/test_quotes.py tests/datasource/test_ratelimit.py -q
uv run ruff check src/sams/services/quotes.py src/sams/datasource/ratelimit.py tests/services/test_quote_board.py tests/services/test_quotes.py --no-cache
```

Expected: PASS。多跑两遍线程那条：`uv run pytest tests/datasource/test_ratelimit.py -q -x --count 3` 若没装
`pytest-repeat` 就跑三次同一条命令，结果必须一致。

- [ ] **Step 5: 提交**

```bash
git add src/sams/services/quotes.py src/sams/datasource/ratelimit.py tests/services/test_quote_board.py tests/services/test_quotes.py tests/datasource/test_ratelimit.py
git commit -m "feat(services): 报价降级链、行情板与三分钟告警"
```

---

### Task 10: 盘中轮询节拍器

**Files:**
- Create: `src/sams/services/intraday.py`
- Test: `tests/services/test_intraday.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_intraday.py`：

```python
"""盘中节拍器：只在常规时段问价、问该问的票、坏一轮不带走整个循环。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from sams.core.errors import TransientError
from sams.services.intraday import IntradayMonitor
from sams.services.quotes import QuoteBoard, QuotePollResult
from support import FrozenClock

T0 = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)


class FakeUniverse:
    def __init__(self, by_market: dict[str, tuple[str, ...]]) -> None:
        self.by_market = by_market
        self.calls = 0

    def build(self):
        self.calls += 1
        by_market = self.by_market

        class View:
            def codes(self, market: str) -> tuple[str, ...]:
                return by_market.get(market, ())

        return View()


class FlakyUniverse(FakeUniverse):
    def build(self):
        self.calls += 1
        if self.calls == 1:
            raise TransientError("universe", "ledger read failed")
        return super().build()


class FakeCalendar:
    def __init__(self, *, trading=None, open_at=None) -> None:
        self.trading = trading or {}
        self.open_at = open_at or {}

    def is_trading_day(self, market: str, day) -> bool:
        return self.trading.get(market, True)

    def is_market_open(self, market: str, when) -> bool:
        return self.open_at.get(market, True)


class FakeQuotes:
    def __init__(self) -> None:
        self.board = QuoteBoard()
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail: dict[str, Exception] = {}

    def poll(self, market, codes, *, now=None) -> QuotePollResult:
        self.calls.append((market, tuple(codes)))
        if market in self.fail:
            raise self.fail[market]
        return QuotePollResult(sources=("p",))


def monitor(settings, *, quotes=None, calendar=None, universe=None):
    return IntradayMonitor(
        settings,
        quotes or FakeQuotes(),
        calendar or FakeCalendar(),
        universe or FakeUniverse({"A": ("600519",), "HK": ("00700",), "US": ("AAPL",)}),
        clock=FrozenClock(T0),
        sleep=lambda _seconds: None,
    )


def test_only_open_markets_are_polled_with_their_own_codes(settings):
    quotes = FakeQuotes()
    minutes = monitor(
        settings,
        quotes=quotes,
        calendar=FakeCalendar(trading={"US": False}, open_at={"HK": False}),
    )

    outcomes = minutes.poll_once(now=T0)

    assert quotes.calls == [("A", ("600519",))]
    skipped = {outcome.market: outcome.skipped for outcome in outcomes}
    assert skipped["US"] == "非交易日"
    assert "非交易时段" in skipped["HK"]
    assert skipped["A"] is None


def test_a_market_with_nothing_to_watch_is_not_polled(settings):
    quotes = FakeQuotes()
    minutes = monitor(settings, quotes=quotes, universe=FakeUniverse({"A": ()}))
    outcomes = minutes.poll_once(now=T0)
    assert quotes.calls == []
    assert outcomes[0].skipped == "没有需要监控的标的"
    assert quotes.board.snapshot()["A"].skip_reason == "没有需要监控的标的"


def test_one_markets_failure_does_not_stop_the_next(settings):
    quotes = FakeQuotes()
    quotes.fail["A"] = TransientError("p", "boom")
    minutes = monitor(settings, quotes=quotes, calendar=FakeCalendar())

    outcomes = minutes.poll_once(now=T0)

    assert [call[0] for call in quotes.calls] == ["A", "HK", "US"]
    assert [outcome.market for outcome in outcomes] == ["A", "HK", "US"]
    assert outcomes[0].error and "boom" in outcomes[0].error
    assert quotes.board.snapshot()["A"].last_error


def test_a_failed_round_is_logged_and_the_loop_continues(settings):
    universe = FlakyUniverse({"A": ("600519",), "HK": (), "US": ()})
    quotes = FakeQuotes()
    minutes = monitor(settings, quotes=quotes, universe=universe)
    rounds = []

    count = minutes.run_forever(max_rounds=2, on_round=rounds.append)

    assert count == 2
    assert len(rounds) == 2 and rounds[0] == ()
    assert quotes.calls and quotes.calls[0][0] == "A"


def test_stop_event_ends_the_loop_before_the_next_round(settings):
    stop = threading.Event()
    rounds = []
    minutes = monitor(settings)

    count = minutes.run_forever(
        stop=stop, on_round=lambda outcomes: (rounds.append(outcomes), stop.set())
    )

    assert count == 1 and len(rounds) == 1


def test_poll_once_is_a_noop_for_closed_markets(settings):
    quotes = FakeQuotes()
    minutes = monitor(
        settings,
        quotes=quotes,
        calendar=FakeCalendar(trading={"A": False, "HK": False, "US": False}),
    )
    minutes.poll_once(now=T0)
    assert quotes.calls == []
    assert set(quotes.board.snapshot()) == {"A", "HK", "US"}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/services/test_intraday.py -q
```

Expected: FAIL，`ModuleNotFoundError: sams.services.intraday`。

- [ ] **Step 3: 实现**

新建 `src/sams/services/intraday.py`：

```python
"""盘中轮询的节拍器：什么时候问、问哪些、跳过时留下什么（spec §4.3）。

只做三件事：

1. 每个市场用交易日历判断「现在该不该问」——非交易日、午休、收盘后一律跳过，
   并把原因写到板上（页面要能解释「为什么没有新报价」）；
2. 构建这一轮的对象全集（自选 ∪ 持仓 ∪ 规则目标 ∪ 在跟观点）；
3. 调 `QuoteService.poll`。取价、降级、落库、告警都在那一层。

**不在这里评估规则**：那一层吃 `QuotePollResult`，由装配决定它跟不跟在这个循环后面。
也不 import 调度器：`UniverseBuilder` 只需要一个 `build()` → `.codes()` 的形状，
把整个调度器拖进来会让「盘中」依赖「盘后」的内部。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import SamsError
from sams.core.retry import Sleeper
from sams.services.quotes import QuotePollResult, QuoteService

logger = logging.getLogger("sams.services.intraday")


class UniverseView(Protocol):
    def codes(self, market: str) -> tuple[str, ...]: ...


class UniverseSource(Protocol):
    def build(self) -> UniverseView: ...


@dataclass(frozen=True)
class MarketPoll:
    market: str
    #: 这一市场被跳过的原因；被跳过时 `result` 为空
    skipped: str | None = None
    #: 这一市场取价整段失败（与「源返回 0 条」不同）
    error: str | None = None
    result: QuotePollResult | None = None


class IntradayMonitor:
    def __init__(
        self,
        settings: Settings,
        quotes: QuoteService,
        calendar,
        universe: UniverseSource,
        *,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Sleeper = time.sleep,
        interval_s: float | None = None,
    ) -> None:
        self.settings = settings
        self.quotes = quotes
        self.calendar = calendar
        self.universe = universe
        self.clock = clock
        self.sleep = sleep
        self.interval_s = interval_s if interval_s is not None else settings.polling_interval_s

    def poll_once(self, *, now: datetime | None = None) -> tuple[MarketPoll, ...]:
        moment = now or self.clock.now_utc()
        universe = self.universe.build()
        outcomes: list[MarketPoll] = []
        for market in sorted(self.settings.markets):
            reason = self._skip_reason(market, moment)
            if reason is not None:
                self.quotes.board.skip(market, reason, now=moment)
                outcomes.append(MarketPoll(market, skipped=reason))
                continue
            codes = universe.codes(market)
            if not codes:
                # 开市中但没有任何对象：不是故障，但页面要能说清「为什么没动」
                self.quotes.board.skip(market, "没有需要监控的标的", now=moment)
                outcomes.append(MarketPoll(market, skipped="没有需要监控的标的"))
                continue
            try:
                result = self.quotes.poll(market, codes, now=moment)
            except SamsError as exc:
                # 只隔离这一个市场：日历源坏了不该让排在后面的市场也不跑
                logger.exception("%s 这一轮取价失败", market)
                self.quotes.board.fail(market, str(exc), now=moment)
                outcomes.append(MarketPoll(market, error=str(exc)))
                continue
            outcomes.append(MarketPoll(market, result=result))
        return tuple(outcomes)

    def run_forever(
        self,
        *,
        stop: threading.Event | None = None,
        on_round=None,
        max_rounds: int | None = None,
    ) -> int:
        """一轮接一轮地跑，返回实际跑过的轮数。

        只接 `SamsError`：那是「这一轮做不成」（账本读不到、日历源坏了），循环本身
        没坏。编程错误照旧炸出去——接住它之后每一轮都失败却还在打日志，比停下更糟。
        """
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            if stop is not None and stop.is_set():
                break
            try:
                outcomes = self.poll_once()
            except SamsError as exc:
                logger.exception("盘中这一轮失败：%s", exc)
                outcomes = ()
            if on_round is not None:
                on_round(outcomes)
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
            if stop is not None:
                if stop.wait(self.interval_s):
                    break
            else:
                self.sleep(self.interval_s)
        return rounds

    def _skip_reason(self, market: str, moment: datetime) -> str | None:
        """常规时段才问价（D114）：盘前盘后不轮询，量会凭空放大且报价不代表常规成交。"""
        cfg = self.settings.markets[market]
        local_day = moment.astimezone(cfg.zone()).date()
        if not self.calendar.is_trading_day(market, local_day):
            return "非交易日"
        if not self.calendar.is_market_open(market, moment):
            return "非交易时段（未开盘/午休/已收盘）"
        return None
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/services/test_intraday.py -q
uv run ruff check src/sams/services/intraday.py tests/services/test_intraday.py --no-cache
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/services/intraday.py tests/services/test_intraday.py
git commit -m "feat(services): 盘中轮询只在常规时段问价"
```

---

### Task 11: 手动请求队列与停止事件

**Files:**
- Modify: `src/sams/services/scheduler/due.py`（`_local_today` 公开为 `local_today`）
- Modify: `src/sams/services/scheduler/service.py`
- Test: `tests/services/test_scheduler_requests.py`

手动触发绕过的**只有**到期计算。「本周期已成功」「上一轮还在跑」「失败后十分钟内」这三条仍由 `JobRunner` 回答（D105）：它们保护的是源与账本，不是时间表。页面看到 `skipped` 与原因，而不是假装跑过。

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_scheduler_requests.py`：

```python
"""手动请求队列：只绕过到期计算，排队顺序稳定，停止事件收得住循环。

到期 job 的记录型 runner 与 `test_scheduler_service.py` 同一形状；这里额外钉的是
「请求排在到期 job 之前」「锚点按市场本地日」「id 在同一冻住的时钟下仍唯一」，
以及 `stop` 在 tick 之间、等待之中两种位置都能结束循环。
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime

import pytest
from support import FrozenClock
from support_scheduler import FakeCalendar

from sams.core.errors import ValidationError
from sams.services.scheduler.runner import RunResult
from sams.services.scheduler.service import RunRequestQueue, Scheduler
from sams.services.scheduler.types import JobOutcome, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo

#: A/HK 已是 10-25（04:00），US 还是 10-24（16:00）：手动锚点必须按市场换算
MOMENT = datetime(2024, 10, 24, 20, 0, tzinfo=UTC)
FRIDAY = date(2024, 10, 25)
THURSDAY = date(2024, 10, 24)
UNIVERSE = Universe(by_market={}, fx_pairs=())


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str, date]] = []

    def execute(self, spec, due, universe):
        self.calls.append((spec.name, spec.market, due.period_key, due.as_of))
        return RunResult(
            job=spec.name,
            market=spec.market,
            period_key=due.period_key,
            status="ok",
        )


class StubUniverse:
    def build(self) -> Universe:
        return UNIVERSE


class _Stop:
    """`threading.Event` 的替身：只记下等待，不等真时间。"""

    def __init__(self, *, already: bool = False) -> None:
        self._set = already
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        self._set = True
        return True


def _noop(ctx):
    return JobOutcome()


def job_spec(name, market, period="daily"):
    return JobSpec(name=name, period=period, market=market, run=_noop)


def make(db, settings, specs, runner, *, clock=None, sleep=None):
    clock = clock or FrozenClock(MOMENT)
    return Scheduler(
        settings,
        specs,
        runner=runner,
        runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        universe=StubUniverse(),
        users=UserRepo(db),
        calendar=FakeCalendar(settings),
        clock=clock,
        sleep=sleep or (lambda seconds: None),
    ), HeartbeatRepo(db, clock=clock)


def test_a_request_runs_before_the_due_jobs_and_anchors_on_market_local_day(db, settings):
    runner = RecordingRunner()
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), runner)

    request = scheduler.request_run("bars", "A")
    report = scheduler.tick()

    # 第一行是手动请求（今天），第二行才是到期计算给的昨天
    assert [call[2:] for call in runner.calls] == [
        ("2024-10-25", FRIDAY),
        ("2024-10-24", THURSDAY),
    ]
    assert report.requests[0].request.id == request.id
    assert [result.period_key for result in report.requests[0].results] == ["2024-10-25"]
    assert report.results[0].period_key == "2024-10-25"


def test_an_explicit_as_of_overrides_the_clock(db, settings):
    runner = RecordingRunner()
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), runner)

    scheduler.request_run("bars", "A", as_of=date(2024, 10, 18))
    scheduler.tick()

    assert runner.calls[0][2:] == ("2024-10-18", date(2024, 10, 18))


def test_a_market_request_covers_every_job_of_that_market(db, settings):
    runner = RecordingRunner()
    specs = (job_spec("bars", "A"), job_spec("rules", "A"), job_spec("bars", "HK"))
    scheduler, _ = make(db, settings, specs, runner)

    scheduler.request_run(market="A")
    report = scheduler.tick(only_market="HK")

    assert [call[:2] for call in runner.calls] == [
        ("bars", "A"),
        ("rules", "A"),
        ("bars", "HK"),
    ]
    assert [result.job for result in report.requests[0].results] == ["bars", "rules"]


def test_a_typo_is_refused_before_it_reaches_the_queue(db, settings):
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    with pytest.raises(ValidationError, match=r"job=nope"):
        scheduler.request_run("nope", "A")
    with pytest.raises(ValidationError, match=r"market=ZZ"):
        scheduler.request_run("bars", "ZZ")

    assert scheduler.pending_requests == 0


def test_request_ids_stay_unique_when_the_clock_does_not_move(db, settings):
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    first = scheduler.request_run("bars", "A")
    second = scheduler.request_run("bars", "A")

    assert first.id != second.id
    assert first.requested_at == second.requested_at == MOMENT
    assert [request.id for request in scheduler.requests.drain()] == [first.id, second.id]
    assert scheduler.pending_requests == 0


def test_the_queue_loses_nothing_when_many_threads_enqueue():
    queue = RunRequestQueue()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        for _ in range(25):
            queue.put(job="bars", market="A", as_of=None, requested_at=MOMENT)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    drained = queue.drain()

    assert len(drained) == 100
    assert len({request.id for request in drained}) == 100


def test_stop_set_before_the_loop_leaves_no_heartbeat(db, settings):
    scheduler, heartbeats = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    assert scheduler.run_forever(stop=_Stop(already=True)) == 0
    assert heartbeats.latest() is None


def test_stop_during_a_tick_ends_before_the_next_round(db, settings):
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    stop = threading.Event()
    reports = []

    def on_tick(report):
        reports.append(report)
        stop.set()

    ticks = scheduler.run_forever(stop=stop, on_tick=on_tick)

    assert ticks == 1
    assert len(reports) == 1


def test_stop_wakes_the_wait_instead_of_the_injected_sleep(db, settings):
    sleeps = []
    scheduler, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        sleep=sleeps.append,
    )
    stop = _Stop()

    ticks = scheduler.run_forever(stop=stop)

    assert ticks == 1
    assert sleeps == []
    assert stop.waits == [settings.scheduler_tick_s]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/services/test_scheduler_requests.py -q
```

Expected: FAIL，`ImportError: cannot import name 'RunRequestQueue' from 'sams.services.scheduler.service'`。

- [ ] **Step 3: 实现**

先把 `src/sams/services/scheduler/due.py` 的 `_local_today` 公开——手动锚点与到期计算必须共用同一个「市场本地今天」（D99），不能再写第二份时区换算：

```python
def local_today(settings: Settings, market: str, now: datetime) -> date:
    """这个市场此刻的本地日期（D99）：同一瞬间，北京和纽约不是同一个「今天」。"""
    return now.astimezone(ZoneInfo(settings.market_cfg(market).tz)).date()
```

`daily_due` 与 `weekly_due` 里的两处 `_local_today(` 改成 `local_today(`。

`src/sams/services/scheduler/service.py` 的 import 段改成：

```python
import itertools
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import SamsError, SchedulerBusy, ValidationError
from sams.services.scheduler.beatfile import write_beat
from sams.services.scheduler.due import CalendarView, Due, due_for, local_today, period_key
from sams.services.scheduler.heartbeat import HEARTBEAT_STALE_S, stale_at
from sams.services.scheduler.report import ScheduleData, collect
from sams.services.scheduler.runner import JobRunner, RunResult
from sams.services.scheduler.types import JobSpec
from sams.services.scheduler.universe import Universe, UniverseBuilder
from sams.storage.repos.task_runs import FAILED, HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo
```

把 `TickReport` 这一节替换成下面四个类型：

```python
@dataclass(frozen=True)
class RunRequest:
    """一次「现在就跑」的请求：跑哪些 job、锚在哪一天。"""

    id: str
    job: str | None
    market: str | None
    as_of: date | None
    requested_at: datetime


class RunRequestQueue:
    """跨线程的请求队列：Web 只排队，调度线程在下一拍执行。

    `task_runs` 只有一个写入方（D105），所以 Web 的「立即同步」不能另开写线程；
    它把请求放进这里，由同一条调度线程执行。id 用序号而不是时间戳：时钟可能被
    冻住（测试）或两个请求落在同一微秒，而按钮回显要求两个 id 不同。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[RunRequest] = deque()
        self._seq = itertools.count(1)

    def put(
        self,
        *,
        job: str | None,
        market: str | None,
        as_of: date | None,
        requested_at: datetime,
    ) -> RunRequest:
        with self._lock:
            request = RunRequest(
                id=f"req-{next(self._seq)}",
                job=job,
                market=market,
                as_of=as_of,
                requested_at=requested_at,
            )
            self._items.append(request)
            return request

    def drain(self) -> tuple[RunRequest, ...]:
        with self._lock:
            items = tuple(self._items)
            self._items.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(frozen=True)
class RequestReport:
    request: RunRequest
    results: tuple[RunResult, ...] = ()


@dataclass(frozen=True)
class TickReport:
    now: datetime
    results: tuple[RunResult, ...] = ()
    waiting: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: 手动请求按请求分组；`results` 里也有它们，按执行顺序排在到期 job 之前
    requests: tuple[RequestReport, ...] = ()

    @property
    def failed(self) -> tuple[RunResult, ...]:
        return tuple(result for result in self.results if result.status == FAILED)
```

`__init__` 的签名在 `beat_path` 之后加一格，并在 `self._beat_id` 之前保存它：

```python
        beat_path: Path | None = None,
        requests: RunRequestQueue | None = None,
    ) -> None:
```

```python
        self.beat_path = beat_path
        #: 手动请求队列：`serve` 里的 Web 线程与本调度线程共用这一个实例
        self.requests = requests if requests is not None else RunRequestQueue()
        self._beat_id: str | None = None
```

`tick` 的整体替换（到期循环内部与原来逐字相同）：

```python
    def tick(
        self,
        now: datetime | None = None,
        *,
        as_of: date | None = None,
        only_job: str | None = None,
        only_market: str | None = None,
    ) -> TickReport:
        moment = now if now is not None else self.clock.now_utc()
        self._beat()
        selected = self._selected(only_job, only_market)
        universe = self.universe.build()
        # 手工请求排在到期 job 之前：人点了按钮，这件事先做。清空放在 universe
        # 构建之后——构建失败时请求还留在队列里，下一拍仍能再试
        manual = self._run_requests(self.requests.drain(), universe, moment)
        results: list[RunResult] = [result for report in manual for result in report.results]
        waiting: list[str] = []
        errors: list[str] = []
        for spec in selected:
            if as_of is not None:
                due = Due(period_key=period_key(spec.period, as_of), as_of=as_of)
            else:
                try:
                    due = due_for(
                        spec.period,
                        self.calendar,
                        self.settings,
                        market=spec.market,
                        now=moment,
                    )
                except SamsError as exc:
                    errors.append(f"{spec.title}: {exc}")
                    continue
                if due is None:
                    waiting.append(spec.title)
                    continue
            self._beat()
            results.append(self.runner.execute(spec, due, universe))
        return TickReport(
            now=moment,
            results=tuple(results),
            waiting=tuple(waiting),
            errors=tuple(errors),
            requests=manual,
        )
```

在 `tick` 之后加三个方法：

```python
    def request_run(
        self,
        job: str | None = None,
        market: str | None = None,
        *,
        as_of: date | None = None,
    ) -> RunRequest:
        """把手动请求排进下一拍；敲错的名字在这里就拒绝。

        入队前跑一遍与 `tick` 相同的筛选：请求线程还拿得到异常，能当场告诉人哪一段
        敲错了；交给调度线程之后，那条错只剩日志里的一行。
        """
        self._selected(job, market)
        return self.requests.put(
            job=job,
            market=market,
            as_of=as_of,
            requested_at=self.clock.now_utc(),
        )

    @property
    def pending_requests(self) -> int:
        return len(self.requests)

    def _run_requests(
        self,
        requests: Sequence[RunRequest],
        universe: Universe,
        moment: datetime,
    ) -> tuple[RequestReport, ...]:
        """这里绕过的只有到期计算：本周期已成功、上一轮还在跑、失败后十分钟内
        这三条仍由 runner 回答（D105）——它们保护的是源与账本，不是时间表。
        """
        reports: list[RequestReport] = []
        for request in requests:
            results: list[RunResult] = []
            for spec in self._selected(request.job, request.market):
                day = request.as_of or self._anchor_day(spec, moment)
                self._beat()
                results.append(
                    self.runner.execute(
                        spec,
                        Due(period_key=period_key(spec.period, day), as_of=day),
                        universe,
                    )
                )
            reports.append(RequestReport(request=request, results=tuple(results)))
        return tuple(reports)

    def _anchor_day(self, spec: JobSpec, moment: datetime) -> date:
        """手动触发没有「到期日」：锚在这次请求的市场本地今天（D99）。

        全局 job（汇率）没有市场时区，按 `moment` 自己的日期定锚；M4-A 的管理页
        只按市场触发日线，不走这一支。
        """
        if spec.market is None:
            return moment.date()
        return local_today(self.settings, spec.market, moment)
```

`run_forever` 的整体替换（原有说明段落保留，补 `stop` 一段）：

```python
    def run_forever(
        self,
        *,
        on_tick: Callable[[TickReport], None] | None = None,
        max_ticks: int | None = None,
        stop: threading.Event | None = None,
    ) -> int:
        """认领心跳，然后一轮一轮跑下去。返回实际跑过的 tick 数。

        `max_ticks=0`（或负数）一轮都不跑，也不认领心跳：留下一个刚打过的拍会把
        下一个常驻循环挡在阈值外面，而这一次连活都没开始。

        `stop` 是给同进程的 `serve` 用的：它在两拍之间（以及等待中）结束循环，
        不必发信号；有 `stop` 时等待走 `stop.wait`，停止请求不用等满一个间隔。
        没有 `stop` 时仍用注入的 `sleep`，CLI 与既有测试的行为不变。

        这里只接 `SamsError`：那一类是「这一轮做不成」（账本读不到、一个 job 都没选出），
        循环本身没坏，下一轮还得试。别的东西（编程错误）在这里没有可信的下一步，
        炸出去比原地重入更值钱——循环接住它之后，每一拍照打，读的那一侧只会把一个
        坏掉的实现看成「还在跑」。
        """
        if max_ticks is not None and max_ticks <= 0:
            return 0
        if stop is not None and stop.is_set():
            # 与 `max_ticks<=0` 同理：认领一行刚打过的心跳会把下一个循环挡在阈值外
            return 0
        self._claim()
        # 认领之后先打一拍：文件从第一轮开始就该存在，而不是等第一个 job 走到
        self._beat()
        ticks = 0
        try:
            try:
                while max_ticks is None or ticks < max_ticks:
                    if stop is not None and stop.is_set():
                        break
                    try:
                        report = self.tick()
                    except SamsError as exc:
                        # 栈只能留在这里：这一句把异常吞了，`tick` 的那一层不再有人能打印它
                        logger.exception("这一轮 tick 失败：%s", exc)
                        report = TickReport(now=self.clock.now_utc(), errors=(str(exc),))
                    if on_tick is not None:
                        on_tick(report)
                    ticks += 1
                    if max_ticks is not None and ticks >= max_ticks:
                        break
                    if stop is not None:
                        if stop.wait(self.settings.scheduler_tick_s):
                            break
                    else:
                        self.sleep(self.settings.scheduler_tick_s)
            except KeyboardInterrupt:
                logger.warning("收到中断，调度器退出")
            return ticks
        finally:
            # 正常退出要把文件带走：留下一个越来越旧的文件，会把「我停了」说成「我卡住了」。
            # 被 SIGKILL 的那一种留得下，那正是「没人正常收尾」的证据
            self._drop_beat_file()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/services/test_scheduler_requests.py tests/services/test_scheduler_service.py tests/services/test_scheduler_due.py -q
uv run ruff check src/sams/services/scheduler tests/services/test_scheduler_requests.py --no-cache
```

Expected: PASS（原有 `test_scheduler_service.py` 一条都不改）。

- [ ] **Step 5: 提交**

```bash
git add src/sams/services/scheduler/due.py src/sams/services/scheduler/service.py tests/services/test_scheduler_requests.py
git commit -m "feat(scheduler): 手动同步排进下一拍，停止事件收得住循环"
```

---

### Task 12: 进程级 `AppCore` 与线程级 `ServiceContext`

**Files:**
- Modify: `src/sams/bootstrap.py`
- Modify: `tests/conftest.py`（追加 `migrated` 夹具）
- Test: `tests/test_bootstrap.py`、`tests/test_adversarial_services.py`

拆分的判据只有一条：**连接属于线程**。`AppCore` 里放的是「全进程一份才正确」的东西（配置、时钟、总线、加密器、数据源路由、手动请求队列）；`ServiceContext` 是一条线程的一整套仓库与服务，各持一条 DuckDB 连接（D113）。调度器也在上下文里造，但只有调度线程会跑它；请求队列来自 `core.requests`，所以 Web 线程 `request_run` 入队的就是同一批请求。

`SystemAlerter` 从 `build_context` 里搬走：它在总线上订阅，而 `serve` 里每条线程一个上下文，各挂一次会让同一条系统告警变成 N 条管理员告警。CLI 仍由 `build_container` 挂一次；`serve` 在计划 2 的通知队列接入时由 `Runtime` 挂一次。

- [ ] **Step 1: 写失败测试**

在 `tests/test_bootstrap.py` 的 import 段改成：

```python
from sams.bootstrap import attach_log_sink, build_container, build_context, build_core
```

`tests/conftest.py` 末尾追加 `migrated` 夹具（`Database` 与 `apply_migrations` 文件头
已经 import 过，不用再加）：

```python
@pytest.fixture
def migrated(settings) -> Settings:
    """db_path 指向一个已迁移的临时文件库。

    `build_core`/`build_context` 会各自 `Database.open` 同一个路径：内存库开不出
    第二条连接，所以这一份必须是真文件（`settings` 夹具的 db_path 已经在 tmp_path 里）。
    """
    database = Database.open(settings.db_path)
    try:
        apply_migrations(database)
    finally:
        database.close()
    return settings
```

追加两条用例：

```python
def test_contexts_get_their_own_connection_but_share_the_core(migrated):
    """DuckDB 同进程多连接共享同一个库；连接、事务深度、注册视图都不得跨线程。

    调度器是每条上下文各造一台的对象，但请求队列是进程级的：从第二个上下文入队，
    第一个上下文的那台调度器要能看见——否则 Web 的「立即同步」永远到不了调度线程。
    """
    core = build_core(migrated)
    first = build_context(core)
    second = build_context(core)
    try:
        assert first.db is not second.db
        assert first.registry is core.registry
        assert first.bus is core.bus and first.clock is core.clock
        assert first.scheduler.requests is core.requests
        first.users.create("alice", "correct-horse-battery")
        assert second.users.get_by_username("alice") is not None

        request = second.scheduler.request_run("bars", "A")
        assert first.scheduler.pending_requests == 1
        assert [item.id for item in first.scheduler.requests.drain()] == [request.id]
    finally:
        first.close()
        second.close()
        core.close()


def test_attaching_the_log_sink_twice_still_logs_each_alarm_once(caplog):
    """`serve` 只有一个进程一条总线：重建装配、或 core 与上下文各挂一次，
    同一条系统告警都不该在日志里出现两遍。"""
    bus = EventBus()
    attach_log_sink(bus)
    attach_log_sink(bus)

    with caplog.at_level(logging.ERROR, logger="sams.bootstrap"):
        bus.publish(SystemAlarm(component="dup", message="hello"))

    assert caplog.text.count("hello") == 1
```

把 `tests/test_adversarial_services.py` 里那条「已知边界」改成现在的承诺：

```python
def test_rebuilding_container_on_same_bus_keeps_one_log_sink(db, settings, caplog):
    """同一个 bus 上只挂一个 log sink：反复装配不该让告警在日志里翻倍。"""
    bus = EventBus()
    build_container(settings, db=db, bus=bus, adapters=stub_all())
    build_container(settings, db=db, bus=bus, adapters=stub_all())

    with caplog.at_level(logging.ERROR, logger="sams.bootstrap"):
        bus.publish(SystemAlarm(component="dup", message="hello"))

    assert caplog.text.count("hello") == 1
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_bootstrap.py tests/test_adversarial_services.py -q
```

Expected: FAIL，`ImportError: cannot import name 'build_core'`；log sink 那条数到 2 次。

- [ ] **Step 3: 实现**

`src/sams/bootstrap.py` 的 import 段补五处（其余不动）：

```python
import weakref
from dataclasses import dataclass, fields
```

```python
from sams.services.scheduler.service import RunRequestQueue, Scheduler
```

```python
from sams.storage.repos.quotes import QuoteHealthRepo, QuoteRepo
```

`attach_log_sink` 整体替换：

```python
#: 已经挂过 sink 的总线。`serve` 里 core 只造一次，但 CLI 的测试与「重建装配」
#: 会反复经过这里：同一个 bus 上挂两个 sink，一条系统告警会在日志里出现两遍。
_SINKED: weakref.WeakSet[EventBus] = weakref.WeakSet()


def attach_log_sink(bus: EventBus) -> None:
    """让 SystemAlarm 至少有一条落地路径（同一个 bus 只挂一次）。

    总线没有订阅者时 publish 是静默 no-op：spec §12 的「采集连续失败降系统告警」
    会在没人察觉的情况下不成立。日志是那条最小路径，
    M3 的 admin 通知渠道订阅同一事件即可，不必改这里。
    """
    if bus in _SINKED:
        return

    def log_alarm(event: SystemAlarm) -> None:
        logger.error("[alarm] %s: %s", event.component, event.message)

    bus.subscribe(SystemAlarm, log_alarm)
    _SINKED.add(bus)
```

把 `Container` 这个 dataclass 整体替换成下面三个：

```python
@dataclass(frozen=True)
class AppCore:
    """进程级共享件：后台线程与 HTTP 线程都拿这一份（D113）。

    `registry_db` 只服务 registry 的源健康落库；它不是任何线程上下文的那条连接。
    `build_container` 让两者共用同一条连接，以保持 CLI「一个进程一条连接」的形状。
    """

    settings: Settings
    clock: Clock
    bus: EventBus
    cipher: Cipher
    registry: AdapterRegistry
    #: 手动请求队列：调度线程与 Web 共用，Web 的「立即同步」才落得到同一台调度器
    requests: RunRequestQueue
    registry_db: Database

    def close(self) -> None:
        self.registry_db.close()


@dataclass
class ServiceContext:
    """一条线程的一整套仓库与服务：一条 DuckDB 连接，不跨线程（D113）。

    每条上下文各造一台调度器，但只有调度线程会 `run_forever`；请求队列来自
    `AppCore.requests`，所以别的线程 `request_run` 入队的是同一批请求。
    """

    settings: Settings
    db: Database
    bus: EventBus
    clock: Clock
    users: UserRepo
    sessions: SessionRepo
    symbols: SymbolRepo
    bars: BarRepo
    corp_actions: CorpActionRepo
    corp_actions_coverage: CorpActionsCoverageRepo
    # 三个基本面仓库带 `_store`：`container.financials` 与 `container.fundamentals`
    # 只差一个后缀，而一个是仓库、一个是读取用例，混用的代价是读代码的人猜
    financials_store: FinancialRepo
    valuation_store: ValuationRepo
    flow_store: FundingFlowRepo
    scan_store: ScanResultRepo
    watchlists: WatchlistRepo
    #: 汇率仓库：`fx` 这个名字留给服务（M3-A 的 `sams sync fx`），带 `_store` 是为了不误用
    fx_store: FxRepo
    #: thesis 的仓库带 `_store`：`container.theses` 是那个读取用例（写入口只有一个）
    theses_store: ThesisRepo
    calendar_store: CalendarRepo
    source_health: SourceHealthRepo
    data_sources: DataSourceRepo
    registry: AdapterRegistry
    accounts: AccountService
    calendar: CalendarService
    sync: SyncService
    corp_action_sync: CorpActionSyncService
    fundamental_sync: FundamentalSyncService
    #: 汇率那条链：没有市场维度，所以它不跟着 `sync`/`fundamental_sync` 走市场路由
    fx_sync: FxSyncService
    fundamentals: FundamentalService
    screen: ScreenService
    analysis: AnalysisService
    #: 估值合成（spec §8.3）：它吃上面那两个读数用例，不是并列的第三路取数
    valuation: ValuationService
    #: 观点的生命周期：它吃上面那个仓库与那份估值读数（spec §9.1）
    theses: ThesisService
    #: 账本的写用例：一次成交跨订单/持仓/现金/流水四张表，入口只有这一个（§5.4）
    ledger: LedgerService
    #: 持仓与汇总的读用例：它只读账本，不改账本
    portfolio: PortfolioService
    #: SIM 的账户仓库：`accounts` 这个名字属于用户账号服务（`AccountService`），
    #: 两个都对得上名字的东西放在一起一定会被误用，所以这里带 `sim_` 前缀
    sim_accounts: SimAccountRepo
    #: 成交/持仓/现金的那个仓库（订单历史 + 状态）
    sim_ledger: SimOrderRepo
    channels_store: ChannelRepo
    alerts_store: AlertRepo
    #: 通道凭据的加密器：CLI 要能在不碰 `storage.services.*` 的情况下掩码
    cipher: Cipher
    #: 发送路由（spec §11）
    notify: NotifyService
    health: HealthService
    source_admin: SourceAdminService
    #: 规则仓库：`rules` 那个名字留给 T-17 的服务，仓库带 `_store`（与 theses/fx 同一命名法）
    rules_store: RuleRepo
    #: 规则引擎的取数装配（spec §10）
    rule_data: RuleData
    #: 一趟求值（`sams rule check` 的出口，T-18 的命令行按它接线）
    rule_engine: RuleEngine
    #: 规则的维护面（T-17）：四样仓库层看不见的检查 + 改参数时回写论上那一列
    rule_service: RuleService
    #: §9.2 建仓派生（`sams rule derive`）
    rule_deriver: RuleDeriver
    #: 周复检（§9.3，`sams rule recheck`）
    rule_rechecker: Rechecker
    #: 报价快照与报价源健康：盘中线程写、Web 读，各走自己的连接
    quotes_store: QuoteRepo
    quote_health: QuoteHealthRepo
    task_runs: TaskRunRepo
    heartbeats: HeartbeatRepo
    #: 常驻调度器（spec §12）：`sams run start|once|jobs` 与健康页的 `[调度]` 读的是同一台
    scheduler: Scheduler

    def close(self) -> None:
        self.db.close()

    # 迁移是显式运维动作，CLI 经由容器触发，不直接 import storage
    def migrate(self) -> list[str]:
        return apply_migrations(self.db)

    def pending_migrations(self) -> list[str]:
        return pending(self.db)

    def applied_migrations(self) -> set[str]:
        return applied(self.db)


@dataclass
class Container(ServiceContext):
    """CLI 的容器：与线程级上下文同一形状，名字留给 CLI 的签名。"""
```

在 `_registry` 之后、`build_container` 之前插入 `build_core`：

```python
def build_core(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    database: Database | None = None,
    adapters: dict[str, BarAdapter] | None = None,
) -> AppCore:
    """进程级共享件，一次进程只造一份。

    `database` 给进来时（`build_container` 就这么做）registry 的源健康与 CLI 的其余
    仓库共用一条连接；不给就自己开一条，只给 registry 用。quote 与调度两条线程
    各开各的连接，不碰这一条。
    """
    cfg = settings or Settings.load()
    event_bus = bus or EventBus()
    attach_log_sink(event_bus)
    clock = clock or SYSTEM_CLOCK
    core_db = database or Database.open(cfg.db_path)
    source_health = SourceHealthRepo(core_db)
    data_sources = DataSourceRepo(core_db)
    registry = _registry(cfg, data_sources, source_health, event_bus, clock, adapters)
    return AppCore(
        settings=cfg,
        clock=clock,
        bus=event_bus,
        cipher=cipher_for(cfg),
        registry=registry,
        requests=RunRequestQueue(),
        registry_db=core_db,
    )
```

把原 `build_container` 的函数体整体搬进下面这个函数（逐字保留各服务与注释，只换掉开头四样东西的来源，并在末尾加上报价仓库与 `requests=`）：

```python
def build_context(
    core: AppCore,
    *,
    database: Database | None = None,
    providers: dict[str, CalendarProvider] | None = None,
    notifier_factory: Callable[[str, Mapping[str, object]], Notifier] | None = None,
) -> ServiceContext:
    """一条线程的一整套仓库与服务。不传 `database` 时自己开一条（D113）。"""
    cfg = core.settings
    database = database or Database.open(cfg.db_path)
    clock = core.clock
    event_bus = core.bus
    registry = core.registry
    cipher = core.cipher
    bars = BarRepo(database)
    symbols = SymbolRepo(database)
    calendar_store = CalendarRepo(database)
    users = UserRepo(database)
    sessions = SessionRepo(database, clock)
    corp_actions = CorpActionRepo(database)
    corp_actions_coverage = CorpActionsCoverageRepo(database)
    financials_store = FinancialRepo(database)
    valuation_store = ValuationRepo(database)
    flow_store = FundingFlowRepo(database)
    scan_store = ScanResultRepo(database)
    theses_store = ThesisRepo(database)
    source_health = SourceHealthRepo(database)
    data_sources = DataSourceRepo(database)
    quotes_store = QuoteRepo(database)
    quote_health = QuoteHealthRepo(database)

    providers = providers if providers is not None else default_providers(cfg)
    calendar = CalendarService(calendar_store, providers, bus=event_bus, clock=clock)
    fundamentals = FundamentalService(financials_store, valuation_store, clock=clock)
    analysis = AnalysisService(bars, corp_actions, corp_actions_coverage, symbols, clock=clock)
    valuation = ValuationService(
        fundamentals, analysis, financials_store, valuation_store, flow_store, clock=clock
    )
    sim_accounts = SimAccountRepo(database)
    sim_orders = SimOrderRepo(database, sim_accounts, markets=cfg.markets, symbols=symbols)
    ledger = LedgerService(sim_accounts, sim_orders, theses_store, users, cfg)
    fx_store = FxRepo(database)
    portfolio = PortfolioService(sim_accounts, sim_orders, bars, fx_store, users, clock=clock)
    channels_store = ChannelRepo(database, cipher=cipher, clock=clock)
    alerts_store = AlertRepo(database, clock=clock)
    notify = NotifyService(
        channels_store,
        alerts_store,
        users,
        bus=event_bus,
        factory=notifier_factory if notifier_factory is not None else build_notifier_factory(),
    )

    watchlists = WatchlistRepo(database)
    rules_store = RuleRepo(database, clock=clock)
    rule_data = RuleData(
        valuation=valuation,
        analysis=analysis,
        bars=bars,
        symbols=symbols,
        accounts=sim_accounts,
        ledger=sim_orders,
        theses=theses_store,
        watchlists=watchlists,
        settings=cfg,
    )
    # 规则告警与系统告警共用这一个 sender：DND、通道选择、`notify_log` 那一行都住在
    # `NotifyService` 那一头，一个组装根里留两条发送路径，迟早一条带上这些而另一条不带
    sender = _NotifyAlertSender(notify)
    rule_engine = RuleEngine(
        rules=rules_store,
        alerts=alerts_store,
        data=rule_data,
        calendar=calendar,
        fx=fx_store,
        bus=event_bus,
        sender=sender,
        settings=cfg,
        clock=clock,
    )
    # 三个规则服务排在最后：它们吃的就是上面那些已经装好的仓库与取数处，
    # 而 `RuleService` 要的是 `rule_data`（notes 里那句「展开成零个对象」由它算）
    rule_service = RuleService(
        rules=rules_store,
        theses=theses_store,
        accounts=sim_accounts,
        channels=channels_store,
        users=users,
        data=rule_data,
        db=database,
        settings=cfg,
        clock=clock,
    )
    rule_deriver = RuleDeriver(rules_store, theses_store)
    rule_rechecker = Rechecker(
        theses=theses_store, data=rule_data, bus=event_bus, clock=clock
    )

    # 下面四个同步服务与 `UniverseBuilder` 原本就地造在 `return Container(...)` 的参数里。
    # 调度器也要吃它们，而同一个服务被造两份时，容器里那个与 job 里那个不是同一个对象：
    # 测试替换或事后调一处参数，另一处照旧跑——症状不是报错，是「改了没生效」
    sync = SyncService(registry, bars, symbols, cfg.markets)
    corp_action_sync = CorpActionSyncService(
        registry, corp_actions, corp_actions_coverage, clock=clock
    )
    fundamental_sync = FundamentalSyncService(
        registry, financials_store, valuation_store, flow_store, symbols
    )
    fx_sync = FxSyncService(registry, fx_store)
    universe_builder = UniverseBuilder(
        users=users,
        watchlists=watchlists,
        accounts=sim_accounts,
        orders=sim_orders,
        rules=rules_store,
        theses=theses_store,
        data=rule_data,
        settings=cfg,
    )
    task_runs = TaskRunRepo(database, clock=clock)
    heartbeats = HeartbeatRepo(database, clock=clock)
    deps = JobDeps(
        settings=cfg,
        registry=registry,
        sync=sync,
        corp_actions=corp_action_sync,
        fundamentals=fundamental_sync,
        fx=fx_sync,
        calendar=calendar,
        rules=rule_engine,
        recheck=rule_rechecker,
        users=users,
    )
    scheduler = Scheduler(
        cfg,
        build_jobs(deps),
        runner=JobRunner(runs=task_runs, bus=event_bus, clock=clock),
        runs=task_runs,
        heartbeats=heartbeats,
        universe=universe_builder,
        users=users,
        calendar=calendar,
        clock=clock,
        # 库被这个循环握着时，外部读者只能读库旁那一拍（D111）。
        # 内存库里 `beat_path` 是 `None`，什么也不写
        beat_path=beat_path(database.path),
        # 所有上下文共用同一只队列：只有调度线程跑循环，但谁都能往里放请求
        requests=core.requests,
    )

    return ServiceContext(
        settings=cfg,
        db=database,
        bus=event_bus,
        clock=clock,
        users=users,
        sessions=sessions,
        symbols=symbols,
        bars=bars,
        corp_actions=corp_actions,
        corp_actions_coverage=corp_actions_coverage,
        financials_store=financials_store,
        valuation_store=valuation_store,
        flow_store=flow_store,
        scan_store=scan_store,
        theses_store=theses_store,
        watchlists=watchlists,
        fx_store=fx_store,
        calendar_store=calendar_store,
        source_health=source_health,
        data_sources=data_sources,
        source_admin=SourceAdminService(data_sources, cfg),
        registry=registry,
        accounts=AccountService(users, sessions),
        calendar=calendar,
        sync=sync,
        corp_action_sync=corp_action_sync,
        fundamental_sync=fundamental_sync,
        fx_sync=fx_sync,
        fundamentals=fundamentals,
        screen=ScreenService(
            financials_store, valuation_store, symbols, scan_store, clock=clock
        ),
        analysis=analysis,
        valuation=valuation,
        # `settings` 是那个「已知市场」的出处：一个没配的市场立不出 thesis，
        # 因为那条链上每一栏都读不到数（D84）
        theses=ThesisService(theses_store, users, valuation, cfg, clock=clock, orders=sim_orders),
        ledger=ledger,
        portfolio=portfolio,
        sim_accounts=sim_accounts,
        sim_ledger=sim_orders,
        channels_store=channels_store,
        alerts_store=alerts_store,
        rules_store=rules_store,
        rule_data=rule_data,
        rule_engine=rule_engine,
        rule_service=rule_service,
        rule_deriver=rule_deriver,
        rule_rechecker=rule_rechecker,
        quotes_store=quotes_store,
        quote_health=quote_health,
        task_runs=task_runs,
        heartbeats=heartbeats,
        scheduler=scheduler,
        cipher=cipher,
        notify=notify,
        health=HealthService(
            database,
            registry,
            calendar_store,
            cfg,
            clock=clock,
            sim_accounts=sim_accounts,
            sim_ledger=sim_orders,
            fx=fx_store,
            # 本机通道的可用性由 monitor 探测，health 只接这一个函数：多一条 import 就红了
            local_status=notify.local_status,
            # 健康页读的就是上面这台调度器留下的那两张台账：不是「它此刻活着吗」——
            # 调度器持有库文件时，第二个进程连不上（DuckDB 的独占锁），这一屏说的是
            # 「它上一次写下结论是什么时候」
            schedule=scheduler.schedule_data,
        ),
    )
```

`SystemAlerter` 那一段从函数体里拿掉，改成下面的独立函数（放在 `build_context` 之后）：

```python
def attach_system_alerter(context: ServiceContext) -> SystemAlerter:
    """把系统告警写进管理员收件箱；一个进程只挂一次。

    `build_context` 不挂：`serve` 里每条线程一个上下文，各挂一次会让同一条系统告警
    变成 N 条管理员告警。CLI 在 `build_container` 末尾挂；`serve` 在通知队列接入后
    由 `Runtime` 挂一次（计划 2）。
    """
    alerter = SystemAlerter(
        alerts=context.alerts_store,
        users=context.users,
        sender=_NotifyAlertSender(context.notify),
        settings=context.settings,
        clock=context.clock,
    )
    alerter.attach(context.bus)
    return alerter
```

`build_container` 整体替换成：

```python
def build_container(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    bus: EventBus | None = None,
    clock: Clock | None = None,
    adapters: dict[str, BarAdapter] | None = None,
    providers: dict[str, CalendarProvider] | None = None,
    notifier_factory: Callable[[str, Mapping[str, object]], Notifier] | None = None,
) -> Container:
    """CLI 的单一入口：core 与上下文共用一条连接，形状与 M3 完全一致。"""
    cfg = settings or Settings.load()
    database = db or Database.open(cfg.db_path)
    core = build_core(cfg, clock=clock, bus=bus, database=database, adapters=adapters)
    context = build_context(
        core,
        database=database,
        providers=providers,
        notifier_factory=notifier_factory,
    )
    attach_system_alerter(context)
    return Container(
        **{field.name: getattr(context, field.name) for field in fields(context)}
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_bootstrap.py tests/test_adversarial_services.py tests/test_layering.py -q
uv run ruff check src/sams/bootstrap.py tests/test_bootstrap.py tests/test_adversarial_services.py --no-cache
```

Expected: PASS。分层守卫若报 `services.scheduler.service` 被 `bootstrap` 引用，说明 `RunRequestQueue` 放错了模块（它必须留在 `services/scheduler/service.py`；`bootstrap` 是组装根，允许 import 任何下层）。

- [ ] **Step 5: 提交**

```bash
git add src/sams/bootstrap.py tests/test_bootstrap.py tests/test_adversarial_services.py
git commit -m "refactor(bootstrap): 进程共享件与线程上下文分家"
```

---

### Task 13: 保留策略：`PruneService` 与 `sams db prune`

**Files:**
- Modify: `src/sams/storage/repos/quotes.py`
- Modify: `src/sams/storage/repos/task_runs.py`
- Create: `src/sams/services/prune.py`
- Modify: `src/sams/bootstrap.py`
- Modify: `src/sams/cli/db.py`
- Test: `tests/storage/test_prune_repos.py`
- Test: `tests/services/test_prune.py`
- Test: `tests/cli/test_prune_cli.py`
- Test: `tests/test_bootstrap.py`

- [ ] **Step 1: 写仓库层的失败测试**

新建 `tests/storage/test_prune_repos.py`：

```python
"""保留策略的仓库层：删除边界只看三个数——日期、天数、条数。

这些用例直接写表而不经过服务：要钉的是「第 N 个交易日」与「第 N 条心跳」这两个
边界本身。服务层的用例再钉「dry-run 与实删用的是同一份计数」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.storage.repos.quotes import QuoteRepo
from sams.storage.repos.task_runs import HeartbeatRepo, TaskRunRepo


def add_quote(db, market, code, day, *, price=10.0):
    db.execute(
        "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
        " VALUES (?, ?, ?, ?, 'test')",
        [market, code, datetime.combine(day, time(2, 0), tzinfo=UTC), price],
    )


def test_session_cutoff_is_the_nth_newest_day_per_market(db):
    """边界按**市场各数各的**：A 股长假那一周港股照常开市，按全库日期数会把
    A 股节前最后一个交易日的快照提前删掉——那正是复盘最需要的一屏。
    """
    for offset in range(7):
        add_quote(db, "HK", "00700", date(2026, 10, 1) + timedelta(days=offset))
    for offset in range(3):
        add_quote(db, "A", "600519", date(2026, 10, 1) + timedelta(days=offset))
    # HK 的第 5 新是 10-03；A 只有 3 天，不足 5 天就整段保留（不出现在字典里）
    assert QuoteRepo(db).session_cutoffs(5) == {"HK": date(2026, 10, 3)}


def test_session_cutoff_rejects_zero(db):
    with pytest.raises(ValidationError, match="keep_sessions"):
        QuoteRepo(db).session_cutoffs(0)


def test_count_then_delete_removes_only_rows_before_each_cutoff(db):
    for offset in range(7):
        add_quote(db, "HK", "00700", date(2026, 10, 1) + timedelta(days=offset))
    for offset in range(3):
        add_quote(db, "A", "600519", date(2026, 10, 1) + timedelta(days=offset))
    repo = QuoteRepo(db)
    cutoffs = repo.session_cutoffs(5)
    assert repo.count_before(cutoffs) == 2
    repo.delete_before(cutoffs)
    remaining = db.query(
        "SELECT market, count(*) AS n FROM quotes_snapshot GROUP BY market ORDER BY market"
    ).to_dicts()
    assert remaining == [{"market": "A", "n": 3}, {"market": "HK", "n": 5}]


def test_an_empty_cutoff_map_deletes_nothing(db):
    """一个市场都没有 5 天历史时，字典是空的：空字典不是「全删」的信号。"""
    add_quote(db, "A", "600519", date(2026, 10, 1))
    repo = QuoteRepo(db)
    assert repo.count_before({}) == 0
    repo.delete_before({})
    assert db.row_count("quotes_snapshot") == 1


def test_task_runs_older_than_the_cutoff_are_counted_and_deleted(db):
    repo = TaskRunRepo(db)
    for run_id, started in (
        ("old", datetime(2025, 1, 1, tzinfo=UTC)),
        ("edge", datetime(2026, 1, 1, tzinfo=UTC)),
        ("new", datetime(2026, 6, 1, tzinfo=UTC)),
    ):
        db.execute(
            "INSERT INTO task_runs (id, job_name, started_at, status)"
            " VALUES (?, 'bars', ?, 'ok')",
            [run_id, started],
        )
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    assert repo.count_before(cutoff) == 1, "等于边界的那一行要留下：判据是 < 不是 <="
    repo.delete_before(cutoff)
    assert [row.id for row in repo.recent("bars", None, limit=10)] == ["new", "edge"]


def test_heartbeats_beyond_the_keep_count_are_the_oldest(db):
    clock = FrozenClock(datetime(2026, 10, 1, tzinfo=UTC))
    repo = HeartbeatRepo(db, clock=clock)
    ids = []
    for _ in range(4):
        ids.append(repo.start(note="tick=60s"))
        clock.advance(minutes=1)
    assert repo.count_beyond(2) == 2
    repo.delete_beyond(2)
    left = db.query("SELECT id FROM scheduler_heartbeats ORDER BY beat_at").to_dicts()
    assert [row["id"] for row in left] == ids[-2:]


def test_heartbeats_keep_rejects_zero(db):
    with pytest.raises(ValidationError, match="keep"):
        HeartbeatRepo(db).count_beyond(0)
    with pytest.raises(ValidationError, match="keep"):
        HeartbeatRepo(db).delete_beyond(0)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/storage/test_prune_repos.py -q
```

Expected: FAIL，`AttributeError: 'QuoteRepo' object has no attribute 'session_cutoffs'`。

- [ ] **Step 3: 实现仓库层**

在 `src/sams/storage/repos/quotes.py` 顶部的 import 改成：

```python
from collections.abc import Iterable, Mapping
from datetime import date, datetime
```

在 `QuoteRepo.save/latest` 之后、`_to_quote` 之前加：

```python
    def session_cutoffs(self, keep_sessions: int) -> dict[str, date]:
        """每个市场各留最近 N 个**有快照的交易日**，返回第 N 新的那一天。

        按市场分开数，不按全库日期数：A 股长假那一周港股照常开市，全库数法会把
        A 股节前最后一日的快照当成「第 8 天」删掉。用快照里出现过的日期而不是交易日历：
        这个函数回答的是「表里有哪些天」，一张空表不该被日历判成「有 5 天要保留」。
        某个市场不足 N 天时不出现在返回值里——那不是「删到空」，而是没有可删的。
        """
        if keep_sessions < 1:
            raise ValidationError("keep_sessions 至少是 1：0 会把每个市场当前的快照全删掉")
        rows = self.db.query(
            "WITH days AS ("
            "  SELECT DISTINCT market, CAST(ts AS DATE) AS d FROM quotes_snapshot"
            "), ranked AS ("
            "  SELECT market, d, row_number() OVER (PARTITION BY market ORDER BY d DESC) AS rn"
            "  FROM days"
            ")"
            " SELECT market, d FROM ranked WHERE rn = ?",
            [keep_sessions],
        )
        return {str(row["market"]): _day(row["d"]) for row in rows.to_dicts()}

    def count_before(self, cutoffs: Mapping[str, date]) -> int:
        where, params = _before_where(cutoffs)
        if not where:
            return 0
        return int(
            self.db.scalar(f"SELECT count(*) FROM quotes_snapshot WHERE {where}", params) or 0
        )

    def delete_before(self, cutoffs: Mapping[str, date]) -> None:
        where, params = _before_where(cutoffs)
        if not where:
            return
        self.db.execute(f"DELETE FROM quotes_snapshot WHERE {where}", params)
```

在 `QuoteRepo` 类之前加两个模块级辅助：

```python
def _day(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _before_where(cutoffs: Mapping[str, date]) -> tuple[str, list]:
    """把「每个市场各自的截止日」拼成一条 WHERE：A 股的截止日与港股的不是同一天。"""
    clauses: list[str] = []
    params: list = []
    for market, day in sorted(cutoffs.items()):
        clauses.append("(market = ? AND CAST(ts AS DATE) < ?)")
        params.extend([market, day])
    return " OR ".join(clauses), params
```

在 `src/sams/storage/repos/task_runs.py` 的 `TaskRunRepo.count` 之前加：

```python
    def count_before(self, cutoff: datetime) -> int:
        return int(
            self.db.scalar("SELECT count(*) FROM task_runs WHERE started_at < ?", [cutoff]) or 0
        )

    def delete_before(self, cutoff: datetime) -> None:
        """超过保留期的执行台账一律删，包括一直没收尾的 `running`。

        九十天还停在 `running` 的唯一解释是崩溃留下的：D105 的六小时判据早就把它
        标成「上一跑没收尾」，再留着只会把「最近一次」永远占住。
        """
        self.db.execute("DELETE FROM task_runs WHERE started_at < ?", [cutoff])
```

在 `HeartbeatRepo.latest` 之后加：

```python
    def count_beyond(self, keep: int) -> int:
        if keep < 1:
            raise ValidationError("keep 至少是 1：0 会把全部心跳删掉")
        return int(
            self.db.scalar(
                "SELECT count(*) FROM scheduler_heartbeats WHERE id NOT IN ("
                "  SELECT id FROM scheduler_heartbeats"
                "  ORDER BY beat_at DESC, rowid DESC LIMIT ?)",
                [keep],
            )
            or 0
        )

    def delete_beyond(self, keep: int) -> None:
        if keep < 1:
            raise ValidationError("keep 至少是 1：0 会把全部心跳删掉")
        self.db.execute(
            "DELETE FROM scheduler_heartbeats WHERE id NOT IN ("
            "  SELECT id FROM scheduler_heartbeats"
            "  ORDER BY beat_at DESC, rowid DESC LIMIT ?)",
            [keep],
        )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/storage/test_prune_repos.py -q
uv run ruff check src/sams/storage/repos/quotes.py src/sams/storage/repos/task_runs.py tests/storage/test_prune_repos.py --no-cache
```

Expected: PASS（7 个用例）。

- [ ] **Step 5: 写清理服务的失败测试**

新建 `tests/services/test_prune.py`：

```python
"""清理服务：保留边界、dry-run 与实删同一份计数、每一步留一行 task_runs。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.services.prune import PruneService
from sams.storage.repos.quotes import QuoteRepo
from sams.storage.repos.task_runs import HeartbeatRepo, TaskRunRepo

NOW = datetime(2026, 10, 23, 4, 0, tzinfo=UTC)


def make(db, clock=None):
    clock = clock or FrozenClock(NOW)
    return PruneService(
        db,
        quotes=QuoteRepo(db),
        task_runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        clock=clock,
    )


def seed_quotes(db):
    # HK 有 7 天、A 只有 3 天：保留 5 天时只有 HK 最旧的两天该走
    for offset in range(7):
        day = date(2026, 10, 1) + timedelta(days=offset)
        for market, code in (("HK", "00700"), ("A", "600519")):
            if market == "A" and offset >= 3:
                continue
            db.execute(
                "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
                " VALUES (?, ?, ?, 10.0, 'test')",
                [market, code, datetime.combine(day, time(2, 0), tzinfo=UTC)],
            )


def seed_task_runs(db):
    for run_id, days in (("old-1", 200), ("old-2", 100), ("new", 1)):
        db.execute(
            "INSERT INTO task_runs (id, job_name, started_at, status)"
            " VALUES (?, 'bars', ?, 'ok')",
            [run_id, NOW - timedelta(days=days)],
        )


def seed_heartbeats(db):
    clock = FrozenClock(NOW - timedelta(hours=10))
    repo = HeartbeatRepo(db, clock=clock)
    for _ in range(5):
        repo.start(note="tick=60s")
        clock.advance(minutes=1)


def test_run_deletes_by_the_three_retention_windows(db):
    seed_quotes(db)
    seed_task_runs(db)
    seed_heartbeats(db)
    report = make(db).run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    assert report.dry_run is False
    assert [(step.name, step.rows) for step in report.steps] == [
        ("quotes_snapshot", 2),
        ("task_runs", 2),
        ("scheduler_heartbeats", 3),
    ]
    assert report.total == 7
    assert db.scalar("SELECT count(*) FROM quotes_snapshot WHERE market = 'A'") == 3
    assert db.scalar("SELECT count(*) FROM quotes_snapshot WHERE market = 'HK'") == 5
    left = db.query("SELECT id FROM task_runs WHERE job_name = 'bars'").to_dicts()
    assert [row["id"] for row in left] == ["new"]
    assert db.row_count("scheduler_heartbeats") == 2


def test_dry_run_counts_are_the_same_numbers_and_change_nothing(db):
    seed_quotes(db)
    seed_task_runs(db)
    seed_heartbeats(db)
    service = make(db)
    dry = service.run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2, dry_run=True)
    assert [step.rows for step in dry.steps] == [2, 2, 3]
    assert db.row_count("quotes_snapshot") == 10, "dry-run 一行都不删"
    assert db.row_count("task_runs") == 3
    assert db.row_count("scheduler_heartbeats") == 5
    real = service.run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    assert [step.rows for step in real.steps] == [step.rows for step in dry.steps]


def test_each_step_leaves_one_prune_row(db):
    seed_quotes(db)
    seed_task_runs(db)
    seed_heartbeats(db)
    make(db).run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    rows = db.query(
        "SELECT note, rows_affected, status FROM task_runs"
        " WHERE job_name = 'prune' ORDER BY rowid"
    ).to_dicts()
    assert [row["note"] for row in rows] == [
        "quotes_snapshot",
        "task_runs",
        "scheduler_heartbeats",
    ]
    assert [row["rows_affected"] for row in rows] == [2, 2, 3]
    assert all(row["status"] == "ok" for row in rows)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quotes_sessions": 0},
        {"task_runs_days": 0},
        {"heartbeats_keep": 0},
    ],
)
def test_zero_retention_is_refused(db, kwargs):
    with pytest.raises(ValidationError):
        make(db).run(**kwargs)
    assert db.row_count("task_runs") == 0


def test_a_failing_step_rolls_back_the_earlier_deletes(db, monkeypatch):
    """三个删除是一个写单元：心跳那一步失败时，报价与台账已经删掉的行必须回来。

    否则「库磁盘满了」的后果不是一次失败的清理，而是「快照少了两天、台账多了三行
    写着已清理」——下一次清理看到的是一份没删干净的账。
    """
    seed_quotes(db)
    seed_task_runs(db)
    seed_heartbeats(db)
    service = make(db)

    def boom(_keep):
        raise RuntimeError("磁盘满")

    monkeypatch.setattr(service.heartbeats, "delete_beyond", boom)
    with pytest.raises(RuntimeError, match="磁盘满"):
        service.run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    assert db.row_count("quotes_snapshot") == 10
    assert db.row_count("task_runs") == 3
    assert db.row_count("scheduler_heartbeats") == 5
```

- [ ] **Step 6: 跑测试确认失败**

```bash
uv run pytest tests/services/test_prune.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'sams.services.prune'`。

- [ ] **Step 7: 实现清理服务并接进组装根**

新建 `src/sams/services/prune.py`：

```python
"""保留策略：把只增不删的三张表按各自的粒度裁掉（D116）。

为什么是一个服务而不是三段 SQL：CLI、常驻进程的每日维护、admin 健康页的按钮
是三个入口，但「保留几天、按什么排序、算多少行」只能有一份答案。dry-run 与实删
必须走同一个计数路径，否则「预演说 10 行、实删 12 行」只是时间差，没人查得出来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.repos.quotes import QuoteRepo
from sams.storage.repos.task_runs import OK, HeartbeatRepo, TaskRunRepo

logger = logging.getLogger("sams.services.prune")

#: `sams db prune` 的三个默认值（spec D116）。它们也是服务与 Web 按钮的默认值：
#: 三处各写一个 5/90/1000，改一处就会得到「命令行说 5 天、健康页按钮删 7 天」
KEEP_QUOTE_SESSIONS = 5
KEEP_TASK_RUN_DAYS = 90
KEEP_HEARTBEATS = 1000

#: 每一步在 `task_runs` 里留下的那一行带这个 job 名
JOB_NAME = "prune"


@dataclass(frozen=True)
class PruneStep:
    name: str
    rows: int


@dataclass(frozen=True)
class PruneReport:
    dry_run: bool
    steps: tuple[PruneStep, ...] = ()

    @property
    def total(self) -> int:
        return sum(step.rows for step in self.steps)


class PruneService:
    def __init__(
        self,
        db: Database,
        *,
        quotes: QuoteRepo,
        task_runs: TaskRunRepo,
        heartbeats: HeartbeatRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.db = db
        self.quotes = quotes
        self.task_runs = task_runs
        self.heartbeats = heartbeats
        self.clock = clock

    def run(
        self,
        *,
        quotes_sessions: int = KEEP_QUOTE_SESSIONS,
        task_runs_days: int = KEEP_TASK_RUN_DAYS,
        heartbeats_keep: int = KEEP_HEARTBEATS,
        dry_run: bool = False,
    ) -> PruneReport:
        for name, value in (
            ("quotes_sessions", quotes_sessions),
            ("task_runs_days", task_runs_days),
            ("heartbeats_keep", heartbeats_keep),
        ):
            if value < 1:
                raise ValidationError(f"{name}={value}：至少要 1；0 会把当前这一份全删掉")

        if dry_run:
            steps, _, _ = self._measure(quotes_sessions, task_runs_days, heartbeats_keep)
            return PruneReport(dry_run=True, steps=steps)

        # 计数与删除在同一个事务里：盘中线程随时会写新快照，先数后删的话，
        # 中间落地的一天会把第 N 新的边界推后，实际删掉的行数与报告对不上
        with self.db.transaction():
            steps, day_cutoffs, run_cutoff = self._measure(
                quotes_sessions, task_runs_days, heartbeats_keep
            )
            self.quotes.delete_before(day_cutoffs)
            self.task_runs.delete_before(run_cutoff)
            self.heartbeats.delete_beyond(heartbeats_keep)
            # 台账写在删除之后：清掉九十天前的行，再记今天这三步做了什么
            for step in steps:
                run_id = self.task_runs.start(JOB_NAME)
                self.task_runs.finish(
                    run_id, status=OK, rows_affected=step.rows, note=step.name
                )
                logger.info("prune %s: 删除 %d 行", step.name, step.rows)
        return PruneReport(dry_run=False, steps=steps)

    def _measure(
        self, quotes_sessions: int, task_runs_days: int, heartbeats_keep: int
    ) -> tuple[tuple[PruneStep, ...], dict[str, date], datetime]:
        day_cutoffs = self.quotes.session_cutoffs(quotes_sessions)
        run_cutoff = self.clock.now_utc() - timedelta(days=task_runs_days)
        steps = (
            PruneStep("quotes_snapshot", self.quotes.count_before(day_cutoffs)),
            PruneStep("task_runs", self.task_runs.count_before(run_cutoff)),
            PruneStep(
                "scheduler_heartbeats", self.heartbeats.count_beyond(heartbeats_keep)
            ),
        )
        return steps, day_cutoffs, run_cutoff
```

修改 `src/sams/bootstrap.py`：

1. import 段加 `from sams.services.prune import PruneService`（与其它 `sams.services.*` 放在一起）。
2. `ServiceContext` 在 `heartbeats: HeartbeatRepo` 之后加：

```python
    #: 保留策略（D116）：CLI、常驻维护与 admin 健康页共用这一份
    prune: PruneService
```

3. `build_context` 里 `heartbeats = HeartbeatRepo(database, clock=clock)` 之后加：

```python
    prune = PruneService(
        database,
        quotes=quotes_store,
        task_runs=task_runs,
        heartbeats=heartbeats,
        clock=clock,
    )
```

4. `build_context` 返回的 `ServiceContext(...)` 里 `heartbeats=heartbeats,` 之后加 `prune=prune,`。

在 `tests/test_bootstrap.py` 末尾追加：

```python
def test_the_container_carries_the_prune_service(container):
    """三个入口共用一份保留策略：命令行删 5 天、健康页按钮删 7 天，是两种故障。"""
    from sams.services.prune import PruneService

    built, _ = container
    assert isinstance(built.prune, PruneService)
```

- [ ] **Step 8: 跑测试确认通过**

```bash
uv run pytest tests/services/test_prune.py tests/test_bootstrap.py -q
uv run ruff check src/sams/services/prune.py src/sams/bootstrap.py tests/services/test_prune.py tests/test_bootstrap.py --no-cache
```

Expected: PASS。若 `_measure` 的注解报 `NameError: datetime`，说明只 import 了 `timedelta`——按上面那句补齐。

- [ ] **Step 9: 写 CLI 的失败测试**

新建 `tests/cli/test_prune_cli.py`：

```python
"""`sams db prune`：三个开关、dry-run 只报不删、实删与报告一致。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

import sams.cli as cli
from sams.storage.db import Database

CONFIG = """
[storage]
db_path = "{db_path}"

[general]
secret_key = "test-secret"

[markets.A]
tz = "Asia/Shanghai"
currency = "CNY"
post_close_min = 30
sources = ["akshare"]

[markets.HK]
tz = "Asia/Hong_Kong"
currency = "HKD"
sources = ["yfinance"]
"""


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    path.write_text(CONFIG.format(db_path=tmp_path / "sams.duckdb"), encoding="utf-8")
    monkeypatch.setenv("SAMS_CONFIG", str(path))
    return path


def seed(db_path):
    """10 行快照（HK 7 天 + A 3 天）、3 行台账、5 条心跳。"""
    database = Database.open(db_path)
    try:
        today = date.today()
        for offset in range(7):
            day = today - timedelta(days=offset)
            for market, code in (("HK", "00700"), ("A", "600519")):
                if market == "A" and offset >= 3:
                    continue
                database.execute(
                    "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
                    " VALUES (?, ?, ?, 10.0, 'test')",
                    [market, code, datetime(day.year, day.month, day.day, 2, tzinfo=UTC)],
                )
        now = datetime.now(UTC)
        for run_id, days in (("old-1", 200), ("old-2", 100), ("new", 1)):
            database.execute(
                "INSERT INTO task_runs (id, job_name, started_at, status)"
                " VALUES (?, 'bars', ?, 'ok')",
                [run_id, now - timedelta(days=days)],
            )
        for index in range(5):
            beat = now - timedelta(hours=5 - index)
            database.execute(
                "INSERT INTO scheduler_heartbeats (id, started_at, beat_at)"
                " VALUES (?, ?, ?)",
                [f"beat-{index}", beat, beat],
            )
    finally:
        database.close()


def open_db(config_path):
    return Database.open(config_path.parent / "sams.duckdb")


def test_prune_dry_run_prints_counts_and_leaves_the_database_untouched(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    capsys.readouterr()
    seed(config_path.parent / "sams.duckdb")

    assert (
        cli.run(
            [
                "db",
                "prune",
                "--dry-run",
                "--quotes-sessions",
                "5",
                "--task-runs-days",
                "90",
                "--heartbeats-keep",
                "2",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "quotes_snapshot: 将删除 2 行" in out
    assert "task_runs: 将删除 2 行" in out
    assert "scheduler_heartbeats: 将删除 3 行" in out
    assert "预计: 7 行" in out

    database = open_db(config_path)
    try:
        assert database.row_count("quotes_snapshot") == 10
        assert database.row_count("scheduler_heartbeats") == 5
        assert database.scalar("SELECT count(*) FROM task_runs WHERE job_name = 'prune'") == 0
    finally:
        database.close()


def test_prune_deletes_and_writes_one_row_per_step(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    capsys.readouterr()
    seed(config_path.parent / "sams.duckdb")

    assert (
        cli.run(
            [
                "db",
                "prune",
                "--task-runs-days",
                "90",
                "--heartbeats-keep",
                "2",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "quotes_snapshot: 删除 2 行" in out
    assert "合计: 7 行" in out

    database = open_db(config_path)
    try:
        assert database.row_count("quotes_snapshot") == 8
        assert database.row_count("scheduler_heartbeats") == 2
        rows = database.query(
            "SELECT note, rows_affected FROM task_runs"
            " WHERE job_name = 'prune' ORDER BY rowid"
        ).to_dicts()
        assert [row["note"] for row in rows] == [
            "quotes_snapshot",
            "task_runs",
            "scheduler_heartbeats",
        ]
        assert [row["rows_affected"] for row in rows] == [2, 2, 3]
    finally:
        database.close()


@pytest.mark.parametrize(
    "flag",
    ["--quotes-sessions", "--task-runs-days", "--heartbeats-keep"],
)
def test_zero_retention_is_a_usage_error(config_path, flag):
    """敲错开关是用法错误（退出 2），不是「这次没删东西」（退出 0）。"""
    with pytest.raises(SystemExit) as exc:
        cli.run(["db", "prune", flag, "0"])
    assert exc.value.code == 2
```

- [ ] **Step 10: 跑测试确认失败**

```bash
uv run pytest tests/cli/test_prune_cli.py -q
```

Expected: FAIL，`SystemExit: 2`（`db` 分组还没有 `prune` 子命令）。

- [ ] **Step 11: 实现 CLI**

把 `src/sams/cli/db.py` 改成：

```python
"""`sams db` —— 建库、迁移状态与保留策略。"""

from __future__ import annotations

import argparse

from sams.cli.common import group
from sams.services.prune import KEEP_HEARTBEATS, KEEP_QUOTE_SESSIONS, KEEP_TASK_RUN_DAYS


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} 不是整数") from None
    if value < 1:
        raise argparse.ArgumentTypeError("要大于 0")
    return value


def add_to(subparsers) -> None:
    actions = group(subparsers, "db", "数据库维护")
    for name, handler, help_text in (
        ("migrate", migrate, "应用尚未执行的迁移"),
        ("status", status, "显示已应用 / 待应用的迁移"),
    ):
        parser = actions.add_parser(name, help=help_text)
        parser.set_defaults(handler=handler)

    parser = actions.add_parser(
        "prune",
        help="按保留策略清理报价快照、任务台账与调度心跳（sams serve 每日 UTC 03:30 也执行）",
    )
    parser.add_argument(
        "--quotes-sessions",
        type=_positive,
        default=KEEP_QUOTE_SESSIONS,
        metavar="N",
        help=f"每个市场保留最近 N 个有快照的交易日（默认 {KEEP_QUOTE_SESSIONS}）",
    )
    parser.add_argument(
        "--task-runs-days",
        type=_positive,
        default=KEEP_TASK_RUN_DAYS,
        metavar="N",
        help=f"task_runs 保留最近 N 天（默认 {KEEP_TASK_RUN_DAYS}）",
    )
    parser.add_argument(
        "--heartbeats-keep",
        type=_positive,
        default=KEEP_HEARTBEATS,
        metavar="N",
        help=f"调度心跳只留最近 N 条（默认 {KEEP_HEARTBEATS}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将删行数，不动库")
    parser.set_defaults(handler=prune)


def migrate(container, args) -> int:
    newly = container.migrate()
    if newly:
        print(f"已应用迁移: {', '.join(newly)}")
    else:
        print("数据库已是最新，无待应用迁移")
    print(f"库: {container.db.path}  表: {len(container.db.table_names())} 张")
    return 0


def status(container, args) -> int:
    done = sorted(container.applied_migrations())
    todo = container.pending_migrations()
    print(f"已应用 {len(done)} 个: {', '.join(done) or '（无）'}")
    print(f"待应用 {len(todo)} 个: {', '.join(todo) or '（无）'}")
    print(f"数据库路径: {container.db.path}")
    return 0


def prune(container, args) -> int:
    report = container.prune.run(
        quotes_sessions=args.quotes_sessions,
        task_runs_days=args.task_runs_days,
        heartbeats_keep=args.heartbeats_keep,
        dry_run=args.dry_run,
    )
    verb = "将删除" if report.dry_run else "删除"
    for step in report.steps:
        print(f"{step.name}: {verb} {step.rows} 行")
    print(f"{'预计' if report.dry_run else '合计'}: {report.total} 行")
    return 0
```

- [ ] **Step 12: 跑测试确认通过**

```bash
uv run pytest tests/cli/test_prune_cli.py tests/services/test_prune.py tests/storage/test_prune_repos.py tests/test_bootstrap.py -q
uv run ruff check src/sams/cli/db.py src/sams/services/prune.py tests/cli/test_prune_cli.py --no-cache
```

Expected: PASS（CLI 3 + 服务 5 + 仓库 7 + bootstrap 全绿）。

- [ ] **Step 13: 提交**

```bash
git add src/sams/storage/repos/quotes.py src/sams/storage/repos/task_runs.py \
  src/sams/services/prune.py src/sams/bootstrap.py src/sams/cli/db.py \
  tests/storage/test_prune_repos.py tests/services/test_prune.py \
  tests/cli/test_prune_cli.py tests/test_bootstrap.py
git commit -m "feat(prune): 三个保留窗口一份判据，dry-run 与实删同一份计数"
```

---

### Task 14: 线程生命周期 `Runtime`、最小 `/healthz` 与依赖/分层闸

**Files:**
- Modify: `pyproject.toml`、`uv.lock`（`uv add`）
- Modify: `tests/test_layering.py`
- Create: `src/sams/services/runtime.py`
- Create: `src/sams/web/__init__.py`
- Create: `src/sams/web/app.py`
- Test: `tests/services/test_runtime.py`
- Test: `tests/web/test_healthz.py`

- [ ] **Step 1: 加依赖**

```bash
uv add fastapi uvicorn jinja2 python-multipart
uv add --dev httpx
uv run python -c "import fastapi, uvicorn, jinja2, multipart, httpx; print('ok')"
```

Expected: `ok`。`pyproject.toml` 的 `dependencies` 出现 fastapi/uvicorn/jinja2/python-multipart，`dev` 组出现 httpx；`uv.lock` 一并更新。四个运行期依赖里 jinja2 与 python-multipart 在计划 3 才用到，但一次装齐，免得执行到页面任务时再改锁文件。

- [ ] **Step 2: 先把分层规则写进去**

改 `tests/test_layering.py`：

1. 文件头注释的图改成：

```python
  core  ←  storage / datasource  ←  services  ←  web / cli
  bootstrap（组装根）是唯一可以同时认识所有实现的地方。
```

2. `ALLOWED` 里 `trading` 之后、`cli` 之前加：

```python
    # web 是 HTTP 适配器：业务判断住在 services，模板只拿视图数据。
    # 它不认识 datasource：某个源能不能报价由 services 转述（D112），
    # 否则模板会开始自己查能力表，页面的行为就不在服务层的测试里了
    "web": {"core", "storage", "services"},
    "cli": {"core", "services", "web"},
```

（把原来的 `"cli": {"core", "services"},` 一行替换掉。）

3. 在 `test_monitor_may_only_reach_core_and_storage` 之后加：

```python
def test_web_and_cli_do_not_reach_data_implementations():
    """web 不认识 datasource，cli 不认识 storage/datasource（bootstrap 除外）。

    放宽这两项不会当场报错，只会让「换存储只动一层」在第一次有人图方便时失效。
    """
    assert ALLOWED["web"] == {"core", "storage", "services"}
    assert ALLOWED["cli"] == {"core", "services", "web"}
```

此时跑 `uv run pytest tests/test_layering.py -q` 应当仍全绿：`src/sams/web` 还不存在，`test_only_known_layer_directories_exist` 看不出差别。

- [ ] **Step 3: 写 `Runtime` 的失败测试**

新建 `tests/services/test_runtime.py`：

```python
"""运行时：线程起停、`/healthz` 的读数、每日 UTC 03:30 的清理。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.core.quotes import Quote
from sams.services.prune import PruneReport, PruneStep
from sams.services.quotes import MarketBoard
from sams.services.runtime import Runtime
from sams.services.scheduler.report import ScheduleData

NOW = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)


class FakeScheduler:
    def __init__(self, *, beat_age_s=5) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.beat_age_s = beat_age_s

    def run_forever(self, *, stop, **_kwargs):
        self.started.set()
        stop.wait(5)
        self.stopped.set()
        return 0

    def schedule_data(self):
        return ScheduleData(
            heartbeat=None, beat_age_s=self.beat_age_s, admins=0, jobs=()
        )


class FakeMonitor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run_forever(self, *, stop, **_kwargs):
        self.started.set()
        stop.wait(5)
        self.stopped.set()
        return 0


class FakePrune:
    def __init__(self) -> None:
        self.called = threading.Event()
        self.calls = 0

    def run(self, *, dry_run=False):
        self.calls += 1
        self.called.set()
        return PruneReport(dry_run=dry_run, steps=(PruneStep("quotes_snapshot", 3),))


class FakeBoard:
    def __init__(self, snapshot=None) -> None:
        self._snapshot = snapshot or {}

    def snapshot(self):
        return self._snapshot


class BrokenDb:
    def scalar(self, sql):
        raise RuntimeError("库坏了")


class BrokenScheduler(FakeScheduler):
    def schedule_data(self):
        raise RuntimeError("台账读不到")


def make_runtime(
    settings,
    *,
    scheduler=None,
    schedule_reading=None,
    monitor=None,
    prune=None,
    board=None,
    database=None,
    clock=None,
):
    engine = scheduler or FakeScheduler()
    return Runtime(
        settings,
        scheduler=engine,
        schedule_reading=schedule_reading or engine.schedule_data,
        monitor=monitor or FakeMonitor(),
        prune=prune or FakePrune(),
        board=board if board is not None else FakeBoard(),
        database=database,
        clock=clock or FrozenClock(NOW),
    )


def test_healthz_lists_every_configured_market_before_the_first_poll(settings):
    payload = make_runtime(settings).healthz()
    assert payload["status"] == "ok"
    assert set(payload["intraday"]) == {"A", "HK", "US"}
    assert all(item["last_poll_at"] is None for item in payload["intraday"].values())
    assert payload["uptime_s"] == 0
    assert payload["version"]


def test_healthz_reports_the_newest_quote_age_and_source(settings):
    quote = Quote(
        market="A",
        code="600519",
        ts=NOW - timedelta(seconds=12),
        price=1500.0,
        source="tencent",
    )
    board = FakeBoard(
        {"A": MarketBoard(market="A", quotes={"600519": quote}, last_poll_at=NOW)}
    )
    payload = make_runtime(settings, board=board).healthz()
    assert payload["intraday"]["A"] == {
        "last_poll_at": NOW.isoformat(),
        "age_s": 12,
        "source": "tencent",
        "ok": True,
    }
    assert payload["intraday"]["HK"]["source"] is None


def test_a_failed_poll_degrades_the_status(settings):
    board = FakeBoard({"A": MarketBoard(market="A", last_error="timeout")})
    payload = make_runtime(settings, board=board).healthz()
    assert payload["status"] == "degraded"
    assert payload["intraday"]["A"]["ok"] is False


def test_a_stale_scheduler_or_unreadable_schedule_degrades_the_status(settings):
    stale = make_runtime(settings, scheduler=FakeScheduler(beat_age_s=10_000)).healthz()
    assert stale["status"] == "degraded" and stale["scheduler"]["stale"] is True

    broken = make_runtime(settings, scheduler=BrokenScheduler()).healthz()
    assert broken["status"] == "degraded"
    assert broken["scheduler"]["stale"] is True
    assert "台账读不到" in broken["scheduler"]["error"]


def test_healthz_reads_the_schedule_through_the_injected_probe(settings):
    """`/healthz` 在 HTTP 线程上跑（D113），调度器的库连接属于调度线程。

    组装根注入一条「每次新开连接」的读法；这条测试把「不许直呼 schedule_data」
    钉住——`BrokenScheduler.schedule_data` 一旦被 healthz 碰到就会抛。
    """
    runtime = make_runtime(
        settings,
        scheduler=BrokenScheduler(),
        schedule_reading=lambda: ScheduleData(
            heartbeat=None, beat_age_s=5, admins=0, jobs=()
        ),
    )
    payload = runtime.healthz()
    assert payload["status"] == "ok"
    assert payload["scheduler"] == {"beat_age_s": 5, "stale": False}


def test_an_unreadable_database_is_reported_not_raised(settings):
    payload = make_runtime(settings, database=BrokenDb()).healthz()
    assert payload["status"] == "degraded"
    assert payload["db"] == {"ok": False, "error": "库坏了"}


def test_maintenance_is_at_0330_utc_and_is_due_tomorrow_after_it(settings):
    runtime = make_runtime(settings)
    assert runtime.next_maintenance_at(NOW) == datetime(2026, 10, 23, 3, 30, tzinfo=UTC)
    after = datetime(2026, 10, 23, 4, 0, tzinfo=UTC)
    assert runtime.next_maintenance_at(after) == datetime(2026, 10, 24, 3, 30, tzinfo=UTC)


def test_run_maintenance_passes_dry_run_and_reports_the_total(settings):
    prune = FakePrune()
    runtime = make_runtime(settings, prune=prune)
    report = runtime.run_maintenance(dry_run=True)
    assert prune.calls == 1 and report.dry_run is True and report.total == 3


def test_start_runs_three_threads_and_stop_joins_them(settings):
    scheduler = FakeScheduler()
    monitor = FakeMonitor()
    prune = FakePrune()
    runtime = make_runtime(settings, scheduler=scheduler, monitor=monitor, prune=prune)

    runtime.start()
    assert scheduler.started.wait(2) and monitor.started.wait(2)
    assert prune.called.wait(2), "维护线程启动即清一次"
    assert set(runtime.threads) == {"scheduler", "intraday", "maintenance"}

    runtime.stop()
    assert scheduler.stopped.wait(2) and monitor.stopped.wait(2)
    assert not any(thread.is_alive() for thread in runtime.threads.values())
    runtime.stop()  # 幂等：停机流程被重复调用不该抛


def test_start_twice_is_refused(settings):
    runtime = make_runtime(settings)
    runtime.start()
    try:
        with pytest.raises(RuntimeError, match="只能启动一次"):
            runtime.start()
    finally:
        runtime.stop()
```

- [ ] **Step 4: 跑测试确认失败**

```bash
uv run pytest tests/services/test_runtime.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'sams.services.runtime'`。

- [ ] **Step 5: 实现 `Runtime`**

新建 `src/sams/services/runtime.py`：

```python
"""常驻进程的线程生命周期与 `/healthz` 的读数（spec §2.1/§2.3）。

`Runtime` 不认识组装根：它拿到的是已经装好的调度器、盘中节拍器、清理服务与行情板。
「谁负责接线」因此只有 `bootstrap` 一处，而这一层可以在测试里用四个假对象跑起来。

线程都是守护线程：主线程退出时它们不该把进程钉住。`stop()` 仍然逐个 join——
守护只保证「不阻止退出」，不保证「退出前把这一轮收干净」。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol

from sams import __version__
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import SamsError
from sams.services.intraday import IntradayMonitor
from sams.services.prune import PruneReport, PruneService
from sams.services.quotes import QuoteBoard
from sams.services.scheduler.report import ScheduleData
from sams.services.scheduler.service import Scheduler

logger = logging.getLogger("sams.services.runtime")

#: 每日维护的钟点（UTC）。不跟「最后一个市场收盘后 N 分钟」走：那个判据会随
#: 夏令时在一年里漂两次，而清理不依赖任何一个市场当天有没有开市（D116）
MAINTENANCE_AT = time(3, 30)

#: 盘中与维护线程的排空上限。调度线程另有 `scheduler_tick_s + 30`（§2.1）
WORKER_STOP_TIMEOUT_S = 10.0


class _DbProbe(Protocol):
    def scalar(self, sql: str) -> Any: ...


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        scheduler: Scheduler,
        schedule_reading: Callable[[], ScheduleData],
        monitor: IntradayMonitor,
        prune: PruneService,
        board: QuoteBoard | None = None,
        database: _DbProbe | None = None,
        clock: Clock = SYSTEM_CLOCK,
        version: str = __version__,
    ) -> None:
        self.settings = settings
        self.scheduler = scheduler
        #: `/healthz` 跑在 HTTP 线程上，不能直呼 `scheduler.schedule_data()`：
        #: 那会用调度线程那条 DuckDB 连接（D113 的同一道线，换了个入口）。
        #: 所以读法由组装根注入——生产传一条每次新开连接的探针，测试传个 lambda。
        self.schedule_reading = schedule_reading
        self.monitor = monitor
        self.prune = prune
        self.board = board
        #: `/healthz` 用一条只回答「读得到吗」的探针；不传就不报这一项
        self.database = database
        self.clock = clock
        self.version = version
        self.stop_event = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._started_at: datetime | None = None

    @property
    def threads(self) -> dict[str, threading.Thread]:
        return dict(self._threads)

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("Runtime 只能启动一次：第二次会把同一批线程跑成两份")
        self._started_at = self.clock.now_utc()
        self._threads = {
            "scheduler": threading.Thread(
                target=self.scheduler.run_forever,
                kwargs={"stop": self.stop_event},
                name="sams-scheduler",
                daemon=True,
            ),
            "intraday": threading.Thread(
                target=self.monitor.run_forever,
                kwargs={"stop": self.stop_event},
                name="sams-intraday",
                daemon=True,
            ),
            "maintenance": threading.Thread(
                target=self._maintenance_loop,
                name="sams-maintenance",
                daemon=True,
            ),
        }
        for thread in self._threads.values():
            thread.start()
        logger.info("运行时已启动：%s", "、".join(self._threads))

    def stop(self) -> None:
        self.stop_event.set()
        timeouts = {
            # 调度线程可能正卡在一个 job 里：等它收尾，但不等一个坏掉的实现
            "scheduler": float(self.settings.scheduler_tick_s) + 30.0,
            "intraday": WORKER_STOP_TIMEOUT_S,
            "maintenance": WORKER_STOP_TIMEOUT_S,
        }
        for name in ("scheduler", "intraday", "maintenance"):
            thread = self._threads.get(name)
            if thread is None:
                continue
            thread.join(timeout=timeouts[name])
            if thread.is_alive():
                logger.warning(
                    "%s 线程 %ss 内没有退出，继续停其余部分", name, timeouts[name]
                )

    def next_maintenance_at(self, now: datetime) -> datetime:
        today = datetime.combine(now.date(), MAINTENANCE_AT, tzinfo=UTC)
        return today if today > now else today + timedelta(days=1)

    def run_maintenance(self, *, dry_run: bool = False) -> PruneReport:
        report = self.prune.run(dry_run=dry_run)
        logger.info(
            "每日清理%s：%s，合计 %d 行",
            "（预演）" if dry_run else "",
            "、".join(f"{step.name}={step.rows}" for step in report.steps),
            report.total,
        )
        return report

    def healthz(self) -> dict[str, Any]:
        now = self.clock.now_utc()
        db_ok, db_error = self._db_probe()
        scheduler, schedule_stale = self._scheduler_reading()
        intraday = self._intraday_reading(now)
        degraded = (
            not db_ok
            or schedule_stale
            or any(not market["ok"] for market in intraday.values())
        )
        return {
            "status": "degraded" if degraded else "ok",
            "db": {"ok": db_ok, "error": db_error},
            "scheduler": scheduler,
            "intraday": intraday,
            "version": self.version,
            "uptime_s": (
                0
                if self._started_at is None
                else max(0, int((now - self._started_at).total_seconds()))
            ),
        }

    def _db_probe(self) -> tuple[bool, str | None]:
        if self.database is None:
            return True, None
        try:
            self.database.scalar("SELECT 1")
        except Exception as exc:  # 健康检查不能因为一次读失败而自己抛出去
            return False, str(exc)
        return True, None

    def _scheduler_reading(self) -> tuple[dict[str, Any], bool]:
        """调度器的读数；读不出来就是「可能死了」，不是让整份 healthz 变成 500。"""
        try:
            schedule = self.schedule_reading()
        except Exception as exc:
            return {"beat_age_s": None, "stale": True, "error": str(exc)}, True
        return {"beat_age_s": schedule.beat_age_s, "stale": schedule.stale}, schedule.stale

    def _intraday_reading(self, now: datetime) -> dict[str, dict[str, Any]]:
        snapshots = self.board.snapshot() if self.board is not None else {}
        reading: dict[str, dict[str, Any]] = {}
        for market in sorted(self.settings.markets):
            board = snapshots.get(market)
            quotes = [] if board is None else list(board.quotes.values())
            newest = max(quotes, key=lambda quote: quote.ts) if quotes else None
            reading[market] = {
                "last_poll_at": _iso(None if board is None else board.last_poll_at),
                # 未来时刻不报负年龄：那是两边时钟不一致，不是「刚更新过」
                "age_s": (
                    None
                    if newest is None
                    else max(0, int((now - newest.ts).total_seconds()))
                ),
                "source": None if newest is None else newest.source,
                # 从未轮询（休市/刚启动）不是故障；轮过且有错才是
                "ok": board is None or board.last_error is None,
            }
        return reading

    def _maintenance_loop(self) -> None:
        self._run_maintenance_guarded()
        while not self.stop_event.is_set():
            now = self.clock.now_utc()
            delay = (self.next_maintenance_at(now) - now).total_seconds()
            if self.stop_event.wait(delay):
                break
            self._run_maintenance_guarded()

    def _run_maintenance_guarded(self) -> None:
        try:
            self.run_maintenance()
        except SamsError:
            # 一次清理失败（盘中被写锁挡住、库暂时只读）不该让明天的清理也没人做
            logger.exception("每日清理失败，下一拍再试")
```

- [ ] **Step 6: 跑测试确认通过**

```bash
uv run pytest tests/services/test_runtime.py tests/test_layering.py -q
uv run ruff check src/sams/services/runtime.py tests/services/test_runtime.py --no-cache
```

Expected: PASS。

- [ ] **Step 7: 写 `/healthz` 的失败测试**

新建 `tests/web/test_healthz.py`：

```python
"""最小 HTTP 入口：`/healthz` 永远 200，降级写在 JSON 的 status 里。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from sams.web.app import create_app


class DegradedRuntime:
    def healthz(self):
        return {
            "status": "degraded",
            "db": {"ok": False, "error": "库坏了"},
            "scheduler": {"beat_age_s": None, "stale": True},
            "intraday": {"A": {"last_poll_at": None, "age_s": None, "source": None, "ok": False}},
            "version": "0.1.0",
            "uptime_s": 3,
        }


def test_healthz_is_200_even_when_degraded():
    """看门狗只认 HTTP 与 status：500 会让它把「进程活着但库坏了」当成进程死了。"""
    response = TestClient(create_app(DegradedRuntime())).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["db"]["error"] == "库坏了"


def test_openapi_and_docs_are_off():
    client = TestClient(create_app(DegradedRuntime()))
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
```

- [ ] **Step 8: 跑测试确认失败**

```bash
uv run pytest tests/web/test_healthz.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'sams.web'`。

- [ ] **Step 9: 实现最小 Web 入口**

新建 `src/sams/web/__init__.py`：

```python
"""Web 层：HTTP 适配器。业务判断全在 services，这里只做请求/响应与模板。"""
```

新建 `src/sams/web/app.py`：

```python
"""M4-A 的 HTTP 入口。

这一版只有 `/healthz`：它回答「这个进程还活着吗、三段线程各自在做什么」。
页面、登录与超媒体在计划 3 接入。`create_app` 只收一个 Runtime、不 import bootstrap：
应用不该知道依赖是怎么装的，否则每加一个页面测试都要先造一个完整容器。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from sams.services.runtime import Runtime


def create_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(title="SAMS", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(runtime.healthz(), status_code=200)

    return app
```

- [ ] **Step 10: 跑测试确认通过**

```bash
uv run pytest tests/web/test_healthz.py tests/services/test_runtime.py tests/test_layering.py -q
uv run ruff check src/sams/web tests/web tests/services/test_runtime.py tests/test_layering.py --no-cache
```

Expected: PASS。

- [ ] **Step 11: 提交**

```bash
git add pyproject.toml uv.lock tests/test_layering.py src/sams/services/runtime.py \
  src/sams/web/__init__.py src/sams/web/app.py tests/services/test_runtime.py \
  tests/web/test_healthz.py
git commit -m "feat(runtime): 三线程起停与 /healthz 读数，Web 层第一次落地"
```

---

### Task 15: `sams serve`、`[报价源]` 与锁住时的 `/healthz` 入口

**Files:**
- Modify: `src/sams/core/sources.py`
- Modify: `src/sams/services/health.py`
- Modify: `src/sams/cli/common.py`
- Modify: `src/sams/cli/health.py`
- Modify: `src/sams/cli/__init__.py`
- Modify: `src/sams/bootstrap.py`
- Create: `src/sams/cli/serve.py`
- Test: `tests/services/test_health.py`、`tests/cli/test_health_cli.py`、`tests/cli/test_health_locked.py`、`tests/test_bootstrap.py`
- Test: `tests/cli/test_serve.py`

这一条把前十四条接成一个能常驻的产品：`build_serve` 造**三条各持一条连接**的上下文（D113），`Runtime` 起三个线程，`uvicorn` 在同一个进程里服务 `/healthz`；`sams health` 多一节 `[报价源]`，并且在库被占用时（D111）把屏幕上唯一还能用的入口——`/healthz`——指出来。

`serve` 与其余命令的签名不同：它拿 `Settings` 而不是 `Container`，因为三条连接不能从「一个进程一条连接」的 CLI 容器里长出来。分派因此发生在 `run()` 装配容器**之前**，而不是在 handler 里。

`build_serve` 不挂 `SystemAlerter`：它在总线上订阅，而三个上下文各有一条连接；订阅者的发送路径属于哪条连接、在哪个线程被 publish，是计划 2 接通知队列时才定得下来的事。CLI 仍由 `build_container` 挂一次。

- [ ] **Step 1: 写 `[报价源]` 的失败测试**

`tests/services/test_health.py` 的 import 段追加：

```python
from sams.core.quotes import QuoteSourceRecord
from sams.storage.repos.quotes import QuoteHealthRepo
```

把 `service` 辅助函数整体替换成（只多一个 `quote_health`）：

```python
def service(
    db, settings, *adapters, local_status=None, schedule=None, quote_health=None
) -> HealthService:
    registry = AdapterRegistry(
        adapters={a.name: a for a in adapters},
        market_sources={"A": tuple(a.name for a in adapters)},
        sleep=lambda seconds: None,
    )
    return HealthService(
        db,
        registry,
        CalendarRepo(db),
        settings,
        local_status=local_status,
        schedule=schedule,
        quote_health=quote_health,
    )
```

在文件末尾追加：

```python
class QuoteOnly:
    """只报价的适配器：能力按 `quotes` 这个方法判定，不按类名。

    `markets` 是注册表的必填项（它反查「这个源被路由到哪些市场」），
    不是报价协议的一部分；没有它，构造注册表时就会炸在能力判定之前。
    """

    name = "tencent"
    markets = ("A",)

    def quotes(self, market, codes):
        raise AssertionError("健康页不该真的去取价")


def test_quote_source_health_is_a_separate_section(db, settings):
    """报价与日线是两条链、两张表：混在一行里，一句「腾讯挂了」说不清挂的是哪条。

    日线那一节由 `sources` 回答，报价这一节由 `quote_sources` 回答——两个列表
    不共享元素，屏幕上那两节也就不会替对方说话。
    """
    repo = QuoteHealthRepo(db)
    repo.save(
        QuoteSourceRecord(
            name="tencent",
            markets=("A",),
            ok=False,
            last_error="HTTP 502",
            consecutive_failures=3,
        )
    )
    report = service(
        db,
        settings,
        StubAdapter("akshare", ["A"], sample_bars()),
        quote_health=repo,
    ).report()

    assert [s.name for s in report.quote_sources] == ["tencent"]
    assert [s.name for s in report.degraded_quote_sources] == ["tencent"]
    assert [s.name for s in report.sources] == ["akshare"], "日线那一节不跟着多一行"
    assert report.quote_sources[0].consecutive_failures == 3


def test_quote_chains_show_each_market_and_an_unconfigured_market_is_empty(db, settings):
    """A 市场没配报价源是这一屏要显示的事实，不是读不下去的理由。

    `AdapterRegistry.chain_for` 在取数路径上对缺市场抛 `DatasourceError`；
    健康页把它折成空链——配置决策不该让整份报告炸掉（同 `_ledger_data` 的表在不在）。
    """
    registry = AdapterRegistry(
        adapters={"tencent": QuoteOnly()},
        market_sources={"A": ("tencent",)},
        sleep=lambda seconds: None,
    )
    report = HealthService(db, registry, CalendarRepo(db), settings).report()

    assert report.quote_chains == {"A": ("tencent",), "HK": (), "US": ()}
```

`tests/cli/test_health_cli.py` 的 import 段追加：

```python
from sams.core.quotes import QuoteSourceRecord
```

把 `report_with` 整体替换成：

```python
def report_with(
    schedule: ScheduleData | None = None,
    notify: NotifyData | None = None,
    quotes: list[QuoteSourceRecord] | None = None,
    chains: dict[str, tuple[str, ...]] | None = None,
):
    return HealthReport(
        db_path=":memory:",
        schedule=schedule,
        notify=notify or NotifyData(),
        quote_sources=quotes or [],
        quote_chains=chains or {},
    )
```

在文件末尾追加：

```python
def test_a_failed_quote_source_is_printed_and_exits_1(invoke) -> None:
    """报价源失败与日线源失败是两条链上的两个结论，但都该让退出码变红。"""
    record = QuoteSourceRecord(
        name="tencent",
        markets=("A",),
        ok=False,
        last_error="HTTP 502",
        consecutive_failures=3,
    )
    code, text = invoke(report_with(quotes=[record], chains={"A": ("tencent",)}))
    assert "[报价源]" in text
    assert "tencent" in text and "失败×3" in text
    assert "最近错误 HTTP 502" in text
    assert "报价未恢复" in text
    assert code == 1


def test_a_quote_source_never_called_is_not_a_failure(invoke) -> None:
    """`ok is None` 是「还没问过」，与「问过且失败」分开：刚启动的进程本来就没问过。"""
    code, text = invoke(
        report_with(
            quotes=[QuoteSourceRecord(name="sina_quote", markets=("A",), ok=None)],
            chains={"A": ("tencent", "sina_quote")},
        )
    )
    assert "未知（未调用）" in text
    assert "尚未被调用过" in text
    assert code == 0


def test_an_empty_or_single_quote_chain_warns_without_touching_the_exit_code(invoke) -> None:
    """空链与单源是配置决策，不是故障：屏幕上标出来，退出码不动。

    把空链算成失败的话，一个只做日线的人每次 `sams health` 都会看到 1，
    而那个码很快就只剩「又有黄色的东西」一个意思。
    """
    code, text = invoke(
        report_with(chains={"A": (), "HK": ("yfinance",), "US": ("yfinance", "sina_quote")})
    )
    assert "这个市场没有报价源" in text
    assert "报价单源（yfinance）" in text
    assert code == 0
```

`tests/cli/test_health_locked.py`：在进程用例的心跳断言之后加一行，并追加两枚直接调用的用例。

```python
        assert "http://127.0.0.1:8765/healthz" in text
```

```python
def test_a_wildcard_bind_is_advertised_as_loopback(tmp_path, capsys, settings) -> None:
    """`0.0.0.0` 不是可访问的主机名：屏幕是这份配置唯一的展示处，
    它不该把一行抄进浏览器才报错的地址印出来。"""
    from dataclasses import replace

    from sams.core.config import ServerCfg

    wide = replace(settings, server=ServerCfg(host="0.0.0.0", allow_lan=True))
    capsys.readouterr()

    code = show_locked(DatabaseLocked(str(tmp_path / "sams.duckdb")), settings=wide)

    text = capsys.readouterr().out
    assert code == EXIT_LOCKED
    assert "http://127.0.0.1:8765/healthz" in text
    assert "0.0.0.0" not in text


def test_an_ipv6_bind_gets_brackets_in_the_url(tmp_path, capsys, settings) -> None:
    """IPv6 地址不带方括号时，冒号会被当成端口分隔符，URL 根本解析不了。"""
    from dataclasses import replace

    from sams.core.config import ServerCfg

    v6 = replace(settings, server=ServerCfg(host="::1", allow_lan=True))
    capsys.readouterr()

    code = show_locked(DatabaseLocked(str(tmp_path / "sams.duckdb")), settings=v6)

    assert code == EXIT_LOCKED
    assert "http://[::1]:8765/healthz" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/services/test_health.py tests/cli/test_health_cli.py tests/cli/test_health_locked.py -q
```

Expected: FAIL，`ImportError: cannot import name 'QuoteSourceRecord'`（Task 3 已建）与
`TypeError: HealthService.__init__() got an unexpected keyword argument 'quote_health'`。

- [ ] **Step 3: 实现 `[报价源]`**

`src/sams/core/sources.py` 的 `SourceHealthView` 整体替换成：

```python
class SourceHealthView(Protocol):
    """健康检查需要的全部能力：读出各源最近结果，以及各市场的备源顺序。

    `chain_for` 只回答「这个市场、这项能力有哪些源」，不真的取数；健康页用它把
    「这个源没有这个能力」与「这个市场只有一个源」分开说（D38 的口径）。
    """

    def health(self) -> list[SourceRecord]: ...

    def chain_for(self, market: str, method: str) -> tuple[str, ...]: ...
```

`src/sams/services/health.py` 的 import 段追加：

```python
from sams.core.errors import SamsError
from sams.core.quotes import QuoteSourceRecord
```

```python
from sams.storage.repos.quotes import QuoteHealthRepo
```

`KEY_TABLES` 里 `"scan_results"` 之后加一行：

```python
    # 盘中快照：保留策略删的就是这张表，行数说明盘中线程写过没有
    "quotes_snapshot",
```

`HealthReport` 的字段在 `schedule_unmigrated` 之后追加：

```python
    #: 报价源健康单独一节：与日线源共用一张表时，「腾讯报价挂了」会跟
    #: 「腾讯日线挂了」挤在同一行里，而两件事的修法不一样（盘中看报价，盘后看 K 线）
    quote_sources: list[QuoteSourceRecord] = field(default_factory=list)
    #: 每个市场的报价备源链：空链是「这个市场根本没配报价源」，单源是「挂了没有备源」。
    #: 两句都不进退出码——它们是配置决策，不是故障（见 `cli/health.py`）
    quote_chains: dict[str, tuple[str, ...]] = field(default_factory=dict)
```

property 区追加：

```python
    @property
    def degraded_quote_sources(self) -> list[QuoteSourceRecord]:
        return [s for s in self.quote_sources if s.ok is False]

    @property
    def unverified_quote_sources(self) -> list[QuoteSourceRecord]:
        return [s for s in self.quote_sources if s.ok is None]
```

`HealthService.__init__` 的签名在 `schedule` 之后追加参数：

```python
        quote_health: QuoteHealthRepo | None = None,
```

函数体追加一句：

```python
        #: 报价源健康是另一张表：不传就是「这一节没接入」（老库、单测的手搭实例）
        self.quote_health = quote_health
```

`report()` 的返回里、`sources=` 之后加两行：

```python
            quote_sources=self._quote_sources(),
            quote_chains=self._quote_chains(),
```

在 `_notify_data` 之前插入两个方法：

```python
    def _quote_sources(self) -> list[QuoteSourceRecord]:
        """报价源健康：表还没建时给空列表，不抛。

        `sams health` 是「先看看这个库怎么了」的入口，0019 之前的库照样要能出这一屏
        （`_ledger_data`/`_notify_data` 同一条线）。空列表与「全部正常」不是同一句话，
        屏幕上那句由 `cli/health.py` 说。
        """
        if self.quote_health is None:
            return []
        return sorted(self.quote_health.load().values(), key=lambda record: record.name)

    def _quote_chains(self) -> dict[str, tuple[str, ...]]:
        """每个市场的报价链；市场没配源时给空链而不是让整份报告炸掉。

        `AdapterRegistry.chain_for` 对「不在路由里的市场」抛 `DatasourceError`
        （那是取数路径该有的严格），健康页在这里是**读配置**：A 市场没有报价源
        是一条要显示的事实，不是这一屏读不下去的理由。
        """
        chains: dict[str, tuple[str, ...]] = {}
        for market in self.settings.markets:
            try:
                chains[market] = self.registry.chain_for(market, "quote")
            except SamsError:
                chains[market] = ()
        return chains
```

`src/sams/cli/common.py` 的 import 段追加：

```python
from sams.core.config import ServerCfg
```

在 `stamp` 之后插入：

```python
def advertised_url(server: ServerCfg, path: str = "") -> str:
    """屏幕上给人复制的地址：通配监听折成回环，IPv6 带方括号。

    `0.0.0.0` 不是可访问的主机名，IPv6 地址不带方括号时冒号会被当成端口分隔符。
    屏幕是这份配置唯一的展示处，它不该复述一个抄进浏览器才报错的字符串。
    """
    host = server.host
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = "https" if server.tls_certfile else "http"
    return f"{scheme}://{host}:{server.port}{path}"
```

`src/sams/cli/health.py` 的 import 段改成：

```python
from sams.bootstrap import Container
from sams.cli.common import EXIT_LOCKED, advertised_url, stamp
from sams.core.config import Settings
from sams.core.errors import DatabaseLocked
```

在 `_print_schedule` 之后、`show` 之前插入：

```python
def _print_quotes(report) -> None:
    """`[报价源]`：盘中那一路的健康与备源，与日线的 `[数据源]` 分开。

    三态沿用日线那一节的措辞：`ok is None` 是「从未被问到」，不是故障——
    刚启动的进程本来就没问过。空链与单源只标黄、不进退出码：那是配置决策，
    而退出码 1 要留给「问过、而且失败了」。
    """
    print("[报价源]")
    if not report.quote_sources:
        print("  （还没有报价源记录：盘中线程未启动，或库里还没有 0019 的形状）")
    for record in report.quote_sources:
        state = (
            "ok"
            if record.ok is True
            else "未知（未调用）"
            if record.ok is None
            else f"失败×{record.consecutive_failures}"
        )
        line = f"  {record.name:<10} {state:<13} 市场 {','.join(record.markets)}"
        if record.last_error:
            line += f"  最近错误 {record.last_error[:60]}"
        print(line)
    for market, chain in report.quote_chains.items():
        shown = " → ".join(chain) or "（空）"
        if not chain:
            mark = "  ⚠ 这个市场没有报价源：盘中取不到价，价格类规则不会触发"
        elif len(chain) == 1:
            mark = f"  ⚠ 报价单源（{chain[0]}），挂了没有备源"
        else:
            mark = ""
        print(f"  {market:<4} {shown}{mark}")
    for record in report.degraded_quote_sources:
        print(f"  ⚠ {record.name} 报价未恢复，检查网络或换源")
    for record in report.unverified_quote_sources:
        print(f"  · {record.name} 的报价尚未被调用过，健康状态未知")
```

`show` 里在 `[数据源]` 那一节之后（两段 `for source in ...` 之后）加：

```python
    _print_quotes(report)
```

`show` 的返回表达式改成：

```python
    return (
        1
        if report.degraded_sources
        or report.degraded_quote_sources
        or report.pending_migrations
        or report.schedule_stale
        else 0
    )
```

`show_locked` 的签名与正文改成：

```python
def show_locked(
    lock: DatabaseLocked,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> int:
    """库被另一个进程握着时的那一屏（D111）：只说还读得到的事实。

    DuckDB 单写者，连只读连接也打不开（1.5.6 实测），所以这一屏**不是**一份缩水的
    健康报告：它读不到表、源、日历、账本。能读的只有库旁那一拍，而且那一拍只证明
    「一个 `sams run start` 还活着」——一条长采集占着库时，这里必须说「不知道」，
    不能借「没有文件」推出「它死了」。

    退出码 3 与 1 分开：1 是「读到了，而且有故障」，3 是「读不到，所以不知道」。
    `settings` 给出时多指一条路：`/healthz` 走 HTTP，不受这把文件锁影响。
    """
    moment = now if now is not None else datetime.now(UTC)
    print(f"库: {lock.path}")
    print("[调度]")
    print(f"  ⚠ {lock}")
    beat = read_beat(beat_path(lock.path))
    if beat is None:
        print("  ⚠ 没有心跳文件：占着库的可能不是常驻调度器（也可能它刚启动）")
    else:
        # 时钟回拨会让年龄为负：报「未来的一拍」没有意义，折成 0
        age = max(0, int((moment - beat.beat_at).total_seconds()))
        if stale_at(age):
            print(
                f"  ⚠ 心跳已经 {_age(age)}没更新：占着库的那个进程可能已经卡住或退出"
                "（锁还没释放）"
            )
        else:
            print(f"  心跳 {_age(age)}前 · PID {beat.pid}（常驻循环还在写）")
    if settings is not None:
        print(
            "  活体读数（HTTP，不受库锁影响）: "
            f"{advertised_url(settings.server, '/healthz')}"
        )
    print("  其余各节需要打开数据库，本次没有读")
    print(f"  退出码 {EXIT_LOCKED} 表示「未知」：库被占用不等于库坏了")
    return EXIT_LOCKED
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/services/test_health.py tests/cli/test_health_cli.py tests/cli/test_health_locked.py tests/test_adversarial_services.py -q
uv run ruff check src/sams/core/sources.py src/sams/services/health.py src/sams/cli/common.py src/sams/cli/health.py tests/services/test_health.py tests/cli/test_health_cli.py tests/cli/test_health_locked.py --no-cache
```

Expected: PASS。`test_health_service_survives_unmigrated_database` 是这里最要紧的一枚：
它拿一个**没有 0019** 的库建 `HealthService`，`_quote_chains` 必须把三个市场的
`DatasourceError` 折成空链，报告照常出得来。

- [ ] **Step 5: 写 `sams serve` 的失败测试**

`tests/test_bootstrap.py` 的 import 段改成：

```python
import sams.bootstrap as bootstrap
from sams.bootstrap import (
    attach_log_sink,
    build_container,
    build_context,
    build_core,
    build_serve,
)
```

在文件末尾追加：

```python
def test_serve_builds_three_contexts_on_one_core(migrated):
    """三条线程各一条连接（D113），但板子、桶与请求队列是同一份。

    板子不是同一个对象时，盘中线程写的那一份 Web 永远看不到；请求队列同理，
    Web 的「立即同步」会入到一个没人读的队列里。
    """
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        assert len(parts.contexts) == 3
        assert len({id(context.db) for context in parts.contexts}) == 3
        assert all(context.registry is parts.core.registry for context in parts.contexts)
        assert all(context.bus is parts.core.bus for context in parts.contexts)
        assert all(
            context.scheduler.requests is parts.core.requests
            for context in parts.contexts
        )
        assert parts.contexts[1].quotes.board is parts.core.board
        assert parts.contexts[1].intraday.quotes is parts.contexts[1].quotes
        assert parts.runtime.board is parts.core.board
        assert parts.runtime.monitor is parts.contexts[1].intraday
    finally:
        parts.close()


def test_prepare_migrates_before_reading_the_snapshot_table(settings):
    """顺序不能反：`load_board` 读的是 0019 才有的列。

    这一枚把探针挂在 `load_board` 上：先迁移后灌板时它能看见 `quotes_snapshot`，
    反过来则是 `CatalogException`——而那个异常在 `serve` 启动时才炸，最难查。
    """
    parts = build_serve(settings, adapters=stub_adapters())
    quotes = parts.contexts[1].quotes
    original = quotes.load_board
    seen: dict[str, bool] = {}

    def probe() -> int:
        seen["table"] = "quotes_snapshot" in quotes.quotes.db.table_names()
        return original()

    quotes.load_board = probe
    try:
        assert parts.prepare() == 0
        assert seen == {"table": True}
    finally:
        parts.close()


def test_healthz_probes_never_touch_a_thread_context_connection(migrated):
    """`/healthz` 在 HTTP 线程上跑（D113）：调度器的库连接属于调度线程。

    把调度器那个方法换成一炸，`healthz` 仍要给出 ok——说明它走的是注入的探针
    （`FreshScheduleProbe` 每次新开一条连接），而不是手里那台 `Scheduler`。
    """
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        parts.prepare()
        parts.contexts[0].scheduler.schedule_data = lambda: pytest.fail(
            "healthz 不该借调度线程的连接"
        )

        payload = parts.runtime.healthz()

        assert payload["status"] == "ok"
        assert payload["scheduler"] == {"beat_age_s": None, "stale": False}
        assert payload["db"] == {"ok": True, "error": None}
    finally:
        parts.close()


def test_a_failed_serve_build_closes_the_connections_it_opened(settings, monkeypatch):
    """半成品也要收干净：第二条连接建不起来时，第一条与 core 的连接必须关。

    留着它们时，重跑 `sams serve` 会撞上自己——DuckDB 把同一个进程里没关的连接
    当成另一个写者，而报错是「数据库正被另一个进程打开」，指不到真正的原因。
    """
    real_context = bootstrap.build_context
    calls = {"n": 0}

    def flaky(core, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二条连接建不起来")
        return real_context(core, **kwargs)

    closed: list[int] = []
    real_close = Database.close

    def record_close(self):
        closed.append(id(self))
        real_close(self)

    monkeypatch.setattr(bootstrap, "build_context", flaky)
    monkeypatch.setattr(Database, "close", record_close)

    with pytest.raises(RuntimeError, match="第二条连接建不起来"):
        bootstrap.build_serve(settings, adapters=stub_adapters())

    assert len(closed) == 2, "contexts[0] 与 core 各一条"
```

新建 `tests/cli/test_serve.py`：

```python
"""`sams serve`：启动前把三件事做完（迁移、灌板、起线程），退出时收干净。

uvicorn 在这一层换成记录参数的替身：真起一个 HTTP 服务属于验收（spec §10），
单测钉的是「传给它的 host/port/worker 是哪几个」与「崩了以后有没有收尾」。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from sams.cli import serve
from sams.cli.common import EXIT_LOCKED
from sams.core.config import ServerCfg
from sams.core.errors import DatabaseLocked, ValidationError


def args(host=None, port=None):
    return SimpleNamespace(host=host, port=port)


class FakeRuntime:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeParts:
    def __init__(self, *, loaded=2):
        self.runtime = FakeRuntime()
        self.loaded = loaded
        self.prepared = False
        self.closed = False

    def prepare(self):
        self.prepared = True
        return self.loaded

    def close(self):
        self.closed = True


@pytest.fixture
def started(monkeypatch):
    parts = FakeParts()
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(serve, "build_serve", lambda settings: parts)
    monkeypatch.setattr(serve.uvicorn, "run", fake_run)
    return parts, seen


def test_a_command_line_port_override_keeps_the_configured_host(settings):
    server = serve._server_for(settings, args(port=9001))
    assert server.host == "127.0.0.1" and server.port == 9001
    assert serve._server_for(settings, args()).port == 8765


def test_a_wildcard_host_from_the_command_line_still_needs_allow_lan(settings):
    """覆盖只合并两项，然后整份重新校验：漏掉 `validate()` 就是一条命令开一扇门。"""
    with pytest.raises(ValidationError, match="allow_lan"):
        serve._server_for(settings, args(host="0.0.0.0"))


def test_start_prepares_then_starts_and_passes_single_worker_flags(
    settings, started, capsys
):
    parts, seen = started

    code = serve.start(settings, args())

    assert code == 0
    assert parts.prepared and parts.runtime.started
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    assert seen["workers"] == 1 and seen["reload"] is False
    assert seen["ssl_certfile"] is None and seen["ssl_keyfile"] is None
    assert parts.runtime.stopped and parts.closed
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765" in out
    assert "行情板已恢复 2 只票" in out


def test_tls_files_go_to_uvicorn_and_the_advertised_url_is_https(settings, started, capsys):
    _, seen = started
    tls = replace(
        settings,
        server=ServerCfg(
            tls_certfile="cert.pem",
            tls_keyfile="key.pem",
        ),
    )

    assert serve.start(tls, args()) == 0

    assert seen["ssl_certfile"] == "cert.pem"
    assert seen["ssl_keyfile"] == "key.pem"
    assert "https://127.0.0.1:8765" in capsys.readouterr().out


def test_a_crashed_server_still_stops_the_threads_and_closes(settings, monkeypatch):
    parts = FakeParts()

    def boom(app, **kwargs):
        raise RuntimeError("端口被占")

    monkeypatch.setattr(serve, "build_serve", lambda settings: parts)
    monkeypatch.setattr(serve.uvicorn, "run", boom)

    with pytest.raises(RuntimeError, match="端口被占"):
        serve.start(settings, args())

    assert parts.runtime.stopped and parts.closed


def test_a_refused_address_exits_1_without_opening_the_database(
    settings, monkeypatch, capsys
):
    monkeypatch.setattr(
        serve, "build_serve", lambda settings: pytest.fail("配置没过就不该开库")
    )

    assert serve.start(settings, args(host="0.0.0.0")) == 1

    assert "allow_lan" in capsys.readouterr().err


def test_a_locked_database_exits_3(settings, monkeypatch, capsys):
    def locked(settings):
        raise DatabaseLocked(settings.db_path, detail="Conflicting lock (PID 42)")

    monkeypatch.setattr(serve, "build_serve", locked)

    assert serve.start(settings, args()) == EXIT_LOCKED
    assert "PID 42" in capsys.readouterr().err


def test_run_dispatches_serve_before_assembling_a_container(settings, monkeypatch):
    """serve 要三条连接：先造 CLI 那条容器会白开一条，还会把它握在手里（D113）。"""
    from sams import cli

    monkeypatch.setattr(
        cli.Settings, "load", classmethod(lambda cls, path=None: settings)
    )
    monkeypatch.setattr(
        cli, "build_container", lambda *a, **k: pytest.fail("serve 不该先造容器")
    )
    seen: dict = {}

    def fake_start(loaded, parsed):
        seen["settings"] = loaded
        seen["args"] = parsed
        return 7

    monkeypatch.setattr(cli.serve, "start", fake_start)

    assert cli.run(["serve", "--port", "9001"]) == 7
    assert seen["settings"] is settings
    assert seen["args"].port == 9001
```

- [ ] **Step 6: 跑测试确认失败**

```bash
uv run pytest tests/test_bootstrap.py tests/cli/test_serve.py -q
```

Expected: FAIL，`ImportError: cannot import name 'build_serve'` 与
`ModuleNotFoundError: sams.cli.serve`。

- [ ] **Step 7: 实现 `build_serve`**

`src/sams/bootstrap.py` 的 import 段追加/修改：

```python
from sams.services.intraday import IntradayMonitor
from sams.services.prune import PruneService
from sams.services.quotes import QuoteBoard, QuoteService
from sams.services.runtime import Runtime
from sams.services.scheduler.report import ScheduleData, collect
from sams.services.scheduler.types import JobDeps, JobSpec
```

`AppCore` 在 `requests` 之后加一个字段：

```python
    #: 行情板是进程级的一份：盘中线程写、`/healthz` 与 Web 读。
    #: 每个上下文各造一块板时，页面看到的永远是自己那块空板
    board: QuoteBoard
```

`build_core` 的返回里加：

```python
        board=QuoteBoard(),
```

`ServiceContext` 在 `quote_health` 之后加两个字段：

```python
    #: 取价服务：盘中线程那一份会真去问源；其它上下文也有，但没人调用 `poll`
    quotes: QuoteService
    #: 盘中节拍器：同样只有盘中线程那一份被 `run_forever`
    intraday: IntradayMonitor
```

`build_context` 在 `quote_health = QuoteHealthRepo(database)` 之后加：

```python
    quotes = QuoteService(
        registry,
        core.board,
        quotes_store,
        quote_health,
        clock=clock,
        # 与盘后同步共用同一个桶：两条线程各自限速等于把 rate_limit_per_s 放大一倍
        bucket=registry.bucket,
        bus=event_bus,
    )
```

在 `universe_builder = UniverseBuilder(...)` 之后加：

```python
    intraday = IntradayMonitor(cfg, quotes, calendar, universe_builder, clock=clock)
```

`build_context` 的 `HealthService(...)` 参数里、`local_status` 之前加：

```python
            quote_health=quote_health,
```

`build_context` 的 `return ServiceContext(...)` 里、`quote_health=quote_health` 之后加：

```python
        quotes=quotes,
        intraday=intraday,
```

在 `build_container` 之后追加：

```python
class FreshDbProbe:
    """`/healthz` 的库探针：每次调用新开一条连接，用完就关。

    HTTP 线程不该借任何后台线程的连接（D113）。`SELECT 1` 很便宜，而这条连接
    只活一次查询——它不会把库文件长期握在手里，也不会把自己的事务深度借给别人。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def scalar(self, sql: str) -> object:
        database = Database.open(self.settings.db_path)
        try:
            return database.scalar(sql)
        finally:
            database.close()


class FreshScheduleProbe:
    """`/healthz` 的调度读数：每次新开一条连接，问的还是那两张台账。

    `Runtime` 手里有一台 `Scheduler`，但它跑在调度线程上；HTTP 线程直呼
    `scheduler.schedule_data()` 会用调度线程那条连接（D113）。这里把同一组查询
    换成「自己的连接 + 同一个 clock」，读数因此与 `sams health` 同源。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        specs: Sequence[JobSpec],
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        self.specs = tuple(specs)
        self.clock = clock

    def __call__(self) -> ScheduleData:
        database = Database.open(self.settings.db_path)
        try:
            return collect(
                runs=TaskRunRepo(database, clock=self.clock),
                heartbeats=HeartbeatRepo(database, clock=self.clock),
                users=UserRepo(database),
                specs=self.specs,
                clock=self.clock,
            )
        finally:
            database.close()


@dataclass
class ServeParts:
    """`sams serve` 的三条线程上下文与那一个 `Runtime`。

    `contexts` 的顺序就是创建顺序：调度线程、盘中线程、维护线程。维护那一份只被
    用来建 `PruneService`（它的连接随后归维护线程独占），三份都不是「主上下文」。
    """

    core: AppCore
    contexts: tuple[ServiceContext, ServiceContext, ServiceContext]
    runtime: Runtime

    def prepare(self) -> int:
        """迁移、再灌板。顺序不能反：`load_board` 读的是 0019 的列。"""
        self.contexts[0].migrate()
        return self.contexts[1].quotes.load_board()

    def close(self) -> None:
        for context in self.contexts:
            context.close()
        self.core.close()


def build_serve(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    adapters: dict[str, BarAdapter] | None = None,
    providers: dict[str, CalendarProvider] | None = None,
    notifier_factory: Callable[[str, Mapping[str, object]], Notifier] | None = None,
) -> ServeParts:
    """`sams serve` 的装配：一条进程级 core + 三条线程上下文 + 一个运行时。

    三个上下文依序造、依序记在 `opened` 里：造到第三个时炸掉，前两条也得关。
    半成品留着连接时，重试会撞上自己的锁，而报错指的是「另一个进程」。
    """
    cfg = settings or Settings.load()
    core = build_core(cfg, clock=clock, bus=bus, adapters=adapters)
    opened: list[ServiceContext] = []
    try:
        for _ in range(3):
            opened.append(
                build_context(
                    core, providers=providers, notifier_factory=notifier_factory
                )
            )
        scheduler_ctx, intraday_ctx, maintenance_ctx = opened
        prune = PruneService(
            maintenance_ctx.db,
            quotes=maintenance_ctx.quotes_store,
            task_runs=maintenance_ctx.task_runs,
            heartbeats=maintenance_ctx.heartbeats,
            clock=core.clock,
        )
        runtime = Runtime(
            cfg,
            scheduler=scheduler_ctx.scheduler,
            # 每次新开连接的读法：HTTP 线程不借调度线程的连接（D113）
            schedule_reading=FreshScheduleProbe(
                cfg, specs=scheduler_ctx.scheduler.jobs, clock=core.clock
            ),
            monitor=intraday_ctx.intraday,
            prune=prune,
            board=core.board,
            database=FreshDbProbe(cfg),
            clock=core.clock,
        )
        return ServeParts(
            core=core,
            contexts=(scheduler_ctx, intraday_ctx, maintenance_ctx),
            runtime=runtime,
        )
    except BaseException:
        for context in opened:
            context.close()
        core.close()
        raise
```

- [ ] **Step 8: 实现 `sams serve` 与命令分派**

新建 `src/sams/cli/serve.py`：

```python
"""`sams serve` —— 常驻进程：盘后调度、盘中轮询与 Web 在同一个进程里。

这个命令与其余命令的形状不同：它拿的是 `Settings` 而不是 `Container`。
三条线程各要一条 DuckDB 连接（D113），而 CLI 的容器只有一条；
所以 `run()` 在装配容器之前就把它分派出去（见 `cli/__init__.py`）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

import uvicorn

from sams.bootstrap import build_serve
from sams.cli.common import EXIT_LOCKED, advertised_url
from sams.core.config import ServerCfg, Settings
from sams.core.errors import DatabaseLocked, SamsError, ValidationError
from sams.web.app import create_app


def add_to(subparsers) -> None:
    # 不设 handler：`run()` 在造容器之前就分派到 `start(settings, args)`，
    # 它的第一个参数不是 Container，放进通用的 `args.handler(container, args)` 会错位
    parser = subparsers.add_parser("serve", help="常驻运行：盘后调度 + 盘中监控 + Web")
    parser.add_argument("--host", help="监听地址，默认取 [server].host")
    parser.add_argument("--port", type=int, help="监听端口，默认取 [server].port")


def _server_for(settings: Settings, args: argparse.Namespace) -> ServerCfg:
    """命令行覆盖只合并 host/port，然后整份重新校验。

    只检查这两项而不跑 `validate()`，`--host 0.0.0.0` 就绕开了 `allow_lan`：
    一条命令把库暴露到局域网，正是 spec §7 要防的事。
    """
    server = settings.server
    merged = replace(
        server,
        host=args.host if args.host is not None else server.host,
        port=args.port if args.port is not None else server.port,
    )
    merged.validate()
    return merged


def start(settings: Settings, args: argparse.Namespace) -> int:
    try:
        server = _server_for(settings, args)
    except ValidationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1

    parts = None
    runtime = None
    try:
        parts = build_serve(settings)
        runtime = parts.runtime
        restored = parts.prepare()
        runtime.start()
        print(f"SAMS 已启动：{advertised_url(server)}（Ctrl-C 停止，单 worker）")
        if restored:
            print(f"  行情板已恢复 {restored} 只票的最近报价")
        uvicorn.run(
            create_app(runtime),
            host=server.host,
            port=server.port,
            workers=1,
            reload=False,
            ssl_certfile=server.tls_certfile,
            ssl_keyfile=server.tls_keyfile,
        )
        return 0
    except KeyboardInterrupt:
        # Ctrl-C 是正常的停机方式：屏幕不该多一段 traceback
        return 0
    except DatabaseLocked as exc:
        # 另一个 `sams serve` 正握着库：与其它命令同一个码（D111）
        print(f"数据库被占用: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except ValidationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    except SamsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            runtime.stop()
        if parts is not None:
            parts.close()
```

`src/sams/cli/__init__.py` 的 import 段把 `serve` 加进列表：

```python
from sams.cli import (
    alert,
    analysis,
    calendar,
    channel,
    db,
    health,
    ledger,
    rule,
    schedule,
    screen,
    serve,
    source,
    sync,
    thesis,
    user,
)
```

`COMMAND_MODULES` 的第一项改成 `serve`：

```python
COMMAND_MODULES = (
    serve,
    db,
    user,
    sync,
    calendar,
    health,
    source,
    analysis,
    screen,
    thesis,
    ledger,
    channel,
    rule,
    schedule,
    alert,
)
```

`run` 整体替换成：

```python
def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.config_path)
    except SamsError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    if args.command == "serve":
        # serve 要三条连接（D113）：它拿 Settings 自己装配，不走 build_container 那一条。
        # 分派必须在这里、在容器之前——先造容器会白开一条并把它握在 CLI 手里
        return serve.start(settings, args)
    try:
        container = build_container(settings)
    except DatabaseLocked as exc:
        # 「库被占用」不是配置错误：那个进程可能正在干活（D111）。
        # health 是唯一还能说出一点东西的命令——库旁那一拍没被锁
        if args.command == "health":
            return health.show_locked(exc, settings=settings)
        print(f"数据库被占用: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except SamsError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command != "db":
            # 迁移幂等，因此非 db 命令顺手补齐表结构：新装环境直接 `sams user add`
            # 不该撞 CatalogException；`db migrate/status` 保留手工检视迁移的语义。
            container.migrate()
        return args.handler(container, args)
    except SamsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        container.close()
```

- [ ] **Step 9: 跑测试确认通过**

```bash
uv run pytest tests/test_bootstrap.py tests/cli/test_serve.py tests/services/test_runtime.py tests/web/test_healthz.py tests/test_layering.py -q
uv run ruff check src/sams/bootstrap.py src/sams/cli/serve.py src/sams/cli/__init__.py tests/test_bootstrap.py tests/cli/test_serve.py --no-cache
```

Expected: PASS。若分层守卫报 `cli/serve.py` 不得 import `sams.bootstrap`，说明
`ASSEMBLY_CONSUMERS` 被改坏了——只有 `cli` 可以引用组装根，`serve` 属于 `cli`。

另外手工确认一次帮助不再需要数据库：

```bash
uv run sams serve --help
uv run sams --help
```

Expected: 两条都退出 0，帮助里能看到 `serve`；`--help` 不打开库（D111 的影子）。
这一步在已有 `data/sams.duckdb` 的机器上同样成立——argparse 在 `Settings.load` 之前返回。

- [ ] **Step 10: 提交**

```bash
git add src/sams/core/sources.py src/sams/services/health.py src/sams/cli/common.py \
  src/sams/cli/health.py src/sams/cli/__init__.py src/sams/cli/serve.py src/sams/bootstrap.py \
  tests/services/test_health.py tests/cli/test_health_cli.py tests/cli/test_health_locked.py \
  tests/test_bootstrap.py tests/cli/test_serve.py
git commit -m "feat(serve): 常驻进程、报价源健康与锁住时的 /healthz 入口"
```

---

### Task 16: 整条链的验证与收尾

**Files:** 无固定改动。验证发现问题就修在对应文件里，并在 Step 6 一并提交。

这一条不是「再跑一遍测试」，而是把前面十五条各自绿着的用例放回同一台机器上：
静态闸、分层闸、线程用例的重复性，以及一个**真实的常驻进程**——它才是用户按下
`sams serve` 之后会遇到的形状。计划 2/3 的盘中规则与页面不在这里验收。

- [ ] **Step 1: 静态闸与分层闸**

```bash
uv run ruff check src tests --no-cache
uv run pytest tests/test_layering.py -q
```

Expected: PASS。分层闸这一枚要特别看两处：`web` 已在 `ALLOWED` 里（Task 14），
`cli/serve.py` 引用 `sams.bootstrap` 不触发「不得 import 组装根」（它属于 `cli`）。
`GATED` 里 `requests` 仍只放行 `datasource` 与 `monitor/notifiers`——`sina_quote`/`tencent`
在 `datasource` 里，`/healthz` 那条链一个 HTTP 客户端都不碰。

- [ ] **Step 2: 全量测试（不联网）**

```bash
uv run pytest -m "not live" -q
```

Expected: PASS，0 failed。若本机没有 `pytest-repeat`，不要为了它改依赖；
重复性用下面那条 shell 循环验。

- [ ] **Step 3: 线程用例连跑三遍**

```bash
for i in 1 2 3; do
  uv run pytest tests/datasource/test_ratelimit.py tests/services/test_runtime.py -q || break
done
```

Expected: 三次都 PASS。限速桶那一条在没有锁时会**偶尔**绿——三次一致才算数；
`Runtime` 那一条钉的是「起三个线程、停三个线程、再停一次不抛」。

- [ ] **Step 4: 真起一次 `sams serve`（scratch 库，不碰 `data/sams.duckdb`）**

```bash
WORK=$(mktemp -d)
CFG="$WORK/settings.toml"
cat > "$CFG" <<TOML
[storage]
db_path = "$WORK/sams.duckdb"

[general]
secret_key = "throwaway-smoke-key"
report_currency = "CNY"
scheduler_tick_s = 60

[server]
host = "127.0.0.1"
port = 18765

[markets.A]
tz = "Asia/Shanghai"
currency = "CNY"
post_close_min = 30
sources = ["akshare", "eastmoney"]

[markets.HK]
tz = "Asia/Hong_Kong"
currency = "HKD"
post_close_min = 30
sources = ["yfinance", "tencent"]

[markets.US]
tz = "America/New_York"
currency = "USD"
post_close_min = 30
sources = ["yfinance", "tencent"]
TOML

uv run sams --config "$CFG" db migrate
uv run sams --config "$CFG" db prune --dry-run
uv run sams --config "$CFG" health
```

Expected:

- `db migrate` 列出 `0001…0019`，退出 0。
- `db prune --dry-run` 打出三个步骤与「合计 0 行」（新库），退出 0。
- `health` 退出 0，屏幕上有 `[报价源]` 与「还没有报价源记录」，`[降级链]` 里三个市场
  都有 `sina_quote`（0019 的种子把它排在链末位）。

然后起常驻进程并验三条：

```bash
uv run sams --config "$CFG" serve > "$WORK/serve.log" 2>&1 &
SERVE_PID=$!
sleep 3
curl -fsS http://127.0.0.1:18765/healthz | python -m json.tool
set +e
uv run sams --config "$CFG" health
echo "CLI exit=$?"
set -e
kill -INT "$SERVE_PID"
wait "$SERVE_PID" || true
grep -E "运行时已启动|每日清理" "$WORK/serve.log"
sleep 1
uv run sams --config "$CFG" health >/dev/null && echo "after-stop exit=0"
rm -rf "$WORK"
```

Expected:

- `/healthz` 是 HTTP 200 的 JSON，含 `status`、`db.ok=true`、三个市场各一条
  `intraday` 读数、`version`、`uptime_s`。盘中时段会看到 `last_poll_at` 与 `source`；
  休市时段 `last_poll_at` 为空、`ok` 仍为 true（「没问过」不是故障）。
- 常驻时的 `sams health` 退出 **3**，屏幕上有「数据库…正被另一个进程…打开」、
  心跳那一行，以及 `http://127.0.0.1:18765/healthz`——地址取自配置，不是写死的 8765。
- `kill -INT` 后进程退出，日志里有「运行时已启动」与一次「每日清理」（启动即清一次）；
  停服后 `sams health` 回到 0。

Windows 上对应的是 `Start-Process uv -ArgumentList ...`、`Invoke-RestMethod`、
`Stop-Process`；同样的四条结论（`/healthz` 可达、CLI 退 3、地址正确、停服后恢复）
必须在 PowerShell 里也成立（验收 §10 第 8 条）。

- [ ] **Step 5: 核对本计划的边界**

这一版结束时应当成立（且只成立这些）：

- `sams serve` 一条命令起三线程与 HTTP；`/healthz` 永远 200，降级写在 `status` 里。
- 盘中线程按 10 秒轮询 A/HK/US 的自选/持仓/规则对象，写 `quotes_snapshot` 与
  `quote_source_health`，并在连续 3 分钟取不到任何报价时发 `quote.<market>` 系统告警。
- `sams health` 有 `[报价源]`、锁住时指到 `/healthz`；`sams db prune` 可预演、可实跑。
- 盘中**规则求值**、告警推送队列、Web 页面仍不在本计划里 → 计划 2（规则/通知）与
  计划 3（页面）。因此 spec §10 里「页面上每 10 秒看到价格」「价格规则在盘中触发」
  这两条要等计划 2/3 验收，别在这一步勾掉。

- [ ] **Step 6: 收尾提交**

若 Step 1–4 修过任何文件：

```bash
git status --short
git add <修过的文件，逐个列出>
git commit -m "fix(m4a): 整条链验证发现的收尾问题"
```

Expected: `git status --short` 里只剩预期的改动（用户自己的 `qodercli_resume.sh` 与
`tmp/` 不许进提交）。没修过文件就不提交——空的验证任务不该造一个空提交。

---

<!-- CONTINUE -->
