# S1-C 每日收盘摘要 v0 · 设计

日期：2026-10-31
状态：已实施（v0）
上游：`docs/superpowers/specs/2026-10-31-expert-review.md`（共识第 8 条、§5.3、Phase 1）；
`docs/superpowers/specs/2026-10-31-product-platform-gap-analysis.md`（§5.3、§5.5、§6）

## 1. 要解决的问题

系统已经有日/周/月盘后任务、规则、假设证伪与六条通知通道，但用户不点开网页就不知道
今晚发生了什么。三支柱里的「监控自动化」缺一条**感知入口**：一天一条，只说变化；
没有变化也要让用户知道系统还活着。

评审给的验收标准：

- **连续 5 个交易日零操作产出摘要**；
- **源失败标「未更新」**，不得写成「无变化」。

本切片（v0）只做规则与假设这两类已有落库结果，不做公告/新闻入库，也不做「接近阈值」。

## 2. 决策

### 2.1 生成时刻：跟着盘后链，不另开时刻

摘要登记为每个市场的 `DAILY` job，排在 `rules` 之后、`calendar` 之前。到期判断沿用
`due.daily_due`，即「该市场收盘 + `post_close_min`」：

| 市场 | `post_close_min` | 摘要时刻（本地） |
| --- | --- | --- |
| A | 30 | 15:30 |
| HK | 30 | 16:30 |
| US | 60 | 摘要落在收盘后 60 分钟 |

A/HK 正好是评审说的「收盘后 30 分钟」。US 沿用配置里的 60，是为了与数据链同一拍、
并落在工作日上午；不为摘要单开一个 30 分钟的到期规则——那会让「摘要读的是当晚入库的
数」这句话多一条要维护的前提。

### 2.2 一条摘要的键：`(user_id, market, as_of)`，写一次就不再改

摘要是**快照**，不是视图。「摘要只读当日已落库告警不重算」（gap-analysis §5.5）：
同一 `(user, market, as_of)` 第二次运行返回已存在的那一行，不覆盖、不重推。
重跑 0 新增由唯一键保证，而不是靠调用方记得先查一次。

只给「在这个市场有东西的人」生成：持仓、自选、观察（`status='observing'` 的观点）
三者至少有一个落在该市场，才写这一行；否则跳过（不写空气摘要，也不推送）。

### 2.3 内容只读当日已落库告警

v0 的「变化」只有一类：**当日晚间（本地日）落库的告警**。规则与假设证伪都走
`alerts` 表，所以读一处就够；不重新求值、不读行情做二次判断。

排除四类：

- `is_test` 的测试告警（D131）；
- `market = 'SYS'` 的系统告警（它们有自己的去处）；
- **当前静音**的标的（S1-B 的单票静音是说「别再为这只票推东西」，摘要也是推送）；
- 空 `code` 的行（正常告警不会出现，防御性过滤）。

每个标的今日最多列 3 条，其余折成「另有 N 条」；同一标的的多条按触发时刻倒序。

「T-1 未处置」= 在今天之前触发、当前仍是 `new`/`acknowledged` 的告警，
**按标的最多 1 条**（取最新），且排除 `deferred_until` 仍在未来的（用户已经说「稍后」，
每天再拎出来一次等于没听他的话）。

### 2.4 三栏：持仓 / 自选 / 观察

| 栏 | 来源 |
| --- | --- |
| 持仓 | `SimOrderRepo` 当前持仓（逐账户并集，去重后按市场） |
| 自选 | `WatchlistRepo.list(user_id)` |
| 观察 | `ThesisRepo.list(user_id, status='observing')` |

同一标的只进第一栏（持仓优先，其次自选，最后观察）。有告警但不在三栏里的标的进
「其他」栏，该栏为空时不出现。只有**有标的**的栏才上屏；一栏有标的、今天却没有变化时
写「无变化」——给一个没有任何标的的用户印三行「无变化」只是噪声。

### 2.5 每天产出，但不是每天都推

行**每天都有**（验收要的是「产出摘要」）；推送有节奏：

- **前两周心跳**：`(user, market)` 的历史摘要少于 **10 条**时，每天都推，
  哪怕正文是「今日无变化」；
- 之后：有变化、或该市场本地日是**周一**、或**当晚有盘后任务失败**时才推；
- 其余日子只落行，网页上看得到，标「未推送（无变化）」。

10 条按交易日算（摘要只在交易日生成），约等于两周。这里不引入「摘要功能上线日」
配置：历史条数是唯一事实，重跑/补跑也不会把心跳期算歪。

### 2.6 源失败：说「未更新」，不说「无变化」

盘后链的结论在 `task_runs` 里。摘要读到同一 `period_key`、同一市场、`status='failed'`
或带 `error` 的任务（排除 `digest` 自己）：

- 头部数据行写「**数据未更新**（bars、valuation 失败）：今天的变化可能不全」；
- 空栏写「数据未更新，无法确认今天的变化」，而不是「无变化」；
- 这种日子**照推**（失败本身是需要知道的），不受 2.5 的「仅变化」限制。

没有任务记录不等于失败：节假日/停产不在这里下结论，数据行只写覆盖只数。
单源失败与连续失败的运维告警仍归既有的 `SystemAlerter`，摘要只做转述。

### 2.7 投递：一条，走默认文字通道

