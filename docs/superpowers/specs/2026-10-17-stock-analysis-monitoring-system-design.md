# 股票分析监控系统 · 设计文档

日期：2026-10-17
状态：待评审
实施范围：**M1–M3 详细设计；M4–M5 仅定义范围与关键决策**

---

## 1. 目标与非目标

### 1.1 目标

面向小团队（少量可信用户）的多市场股票分析、监控与模拟交易系统，覆盖 A 股、港股、美股。核心能力链：

```
数据采集 → 指标与估值分析 → 投资观点(Thesis) → 自动派生监控规则 → 多渠道告警
                                                    ↓
                                          批注留痕 + 复盘闭环
```

系统运行在本机常驻进程，架构上保持可平滑部署到服务器；不接券商实盘。

### 1.2 非目标

- **不做实盘交易**：无券商接口，只有模拟账本。将来若需实盘，属新立项。
- **不做机器学习黑盒预测**：所有估值与信号必须可解释到具体数据来源。
- **不做细粒度 RBAC**：只有 admin / 普通用户两种角色。
- **不做多写入高并发存储**：单进程写入，服务化拆分留到演进阶段。
- **M1–M3 不实现**：批注系统、表达式 DSL、回测、新闻公告爬虫（见 §14 范围）。

---

## 2. 关键决策记录

| # | 决策 | 理由 |
|---|---|---|
| D1 | 分层单体（方案 A），而非多服务或重型量化框架 | 单人/小团队链路最短；层边界清晰，未来可拆服务 |
| D2 | Python + Streamlit | akshare/ta/backtrader 生态现成，数据源与回测成本最低 |
| D3 | DuckDB + Parquet 存储，Polars 做指标计算 | 单文件、分析型查询快、无运维；Polars 比 pandas 快且省内存 |
| D4 | 市场数据全局共享，用户数据按 `user_id` 隔离 | 行情不重复存储；加用户零成本 |
| D5 | 跨层只走接口 + 进程内事件总线 | 事件即未来的消息队列消息，A→B 迁移成本被限制在总线实现 |
| D6 | 采集器/通知器/分析引擎均用注册表模式可插拔 | 新增实现 = 一个文件 + 注册，不改既有代码 |
| D7 | 规则参数存 JSON，`condition_kind` 字段区分实现方式 | 模板表单先行，表达式 DSL 后续接入不改表结构 |
| D8 | 调度以「市场本地时区 + 该市场当日时段」为准 | 三市场时段/节假日/夏令时不同，用系统时区必然错 |
| D9 | **反未来函数为硬约束**：盘中与回测共用同一指标实现 | 这类系统最常见的致命错误是信号失真 |
| D10 | 指数/板块作为 informative 参照数据，不做独立盘中轮询 | 避免大盘跳水时全体自选股刷屏 |
| D11 | admin 只读可见全部用户数据，通知凭据脱敏 | 熟人小团队，协助排障与互相参考价值大于隐私顾虑 |
| D12 | 批注默认仅自己可见，用户可标记共享；admin 可配置共享策略 | 保护思路独立性，同时保留协作开关 |
| D13 | 指标计算库 Polars；与 backtrader 交互时在边界处转 DataFrame | backtrader 只接受 pandas，转换点集中在一处 |

---

## 3. 总体架构

```
┌──────────────────────────────────────────────────────┐
│  表现层   Streamlit 看板 (M4)  │  CLI 工具 (M1 起)     │
├──────────────────────────────────────────────────────┤
│  用户层   认证 + 两角色权限 + admin 管理后台            │
├──────────────────────────────────────────────────────┤
│  分析层   分析引擎(接口)                                │
│           ├ 技术指标引擎 (MA/MACD/RSI/BOLL/ATR/量能)    │
│           ├ 基本面引擎 (PE/PB 分位/ROE/增速)            │
│           ├ 资金流引擎 (主力/北向)                       │
│           └ [预留] qlib 因子后端                        │
│         规则引擎 (触发 + 过滤 + 节流 → 命中事件)         │
│         估值合成 (区间/买入区/止损/目标/赔率)            │
├──────────────────────────────────────────────────────┤
│  扩展缝   事件总线 (进程内 pub/sub)                     │
│          QuoteUpdated | IndicatorUpdated | RuleHit      │
│          AlertTriggered | NotifyFailed | ThesisDrifted  │
├──────────────────────────────────────────────────────┤
│  存储层   数据仓库层 Repository (唯一读写入口)           │
│          DuckDB (结构化) + Parquet (原始行情归档)        │
├──────────────────────────────────────────────────────┤
│  数据层   采集适配器：A股 / 港股 / 美股 + 多源降级        │
├──────────────────────────────────────────────────────┤
│  调度层   交易日历驱动的按市场独立循环                    │
│  通知层   通知器(接口)：企微钉钉 / 邮件 / 桌面 / PushPlus │
└──────────────────────────────────────────────────────┘
```

