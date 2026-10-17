# M1 地基实施计划

**Goal:** 交付可运行地基——配置、事件总线、分市场交易日历、DuckDB 存储与仓库层（用户数据硬隔离）、A 股主备源与港美股日线采集、CLI 运维入口。

**Architecture:** 分层单体。`core` 无内部依赖；`storage` 是唯一读写入口；`datasource` 只做「外部接口 → 标准列集」规范化并按市场路由降级；`cli` 把三者串起来。跨模块通信走进程内事件总线，为 A→B 拆分保留边界。

**Tech Stack:** Python 3.13（uv）、DuckDB + Polars + PyArrow、exchange_calendars（港/美日历）、akshare（A 股行情/日历）、yfinance（港美股日线）、bcrypt + cryptography、pytest + ruff。

**Spec:** `docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md`（§14 M1）。

> 本计划是任务清单形态：用户要求直接开发并自测，故代码以实际实现为准，此处不重复贴代码。M2（指标与估值）、M3（规则与告警）各自另立计划。

**本计划不含** theses/rules/alerts/channels/annotations/模拟仓 的仓库层与业务逻辑（DDL 一次建全，实现随 M2/M3）。

---

## 约定（工程师零上下文须知）

1. 不要用系统 Python（anaconda 3.9.7）。所有命令 `uv run ...` 前缀。
2. 测试禁止真实网络：外部调用打 `@pytest.mark.live`，由 `addopts = -m "not live"` 默认排除。可测性靠把 payload→DataFrame 的解析做成纯函数，测试喂录制数据。
3. 幂等是本期质量底线：重复入库不产生重复行（DuckDB 主键 + `INSERT OR REPLACE`）。
4. 用户隔离在仓库层兜底：用户域方法第一个参数是 `user_id`；admin 全量只读走显式方法名 `list_all_*`，不用 `as_admin` 布尔开关。
5. 价格/金额一律 DOUBLE，不引入 Decimal。

## 文件结构

落地后的实际结构（相对本计划初稿的差异见文末「自测记录」）：

```
pyproject.toml                        依赖、[tool.pytest.ini_options]（含 pythonpath=tests）、[tool.ruff]
config/settings.example.toml          提交；config/settings.toml 本地复制且 gitignore
src/sams/core/config.py               Settings / MarketCfg + 环境变量覆盖
src/sams/core/errors.py               ValidationError, DatasourceError, RateLimited, EmptyResponse, FieldDrift
src/sams/core/events.py               Event 子类 + EventBus（同步、异常隔离）
src/sams/core/timeutil.py             交易日历合成与时段判定、盘后触发点
src/sams/core/calendar.py             端口：CalendarProvider / CalendarStore（不 import 任何下层）
src/sams/core/sources.py              SourceRecord + SourceHealthStore / SourceHealthView 端口
src/sams/storage/db.py                Database 包装（open/execute/register/table_names）
src/sams/storage/migrations/0001_init.sql       spec §5 全部表 DDL
src/sams/storage/migrations/0002_source_health.sql  源健康按源覆盖式落库（跨进程可见）
src/sams/storage/migrate.py           schema_migrations 驱动，幂等
src/sams/storage/repos/{users,sessions,symbols,bars,calendar,watchlists,source_health}.py
src/sams/datasource/base.py           BarAdapter 协议、标准列常量、normalize_bars、code_in_market
src/sams/datasource/ratelimit.py      TokenBucket + backoff_sleep（注入时钟）
src/sams/datasource/calendar_providers.py  CnSinaProvider(akshare)、XcalProvider(exchange_calendars)
src/sams/datasource/{akshare_cn,eastmoney,yfinance_us}.py   三个源的适配器
src/sams/datasource/registry.py       市场→源列表，降级重试与源健康
src/sams/services/{calendar,sync,health,accounts}.py  用例编排（日历补齐、采集入库、自检、账号）
src/sams/bootstrap.py                 组装根：唯一同时认识所有实现的地方
src/sams/cli/{__init__,common,db,user,sync,calendar,health}.py  命令分发与展示
tests/                                镜像 src 结构；support.py 放替身与样本；test_layering.py 分层守卫
```

## 任务