- 摘要不是告警：不写 `alerts`，不占收件箱，不进 `no_open_alert` 与健康积压。
- 只走**一条**通道：用户启用的文字通道（wecom/dingtalk/pushplus/email）里
  `is_default` 的那条；没有默认就取第一条启用的。desktop/voice 的 200/140 字节
  装不下摘要，明确不接。
- 免打扰照旧生效（摘要按 `mid` 处理）：被压制时写 `suppressed` 与原因，网页仍可读。
- 每次投递的结局写回摘要行（`sent` / `failed` / `suppressed` / `no_channel` + 一句话），
  不再另开 `notify_log`——它没有 alert 可挂。

投递走既有通知队列（`AlertQueue` 新增 `DigestJob`），调度线程只落行与入队；
队列满时把 `no_channel` 与原因写回摘要行，与告警「满了可在收件箱重发」同一条纪律。

### 2.8 实现落点

| 层 | 文件 | 责任 |
| --- | --- | --- |
| core | `core/notify.py` | `DigestMessage`、`format_headline` 认它、`DIGEST_KINDS` |
| storage | `repos/digests.py` + 迁移 0035 | 唯一键、读最新、记投递结局 |
| services | `services/digest.py` | 组装正文、心跳/推送判断、调 sender |
| scheduler | `scheduler/jobs.py` | `run_digest`，排在 `rules` 之后 |
| monitor | `monitor/router.py`、`monitor/queue.py` | `deliver_digest`、`DigestJob` |
| web | `pages/digest.py`、模板 | `/digest` 列表与详情、导航入口 |

`DigestService` 只收一个 `Callable[[str], None]`（参数是摘要 id）；队列版在
`bootstrap` 里接线，`services` 不 import `monitor`。

## 3. 数据模型（迁移 0035）

```sql
CREATE TABLE IF NOT EXISTS digests (
  id                VARCHAR PRIMARY KEY,
  user_id           VARCHAR NOT NULL,
  market            VARCHAR NOT NULL,
  as_of             DATE NOT NULL,
  title             VARCHAR NOT NULL,
  body              VARCHAR NOT NULL,
  has_changes       BOOLEAN NOT NULL DEFAULT FALSE,
  heartbeat         BOOLEAN NOT NULL DEFAULT FALSE,
  source_note       VARCHAR NOT NULL DEFAULT '',
  pushed            BOOLEAN NOT NULL DEFAULT FALSE,
  delivery_outcome  VARCHAR NOT NULL DEFAULT '',
  delivery_error    VARCHAR NOT NULL DEFAULT '',
  delivered_at      TIMESTAMP,
  created_at        TIMESTAMP NOT NULL DEFAULT now(),
  UNIQUE (user_id, market, as_of)
);
CREATE INDEX IF NOT EXISTS idx_digests_user_date ON digests(user_id, as_of DESC);
```

## 4. 正文格式

纯文本，网页与推送同一份（不再写第二套渲染）：

```
A · 2026-10-30 收盘摘要

【持仓】
· 600519 贵州茅台：R-19 止损 hit（1,392.05 / 1,400.00）— /alerts/…
· 601088 中国神华：昨日未处置 strong「…」— /alerts/…

【自选】
· 无变化

【观察】
· 无变化

数据：日线/估值/财务已更新；覆盖 持仓 2、自选 3、观察 1。
```

- 标题行带市场与交易日；
- 链接用 `settings.server.public_base_url`，没配就相对路径；
- 数字与措辞全部来自告警 payload（`value_text`/`threshold_text`），摘要不再格式化一次。

## 5. 错误处理

| 情形 | 行为 |
| --- | --- |
| 同一键重复运行 | 返回已有行，`created=False`，不重推 |
| 用户没有任何相关标的 | 不写行、不推送 |
| `task_runs` 有失败 | 正文标「数据未更新」，照推 |
| 没有可用的文字通道 | `no_channel` + 原因写回行 |
| 在免打扰窗口 | `suppressed` + 窗口原因写回行 |
| 通知队列满 | `no_channel` + 原因写回行 |
| 投递抛预期外异常 | `failed` + 异常摘要写回行；不带走调度线程 |

## 6. 测试

1. 仓库：同键第二次 `save` 返回 `created=False` 且正文不变。
2. 服务：今日告警进对应栏；`is_test`/`SYS`/静音标的排除；T-1 未处置每标的最多 1 条；
   未来 `deferred_until` 的排除。
3. 服务：无告警 → 三栏「无变化」、`has_changes=False`。
4. 服务：前 10 条每天都 `push=True`；第 11 条无变化非周一 `push=False`；周一/有变化 `True`。
5. 服务：`task_runs` 有失败 → 正文含「未更新」且不含「无变化」，`push=True`。
6. 调度：`digest` 排在 `rules` 之后；重跑 0 新增、0 重推。
7. Web：`/digest` 按交易日倒序列出最近 30 条及投递结局（含「未推送（无变化）」）；
   详情 `/digest/{id}` 印库里那一行，别人的摘要与不存在的 id 同答 404；空态给一句话。
8. 全量回归。

## 7. 非目标（v0 明确不做）

- 公告/新闻入库与关键词（S3）；
- 「接近阈值」「连续 N 期方向」这类趋势行（评审第 7 条，需要先有趋势规则）；
- 全市场逐票摘要、组合 NAV/回撤；
- 多通道群发与邮件模板改版；
- 摘要的自动重发（投递失败只在页面上可见，重发入口留给后续切片）。