### 3.1 模块划分

每个模块是独立可测的 Python 包，箭头表示唯一允许的依赖方向。

| 模块 | 职责 | 依赖 |
|---|---|---|
| `core` | 配置加载、事件总线、交易日历、通用类型、日志 | 无 |
| `storage` | 仓库层：行情/财务/用户/规则/观点/告警读写接口 | core |
| `datasource` | 采集适配器，按市场路由，多源降级，限流退避 | core |
| `analysis` | 分析引擎接口 + 指标/基本面/资金流实现 + 估值合成 | core, storage |
| `monitor` | 规则引擎 + 规则模板库 + 通知器接口与渠道实现 | core, storage, analysis |
| `trading` | 回测 + 模拟账本（M5） | core, storage, analysis |
| `scheduler` | 常驻循环、任务注册表、按市场调度 | 以上全部 |
| `web` | Streamlit 看板、登录、用户隔离、admin 后台（M4） | 以上全部 |
| `cli` | 运维入口：同步、重放、规则测试、健康检查 | 以上全部 |

**扩展性约束**（必须遵守，否则 A→B 迁移失败）：

1. 禁止跨层 import 内部实现，只允许依赖上表声明的接口。
2. 数据读写 100% 收口在 `storage`，换 PostgreSQL/TimescaleDB 只动这一层。
3. 所有跨模块通信传递事件对象，禁止模块间直接函数回调。
4. 采集器、通知器、分析引擎、规则模板、调度任务均为注册表；新增强制走注册，不接受在核心代码里加 `if market == ...` 分支。

---

## 4. 用户、权限与管理后台

### 4.1 认证

`users` 表 + bcrypt 密码哈希 + 服务端会话。无自助注册，由 admin 开号（少量用户场景下这是优点：可控）。会话密钥来自环境变量。

### 4.2 资源权限矩阵

| 资源 | 普通用户 | admin |
|---|---|---|
| 行情/财务/资金流/指数 | 登录即可读（全局共享） | 同 |
| 自选股、规则、通知渠道、告警历史、观点、模拟账本 | 仅本人读写 | **只读可见全部** |
| 通知凭据（token/webhook） | 仅本人读写，密文存储 | 只见启用状态，不见明文 |
| 批注 | 仅本人读写；标记共享后团队可见 | 只读可见全部；可配置共享策略 |
| 全局开关（扫描/板块监控）、系统健康、用户管理、通知审计 | 不可见 | 读写 |

实现方式：`storage` 的每个用户域查询强制接受 `user_id` 参数，admin 查询走显式的 `as_admin=True` 分支——隔离在仓库层兜底，不依赖上层代码自觉。

### 4.3 管理后台（admin 专属）

| 功能 | 内容 |
|---|---|
| 用户管理 | 创建/禁用、重置密码、最近登录 |
| 全局开关 | 全市场选股扫描、指数/板块盘后监控的启用与参数 |
| 系统健康 | 采集任务成功率、最近失败原因、数据新鲜度（各市场最后更新时间）、调度器心跳 |
| 通知审计 | 全局发送日志：谁、哪条规则、哪个渠道、成功/失败、失败原因 |
| 数据源配额 | 各免费接口调用频率，接近限流阈值时预警 |
| 规则忽略率 | 各用户各规则的触发/忽略统计，供 admin 提示"该关掉噪音规则"（M5 完善） |

---

## 5. 数据模型与存储

### 5.1 市场域（全局共享）

