# M4-A 盘中监控与看板 · 设计

日期：2026-10-23
状态：**设计已定，待实施**。本文只覆盖第一个「可用的产品」增量（M4-A）；批注、推送带批注与完整复盘闭环归 M4-B，已单独设计（[M4-B 设计](./2026-10-23-m4b-annotations-review-design.md)），边界见 §12。
关联文档：[总体设计](./2026-10-17-stock-analysis-monitoring-system-design.md)（下称「总 spec」）。

---

## 0. 决策摘要

| 编号 | 决定 | 理由 |
|---|---|---|
| D112 | 表现层由 Streamlit 改为**单进程 FastAPI + Jinja2 + HTMX + 本地 ECharts**，修订总 spec 的 D2 | DuckDB 单写者（D111）：常驻调度与看板分属两个进程时，看板连只读连接都打不开。单进程是唯一既不给调度器加 IPC、又不复制读模型的形态；Streamlit 的每会话脚本重跑与独立进程鉴权反而增加运行件 |
| D113 | 新增 `sams serve`：一个进程内跑盘后调度线程、盘中轮询线程与 uvicorn（单 worker、禁 reload）；三处各用**独立 DuckDB 连接**，不共享连接/事务状态 | 同进程多连接共享同一个 DB 实例已实测；共享连接会把 `_tx_depth`、`register` 视图与游标搅在一起。写事务保持短，冲突有限重试 |
| D114 | 新增数据能力 `quote`：腾讯批量报价为主，`sina_quote`（仅报价）为备；A/HK/US 三市场；只轮询常规交易时段 | 报价是独立于日线的一类数据，必须有自己的能力、健康与降级链；把报价塞进日线源会让「日线坏了」与「报价坏了」混成一行 |
| D115 | 盘中只评估 **9 个价格类模板**；MA/ATR/止损线/目标价/持仓期最高价用上一收盘，`holding_high` 叠加当日盘中高点；模板是否可盘中评估收成 `Template.intraday` 单一出处 | 用户选定「实时价 + 收盘指标」。未完成 K 线重算指标会抖动且与收盘结论语义混在一起，留待以后显式引入 `intraday=true` 的估值口径 |
| D116 | `quotes_snapshot` 补 `open/prev_close/high/low`，新增 `quote_source_health` 表；快照保留 5 个交易日，`task_runs` 90 天、心跳只留最近 N 条，由 `sams db prune` 与 `sams serve` 的每日维护执行 | 盘中判 `urgent` 要开盘价与前收盘；报价源健康不能与日线源健康共用一行；U27 的只增不删在这一层闭合 |
| D117 | 常驻期间 CLI 全部退 3 是既定行为，日常写操作搬进 Web：自选、告警处理、规则新建/启停/改参、成交录入、观点推进、我的通道/DND/密码、admin 用户/源/健康 | 产品不能要求「改一条规则先停服务」。CLI 保留给停服维护、脚本与 `sams health` |
| D118 | 盘中通知不在轮询线程同步发：`NotifyService` 拆成 `record`（只落告警）与 `deliver_existing`（按行重建消息再发送）；队列只传 `alert_id`，单 worker 调 `deliver_existing`，沿用现有路由/重试/免打扰 | 现有 `EventBus` 同步分发、通知含 HTTP/语音，轮询线程被一个慢通道拖住就会错过下一批报价；只传 id 让公告在重启后仍可手动重发，M4-B 的发送时上下文也有地方接入 |
| D119 | Web 默认只绑 `127.0.0.1`；LAN 必须显式 `allow_lan = true`。会话复用 `users`/`user_sessions`，Cookie `HttpOnly` + `SameSite=Lax`（TLS 时 `Secure`），POST 全量 CSRF，登录限速 | 产品从本机走到局域网，安全边界必须写进配置而不是靠「应该没人访问」 |
| D120 | 盘中告警的 `payload_json` 记 `intraday=true`、报价时间与来源；`/healthz` 报进程、库、调度心跳、各市场最近报价；`sams health` 在库被占用时指到 `/healthz` | 复盘与排障要看得出这条告警是盘中还是收盘判的；CLI 在常驻时读不到库，仍需要一个可机读的活体信号 |
| D130 | Web **新建规则**：`/rules/new` 用模板元数据生成表单，支持全部现有模板；标的页提供 3 个止损预设快捷入口。Web 新建的规则默认开 `no_open_alert`（可取消），既有规则不改 | 常驻时 CLI 退 3；如果只能改参不能新建，第一天设止损仍要停服。这是产品评审认定的头号断点 |
| D131 | 首运行与数据就绪：`/` 空状态给「加自选→配通道→同步数据→建规则」清单；`/admin/health` 提供「立即同步该市场」（交给调度线程执行，写 `task_runs`）；`/settings/test-alert` 发一条端到端测试告警（`alerts.is_test=true`，不计复盘、不挡 `no_open_alert`） | 新库的日线要等收盘后才有，不解决「第一天打开是空图」就谈不上可用；测试告警让用户在开盘前就把规则→通道→收件箱整链走通 |
| D132 | 普通用户新增 `/settings`：我的通道（增删改/启停/测试）、免打扰、改密码、显示名；`/admin/channels` 只做全局只读与掩码，不再替用户改通道；迁移 0019 增 `users.display_name` | 通道是用户自己的投递路径，团队第二个用户必须能自助配置；admin 替所有人改凭据既越权又无法审计 |
| D133 | 告警降噪：收件箱按 `(market, code)` 合并连续命中为一行，展开看每条规则；Web 新建规则默认 `no_open_alert`；页面显示被过滤的原因（冷却/已有未处理/免打扰） | 9 个模板 × 多条规则同时命中一只票时，逐条推送等于让用户关掉通知。合并展示不改 `alerts` 一行一命中的账本口径 |
| D134 | 行情状态从 `stale` 布尔扩成五态：`live`/`delayed`/`closed`/`stale`/`no_data`；盘中告警正文追加「报价时间/来源」；首页在通知连续失败时显示横幅 | 「收盘」「延迟」「停牌」「没数据」在交易者眼里是四件事；只给一个 stale 会让用户误判系统坏了还是市场没开 |