- [x] **T1 工具链与骨架**：`uv init --package` 已完成；配置 pytest markers、ruff、`config/settings.example.toml`、`sams = "sams.cli:main"`。
- [x] **T2 `core/errors.py` + `core/config.py`**：toml 加载、`SAMS_*` 环境变量覆盖、缺 `secret_key` 报错、`market_cfg()` 未知市场报错、`polling_interval` 非正数报错。
- [x] **T3 `core/events.py`**：`EventBus.subscribe/publish`，按类型精确分发 + `Event` 通配；某 handler 抛异常不中断其余 handler，异常在日志与返回值中可见（§12 任务隔离）。
- [x] **T4 `storage/db.py` + `migrations/0001_init.sql` + `migrate.py`**：spec §5 全部表（含 `user_sessions`）；重复执行迁移不重复应用；表存在性断言；`INSERT OR REPLACE` 幂等冒烟。
- [x] **T5 `core/timeutil.py`**：`sessions_for_day`（HK/US 常规+可选盘前盘后、A 股两段）、`is_market_open`、`post_close_run_at`。必测：A 股午休边界、美股夏令时切换日（2026-03-08/09 UTC 偏移变化）、港股半天、默认不含盘前盘后。
- [x] **T6 `core/calendar.py` + `storage/repos/calendar.py`**：`CalendarService` 读 DB、缺失时调 provider 补齐并入库；日历源全部不可用时 `default_is_trading=True`（§6.3 代价不对称）并降系统告警事件；半天日以「该年 session 时长众数」判定，不猜 API。
- [x] **T7 `core/calendar_providers.py`**：CnSinaProvider（akshare `tool_trade_date_hist_sina`，两段时段）、XcalProvider（XHK/XNYS，tz 取 `calendar.tz`，含提前收市）。live 测试：2026-10-01 A 股休市、2026-12-24 港股半天、2026-07-03 美股提前收市。
- [x] **T8 `repos/users.py`**：bcrypt 校验、密码 ≥12、用户名唯一、禁用后不能认证、admin 判定；**同密码两次创建哈希不同**（防"忘记加盐"回归）。
- [x] **T9 `repos/sessions.py`**：明文 token 只返回一次、库内只有 sha256、过期与吊销后校验失败。
- [x] **T10 `repos/symbols.py` + `repos/bars.py`**：upsert 幂等（重跑行数不变、同主键更新生效）、`latest_date`、区间读取、`(market, code)` 唯一性。
- [x] **T11 `repos/watchlists.py`**：同用户同标的不重复；**用户 B 的查询绝不返回用户 A 的行**；`list_all_*` 返回全部并带 user_id；admin 只读（无 `update/delete`）。
- [x] **T12 `datasource/base.py` + `ratelimit.py`**：`normalize_bars` 统一列/类型/丢弃无收盘价的行并记录被丢行数、日期升序；TokenBucket 注入假时钟；退避序列 1/4/16。
- [x] **T13 `datasource/eastmoney.py`**：录制 JSON → `pl.DataFrame`；缺字段 → `FieldDrift`；空数据 → `EmptyResponse`。
- [x] **T14 `datasource/akshare_cn.py`**：录制 DataFrame → 标准列；live 测试标记。
- [x] **T15 `datasource/yfinance_us.py`**：`code_in_market`（AAPL→AAPL，00700→700.HK）；录制数据规范化；live 测试标记。
- [x] **T16 `datasource/registry.py`**：主源 `RateLimited` → 自动切备源且 health 记录；两源皆挂 → `DatasourceError`；每源结果带 `source` 列。
- [x] **T17 `cli.py`**：`db migrate`、`user add/list/passwd/enable/disable/promote`、`login`、`sync`、`calendar refresh/check`、`health`。逻辑写在可测函数里，argparse 只做分发。
- [x] **T18 自测与验收**（见下）。

## 验收清单（spec §14 M1）

1. `uv run sams db migrate` 建库；重跑不重复应用迁移。
2. `uv run sams sync --market A --code 600519 --start 2024-01-01 --end 2024-02-01` 成功入库；**连续执行两次行数完全一致**。
3. `uv run sams sync --market US --code AAPL ...`、`--market HK --code 00700 ...` 入库且 `currency` 正确（USD/HKD）。
4. `uv run sams calendar check --market A --date 2026-10-01` → 休市；`--market US --date 2026-03-09` → 开盘且 UTC 时刻体现夏令时。
5. `uv run sams user add` + `login`；用户 B 查询不到用户 A 的自选股。
6. `uv run pytest -q` 全绿；`uv run ruff check .` 干净。
7. `uv run sams health` 显示各表行数、日历覆盖年份、各源最近错误。