```
symbols(code, market, name, list_status, lot_size, currency, exchange, sector)
  自然键: (market, code)

bars_daily(code, market, date, open, high, low, close, volume, amount,
           turnover, adj_factor)
  自然键: (market, code, date)   幂等 upsert
  adj_factor: 复权因子，除权除息换算依据

quotes_snapshot(code, market, ts, price, pct_chg, volume, amount,
                bid1, ask1, is_stale, status)
  盘中滚动，仅保留近 5 个交易日；历史一律走 bars_daily

funding_flow(code, date, main_net_in, north_net, north_hold, margin_bal)
financials(code, market, report_date, pe_ttm, pb, roe, revenue_yoy,
           profit_yoy, dividend_yield, market_cap)
indices(index_code, market, name)
index_constituents(index_code, code, date, weight)
scan_results(date, strategy_id, universe_size, hits_json)
trading_calendar(market, date, is_trading, is_half_day, sessions_json, tz)
```

**实时数据不做长期存储**：tick/快照只服务当日监控，长期只用日线。需要分钟级监控时新增 `bars_minute` 表，仓库层接口不变。

### 5.2 用户域（按 `user_id` 隔离）

```
users(id, username, pwd_hash, is_admin, status, created_at, last_login_at)

watchlists(user_id, code, market, added_at, note)
  自然键: (user_id, market, code)

theses(id, user_id, code, market, style, status, entry_low, entry_high,
       stop_price, target_price, reward_risk, win_rate, win_rate_samples,
       created_at, updated_at, archived_at)
  status: observing|proposed|opened|tracking|hit_target|hit_stop|invalidated|archived

valuation_snapshots(id, thesis_id, date, scores_json, breakdown_json,
                    fair_low, fair_high, entry_low, entry_high, stop, target,
                    reward_risk, computed_at)
  breakdown_json 必须能追溯到具体输入数据（可解释性要求）

rules(id, user_id, name, enabled, target_type, target_ref,
      kind, condition_kind, params_json, filters_json, cooldown_min,
      severity, channel_ids, thesis_id, created_at, updated_at)
  condition_kind: 'template' | 'expression'(M5) | 'python'(不实现)
  thesis_id 为空 = 手工规则；非空 = 观点派生规则（不可删只可停用）

rule_revisions(id, rule_id, changed_by, before_json, after_json, changed_at)
  防止"跌破后偷偷下移止损线"可追溯

channels(id, user_id, kind, config_encrypted, enabled, last_ok_at)
  kind: wecom|dingtalk|email|desktop|voice|pushplus|serverchan
  Fernet 加密，密钥来自环境变量，禁止入库明文

alerts(id, user_id, rule_id, code, market, triggered_at, stage,
       payload_json, status, handled_at, handle_note)
  stage: pre|hit|urgent
  status: new|acknowledged|acted|dismissed
  dismissed 必须带 reason（喂给忽略率统计）

notify_log(id, alert_id, channel_id, ts, success, error, attempt)

sim_accounts(id, user_id, name, init_cash, currency)
sim_orders(id, account_id, code, side, qty, price, ts, fee, thesis_id)
sim_positions(account_id, code, qty, avg_cost, open_ts, last_price)
  peak_price 不入库，由开仓日起 bars_daily.high 推导（保证规则评估是纯函数）

annotations(id, user_id, target_type, target_ref, body_md, tags_json,
             attachments_json, pinned, visibility, created_at, updated_at)
annotation_revisions(id, annotation_id, user_id, before_md, after_md, edited_at)
  target_type: symbol|thesis|rule|alert|order
  visibility: private|team          （M4 实施）

thesis_events(id, thesis_id, from_status, to_status, actor_id, note, ts)
  观点状态变更时间线，供回看决策轨迹

task_runs(id, job_name, market, started_at, finished_at, status,
          rows_affected, error)
  调度任务执行记录，admin 健康页数据源
```

### 5.3 写入原则

- **幂等**：所有采集写入按自然键 upsert，重试不产生重复行。
- **陈旧标记**：采集失败时保留上次数据并置 `is_stale=true`；规则引擎遇 stale 数据**跳过不评估**。
- **归档**：`quotes_snapshot` 按日滚动清理；原始接口响应落 Parquet 保留 30 天便于回放排查。

---

## 6. 交易日历（分市场）

调度正确性的基础，独立成 `core/calendar`。

### 6.1 数据模型

`sessions_json` 用数组容纳各市场时段差异：

```
A股:  [{open:"09:30",close:"11:30"},{open:"13:00",close:"15:00"}]
港股: [{open:"09:30",close:"16:00"}]
美股: [{open:"09:30",close:"16:00"}]  (含盘前盘后另加 pre/post 段)
```