---

## 1. 目标与非目标

### 1.1 目标（M4-A 完成时）

1. 本机或局域网内，一个 `sams serve` 进程即可 24/7 运行：盘后任务照旧，盘中每 10 秒取一次自选/持仓/规则对象的报价，价格类规则实时评估并按既有冷却/过滤/路由告警。
2. 浏览器里完成每天要做的事：看行情与持仓、看告警并处理、新建/调参/启停规则、录一笔成交、推进观点、配置自己的通知通道、管理用户/数据源。
3. 新库第一次打开就能走完「加自选 → 配通道 → 同步数据 → 建规则」；开盘前能用测试告警验证整条投递链。
4. 多用户彼此隔离；admin 能看到全量与管理系统事务；权限在服务端逐路由校验。
5. macOS / Windows / Linux 同一套代码；不依赖 macOS 专有命令（桌面/语音通知按平台自动降级）。
6. 常驻时外部仍能判断「活着/卡住/退出」：`/healthz` 与既有 `<db>.heartbeat.json` 双通道。

### 1.2 非目标（M4-A 不做）

- 盘中重算 MA/RSI/ATR 等指标（用未完成 K 线）；不做盘中估值快照。
- 盘前/盘后时段的自动轮询（配置里的 `extended` 保留，规则显式 opt-in 留到后续）。
- **集合竞价不轮询**：M4-A 从常规连续交易时段开始。总 spec 里「集合竞价即报」在 M4 不兑现，文案与验收同步改掉；留给后续显式的 auction 快照（U7 的下一层）。
- 全市场扫描、指数/板块盘中参照、临时休市的市场级裁决（总 spec U2/U15）。
- 批注、附件、推送带批注、完整复盘闭环（M4-B）。
- 收盘摘要推送、服务安装器（systemd/launchd/Windows 服务）与在线备份；这些只给部署文档或留到 M5。
- 多实例/高可用、Docker、Postgres 迁移（M5 及以后）。单机 DuckDB 是本版的设计边界。
- 移动端原生 App；浏览器响应式页面即可。

---

## 2. 运行形态

### 2.1 进程与线程

```
sams serve
├── uvicorn（单 worker，禁 reload）        —— HTTP，FastAPI
├── scheduler thread                        —— 现有 Scheduler.run_forever（盘后 job，60s 检查）
├── intraday thread                         —— 10s 轮询 + 盘中评估
├── notify worker thread                    —— 队列消费，发送告警
└── maintenance thread（或调度线程内的每日时点）—— 每日 prune、报价源健康汇总
```

- **一个进程是唯一持库者**。uvicorn 多 worker 会在第二个 worker 打开库时得到 `DatabaseLocked`；启动时显式校验并拒绝多 worker 配置（`workers=1` 写死）。
- **连接归属**：调度线程、盘中线程、通知 worker、每个 HTTP 工作线程各自持有独立的 `Database`（同进程多连接）。任何连接、事务深度、注册视图都不跨线程共享。
- **共享对象**：`Settings`、`Clock`、`EventBus`、`Cipher`、`SourceRegistry`（构造后只读）、`QuoteBoard`、`VersionGate`（§5.4）、停止事件。共享可变对象必须自带锁。
- **写路径**：所有写操作短事务；盘中快照批量写、盘后 job 按批写。遇到 DuckDB 写冲突（同进程连接之间）做有限重试（3 次，10/50/200ms），仍失败则记 `task_runs`/日志并让下一轮重试；不允许长事务阻塞 10 秒轮询。
- **停机**：SIGTERM/SIGINT → 置停止事件 → 等调度线程退出当前 job（上限 `scheduler_tick_s + 30s`）→ 停盘中线程与通知 worker（有限时间排空）→ 关所有连接 → 删心跳文件 → uvicorn 退出。超时则记日志后继续退出；未发出的告警仍在 `alerts`/`notify_log` 里可见。

### 2.2 与现有入口的关系

- `sams serve`：完整产品。盘后调度 + 盘中轮询 + Web。
- `sams run start`：保留为**只跑盘后调度**的无头模式（M3-D 行为不变），用于诊断或不需要网页的机器。
- 其余 CLI 命令：`sams serve` 运行时因库被占用退 3（D111）；`sams health` 退 3 并读心跳文件，提示打开 `/healthz`。停服后 CLI 恢复。
- 首个 admin 仍用 `sams user add --username ... --admin` 创建（停服状态），之后用户管理在 `/admin/users`。

### 2.3 健康与看门狗

- 既有 `<db>.heartbeat.json`：由调度线程每拍与每个 job 前写，语义不变（D111）。
- `/healthz`：JSON，HTTP 200。字段：
  - `status`: `ok` / `degraded`
  - `db`: 可读布尔与错误
  - `scheduler`: `beat_age_s`、`stale`
  - `intraday`: 每个市场 `{last_poll_at, age_s, source, ok}`；休市时 `last_poll_at` 可为空且不算故障
  - `version`、`uptime_s`
- `/healthz` 在调度线程卡住但进程仍活着时返回 `degraded`；进程死掉时 HTTP 不可达。两者与 `sams health` 的退出码 0/1/3 合起来覆盖「读得到/读不到/进程在/不在」。
- admin 的 `/admin/health` 展示同一份数据 + `HealthService` 的详细报告 + 最近 `task_runs`。

---

## 3. 模块与文件结构