---

## 自测记录（2026-10-17）

### 结构相对初稿的增量

按「模块化、边界清晰、便于维护」的要求调整了四处，都是结构性的，没有新增功能：

1. **core 只留端口。** 初稿把 `CalendarService` 写在 `core/calendar.py`，它要读写仓库，于是 core 反向 import 了 storage。现在 core 只有 `CalendarProvider` / `CalendarStore` / `SourceRecord` / `SourceHealthStore` 这类契约，编排落在 `services/`。
2. **services 层显式化。** `calendar.py`（日历补齐与保守判定）、`sync.py`（采集→入库）、`health.py`（自检汇聚）、`accounts.py`（「禁用/改密必须连带吊销会话」这条跨表不变式，原先在 CLI 里重复了两遍）。CLI 因此只剩参数解析与展示，不再 import storage/datasource 的任何实现。
3. **源健康落库（0002 迁移）。** 采集进程与 `sams health` 是两个进程，进程内记录一退出就清零，健康检查于是永远报「一切正常」。改为 `source_health` 表按源覆盖式写入，registry 启动时读回。
4. **分层守卫测试。** `tests/test_layering.py` 用 AST 扫 import：core 不依赖任何人，storage 与 datasource 互不相干，services 可以编排两者，cli 只碰 core/services 与组装根；同时认识三层的模块只能是 `bootstrap.py`。规则被机器看着，才不会被打散。

另外两处是实现期的事实修正：

- `datasource/calendar_providers.py` 从 core 移到 datasource（它 import akshare 与 exchange_calendars，本就不属于核心层）。
- yfinance 改走 `Ticker.history()`。`yf.download()` 把 429 吞成空帧只打日志，采集层因此把「被限流」当成「区间无数据」——既不重试也不换源，健康报告还说一切正常。`history()` 会真抛异常，`classify_error` 立刻归入 `RateLimited`。同时修正 `end` 是开区间导致的末日漏取。

### 跑过什么

```
uv run pytest -q            263 passed, 1 deselected（live）
uv run pytest -m live       1 passed
uv run ruff check .         All checks passed
```

CLI 实跑（独立临时库，不碰开发库）：

| 动作 | 结果 |
| --- | --- |
| `db migrate` ×2 | 第二次「已是最新」；31 张表 |
| `user add` / `login` / `user list` | 签发 token，`sessions.verify` 可校验 |
| `sync --market A --code 600519` | akshare 连接被断 → 退避重试 2 次 → **自动降级 eastmoney 取回 7 条**；重跑 akshare 恢复，`bars_daily` 仍 7 行（幂等） |
| `sync --market US --code AAPL` | 真实 429 → 3 次退避 → `所有源均失败`，exit 1 |
| `health`（另一个进程） | 显示 `yfinance 失败×2 … 未恢复`，exit 1——源健康跨进程生效 |
| `calendar refresh A/HK/US 2026` | 242 / 247 / 251 个交易日 |
| `calendar check` | 2026-11-26 美股休市、2026-11-27 半天 09:30–13:00、2026-12-24 港股半天 09:30–12:00（UTC 01:30–04:00）、美股夏令时切换前后开市 UTC 从 14:30 变 13:30 |
| `sync --market JP` / `calendar refresh --market JP` | 配置缺失报错清晰，exit 1 |

修掉的一个显示 bug：`calendar check` 的 UTC 一栏直接复用本地时间格式化，没做 `astimezone(UTC)`；现已按港股半天 09:30→01:30 的断言锁住。

### 验收清单里还欠一条

第 3 项（US/HK 日线入库且 `currency` 正确）没跑通：Yahoo Finance 对当前出口持续限流，拿不到真实日线。失败链路与健康上报已按上面的真实 429 验证过，入库与币种字段由单测覆盖（`services/test_sync.py`）。等网络通畅时补一次 `sams sync --market US/HK` 即可结案。