半天日（港股圣诞前夕、美股感恩节次日等）`is_half_day=true` 且 sessions 只有一段。

美股的盘前/盘后时段在 sessions 中以 `pre`/`post` 标记的独立段存在，**默认不参与盘中轮询**——常规时段之外是否轮询由规则的 `time_window` 过滤条件显式 opt-in（如"美股盘前涨跌幅>5%"），避免轮询量凭空放大 3 倍。

### 6.2 各市场来源与特殊处理

| 市场 | 权威来源 | 特殊点 |
|---|---|---|
| A 股 | akshare 官方交易日历 | 节假日调休会临时多放（国庆前后补班不调市），`chinese_calendar` 仅兜底 |
| 港股 | 交易所日历 | **台风/黑暴雨临时停市无法预知**：盘中连续返回空且非时段边界 → 判定临时休市，停止轮询并记系统日志，不发用户告警 |
| 美股 | NYSE 日历 | `zoneinfo("America/New_York")` 处理**夏令时**；含提前收市半天 |

### 6.3 刷新与兜底

- 年初批量拉全年入库；每月 1 号增量校正（覆盖临时调整）；拉取失败沿用旧数据并降 admin 系统告警。
- **日历数据缺失时默认按"是交易日"轮询**。判断错方向的代价不对称：多轮询几次无害，漏掉真实交易日有害。
- 注意 §5.3 的"宁缺勿滥"只约束**告警输出**，不约束**数据采集**——采集宁多勿漏，告警宁少勿错。

### 6.4 按市场独立调度

```
对每个市场 m:
  if not calendar.is_trading(m, today): 跳过该市场
  盘中轮询窗口 = m 本地时区下 sessions 展开
  盘后触发点 = 最后一个 session.close + m 的数据延迟偏移
```

盘后偏移：A 股 15:30–16:00（北向/资金流才齐），港股 16:30，美股收盘对应北京时间次日凌晨——美股任务按市场本地时间计算触发点、落到 UTC 时间戳调度，实际执行落在北京时间上午，避免半夜无人时失败无感知。

---

## 7. 采集层

### 7.1 适配器接口

```python
class QuoteAdapter(Protocol):
    markets: frozenset[str]
    def daily_bars(self, code, start, end) -> pl.DataFrame: ...
    def snapshots(self, codes) -> pl.DataFrame: ...
    def funding_flow(self, codes, date) -> pl.DataFrame: ...
    def financials(self, code) -> pl.DataFrame: ...
    def health(self) -> AdapterHealth: ...   # 是否限流/降级中
```

实现：`AkshareAdapter`(A 股主源)、`EastmoneyAdapter`(A 股备源 + 资金流)、`YfinanceAdapter`(美股/港股日线)、`StooqAdapter`(美股备源)。注册表按 `market → [主源, 备源...]` 路由。

### 7.2 多源降级与限流

- 主源失败 → 自动切备源，同一批数据标记 `source` 字段便于对比质量。
- 所有外部调用走统一限速器（默认令牌桶 5 req/s，可配），触发 429 时指数退避（1s/4s/16s，3 次上限）。
- 连续失败或频繁限流 → admin 系统告警（监控系统必须监控自己）。

### 7.3 批量策略

盘中快照一次请求覆盖「所有用户自选股 ∪ 启用规则涉及标的 ∪ 被引用的指数」。用去重后的并集，多用户不放大请求量——这是市场数据共享设计带来的直接收益。

---

## 8. 分析与估值引擎（M2）

### 8.1 反未来函数硬约束

- 任何指标在时点 T 的取值只能依赖 T 及之前的数据。
- **盘中与盘后/回测共用同一份实现**，不接受"两套代码凑巧算一样"。
- 单测包含未来函数检查：篡改 T+1 之后的数据，T 日指标结果必须逐位不变。
- 盘中用当日快照合成"未完成 K 线"参与计算时，必须在 `valuation_snapshots.breakdown_json` 中标记 `intraday=true`，避免把盘中未定型的日线形态当成收盘结论。

### 8.2 指标清单（M2）

MA(5/10/20/60/120/250)、EMA、MACD、RSI(6/14)、BOLL(20,2)、KDJ、ATR(14)、OBV、DMI、CCI、WR、成交量均线、换手率、**周线与月线重采样指标**（`bars_daily → pl.resample` 生成周/月 K，再算 MA5/10/20/60 与 RSI/MACD）。