新增（路径以仓库根为准）：

```
src/sams/core/quotes.py              Quote、QuoteBatch、报价时新鲜度判定
src/sams/core/version.py             VersionGate：跨线程失效计数
src/sams/datasource/quote.py         QuoteAdapter 协议、批量/回显校验辅助
src/sams/datasource/sina_quote.py    Sina 仅报价适配器（A/HK/US）
src/sams/datasource/tencent.py       增加 quotes(...)
src/sams/storage/migrations/0019_quotes_and_prune.sql
src/sams/storage/repos/quotes.py     QuoteRepo
src/sams/storage/repos/quote_health.py
src/sams/services/quotes.py          QuoteService：轮询、降级、入库、QuoteBoard
src/sams/services/intraday.py        IntradayMonitor：时段判断、universe、评估循环
src/sams/services/rules/intraday.py  日线快照缓存 + 报价叠加（SnapshotOverlay）
src/sams/services/notify_queue.py    QueuedAlertSender + NotifyWorker
src/sams/services/runtime.py         Runtime：线程生命周期、/healthz 数据、维护任务
src/sams/web/                        FastAPI 应用
  app.py, auth.py, context.py, forms.py
  routes/{pages,partials,actions,rules,settings,admin,api}.py
  templates/*.html
  static/{htmx.min.js,echarts.min.js,app.css,app.js}
src/sams/cli/serve.py                sams serve
tests/...                            见 §10
```

修改：

- `src/sams/bootstrap.py`：拆出进程级 `AppCore`（settings/clock/bus/cipher/registry）与线程级 `ServiceContext`（Database + repos + services）；`build_container` 保持 CLI 兼容，`serve` 用新工厂。
- `src/sams/core/rules.py`、`src/sams/services/rules/templates.py`：`Template.intraday` 标志。
- `src/sams/services/rules/snapshot.py`：`RuleData.read(..., quote=...)` 与日线部分拆分。
- `src/sams/services/rules/engine.py`：接受报价叠加与缓存；发送端口可注入队列版。
- `src/sams/services/health.py`：增加报价源健康与 `/healthz` 需要的数据。
- `src/sams/services/scheduler/service.py`：增加 `request_run(job_name, market)` 的线程安全请求队列，由调度线程在下一拍执行（手动同步走这里，不新开写线程）。
- `src/sams/storage/repos/users.py`：`display_name` 读写、本人改密码。
- `src/sams/cli/__init__.py`：注册 `serve`；`DatabaseLocked` 的 health 分支指向 `/healthz`。
- `src/sams/cli/health.py`：`show_locked` 增一句「网页健康看 http://host:port/healthz」。
- 总 spec：D2 修订、架构图表现层、M4 范围指向本文。

---

## 4. 盘中数据链路

### 4.1 报价契约

```python
@dataclass(frozen=True)
class Quote:
    market: str
    code: str
    ts: datetime              # 源给的报价时刻；源不给则用取回时刻，并标 is_stale
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
    status: str = ""          # 源侧交易状态（有就带，不猜）
    delayed: bool | None = None  # 源明确标延迟才为 True；不标 ≠ 保证实时
    source: str = ""
    is_stale: bool = False

@dataclass(frozen=True)
class QuoteBatch:
    quotes: tuple[Quote, ...]
    skipped: tuple[str, ...]      # 源明确没有的代码
    errors: tuple[str, ...]       # 单只解析失败的原因
```

- 价格必须有限且 > 0；不满足的整只丢弃并记 `errors`，不写 0 或 NaN。
- 适配器必须校验源回显的代码与请求一致；不一致整只拒绝（与日线 `require_echoed_code` 同一取向）。
- `ts` 一律转 UTC 存储；展示按市场时区。

### 4.2 源与降级

- 腾讯 `qt`：一次请求多个代码，A/HK/US 全覆盖。`tencent.quotes(market, codes)`。
- 新增 `sina_quote`：`hq.sinajs.cn` 批量报价，A/HK/US；只提供 `quote` 能力，不注册日线能力，避免把「日线不支持的市场」混进日线链。
- 路由：沿用 `data_source_routes` 的按市场顺序，按 `quote` 能力过滤；`sina_quote` 由迁移 0019 追加到每个市场链末位（若库中已有该源则不动）。报价源健康**单独**记 `quote_source_health`，不与日线 `source_health` 混写。
- 单源失败/超时：换下一个源；部分代码返回：只写返回的那些，其余在下一轮重试，不把未返回的标成 0。
- 全部源失败：保留 `QuoteBoard` 上一笔，标 `is_stale=true` 并记年龄；页面明示；不评估价格规则；连续 3 分钟整市场无新报价 → admin 横幅 + 一条 `SystemAlarm`（`component=quote.<market>`），恢复后自动清横幅（告警不撤回，历史留痕）。
- 请求预算：按 `rate_limit_per_s` 与批量大小（默认 50 只/请求）拆批；每轮每市场最多 `ceil(标的数/50)` 个请求。

### 4.3 轮询与行情板

- 间隔：`Settings.polling_interval_s`（默认 10 秒）。盘中线程每轮对每个市场：
  1. 用 `CalendarService` 与市场 `tz` 判断：非交易日、当前不在任何常规 session 内（含午休）→ 跳过，记 `last_skip_reason`。
  2. 组装标的：启用用户的 watchlist ∪ 持仓 ∪ 规则 target（`symbol`/`watchlist`/`position`/`thesis` 展开后的代码），按市场去重。
  3. 取价、写 `quotes_snapshot`、更新 `QuoteBoard`、发布 `QuoteUpdated`（每只一条，`ts` 用报价时刻）、记源健康。
  4. 触发盘中评估（§5）。
- `QuoteBoard`：`dict[(market, code)] -> Quote` + 每市场 `last_poll_at`/`last_error`，读写均加锁；`snapshot()` 返回副本。启动时用库里最近一笔填充。
- **行情状态五态**（D134），按标的计算并在页面上逐只显示：
  - `closed`：日历判定当前不在常规时段（含午休、周末、假日）；
  - `no_data`：开市中且从未取到过这只票的报价；
  - `stale`：有报价但年龄 > `quote_stale_s`；
  - `delayed`：源明确标了延迟（`Quote.delayed=True`）；
  - `live`：开市中、新鲜、源未标延迟。不承诺交易所级实时。
- 页面只读 `QuoteBoard`；历史图表读 `bars_daily`，不读快照历史。快照历史只用于排障与回放。
- 首页与 admin 健康页在最近 15 分钟 `notify_log` 失败数 ≥ 3 时显示横幅；横幅可点进 `/admin/health` 的通知审计，不自动重发。

### 4.4 存储、迁移与清理

迁移 `0019_quotes_and_prune.sql`：

```sql
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS open DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS prev_close DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS high DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS low DOUBLE;
CREATE INDEX IF NOT EXISTS idx_quotes_snapshot_lookup
    ON quotes_snapshot (market, code, ts);

-- 首运行与降噪
ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
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
-- 若 data_sources 里没有 sina_quote：插入该源并把路由追加到 A/HK/US 末位
```

- 写入：`INSERT OR REPLACE INTO quotes_snapshot (...)`，一批一个事务。
- `alerts.is_test=true` 的行只出现在收件箱（带「测试」徽标），不参与 `no_open_alert`、健康积压与 M4-B 复盘；可用 dismiss（原因固定为 `test`）清理，不进复盘统计。
- 清理：`sams db prune` 三个开关，默认 `--quotes-sessions 5 --task-runs-days 90 --heartbeats-keep 1000`；`--dry-run` 打印将删行数。`sams serve` 启动时执行一次、之后每天 UTC 03:30 执行（帮助里写明）；每一步的删除行数写日志与 `task_runs`（`job_name=prune`）。
- 迁移只加列/加表/追加路由，不改既有行；迁移校验沿用 0004 的 checksum。

---

## 5. 盘中规则评估

### 5.1 哪些模板可盘中评估

`core.rules.Template` 增加 `intraday: bool`，`services/rules/templates.py` 的 `TABLE` 与 `TEMPLATES` 共用这一份判据。

| 可盘中（9） | 不可盘中（5） |
|---|---|
| `stop.fixed`、`stop.pct`、`stop.trailing`、`stop.atr`、`stop.ma`、`stop.breakeven`、`tp.tiered`、`price.cross_above`、`price.cross_below` | `stop.time`、`stop.thesis_drift`、`thesis.odds_below`、`thesis.drift`、`scheduled.reevaluate` |

不可盘中的 5 个仍由盘后 job 评估；页面在规则旁标注「收盘评估」。

### 5.2 报价叠加到 Snapshot

`RuleData.read(target, as_of, position, quote=None)`：

- `quote` 存在时覆盖：`price=quote.price`、`price_on=today`、`price_age_days=0`、`day_open=quote.open`、`prev_close=quote.prev_close`。
- 日线部分（MA、ATR、止损线、目标价、持仓期最高价、赔率、财报日）照常从上一收盘读。
- `holding_high = max(日线持仓期最高价, quote.high or quote.price)`：让 trailing/breakeven 在当天创新高后立刻把线抬上去；日线部分为 `None` 时仍为 `None`（不足 `TRAILING_MIN_BARS` 时不拿现价硬凑）。
- `price.cross_*` 的「另一侧」：盘中用**上一轮报价**；当天首轮用 `prev_close`。这样穿越只报一次，不会每 10 秒重报；首轮仍保留「昨收在另一侧、现价已穿」的跳空语义。
- 报价 `is_stale` 或 `age > quote_stale_s`（默认 60 秒）：不评估价格类，返回 `Skipped(reason=...)`，不落告警。

### 5.3 评估循环

- 每轮按市场取一次启用规则（`rules.list_enabled()`），按 user 分组。
- 对每条规则的每个 target：取缓存中的日线 `Snapshot` → 叠加本轮 `Quote` → 走 `TABLE[kind]` → `run_filters` → 冷却 → 落库 → 入通知队列。
- 过滤、冷却、去重键、`no_open_alert`、免打扰、路由全部沿用 M3；不另写一套。
- **Web 新建的规则默认 `filters.no_open_alert=true`**（表单可取消）：同一只票上还有未处理告警时，先不叠加第二条。既有规则不改，避免静默改变用户已有语义。
- 告警 `payload_json` 增 `intraday: true`、`quote_ts`、`quote_source`；`alerts.stage` 仍用 `pre/hit/urgent`。盘中告警正文追加一行「报价 14:03:12 · 腾讯 · 延迟未标注」。
- 同一轮同一 `(rule_id, market, code, account_id)` 最多一条（现有冷却键）。
- 收件箱（`/alerts`）按 `(market, code)` 合并连续命中：一行显示「贵州茅台 3 条告警（2 未处理）」，展开看每条规则；`alerts` 仍一行一命中，统计口径不变。被过滤的规则在规则详情页显示原因（冷却/已有未处理/免打扰/非盘中模板）。

### 5.4 日线快照缓存与失效

- 缓存键：`(user_id, rule_id, market, code, account_id, thesis_id)`；值：日线部分 `Snapshot` 与生成时刻。
- 失效：
  - 规则的新增/修改/启停/删除 → `VersionGate.bump("rules")`；
  - 观点新建/推进/改线 → `bump("theses")`；
  - 成交录入/账户变更 → `bump("ledger")`；
  - 日线同步完成 → `bump("bars")`（盘后 job 每市场完成后）。