周线数据不入库，由日线按需重采样——避免"日线改了周线忘改"的一致性 bug。

### 8.3 估值合成（可解释规则模板，非黑盒）

`style` 定义打分模板（内置两套：`value` 价值型、`trend` 趋势型；用户可复制改参数）：

```
分项打分（0–100，每项带 evidence 字段指明数据来源）
  技术面：均线位置/周线趋势/布林带位置/量能配合
  基本面：PE-PB 历史分位、ROE、营收与利润增速
  资金面：主力净流入方向、北向增减持
        ↓ 按 style 权重合成
合理价值区间 fair_low ~ fair_high
买入区间 entry_low ~ entry_high   （价值型：分位 <20% 且接近 fair_low；
                                    趋势型：突破周 MA20 回踩不破）
止损价 stop   （策略可选：固定/pct/ATR2/周MA20，见 §10.2）
目标价 target （fair_high 或前高，二者取近）
赔率 reward_risk = (target - price) / (price - stop)
胜率 win_rate     （M5 由回测回填；M2/M3 阶段留空并显示"未回测"）
```

约束：`win_rate_samples < 30` 时界面必须显示"样本过少"，且该值不得参与任何排序或推荐。

---

## 9. 投资观点 Thesis：决策闭环

`thesis` 是自选股、分析、监控、模拟仓四者的纽带。

```
分析引擎估值 ──→ Thesis ──┬──→ 自动派生监控规则 ──→ 告警
   区间/买入区/止损/目标      │
   赔率                      ├──→ 模拟仓关联 ──→ 持仓 vs 计划对比
   胜率(M5)                  │
                             └──→ 周复检任务 ──→ 观点漂移告警
```

### 9.1 生命周期

`observing → proposed → opened → tracking → hit_target | hit_stop | invalidated → archived`

非法状态跳转（如未建仓直接归档）在 `storage` 层拒绝。每次状态变更写 `thesis_events`（时间线，供回看"我当时为什么买"）。

### 9.2 建仓时自动派生规则

| 派生规则 | kind | 说明 |
|---|---|---|
| 进入买入区间 | `price.cross_above/below` | 提示可执行计划 |
| 止损预警三级 | `stop.*` | pre/hit/urgent，见 §10 |
| 达到目标价 | `price.cross_above` | 分批止盈提醒 |
| 赔率跌破阈值 | `thesis.odds_below` | 默认阈值 1.5 |
| 财报发布日复检 | `scheduled.reevaluate` | 事件驱动复检 |
| 观点漂移 | `thesis.drift` | 周复检偏离超一档 |

派生规则参数改动同步回写 thesis 并留 `rule_revisions`；`thesis_id` 非空的规则**不可删除，只能停用**，避免"悄悄关掉止损提醒"。

### 9.3 周复检

对 `opened/tracking` 状态观点每周重算估值快照，与建仓时快照对比：分项打分下滑超过一档 → 发 `ThesisDrifted` 事件 → 推"观点漂移，请复核"。**系统只提示，不替用户决定平仓。**

---

## 10. 监控规则体系（M3）

### 10.1 规则结构

```
规则 = 对象 + 触发条件 + 过滤条件 + 节流 + 严重级别 + 渠道
```

**过滤条件**（降噪主力，全部为可组合开关）：

- `market_rel`：相对指数强弱，如"跌幅>3% 且 沪深300 跌幅<1%"才算个股问题
- `time_window`：开盘前 5 分钟不推价格类（集合竞价噪音）
- `data_freshness`：数据 stale 则跳过
- `exclude_halt`：停牌不推
- `no_open_alert`：该股票已有未处理告警时不重复推

**节流**：`cooldown_min`（默认 30），同规则同标的冷却期内不重复推送。进程重启从 `alerts` 表恢复冷却状态，不重复推送。防刷屏是监控系统体验的生死线。

**评估是纯函数**：`evaluate(event, rule, history, state) -> Hit | None`，不发通知、不碰 IO。因此可单测全覆盖，也可在历史数据上重放验证规则写得好不好（`cli rules replay`）。

### 10.2 止损止盈专项（九种，可叠加）

| kind | 定义 | 价值 |
|---|---|---|
| `stop.fixed` | 跌破绝对价 X | 来自 thesis 止损价 |
| `stop.pct` | 成本 -8% | 无 thesis 时兜底 |
| `stop.trailing` | 持仓期最高价回撤 x% | 锁盈核心；`peak_price` 由日线推导 |
| `tp.tiered` | 目标价 50%/100% 分批提醒 | 解决"涨一半不敢拿" |
| `stop.atr` | 成本 - 2×ATR14 | 高波动票不被正常噪音打掉 |
| `stop.ma` | 跌破周 MA20 / 日 MA60 | 与周K监控打通 |
| `stop.breakeven` | 浮盈超 x% 后止损上移至成本 | **不让盈利单变亏损单** |
| `stop.time` | 持仓 N 日未达预期 → 提醒复核 | 阴跌不触发价格线时的兜底 |
| `stop.thesis_drift` | 打分掉档/赔率崩塌 | 价格没跌但理由已失效 |

**三级触发**：`pre`（距止损 3%，弱渠道）→ `hit`（跌破，企微+桌面+语音）→ `urgent`（跳空低开破位，集合竞价阶段即报，不给拖延机会）。

**A 股执行约束必须写进告警文案**（否则告警误导决策）：

- T+1：文案标「今日买入，受 T+1 限制无法当日卖出」
- 跌停封板：文案标「跌停封板，可能无法按预期价执行」

**除权除息**：`bars_daily.adj_factor` 驱动成本价与止损价换算，避免每次除权误报"跌破止损"——列为 M3 强制验收用例。

### 10.3 M3 内置模板清单（约 40 个，只用日线/快照即可算）

| 类别 | 模板 | 市场 |
|---|---|---|
| 价格 | 突破/跌破指定价；N 日新高/新低；触及涨停/跌停；距涨停<x%；涨停打开；52 周高点回撤 x%；缺口回补；整数关口 | A/港/美 |
| 周K/月K | 突破/跌破 周 MA5/10/20/60；站上/跌破 周线前高前低；连续 N 周收于均线上方；月线跌破 20 月线；周线金叉/死叉；周 RSI 超买超卖 | 全 |
| 均线形态 | 上穿/下穿 日 MA5~250；多空排列形成；连续 N 日站上/跌破；长上影/下影/十字星/吞没；跳空缺口；布林上下轨突破；布林收口 | 全 |
| 指标 | MACD 金叉死叉/零轴/柱连续放大 N 根；RSI>70 或 <30；RSI 顶底背离；KDJ/CCI/WR/OBV 新高/ATR 放大 | 全 |
| 量能 | 放量 >N 日均量 x 倍；缩量地量；量价背离；换手率 >x% 或突增；成交额突破 | 全 |
| 涨跌幅 | 单日 >x%；5/20 日累计 >x%；盘中振幅 >x%；连续 N 日涨/跌 | 全 |
| 集合竞价 | 竞价涨幅 >x%；竞价量比 >N；竞价未匹配大额买盘 | A/港 |
| 持仓账户 | 浮盈/浮亏 >x%；距止损 <x%；赔率 <1.5；仓位超配/低配；组合当日盈亏；组合回撤；板块集中度 | 关联模拟仓 |
| 定时 | 盘前关注清单；收盘总结；周五观点复盘 | 全 |

M5 扩展：资金流向类（主力/北向/融资/龙虎榜/大宗）、事件公告类（解禁/除权/停复牌/评级/关键词）、估值分位类、全市场选股扫描类、参照相对类。

### 10.4 配置方式

M3 走**模板表单**：选模板 → 填参数（标的、阈值、过滤、渠道）→ 保存。规则内容统一存 `params_json`，`condition_kind='template'`。M5 加 `condition_kind='expression'`（受限表达式，如 `ma20 > ma60 and rsi < 30`）时表结构不变，仅新增一个条件求值实现。不实现 Python 插件式规则（安全与可维护性代价过高）。

---

## 11. 通知层

```python
class Notifier(Protocol):
    kind: str
    def send(self, msg: AlertMessage) -> NotifyResult: ...
    def probe(self) -> bool: ...   # admin 后台"测试发送"
```

实现：`wecom`/`dingtalk`（群机器人 webhook）、`email`（SMTP）、`desktop`（macOS `terminal-notifier`）、`voice`（macOS `say`，仅用于 `强` 级告警，与桌面通知并发）、`pushplus`/`serverchan`（个人微信，扫码关注，无官方 API 的第三方通道）。`desktop` 与 `voice` 依赖本机桌面环境，部署到服务器后自动禁用（由 `core` 按运行环境注册，不算失败）。