- 盘中线程每轮比较各版本号；变化的键整类丢弃。另设 5 分钟兜底刷新，防止漏 bump。
- 缓存只存日线部分，**不缓存报价**；报价每轮都是新的。
- 缓存未命中时才走 `RuleData.read` 的日线 IO；命中时只做报价叠加。

### 5.5 通知队列

- `NotifyService` 拆成两段，`dispatch` 保留为二者的组合（CLI/盘后同步路径行为不变）：
  - `record(message, *, user_id, channel_ids, rule_id, account_id) -> Alert`：只写 `alerts`，不发送；
  - `deliver_existing(alert_id) -> DispatchResult`：从 `alerts` 行重建 `AlertMessage`（正文在 `payload_json.extra.body`，其余字段在行与 payload 顶层），走现有选路/发送并写 `notify_log`。消息**不靠内存对象传递**，所以进程重启后仍能重发。
- 引擎的发送端口改为 `AlertSender` 的两个实现：
  - 现有 `NotifyService.dispatch`（CLI/盘后同步路径）；
  - `QueuedAlertSender`：先 `record` 落库，再把 `alert_id` 入 `queue.Queue`，立即返回「已排队」。
- `NotifyWorker` 单线程：从队列取 `alert_id` → `NotifyService.deliver_existing`。失败按现有 3 次退避；仍失败发 `NotifyFailed` 事件并在 admin 告警详情显示。
- `deliver_existing` 预留一个可注入的上下文提供者（默认 `None`）；M4-B 的「推送带最近 3 条批注」在发送时接在这里，不改变队列与路由。
- 队列有界（默认 1000）；满时阻塞 1 秒后丢弃**发送**（告警已落库）并记 `notify_log.outcome='failed'`，绝不丢告警行。
- 停机：停止事件后最多排空 10 秒；剩余项保留在库中，admin 可手动「重发」。
- 盘中线程不直接调用任何网络/语音通道；`EventBus` 同步分发的订阅者只允许轻量操作（本版只有 `QuoteUpdated` 的记账/调试订阅）。

---

## 6. Web 应用

### 6.1 结构与依赖

- 依赖新增：`fastapi`、`uvicorn[standard]`、`jinja2`、`python-multipart`；dev 增 `httpx`（TestClient）。前端资源本地 vendoring：`htmx.min.js`、`echarts.min.js`、`app.css`、`app.js`；不引 CDN，离线可用。
- `app.py` 工厂 `create_app(core, runtime) -> FastAPI`；lifespan 只负责启动/停止 `Runtime`（测试可传入已启动的 fake runtime）。
- 路由返回 HTML；局部刷新返回 HTML fragment；图表数据走 `/api/...` JSON。路由层薄，只做鉴权、表单校验与 service 调用，不写业务规则。
- 同步路由用 `def`（FastAPI 线程池），不写 `async def` 包同步 service。

### 6.2 认证、会话与 CSRF