- **路由**：规则绑定渠道列表；未指定时降级到用户默认渠道；默认渠道也没有则写 `notify_log` 失败记录，不静默丢弃。
- **严重级别 → 渠道默认映射**（可覆盖）：`强`(止损/赔率崩塌/停牌/解禁) → 语音+企微+桌面；`中` → 企微；`弱`(盘后总结/扫描) → 邮件或仅 Web 查看。
- **重试**：失败退避重试 3 次；最终失败不影响规则引擎继续运行；`NotifyFailed` 事件让 admin 后台可见。
- **消息格式**：`[市场] 名称 代码 | 规则描述 | 当前值/阈值 | 时间 | 阶段`，附操作提示（如"距止损 2.1%，T+1 限制"）；各渠道按长度限制裁剪。
- **静默**：非交易时段/节假日由调度层控制，不写进规则本身；用户级免打扰时段独立配置。
- 凭据 Fernet 加密，日志与 admin 界面一律脱敏。

---

## 12. 调度与错误处理

单进程常驻，`scheduler` 任务注册表 + 按市场独立循环（见 §6.4）。

```
盘中: 每 N 秒(默认 10) → 批量快照 → QuoteUpdated 事件
        → analysis 增量算指标 → monitor 规则评估(含过滤/节流)
        → RuleHit → Alert 落库 → notify
盘后(每市场独立时点): 日线增量入库 → 财务/资金流更新
        → 指标重算(收盘确认) → 观点估值重算 → [开关]全市场扫描
周任务: 观点漂移复检
```

**错误处理原则**：

- **宁缺勿滥**：数据缺口、指标算不出、依赖失败 → 一律**不触发告警**，而非可能错误地触发。误报三次，用户就会关掉通知。
- **采集侧相反**：日历不确定时按交易日轮询，宁多勿漏（见 §6.3）。
- **系统自监控**：连续采集失败、调度心跳丢失、日历刷新失败 → admin 系统级告警，与用户告警分开渠道。
- **崩溃恢复**：重启后恢复节流状态与当日已推送记录，不重复打扰。
- **任务隔离**：任一盘后任务失败不阻塞其他任务，失败原因落 `task_runs` 表供健康页展示。

---

## 13. 测试策略

| 层 | 方式 | 重点用例 |
|---|---|---|
| 规则评估 | 纯函数单测，覆盖率最高 | 阈值边界（刚好等于）、冷却期内二次命中、过滤条件组合、跨市场时段边界 |
| **反未来函数** | 专项用例 | 篡改 T+1 数据后 T 日指标逐位不变；盘中未定型 K 线不产出收盘结论 |
| 交易日历 | 单测 + 真实快照 fixture | A 股调休补班、港股半天与临时停市、美股夏令时切换日、美股提前收市 |
| 止损止盈 | 状态推导单测 | trailing 的 peak 推导正确；**除权除息后不误报跌破止损**；T+1/跌停文案分支 |
| 采集适配器 | 契约测试（录制真实响应） | 空数据、字段缺失、429 限流、停牌异常值 → 不崩且标记 stale |
| 仓库层 | 内存 DuckDB 单测 | 幂等 upsert 重跑无重复行；**用户隔离**（查 A 拿不到 B）；非法状态跳转被拒 |
| 指标/估值 | 黄金样例对比 | MA/MACD/RSI 与已知值一致；赔率公式与手算一致；分项 evidence 可追溯 |
| 观点生命周期 | 状态机测试 | 合法/非法跳转；派生规则随参数变更同步；派生规则不可删 |
| 通知器 | mock 发送 | 重试 3 次、渠道降级、脱敏、失败不影响引擎 |
| Web/admin | 冒烟 | 登录、越权访问被拒、admin 只读页可访问 |
| 回测(M5) | 小样本手算对齐 | 胜率/盈亏比统计一致 |

**不做 E2E 全链路自动化**（依赖真实行情源，不稳定且慢），改为每期结束人工走一遍验收清单。

---

## 14. 里程碑

### M1 地基（详设完成，本 spec 实施起点）