- 登录：`POST /login`（username/password）→ `UserRepo.authenticate` → `SessionRepo.issue(ttl_days=server.session_ttl_days)`（默认 7）→ 写 Cookie `sams_session`。
- Cookie：`HttpOnly`、`SameSite=Lax`、`Path=/`；`server.tls_certfile` 非空时 `Secure`。
- 空闲超时：另发 `sams_active` 签名 Cookie（`HMAC(secret_key, ts + nonce)`），每次请求更新；超过 `server.idle_timeout_min`（默认 720）→ 吊销会话、清 Cookie、跳登录。签名用标准库 `hmac`/`secrets`，不引第三方。
- CSRF：登录时生成随机 token，放进 `sams_csrf`（HttpOnly，签名）并在每个表单渲染为隐藏字段；所有 POST 校验「表单 token == Cookie token」。失败 403。
- 登录限速：进程内按 `(client_ip, username)` 计失败，`server.login_max_failures`（默认 5）每 `login_window_min`（默认 5）清零；超限返回 429 并给等待秒数。成功登录清零。IP 取 `request.client.host`，不信任 `X-Forwarded-For`（除非将来显式配置可信代理）。
- 登出：吊销当前 token，清 Cookie。
- 安全响应头：`Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'`、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: same-origin`。
- 密码重置/禁用用户：admin 操作后 `SessionRepo.revoke_all(user_id)`；当前浏览器若被禁用，下一请求即失效。

### 6.3 页面与动作

| 路径 | 方法 | 内容 | 写操作 |
|---|---|---|---|
| `/login` `/logout` | GET/POST | 登录/登出 | 会话 |
| `/` | GET | 三市场状态、自选涨跌、今日告警计数、持仓摘要；空库显示首运行清单；通知失败横幅 | — |
| `/watchlist` | GET | 行情表（10s 局部刷新），报价时间/源/五态行情状态 | `POST /watchlist/add`、`POST /watchlist/remove` |
| `/symbol/{market}/{code}` | GET | 报价、日 K + MA20/60/止损/目标、规则、观点、近期告警 | 规则启停/跳转、观点跳转、新建规则预设 |
| `/portfolio` | GET | 账户、持仓、成本/浮盈/距止损、多币种与汇率截止日 | `POST /ledger/fill`（买卖成交录入） |
| `/theses` | GET | 观点列表/详情/时间线 | `POST /theses/create`、`POST /theses/{id}/advance` |
| `/rules` | GET | 规则列表、参数、启停、最近命中、最近被过滤原因 | `POST /rules/{id}/toggle`、`POST /rules/{id}/edit` |
| `/rules/new` | GET/POST | 新建规则：选模板 → 参数 → 严重级/冷却/通道/过滤；标的页带预设进入 | `POST /rules/new` |
| `/alerts` | GET | 按标的合并的收件箱、筛选（状态/档位/市场/日期） | — |
| `/alerts/{id}` | GET | 详情、payload、通知记录、报价时间/源 | `POST /alerts/{id}/acted`、`/dismiss`（原因必填）、`/resend` |
| `/settings` | GET | 我的资料、显示名、通知通道、免打扰、改密码 | `POST /settings/profile`、`/settings/password`、`/settings/dnd` |
| `/settings/channels` | GET/POST | 我的通道（掩码、启停、测试） | add/edit/enable/disable/probe |
| `/settings/test-alert` | POST | 端到端测试告警（标 `is_test`） | 创建并发送 |
| `/admin/users` | GET | 用户列表 | create/disable/enable/set_admin/reset_password |
| `/admin/sources` | GET | 数据源、路由、健康 | enable/disable、probe |
| `/admin/channels` | GET | 全局通道健康（掩码、启用状态） | 只读 |
| `/admin/health` | GET | HealthService + task_runs + 心跳 + 报价源 + `/healthz` 同源数据 | `POST /admin/sync`（立即同步某市场）、prune（二次确认） |
| `/healthz` | GET | JSON | — |
| `/api/symbol/{market}/{code}/bars` | GET | 图表数据（区间、复权口径随响应） | — |
| `/api/partials/quotes` | GET | 行情表 fragment | — |

- 所有列表分页（默认 50）；告警筛选：状态、档位、市场、日期范围。
- `/rules/new` 的表单字段由 `core/rules.py` 的模板契约生成（模板、参数、严重级、冷却、通道、过滤），不手写第二份参数表；保存后若标的日线不足，页面明确说「需要 N 根，当前 M 根」，不静默失败。
- `/admin/sync` 只接受配置里存在的市场，把请求交给调度线程（`Scheduler.request_run`），请求线程立即返回 202；页面显示排队/运行/结果，不新开写线程。
- 错误页：400/403/404/409/429/500；表单校验失败回填原值并在字段旁说明。`DatabaseLocked` 在 Web 内不应出现（同进程）；若出现，返回 503 并提示外部进程占库。
- `/admin/health` 的 prune 按钮只调用 `db prune` 的 service，不在请求线程里做长删除；显示最近一次结果。
- 密码修改要求当前密码，成功后吊销其他会话；admin 重置密码仍走 `/admin/users`。

### 6.4 页面数据来源

- 行情：`QuoteBoard`。
- 日线/指标/复权：`AnalysisService`（与 CLI 同一入口）。
- 持仓/币种：`PortfolioService` / `SimAccountRepo`。
- 观点：`ThesisService`。
- 规则/告警：`RuleService`/`RuleRepo`、`AlertRepo`。
- 健康：`HealthService` + `Runtime`。
- Web 不直接写 SQL；需要的新查询在 service/repo 层补方法，并带用户隔离。

---

## 7. 数据隔离与安全

- 每个请求先解析会话得到 `user_id`；所有普通页面与动作强制 `user_id == current_user.id`。URL 里的 `account_id`/`thesis_id`/`alert_id` 等一律先用仓库按 `user_id` 读取，不存在或不属于当前用户都返回 404（不泄露「存在但不是你的」）。
- admin 的跨用户只读页（用户/源/健康）使用显式的 `list_all()`/跨用户查询入口；不复用普通用户的 `list_for`，也不允许 admin 页面直接拼 SQL。admin 不 impersonate 普通用户。批注的管理读取边界归 M4-B（D139）。
- 用户输入（代码、参数、备注、原因）在服务层用现有校验；模板自动转义，`body_md`（M4-B）才需要 Markdown 渲染并做 HTML 清洗。
- 通道凭据：页面只显示掩码；编辑表单留空表示保持原值；任何日志/异常不回显明文。普通用户在 `/settings/channels` 管理自己的通道；admin 只能看全局启用状态与掩码，不替用户改凭据。
- 显示名：`display_name` 只用于展示，登录仍用 `username`；空值回退用户名；模板自动转义。
- 测试告警：走真实 `record + deliver_existing` 链，但不计入复盘/健康积压；按用户 5 分钟 1 条限速，防止拿它刷通道。
- 审计：写操作在应用日志记 `user_id`、动作、目标 id、结果；不新增审计表（M4-B 的告警动作历史另表）。
- LAN：默认 `host=127.0.0.1`；`host` 非回环且 `allow_lan=false` → 配置校验拒绝启动。启用 LAN 时启动日志打印警告与访问地址。TLS 可选；跨不可信网络必须由用户配置证书或反代。

---

## 8. 配置

`settings.example.toml` 增：

```toml
[server]
host = "127.0.0.1"          # LAN 共享改 0.0.0.0，并显式 allow_lan = true
port = 8765
allow_lan = false
session_ttl_days = 7
idle_timeout_min = 720
login_max_failures = 5
login_window_min = 5
tls_certfile = ""           # 留空 = HTTP
tls_keyfile = ""
display_tz = "Asia/Shanghai"  # 页面「今天」与 M4-B 复盘窗口的边界；行情时间仍按市场时区