core（配置/事件总线/交易日历/类型）、storage（全部表 + 仓库层 + 用户隔离）、datasource（A 股主备源 + 美股港股日线）、cli（`sync`/`calendar check`/`health`）、users+权限+admin 后台骨架。

**验收**：`python -m cli sync --market A --code 600519` 拉取入库；重复执行无重复行；A 股全年日历与交易所一致；用户 B 查不到用户 A 的自选股；日历 fixture 单测全绿。

### M2 分析与观点（详设 §8 §9）

指标引擎（含周/月重采样）、基本面与资金流接入、估值合成、thesis CRUD 与生命周期、`cli thesis show` 文本报告、未来函数检查用例。

**验收**：任一自选股输出估值报告——合理区间、买入区、止损、目标、赔率，每个分数能点开看到数据来源。

### M3 监控告警（详设 §10 §11 §12）

40 个规则模板（含周K/月K 与止损止盈九种）、过滤条件组合、三级触发、事件总线、**五个通知渠道**（企微钉钉/邮件/桌面/语音/PushPlus）、按市场调度器、stale 处理、崩溃恢复、admin 系统健康页、**模拟账本基础**（手动录入订单与持仓，供持仓类与止损止盈规则使用；不含回测）。

**验收**：**挂机运行一个真实 A 股交易日**——自选股无漏报、无同规则 30 分钟内刷屏、非交易时段全静默、除权股无误报、重启后不重复推送；录入一笔模拟持仓后 `stop.pct`/`stop.trailing` 规则可按成本价正确评估。

### M4 范围（本 spec 不展开实施细节）

Streamlit 看板（行情/观点/持仓对比/告警历史/admin 全量只读）、**批注系统**（多态挂载、Markdown+标签+截图附件、编辑历史、默认私有可标共享、admin 可配共享策略）、**推送带上下文**（告警附该股最近 3 条批注）、**复盘闭环**（告警标记 ack/acted/dismissed，dismissed 必填原因）。

### M5 范围

backtrader 回测引擎（复用 M3 已有的模拟账本模型，不另建一套）、胜率回填（标注样本数）、止损参数横向比较（8% vs 12% vs trailing vs ATR）、表达式 DSL、资金流/事件公告/估值分位/全市场扫描/指数板块盘后监控（配置开关）、规则忽略率统计、Docker 化与服务器部署、qlib 因子后端可选接入。

---

## 15. 开源借鉴

| 项目 | 借鉴点 |
|---|---|
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | 多渠道推送统一抽象、决策仪表盘输出形态、多源降级、自选股批量导入（CSV/剪贴板）、GitHub Actions 跑盘后批量（免服务器选项） |
| [tick-stock-panel](https://github.com/topics/a-stock) | DuckDB+Polars 选型验证、异动三分类（竞价/盘中/偏移）、语音推送强告警 |
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 多视角分析师分工 + 风险层合议，对应四维打分与赔率约束 |
| [freqtrade](https://www.freqtrade.io/en/stable/strategy-customization/) | dry-run 模拟账本设计、**informative pairs** 参照标的机制（§10.1 过滤条件）、webhook 载荷规范 |
| [qlib](https://github.com/microsoft/qlib) | **PIT 数据理念 → §8.1 反未来函数硬约束**；因子/回测分层 |

不采纳：LLM 黑盒打分（保持可解释优先，M5 后可作可选增强层）。

---

## 16. 风险与应对

| 风险 | 应对 |
|---|---|
| 免费数据源限流或字段变更（akshare 频繁调整） | 多源降级 + 契约测试锁定字段 + 原始响应留 Parquet 便于回放；数据质量页可见 |
| 告警噪音导致用户关掉通知 | 过滤条件 + 节流 + 严重级别分级 + 忽略率统计反推关停建议 |
| 胜率数字被误当承诺 | 样本数强制展示、<30 标"样本过少"、不参与排序推荐 |
| 港股临时停市无法预知 | §6.2 的启发式判定 + 静默 |
| DuckDB 单写入者限制未来拆分 | 读写全收口 `storage`；A→B 时换库只动一层（D5） |
| 范围偏大导致一期做不完 | M1–M3 为最小可用闭环；先跑通再排 M4/M5 |

---

## 17. 免责声明

所有估值区间、赔率、胜率、信号与提醒均为**基于历史统计的规则化参考，不构成投资建议**。系统不接入实盘，不代用户做任何交易决策。