[general]
quote_stale_s = 60          # 报价超过这么久未更新 ⇒ stale，不评估价格类
```

- 校验：端口 1–65535；TTL/闲置/限速为正；`tls_certfile` 与 `tls_keyfile` 要么都空要么都存在；非回环 + `allow_lan=false` 拒绝；`display_tz` 必须能被 `ZoneInfo` 解析。
- `polling_interval_s` 终于有消费者（盘中线程）；`scheduler_tick_s` 仍只管盘后。
- CLI `sams serve --host/--port` 可临时覆盖，但 `allow_lan` 仍须配置为 true（防止一条命令把库暴露到 LAN）。

---

## 9. 错误处理与边界

- **午休/休市**：不轮询、不评估；页面显示「休市/午休」与下一次开市时间（来自日历）。
- **集合竞价**：不轮询；页面在 9:15–9:30 显示「连续竞价 9:30 开始」，不承诺竞价告警。
- **半天市**：按日历 sessions；收盘后不再轮询。
- **报价缺失/停牌**：`status` 能读就展示；不能读且价格缺失 → 保留上一笔并标 `no_data`/`stale`，不评估；不推断停牌（U15 的取向）。
- **新库无数据**：标的页显示空图与「还没有日线，可在管理页立即同步」，相关模板显示所需根数；不把缺数据报成规则失败。
- **手动同步失败**：`task_runs` 记失败原因，页面保留错误；不阻塞盘中线程，下一拍照常轮询。
- **部分代码失败**：只影响该代码；下一轮重试；不把整批标坏。
- **写冲突**：重试 3 次；仍失败以 503/页面错误呈现，告警不丢（先落库或下一轮重算）。
- **通知通道全挂**：告警仍落库，`notify_log` 记失败/no_channel；admin 可见并可重发。
- **进程重启**：`QuoteBoard` 从库中最近一笔恢复；冷却从 `alerts` 恢复（M3 既有）；已发出的告警不重发（既有账本）。
- **调度线程卡住**：`/healthz` 报 `degraded`；`sams health` 仍按心跳文件判断；admin 页面显示心跳年龄。
- **数据库损坏/不可写**：启动失败并打印可处置的错误；运行中写失败转 503 并记日志，不静默吞。

---

## 10. 测试与验收

### 10.1 离线闸（每次提交）

- `ruff check` 全绿；分层守卫更新：`web` 与 `cli` 同级，只能向下 import `{core, storage, services}`，`services`/`storage` 不得 import `web`；新增文件自动进参数化用例。
- 单测/集成（FakeClock、FakeQuoteSource、内存 DuckDB、TestClient）：
  - 报价解析：A/HK/US 各一枚真实响应 fixture；代码回显不符整只拒绝；部分代码缺失；价格为 0/NaN 丢弃；`ts` 为源时刻；stale 判定。
  - 降级：主源超时→备源；两源都失败→保留上一笔 + stale；源健康分别落库。
  - 时段：交易日/周末/午休/半天市/非交易时段不轮询；时区与 DST 用固定时钟。
  - 轮询：批量拆分、去重 universe、快照 upsert 幂等、`QuoteUpdated` 发布、`QuoteBoard` 线程安全。
  - 盘中评估：9 个模板的命中/不命中/边界；`holding_high` 叠加；cross 首轮用昨收、后续用上一轮；stale 跳过；5 个非盘中模板不进入盘中循环。
  - 缓存：规则/观点/成交/日线 bump 后失效；未 bump 时 5 分钟兜底；缓存键含 `account_id`。
  - 通知队列：先落库再入队；worker 发送与重试；队列满不丢告警；停机排空。
  - Web：登录/登出/错误口令/限速/会话过期/空闲超时；CSRF 缺失 403；每个页面 200；用户 A 访问 B 的 account/thesis/alert/symbol 返回 404；非 admin 访问 `/admin/*` 403；admin 写操作生效；规则编辑后盘中缓存失效；告警 acted/dismiss（原因为空拒绝）；`/healthz` 字段与 degraded。
  - 规则新建：14 个模板的最小合法参数、非法参数回显、Web 新建默认 `no_open_alert`、日线不足时的说明、创建后 `VersionGate` bump。
  - 设置：本人改密码需旧密码并吊销其他会话；通道增删改/掩码/测试；DND 生效；admin 不能替用户改通道。
  - 首运行：空库首页清单；`/admin/sync` 排队、执行、失败可读；测试告警 `is_test` 不进 `no_open_alert`/健康积压/复盘；测试告警失败时页面明示哪个通道失败。
  - 收件箱：按 `(market, code)` 合并、展开后每条规则可操作、被过滤原因可见；行情五态在休市/无数据/stale/延迟下的显示。
  - 生命周期：`serve` 启动/停止线程、停机删心跳文件、uvicorn 单 worker 校验、重复启动第二个进程得到 `DatabaseLocked`（既有用例扩展）。
  - prune：三个开关、dry-run、迁移后旧库可跑、删除行数与保留边界。
- 全量离线套件必须全绿；新增模块的 layering 参数化用例数量随之增加。

### 10.2 真实运行验收（人工，真实网络）

1. 全新库：按首页清单加自选、配通道、发测试告警（收到）、在 Web 新建一条止损规则；不需要停服 CLI。
2. 在一个真实 A 股交易日，`sams serve` 挂机：自选/持仓/规则对象每 10 秒更新；页面报价时间、源与五态状态可见。
3. 构造一条价格止损规则，盘中触发一次；30 分钟冷却内不重复；同标的多条规则在收件箱合并展示；通知收到；dismiss 必须填原因。
4. 断网 3 分钟：页面标 stale，不产生价格告警；恢复后自动续上；admin 横幅出现并清除。
5. 收盘后盘后 job 照旧跑；盘中与盘后互不阻塞。
6. 重启服务：不重复推送已发告警；`QuoteBoard` 恢复最近报价；CLI 在常驻时退 3、`sams health` 退 3 且读心跳文件、`/healthz` 可达。
7. 局域网另一台设备登录、看行情、处理告警、自己配通道；用户隔离（第二个账号看不到第一个的自选/持仓/告警）。
8. Windows 或 Linux 上 `uv run sams serve` 能启动、页面可用、桌面/语音通知按平台降级（不崩溃）。
9. 迁移 0019 在旧库上幂等；`sams db prune --dry-run` 与实删行数一致；`/admin/sync` 在旧库上能把某市场日线补齐。

### 10.3 性能/容量

- 200 只标的、10 秒轮询：单轮取价 ≤ 5 秒（P95），评估 ≤ 2 秒；页面首屏 ≤ 1 秒（本机）。
- 快照 5 个交易日（200 只 × 4 小时 × 360 轮 ≈ 288k 行/日）在 DuckDB 可接受；prune 后库大小稳定。
- 不达标时先降标的数/提高间隔，不改语义。

---

## 11. 实施顺序（供写计划）

1. **运行时骨架**：`AppCore`/`ServiceContext` 拆分、`VersionGate`、`Runtime` 线程生命周期、`sams serve`、`/healthz`、单 worker 校验；测试生命周期与连接隔离。
2. **报价与快照**：迁移 0019、`Quote`/`QuoteRepo`/`quote_source_health`、腾讯与 `sina_quote` 适配器、降级、`QuoteBoard`、轮询时段判断；测试解析/降级/时段。
3. **盘中评估**：`Template.intraday`、`RuleData` 报价叠加、交叉语义、缓存与失效、`QueuedAlertSender`/`NotifyWorker`；测试 9 模板与队列。
4. **Web 只读页**：FastAPI 骨架、登录/会话/CSRF/限速、总览（含首运行清单与失败横幅）、自选、标的、持仓、观点、按标的合并的告警收件箱、`/healthz`；测试鉴权与隔离。
5. **Web 写操作**：自选增删、告警 acted/dismiss/resend、规则新建（模板表单）/启停/改参、成交录入、观点创建/推进、`/settings`（资料/密码/通道/DND/测试告警）；每次写后 bump 与审计日志。
6. **Admin 与运维**：用户、数据源/路由（启停/探针）、全局通道只读、健康页、`/admin/sync`、prune；迁移/旧库测试。
7. **加固与验收**：LAN/TLS 配置、安全头、错误页、真实交易日验收、Windows/Linux 冒烟、部署/备份文档与 README 更新。

每一步独立可测、可提交；M4-A 全部完成后才进入 M4-B。

---

## 12. 与 M4-B 的边界

M4-B 已单独设计：[批注、推送上下文与复盘闭环](./2026-10-23-m4b-annotations-review-design.md)。M4-A 只落接线点，不实现写入：

- 标的/观点/规则/告警/成交详情页预留「批注」空状态；M4-B 再填面板。
- `NotifyService.deliver_existing` 预留 `AnnotationContextProvider` 注入点（默认 `None`）；M4-B 在发送时取「接收人可见的最近 3 条」。
- 告警状态在 M4-A 仍是 `new/handled/dismissed`；`acknowledged`、动作类型、原因码与复盘统计归 M4-B。M4-A 的 `/acted` 只写 `handled` 与备注。
- 附件目录、上传入口、Markdown 渲染与下载路由归 M4-B；M4-A 不引入 `markdown-it-py`/`Pillow`。
- M4-A 的 `sams db prune` 已有 quotes/task_runs/heartbeats 三项；附件回收在 M4-B 追加。

---

## 13. 已知限制与风险

- **单节点**：DuckDB 单写者，无法多实例；将来要多机/高可用需换服务端数据库（总 spec U8）。
- **行情质量**：腾讯/Sina 的报价延迟与字段口径由源决定；页面如实显示时间与来源，不承诺交易所级实时。美股尤其可能延迟。
- **常驻即锁库**：`sams serve` 运行时 CLI 不可用（D111）；首次建 admin、迁移、停服维护仍走 CLI。产品内的管理面必须覆盖日常操作，否则会退化成「停服改配置」。
- **通知延迟**：队列与单 worker 意味着突发大量告警时发送排队；告警不丢，但到达可能晚于页面。批量 ack/静音/稍后提醒在 M4-B。
- **集合竞价不覆盖**：M4-A 只从连续竞价开始；跳空风险靠开盘第一拍（9:30）而不是 9:25 竞价结果。总 spec 的「竞价即报」已同步降级。
- **无服务安装器/在线备份**：README 给 systemd/launchd/Windows 任务计划样例；备份要求停服后复制 DB（M4-B 再加附件目录）。不把「终端关了就停」藏起来。
- **Web 与调度同生共死**：Web 崩溃会带走调度；由系统服务管理器拉起。若将来要求独立发布，需回到两进程 + API（评审记录的反转条件）。
- **Windows/Linux**：桌面/语音通知依赖平台命令，缺失时降级为不可用；核心路径不依赖 macOS。
- **测试不能替代真实交易日**：离线闸覆盖语义与边界；第 10.2 节的人工验收仍是发布门槛。

---

## 14. 总 spec 需要同步的改动

- D2：`Python + Streamlit` → `Python + FastAPI/Jinja2/HTMX`（保留 akshare/backtrader 生态理由，删掉 Streamlit）。
- 架构图表现层：`Streamlit 看板 (M4)` → `Web 看板 (M4)`。
- `web` 行：写明 `sams serve`、登录、用户隔离、admin 后台。
- M4 范围：拆成 M4-A（本文）与 M4-B（批注/推送上下文/复盘闭环），并链到本文；M4-A 的首次可用范围包含 Web 新建规则、用户设置与首运行/手动同步（D130–D132）。
- §10 验收与 §15.1 对比表里「集合竞价即报」改成「连续竞价第一拍」；竞价快照留待 U7 下一层。
- §5.2 `alerts` 增 `is_test`（测试告警不计复盘/健康积压）；`users` 增 `display_name`。
- U7：盘中快照与 `QuoteUpdated` 在 M4-A 闭合后标记已闭合（竞价部分不闭合）。
- U27：`sams db prune` 落地后标记已闭合（保留 `task_runs`/心跳/quotes 三个保留项）。
- D111 的 `sams health` 锁住屏增加 `/healthz` 提示（实现时同步）。
