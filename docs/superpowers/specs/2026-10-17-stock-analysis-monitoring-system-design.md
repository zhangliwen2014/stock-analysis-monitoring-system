# 股票分析监控系统 · 设计文档

日期：2026-10-17
状态：M1 已实现并自测通过（477 用例离线绿 + CLI 冒烟），本文档已同步 M1 落地的口径决定；M2–M3 待实施计划
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
- **不接券商持仓同步**：真实账户持仓通过手动录入或 CSV 导入形成镜像账本（D18），系统不读取也不写入券商。富途/IB 的查询接口对接属 M5 之后的独立决定，A 股个人无官方接口只能手工。
- **不做机器学习黑盒预测**：所有估值与信号必须可解释到具体数据来源。LLM 在 M5 后仅允许用于**公告/新闻的分类打标**（利空/利好/无关 + 类别），其输出不参与价格区间、赔率与胜率的计算。
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
| D14 | 增设 M0 对照期：并行部署 daily_stock_analysis 两周，用它裁剪 M3 的 40 个规则模板 | 真实使用比想象更能确定需求，可砍掉大量无用实现 |
| D15 | 通知渠道与自选股导入两块直接移植 DSA（MIT）的成熟实现，不自研 | 这两处重复造轮子性价比最低 |
| D16 | 与 DSA 并行使用而非 fork 改造：它做每日信息报告，本系统做盘中风控内核 | 其架构无常驻事件总线、无 thesis 对象、无账户风险语义，硬塞成本高于自研 |
| D17 | LLM 仅用于公告/新闻分类打标，不参与估值计算 | 补住"突发事件钝感"短板，同时不破坏可解释性原则 |
| D18 | 账本定位 = 真实券商账户的手动/CSV 录入镜像，一个用户可持多账户；持仓粒度 (account_id, code)；展示为分账户 + 跨账户按报告币种汇总 | 多账户的现实动机是"各账户分别盯止损，同时知道总暴露"；不建 mirror/sim 双模式，一套模型覆盖两种用途 |
| D19 | 币种三权分立：标的币种（存储与计算）、账户多币种现金池、报告币种（仅展示）；录入永不折算 | 一个券商账户可同时含 HKD/USD/CNY 持仓；录入时折算一次会导致汇率变动后市值错账 |
| D20 | 盈亏拆分为 price_pnl（标的币种）与 fx_pnl（汇兑），汇总单列汇兑贡献 | 否则无法解释"持仓币种涨 5% 而账户只赚 2%"，也无法判断该怪股票还是怪汇率 |
| D21 | 交易规则（T+1、涨跌停、时段、日历）绑定标的市场；账户只决定现金/佣金/保证金/汇总币种 | 账户与市场不是一一对应关系 |
| D22 | 日线只存不复权价，`adjust` 进自然键；qfq 永不落库，hfq 由 `corp_actions` 现算（§5.5） | qfq 回溯改写历史 = 用 T 之后的信息算 T 的指标，违反 D9 且回测不可复现；主键事后加维度只能重建表 |
| D23 | 成交量统一「股」（A 股源给手，适配层 ×100），成交额统一「本币元」，港美股成交额留 NULL（§5.1） | 不换算则放量/换手/成交额阈值在三个市场各差 100 倍且无处报错；臆造 `volume×close` 会让阈值看似可用实则失真 |
| D24 | 时间、DB、总线、适配器全部经 `bootstrap.Container` 注入；「读现在」只走 `Clock` port（§3.2、§7.5） | 留一处 `datetime.now()` 直读，跨日/跨月边界的用例就变成不可复现的随机失败，下一个人只会把断言改松 |
| D25 | 源健康为三态（正常/故障/未知并落库），`EmptyResponse` 既不成功也不失败（§7.4） | 停牌与区间无数据是市场事实，计入失败会烧掉失败预算、让健康的源显示「未恢复」；反之掩盖真取不到数 |
| D26 | 迁移文件名 = 不重复的四位零填充序号，已应用迁移带 SHA-256 校验和，逐文件事务（§5.6） | 改一次已应用的迁移对新库生效、对老库永久不生效，而健康页显示「已是最新」——两台机器两套 schema 是静默的 |
| D27 | `Attempt.tries` 记录实际调用次数而非配置值（§7.2） | 报告不存在的重试次数，等于让运维据此判断「源还在工作」；宁可少报 |
| D28 | 数据源分两层：**实现**在代码里（`datasource/sources.py` 的 `@source(kind)` 注册表），**配置**在库里（`data_sources` + `data_source_routes`，按市场排优先级），`sams source` 是维护入口；`config/settings.toml` 只在表为空时兜底（§7.6） | 可插拔的代价是「谁能加源」。把 import 路径交给管理后台等于给一个能读库的界面开远程代码执行；只交 `kind` + 白名单 options，加源需要发代码、加测试，而调链、停用、换优先级不用 |
| D29 | 每个适配器都必须验证响应回写的标的：东财比对 `data.code`、腾讯按请求的 symbol 取 `data` 节点、akshare 比对回写的「股票代码」，不一致整批按 `FieldDrift` 拒收（§7.1） | 拿错票不表现为字段缺失：`000001` 的曲线挂到 `600519` 名下，看起来只是一只跌得很难看的茅台，入库后事后无从查出 |
| D30 | 仓库层对 K 线做**最小**脏值守卫：`close<=0` 或日期缺失的行丢弃并记日志；非正的 `open/high/low` 与不自洽的极值（`high<low`、`high<close`、`low>close`）置 NULL 不丢行；负成交量原样存（§5.3） | 收盘 0 会把区间跌幅变成 −100%，止损规则据此打死一只好票——这种值根本不配当事实。但丢弃一根「开盘价坏了」的 K 线会让「源给脏数」和「这天没数据」在库里同形，所以只废掉坏字段 |
| D31 | 代码归一只有一个出处（`core/codes.py::canonical_code`），采集、自选清单、仓库写入路径都过它；币种例外按号段判定（港股人民币双柜台取 `80000–89999` 整个 8 字头段） | 同一只票长出两个主键，读不到数据还不报错，是最难查的「数据没进来」；双柜台只认 `startswith("80")` 会漏掉 8 万以后那批，同公司的两条曲线被当成两种币，界面上永远显示一个不存在的折价 |
| D32 | 日历的「某年已覆盖」按**首尾都铺到**判定，不按 `count(*)>0`；补齐后重新走一遍覆盖判定，且一个进程对同一个年份最多问来源一次（§6.3） | 来源给半截时，下半年在库里一行都没有，而「已覆盖 + 无行 = 休市」会把 Jul~Dec 全判成休市——监控静默停摆半年。反过来，未覆盖又每次都问来源，`next_trading_day` 连着问 30 天就是打 30 次 |
| D33 | `sams health` 除了行数，还按市场报「最后一根 K 线的日期、成交额覆盖率、负量行数」，并在 `corp_actions` 为空时明说库存的是裸价（§7.6） | 行数回答不了「现在能不能拿这些数据算规则」：30000 行可能是 300 只票十年，也可能是三天前停更的三只。字段缺失不是错误，但依赖它的规则永不触发，而只有这一屏会说 |
| D34 | **一条因子公式吃所有公司行为**：`ex_ref = (前收 − 每股现金 + 每股新股价 ×(股数乘数 − 1)) / 股数乘数`，`factor = 前收 / ex_ref`，`hfq_t = 裸价_t × ∏(除权日 ≤ t 的 factor)`；同日多件送转按「新增比例相加」合成乘数（10送3转2 = 1.5×），源口径的「每 10 股」在解析层换算成每股（§5.5、`analysis/adjust.py`） | 交易所的除权参考价本来就是这一个式子。分开写「分红因子」「送转因子」会漏项，而漏一项的序列照样单调、图上看不出问题；单位差一个数量级的因子会一路乘进所有指标 |
| D35 | 因子只在**读取层**现算，库里只存裸价与 `corp_actions` 事实行，不落 `close_hfq`（D22 的延续） | 除权记录是会补的（今天同步漏了一条，下周源给了）。存算出来的序列等于把旧因子的错误永久留在库里，而重算只是读取时的乘法 |
| D36 | `corp_actions_coverage` 把「这只票从没分红」与「从没查过这只票」分开记：`full_history` 的源确认到今天（今天越过 `covered_to` 即失效），按区间拉的源只确认请求过的区间。区间收益在拿不到覆盖确认时拒绝出数（§5.1） | `corp_actions` 里 0 行有两种含义，而裸价区间涨跌在第二种情况下是错数。不加这一层，「没同步过」与「没有除权」在库里同形，读的人只能靠猜 |
| D37 | A 股证据守卫：K 线跨某天的裸价跳变超过该板块最宽涨跌幅上限 ×1.5，而 `corp_actions` 没有对应记录 ⇒ 拒绝出数（不做「猜一个因子补上」） | A 股有涨跌停这个硬边界，跳变超出它只能是除权或坏数据，两种都不该给出一个看起来正常的涨跌。港股/美股没有这个边界，所以那边靠 D36/D39 |
| D38 | 数据源按**能力**路由（`daily_bars` / `corp_actions` 两条独立链，能力由方法是否存在推出而非自报），缺能力的源显式跳过并进 `skipped` 输出；备源计数也按能力数（§7.1、`sams health`、`sams source`） | 静默跳过的症状是「这只票从没分过红」，而裸价跨除权日的涨跌是一个看起来完全正常的数。把「仅公司行为」的源算成日线备源，健康页会在市场真失明时说一切正常，管理后台也会允许摘掉最后一个能取价格的源 |
| D39 | 发生过但折不出数的公司行为记 `kind=unknown`：三个数值列必须全空、`note` 必须带源给的原话，读取层见到它**拒绝**出数而不是忽略 | 港股的实物分派（分美团股、分阅文股）、红利认股权证这类既不是每股现金也不是股数乘数。硬解析一个数会把不确定的东西乘进序列；丢掉它则等于「这天没发生过」——而跨那天的涨跌是错数。拒绝是可恢复的：补上记录就有数，错数看不出来 |

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

### 3.2 装配与依赖方向（M1 实现）

M1 落地的包是 `core` ← `storage` / `datasource` ← `services` ← `cli`，加上唯一的装配根 `bootstrap.py`。
`analysis`/`monitor`/`trading`/`scheduler`/`web` 是 M2–M5 的目标包，M1 尚未创建；`services`
是它们在 M1 的过渡容器（accounts / watchlist / health / sync / ingest），M2 起按上表拆出去。

三条把「依赖方向」从注释变成可执行约束的做法：

- **`bootstrap.Container` 是唯一装配根**：`build_container(settings, db=…, bus=…, adapters=…, clock=…)`
  构造 DB、事件总线、注册表、全部仓库与服务。业务模块**不自己连库、不自己 new 适配器、不自己读环境**——
  否则「换存储只动一层」在第一个方便的地方就被破掉。DB 路径、限速参数、时区只从 `Settings` 来。
- **分层守卫是测试不是文档**：`tests/test_layering.py` 用 AST 解析每个模块的 import，断言依赖方向
  与上表一致、`core` 不 import 任何上层、凑齐三层的那一个模块只能是 `bootstrap.py`；同一套 AST 还按
  GATED 表管第三方库的落点（`duckdb` 只在 `storage/db.py`，网络库只在 `datasource/`，`pandas` 全库禁止）。
  这类断言唯一可靠的写法是遍历 AST，而不是 grep 字符串——「换存储只动这一层」能不能兑现，
  取决于有没有人在方便的地方顺手 `import duckdb`。
- **时间、总线、DB、适配器全部注入**：`Clock`（§7.5）、`EventBus`、`Database`、适配器字典。
  默认值指向生产实现（`SystemClock`），测试传冻结实现。留一个直读 `datetime.now()` 的角落，
  跨日/跨月边界的用例就会变成不可复现的随机失败。

`build_container` 每次调用都会往传入的 bus 上再挂一个 `SystemAlarm` 日志 sink。**同一进程内复用
同一个 bus 重复 build 会成倍告警**——CLI 是单进程一次 build，不受影响；长进程（M4 服务）必须
build 一次并把 container 传下去，而不是每个请求 rebuild。

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
| 系统健康 | 采集任务成功率、最近失败原因、数据新鲜度（各市场最后更新时间 + 字段覆盖率，D33）、降级链与单源标记、调度器心跳 |
| 通知审计 | 全局发送日志：谁、哪条规则、哪个渠道、成功/失败、失败原因 |
| 数据源维护 | 增删源、排优先级、启停、试跑（M1 是 `sams source`，§7.6；Web 后台在 M4 复用同一批 service 方法） |
| 数据源配额 | 各免费接口调用频率，接近限流阈值时预警 |
| 规则忽略率 | 各用户各规则的触发/忽略统计，供 admin 提示"该关掉噪音规则"（M5 完善） |

---

## 5. 数据模型与存储

### 5.1 市场域（全局共享）

```
symbols(code, market, name, list_status, lot_size, currency, exchange, sector)
  自然键: (market, code)

bars_daily(code, market, date, adjust, open, high, low, close, volume, amount,
           source, updated_at)
  自然键: (market, code, date, adjust)   幂等 upsert
  adjust: M1 只写 'raw'（不复权价）。列放进主键是刻意的：DuckDB 不能 ALTER 主键，
          等 M2/M3 跑出指标再补复权维度只能重建表重灌，而那时"哪条序列用哪套口径算的"已无从考证
  volume: 统一为「股」——A 股两个源给的是「手」，适配层已 ×100 换算。
          不换算则放量倍数、换手率、成交额阈值在三个市场之间各差 100 倍且无处报错
  amount: 统一为「本币元」；港美股源不提供成交额，留 NULL（不臆造 volume×close 的近似值）
  turnover: 不入库。换手率 = volume / 流通股本，是读取层派生量；M1 不采流通股本，
            留一个没有生产者的列会被下游当成可信口径（adj_factor 同此被删）

corp_actions(market, code, ex_date, kind, cash_per_share, ratio, price, src, note)
  自然键: (market, code, ex_date, kind)
  除权除息事件原表，后复权序列的唯一推算依据（§5.5）
  kind: dividend | split | bonus | transfer | rights | unknown
  数值口径（D34，一律**每股**，源侧的「每 10 股」在解析层换算）：
    cash_per_share 每股税前现金（交易所按税前算参考价）
    ratio          除权后/除权前的股数乘数（送、转、拆合股共用它；同日多件相加而非相乘）
    price          每股新股应付现金，只有 rights 用（配股要它 + ratio 两个数才推得出参考价）
  unknown = 这件事真发生了但折不出数（港股实物分派、认股权证）：三个数值必须全空、
    note 必须带源给的原话，读取层见到它拒绝出数而不是当成没发生过（D39）
  只存「实施分配」的行：预披露/股东大会通过的行带不了除权日，把没发生的除权乘进序列
    比漏一次更难被发现
corp_actions_coverage(market, code, src, covered_from, covered_to, full_history, fetched_at)
  自然键: (market, code, src)
  「这只票的分红记录已经被这个源查过，且查到 covered_to」的凭据（D36）。
  与 corp_actions 分表是因为 0 行有两种含义：从没分过红 / 从没查过。区间收益只在这个
  凭据覆盖到区间时才出数；full_history 的源确认到今天，今天一旦越过 covered_to 就失效

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

source_health(name, markets, ok, last_error, last_ok_at, consecutive_failures, updated_at)
  落库而非驻内存：采集与 `sams health` 是两个进程，进程内的失败记录一退出就没了，
  健康检查因此永远报「一切正常」（§7.4）

data_sources(name, kind, options_json, enabled, note, created_at, updated_at)
  自然键: name（唯一）。`kind` 是代码里注册过的实现名，`name` 是运维起的别名——
  同一个 kind 可以挂两个具名源（例如两个不同限速配置的 eastmoney 账号位）
data_source_routes(market, name, priority)
  自然键: (market, name)；priority 从 1 开始、同市场内不得重复
  0007 迁移建表并按 §7.1 的现有链播种。两张表在库缺失时**读静默、写报错**：
  `sams health` 与账号命令不该因为某个库还没迁移就跑不起来，而管理命令必须报错

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

rules(id, user_id, account_id, name, enabled, target_type, target_ref,
      kind, condition_kind, params_json, filters_json, cooldown_min,
      severity, channel_ids, thesis_id, created_at, updated_at)
  account_id: 持仓/账户类规则必填（成本价与止损基准来自该账户持仓）；
              纯行情类规则为 NULL（同一规则可服务多账户）
  condition_kind: 'template' | 'expression'(M5) | 'python'(不实现)
  thesis_id 为空 = 手工规则；非空 = 观点派生规则（不可删只可停用）
  thesis 是标的层唯一判断，可被 1:N 个账户执行；账户级 override 止损参数
  （如信用账户需更早止损）写入本表 params_json 并留 rule_revisions 痕迹

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

sim_accounts(id, user_id, name, broker, market_scope, fee_model,
             margin_enabled, report_currency)
  一个用户可持多个账户（对应真实券商账户），持仓手动或 CSV 录入（D18）
  账户不持单一币种：同一券商账户可同时含港股(HKD)、美股(USD)、沪港通标的(CNY)，
  故本表不设 currency 字段，币种余额见 sim_cash_balances
  本表只声明券商、可交易市场范围、佣金模型、是否信用账户、汇总展示币种
  market_scope 仅用于校验"该账户能否下这个市场的单"，不决定交易规则

sim_cash_balances(account_id, currency, amount, updated_at)
  多币种现金池：同一账户每个币种一行余额
  自然键: (account_id, currency)

sim_orders(id, account_id, code, market, currency, side, qty, price, ts,
           fee, fee_currency, fx_rate_applied, thesis_id)
  currency 恒等于 symbols.currency（标的币种），录入与存储一律不做折算
  fx_rate_applied 仅当该笔订单伴随换汇时记录，用于现金池对账

sim_positions(account_id, code, market, currency, qty, avg_cost, open_ts,
              last_price, last_price_ts)
  粒度 (account_id, code)：同一只票在不同账户有各自独立的成本与数量
  全部以标的币种计价；peak_price 不入库，由开仓日起 bars_daily.high 推导
  （保证规则评估是纯函数）

fx_trades(id, account_id, from_ccy, to_ccy, from_amt, to_amt, rate, ts)
  账户内换汇流水；缺这张表则多币种现金池无法与券商对账

fx_rates(date, base_ccy, quote_ccy, rate, source, is_stale)
  需覆盖实际持币组合对：USD/CNY、HKD/CNY、USD/HKD、CNY/HKD 等；
  缺失时沿用最近值并置 is_stale，汇总页必须显示"汇率截至 X 日"

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

- **幂等**：所有采集写入按自然键 upsert，重试不产生重复行；重叠区间共享的日期是**替换**而非追加（`sync_daily` 连跑两遍行数不变，是 M1 的验收用例）。
- **陈旧标记**：采集失败时保留上次数据并置 `is_stale=true`；规则引擎遇 stale 数据**跳过不评估**。
- **归档**：`quotes_snapshot` 按日滚动清理；原始接口响应落 Parquet 保留 30 天便于回放排查。
- **仓库层只挡「存进去就会被当成真数用」的那几类**（M1 实现的 `bars` 写入校验，D30）：
  1. **口径混淆**——`adjust` 由仓库注入，调用方在 DataFrame 里自带 `adjust` 列直接拒绝；`date` 列必须是可解析的日期类型（普通 `DATETIME` 也拒绝，否则同一批数据在两个源之间静默变成两种口径）；`count`/`load`/`latest_date` 共用同一 `adjust` 口径，不允许一个按 raw、一个按全部。
  2. **NaN 覆盖好数据**——`is_not_null` 挡不住 NaN，pandas 侧的「缺数据」恰恰是 NaN；存成 `close=NaN` 会覆盖上一根好 K 线并污染此后全部指标。收盘价必须有限。
  3. **收盘价为 0**——它和 NaN 走同一条丢弃路径并记同一句日志。0 会被读成「今天收盘 0 元」：区间跌幅立刻 −100%，与一次真暴跌在库里没有任何区别，止损规则据此打死一只好票。
  4. **坏掉的字段不陪葬整根 K 线**——非正的 `open/high/low` 置 NULL（那是「这个字段没数据」，不是价格）；`high<low`、`high<close`、`low>close` 三种不自洽一并把 `high`/`low` 两头置空，close 与 volume 照存。丢弃整行会让「开盘价坏了」看起来像「这天没上市」。
- **原样存、不清洗的部分（有意为之）**：负成交量/负成交额照单入库。那是源侧数据质量问题，属于采集与清洗的职责；在仓库层丢弃会让「源给了脏数」和「这一天没数据」在库里长得一模一样。
- **注册视图是连接级的**：`register` 后写入失败必须 `unregister`，否则下一批数据读到上一批的残留，表现为「换个代码却拿到旧数据」。
- **用户隔离在仓库层兜底**：常规读只按 `user_id`；跨用户读必须有显式的 `list_all()` 审计入口（§4.2）。

### 5.4 多币种模型（一个账户含多币种持仓）

**币种三权分立**——混淆这三者是跨市场账本最常见的错误来源：

| 角色 | 定义 | 用在何处 |
|---|---|---|
| **标的币种** `symbols.currency` | 股票报价与结算币种（A股 CNY / 港股 HKD / 美股 USD / 沪港通港股 CNY 交收） | 行情、订单价、持仓成本、盈亏**存储与计算的唯一口径** |
| **账户现金池币种** `sim_cash_balances` | 账户按币种分行的可用余额 | 判断"该账户有无钱买这只票"，必须同币种匹配 |
| **报告币种** `sim_accounts.report_currency` | 用户选定的汇总展示币种（默认 CNY） | **只出现在展示层**，从不回写存储 |

两个「市场默认币种」推不出来的例外，币种由**代码号段**决定（`core/codes.py`，M1 已实现）：

- A 股 B 股：`900xxx` 沪市 USD、`200xxx`/`26x` 深市 HKD——按市场默认填 CNY 会让市值与盈亏差一个汇率，且没有任何一处报错。
- 港股人民币双柜台：整个 `80000–89999` 段（`80700` 腾讯-R、`83690` 小米-R、`89988` 阿里-R……）以 CNY 报价，同一公司的普通柜台 `00700` 是 HKD。**按数值区间判，不按前缀**：`startswith("80")` 会漏掉 8 万以后那批（D31）；而 `08xxx` 是 GEM 创业板、只差一个前导 0，仍是 HKD。

同样按市场决定的还有 `lot_size`：A 股 100、美股 1、**港股不假设**（每只票由 `symbols.lot_size` 逐只给出，港股一手可以是 100/200/500/1000/2000 股）。用一个市场级默认值会让「可买几手」的仓位计算整批偏倍。

四条硬规则：

1. **录入与存储永远用标的币种，不做折算**。折算只在展示层发生，每次展示按当日汇率重算。录入时折算一次并保存，是"汇率变了而市值不动"这类错账的根源。
2. **收益拆成两段显示**，否则无法解释"美股涨 5%、账户只赚 2%"：

```
price_pnl = (last_price - avg_cost) × qty        // 标的币种，反映投资本身
fx_pnl    = 本币市值 × (今日汇率/基准汇率 - 1)     // 报告币种，反映汇率贡献
持仓总变动(报告币种) = price_pnl × 基准汇率 + fx_pnl
```

分账户与跨账户汇总均给本币口径与报告币种口径两套数字，并单列 `fx_pnl` 合计。
3. **阈值类规则的币种口径必须明确**：百分比类（浮亏 >8%、单日涨跌 >5%）无币种歧义，优先采用；金额类（主力净流入 >1000 万）恒用**标的币种**，界面同时显示折算值。系统内不允许出现未标注币种的金额阈值。
4. **换汇是显式流水**（`fx_trades`），不是直接改余额字段——缺它多币种现金池无法与券商对账。M3 允许手工调整现金余额并写审计记录，`fx_trades` 完整实现排 M5。

附带影响：交易日历、时段、T+1/T+0、涨跌停由**标的所属市场**决定（§6、§10.2）；账户只决定现金、佣金、保证金与汇总币种。同一只美股在不同券商账户的成本差异来自佣金与换汇价，而非交易规则。

### 5.5 复权口径（M1 定死，不再改）

- **库里只有不复权价**（`adjust='raw'`）。原始价是唯一「不因后续事件回溯改写」的数据，存了它就能重算任何口径；反过来不行。
- **前复权永不落库**。qfq 会随每次除权回溯改写全部历史收盘价——用 T 之后的信息算 T 的指标，直接违反 D9 反未来函数；同一份 qfq 序列在两个时点取用会得到两组不同的历史收盘价，回测结果因此不可复现。需要 qfq 显示时在读取层用 `corp_actions` 现算。
- **后复权由 `corp_actions` 推导**，同样是读取层产物（M2-A 落地，D34–D39）：
  `hfq_t = raw_t × ∏(除权日 ≤ t 的换算因子)`，因子公式见 D34——所有公司行为走同一个式子，
  同日多件送转按「新增比例相加」合成乘数，源侧的「每 10 股」在解析层就换算成每股。
  实现在 `analysis/adjust.py`（因子与 hfq 序列）与 `analysis/returns.py`（区间收益），
  采集入口 `sams sync actions --market … [--code …]`。
- **因子不落库**（D35）：除权记录是会补的，存算出来的序列等于把旧因子的错误永久留在库里；
  重算只是读取时的乘法，而库里那行事实才是可核对的东西。
- **读取层会拒绝出数，且每条拒绝都带原因**：区间没有覆盖确认（D36，`corp_actions_coverage`
  把「从没分过红」与「从没查过」分开记）、A 股裸价跳变超过该板块最宽涨跌幅上限（D37，
  多半是漏记一次除权）、K 线上出现没有对应记录的除权日、遇到折不出数的 `kind=unknown`
  （D39，港股的实物分派与认股权证属于这种）。
- 价格与金额一律 DOUBLE：本系统用于分析与监控，不做账务清算，浮点误差量级远小于免费源自身的数据误差。

### 5.6 迁移与 schema 校验（M1 实现）

`storage/migrations/NNNN_描述.sql` 按文件名顺序执行，每条约定：

- **文件名必须是四位零填充、不重复的序号前缀**（`0007_add_xxx.sql`）。不补零时 `10_` 排在 `1_` 之前（依赖就此颠倒，且只在某个特定库上才炸）；序号重复时两个文件共用一条 `schema_migrations` 记录。不合规**启动即失败**，不等跑到一半再炸。
- **`schema_migrations.checksum` 挡下「改了已应用的迁移」**：0001 全是 `CREATE TABLE IF NOT EXISTS`，只记文件名的话，改一次 0001 对新库生效、对老库永久不生效，而 `sams health` 会显示「已是最新」。校验和不符 → 启动即失败；老库里 NULL 校验和的行不做洗白（补一个「向后兼容的重放校验」：把全部迁移在临时库上重放，比对 main schema 的表签名）。
- **每个迁移文件一个事务**：失败的文件不记入 `schema_migrations`，库里不留半套 schema。
- 迁移依赖的 schema 检查一律限定 `table_schema='main'`，否则别的 schema 里同名表会把它糊弄过去。

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
| 港股 | 交易所日历（`exchange_calendars` XHKG） | **台风/黑暴雨临时停市无法预知**——裁决规则见 §6.5，不能从「某个代码返回空」推断 |
| 美股 | NYSE 日历（`exchange_calendars` XNYS） | `zoneinfo("America/New_York")` 处理**夏令时**；含提前收市半天 |

### 6.5 临时休市的裁决（M1 明确的口径）

原设计写「盘中连续返回空且非时段边界 → 判定临时休市」，这条**不可实现**，理由是信号本身没有区分力：

- 个股停牌、代码错误、新股尚未有数据、源被限流——四者都表现为「返回空」，与休市在数据层长得一模一样。
- 真休市时源侧通常给的是**空区间**或 HTTP 错误，而空区间在采集层的正确解释是「这一天没有 K 线」，不是「交易所关了」。

可实现的裁决只用**市场级**证据，不用代码级证据：

1. 日历是权威：`is_trading(m, d)=false` 就不轮询，也不因为「源给了数据」而改成交易日（源可能给非交易日的脏行）。
2. 休市只由**市场级参照**（指数快照的交易所状态字段 / 交易所公告接口）与日历不一致时发现；M4 接入实时轮询后实施。
3. 发现不一致 → 记 `SystemAlarm` + 写「日历待校正」，由 §6.3 的每月 1 号增量校正覆盖回库，**不**当场改 `trading_calendar`，也不给用户发告警。
4. 代码级 `EmptyResponse` 在 M1 的语义是「无数据，非故障也非成功」（§7.4）：既不消耗失败预算，也不触发任何休市推断。

M1 已完成的部分：`trading_calendar` 表 + XHKG/XNYS 生成 + `is_trading`/`sessions` 查询 + 幂等 upsert。第 2、3 条依赖盘中轮询（M4），M1 不实现、也不假装实现。

### 6.3 刷新与兜底

- 年初批量拉全年入库；每月 1 号增量校正（覆盖临时调整）；拉取失败沿用旧数据并降 admin 系统告警。
- **日历数据缺失时默认按"是交易日"轮询**。判断错方向的代价不对称：多轮询几次无害，漏掉真实交易日有害。
- **「某年已覆盖」= 该年的行同时铺到年初与年末**（各留 14 天余量，元旦/春节/圣诞本身休市），不是 `count(*)>0`（D32）。半截日历 + 「已覆盖 + 无行 = 休市」= Jul~Dec 静默判成休市，正是 §6.3 自己写着要避免的那类失效。今年拿「今天」当上界，但下界不早于 1 月底，否则一月初的一次残缺刷新就能宣告整年已覆盖。
- **补齐成功后要重新走一遍覆盖判定**：否则同一个周末第一次问得到「交易日」（保守降级）、第二次得到「休市」——`sams calendar check` 会先把同一天打印两次且结论相反。
- **一个进程对同一年份最多问来源一次**（含「问到了但还是半截」）。`next_trading_day` 会连着问 30 天，每次都补齐就是打 30 次同一个来源；`sams calendar refresh` 是显式动作，它会作废这条登记。
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
class BarAdapter(Protocol):            # M1 落地：日线能力
    markets: frozenset[str]
    def daily_bars(self, market, code, start, end) -> pl.DataFrame: ...

class CorpActionAdapter(Protocol):     # M2-A 落地：与日线相互独立的另一种能力（D38）
    markets: frozenset[str]
    corp_actions_full_history: bool    # 决定覆盖标记怎么记（D36）
    def corp_actions(self, market, code, start, end) -> pl.DataFrame: ...

class QuoteAdapter(Protocol):          # M2–M3
    def snapshots(self, codes) -> pl.DataFrame: ...
    def funding_flow(self, codes, date) -> pl.DataFrame: ...
    def financials(self, code) -> pl.DataFrame: ...
    def health(self) -> AdapterHealth: ...   # 是否限流/降级中
```

实现（M1 范围，`datasource/sources.py` 的注册表里各有一行）：

| kind | 市场 | 在链上的位置 | 已知不提供 |
|---|---|---|---|
| `akshare` | A | 日线主 + 公司行为主 | 配股（rights，见 U16）；未实施的预案（只收「实施分配」） |
| `eastmoney` | A | 日线备 1 | 公司行为（与 akshare 同一上游，加了不算独立故障域） |
| `tencent` | A/HK/US | 日线 A 备 2、港美股备 1 | `amount`（成交额）、换手率、流通股本、公司行为 |
| `sina` | A | 日线备 3 | 单次上限 1024 根，更早的历史取不到；公司行为 |
| `yfinance` | HK/US | 日线主 + 公司行为主（Yahoo `get_actions`：分红/拆股/资本利得） | `amount` 港美股一律 NULL（Yahoo 的 Volume 是股数不是金额）；美股公司行为单源 |
| `akshare_hk` | HK | 公司行为备（东财港股分红派息，**没有日线能力**） | 日线；实物分派/认股权证折不出数 ⇒ 记 `unknown`（D39） |

注册表按 `market → [主源, 备源...]` 路由，链的内容来自库（§7.6），不是硬编码。
**一条链服务两种能力**（D38）：`daily_bars` 与 `corp_actions` 各自走同一条优先级链，但只走
真的实现了那个方法的源，缺能力的源**显式跳过并进 `skipped` 打印出来**——静默跳过的症状是
「这只票从没分过红」，而裸价跨除权日的涨跌是一个看起来完全正常的错数。备源计数与
`sams source` 的「不许弄瞎市场」判据同样按能力算：只有一个「仅公司行为」源的市场，日线仍是单源。

- `snapshots`/`funding_flow`/`financials` 属 M2–M3，M1 的 `BarAdapter` Protocol 只声明 `daily_bars(market, code, start, end)`——把未实现的成员写进 Protocol 会诱使注册图为不存在的功能返回成功。
- **没有 `StooqAdapter`**（原计划的美股备源）：本机到 stooq 的网络不通，写了也无法自测。港美股的降级目标改由腾讯 gtimg 承担——它是实测唯一同时满足「三市场覆盖」「免费、无 key、无 cookie、不看 referer 签名」的接口，U5 因此闭合。
- `EastmoneyAdapter.secid()` 负责市场前缀编号（沪 1 / 深 0 / 港 116 / 美 105）——前缀错一位会静默拿到另一只票的历史，所以解析时会**校验响应里的 `data.code` 与请求代码规范形态一致**，不一致按 `FieldDrift` 失败。同理 `normalize_bars` 的 `volume_scale`（A 股 手→股 ×100，港美股 股）在适配层完成，入库的永远是「股」。
- **「验证回写的标的」是每个源的义务，不是某个源的例外**（D29）：东财比 `data.code`，akshare 比回写的「股票代码」列，腾讯**按请求的 symbol 去 `data` 里取节点**而不是 `next(iter(data.values()))`。取错一位的后果不是报错，是把另一只票十年收盘价挂到这只票名下，而那条序列字段齐全、日期连续、看起来完全正常。新浪的响应不回写代码，所以它只能靠「取到 0 根就 `EmptyResponse`」这条兜底（§7.4）。
- `volume_scale` 在适配层完成（A 股 手→股 ×100，港美股已是股），入库的永远是「股」；两个独立源对同一只票同一天实测差 0.00015% 以内，这条换算被当作可验证的事实而不是约定。

### 7.2 多源降级与限流

- 主源失败 → 自动切备源，同一批数据标记 `source` 字段便于对比质量。
- 所有外部调用走统一限速器（默认令牌桶 5 req/s，可配），触发 429 时指数退避（1s/4s/16s，3 次上限）。
- **429 必须是 `RateLimited`，不能是笼统的 `DatasourceError`**（东财/腾讯/新浪三家一致）：路由器对前者「在本源退避后重试」，对后者「立刻换源」。限流时换源等于把备源也一起打爆，而这恰好发生在主源最需要我们保护备源的时候。
- 连续失败或频繁限流 → admin 系统告警（监控系统必须监控自己）。
- **`Attempt.tries` 是实际调用次数，不是配置值**：`FieldDrift` 一次就定性（重试也是同一份错误），把 `attempts_per_source=3` 抄进 `tries` 等于对调用方说不存在的事。CLI/健康页显示的「×N」由此才可信。
- 永久性错配在**构造注册表时**就失败（市场没有任何源可服务），而不是等到第一次采集；反之「这个源从没被这个市场路由过」绝不写进 `source_health`——那会把配置错误伪装成源故障。
- 限流器与退避一律用注入的 `Clock`（见 §7.4），测试里冻结时钟即可断言「等的正好是配置的间隔」。

### 7.3 批量策略

盘中快照一次请求覆盖「所有用户自选股 ∪ 启用规则涉及标的 ∪ 被引用的指数」。用去重后的并集，多用户不放大请求量——这是市场数据共享设计带来的直接收益。

### 7.4 源健康三态与系统告警（M1 实现）

监控系统必须监控自己，而「自己现在怎么样」必须落库（`source_health`，见 §5.1），否则进程一退出健康检查就失忆。三态：

| 状态 | `ok` | `consecutive_failures` | 含义 |
|---|---|---|---|
| 正常 | `True` | 0 | 本次取到数据 |
| 故障 | `False` | +1 | 网络/限流/字段漂移等**源侧**失败 |
| 未知 | `NULL` | 0 | 从没被调用过；或整批 `EmptyResponse`（无数据） |

- **`EmptyResponse` 既不算成功也不算故障**：停牌、区间无数据是市场事实，不是源坏了。把它计入失败会烧掉失败预算、让一个健康的源显示成「未恢复」；把它算成成功又会掩盖真的取不到数。二者都不取，`ok=NULL`、失败计数归零、不发告警。
- 状态从二态改三态需要迁移（0005）：`ok BOOLEAN NOT NULL` 存不下「未知」，Python 侧的解码分支会永远走不到，而下一个人真去落这个值时撞的是 `ConstraintException`。DuckDB 不能 ALTER 掉约束，按 0003 的方式重建表。
- **`SystemAlarm` 每次故障连击只发一次**（`consecutive_failures` 从 0 变 1 的那一刻），恢复后再失败才算新一轮。每失败一个代码发一条 = 一批停牌股刷屏。告警由 `bootstrap` 的日志 sink 消费（`logging.error`），业务模块不直接写日志——spec §12 的「core 不发通知」在系统域同样成立。
- 告警粒度是**源**，不是「源×市场」（yfinance 港美股共用一条）。港美股现在已有备源，但「这个市场此刻还有没有人接」由 `sams health` 的降级链屏回答（§7.6），所以先不拆表——见 §18 U3。

### 7.5 Clock port（时间读取的唯一入口）

`core/clock.py` 提供 `Clock`（`now()` / `today(tz)` / `sleep(s)`），`SystemClock` 为生产实现，测试注入 `FrozenClock`。

**所有**「读现在」都必须走注入的 clock：会话签发与过期（`storage/repos/sessions.py` 的 `verify` 同时比 `expires_at` 与 `created_at`，用 SQL `now()` 会让冻结时钟测不动）、吊销 `revoked_at`、告警 `ts`、限速器等待、退避计时、`source_health.last_ok_at`。留一处 `datetime.now()` 直读，测试就会在跨日/跨月边界随机失败——而那种失败复现不了，下一个人只会把断言改松。M2 的规则冷却与 M4 的调度时刻同样必须接 clock。


### 7.6 数据源管理后台（M1 实现：`sams source`）

D28 的两层在界面上长成这样：

```
sams source list | kinds                       # 链现在是什么样 / 有哪些 kind 可用
sams source add <kind> [--name N] [--opt k=v]  # 同一个实现可以挂成两个具名源
sams source update <name> [--opt ...] | enable | disable | remove
sams source route <market> <name> [priority] | unroute | move
sams source probe <market> <code> <start> <end> [--source NAME]
```

- **管理界面只交 `kind`，不交 import 路径**。`--kind` 必须是代码里注册过的名字，`--name` 才是自由起的别名，`--opt` 只能命中该实现声明过的白名单键（如 `timeout`）。一个能改库的界面如果接受任意 `module:Class`，等于给读库的凭据开了远程代码执行。
- **库里有权，配置只做兜底**：`data_sources` 表非空时链完全来自库；`config/settings.toml` 的 `[markets.*].sources` 只在表为空（库还没迁移）时生效。两套权威并存时，运维改的那一套必须说话。
- **不许把链改暗**：`disable`/`remove`/`unroute` 若会让某个已配置市场失去最后一个**能取日线**的启用源，直接拒绝（`sams source` 返回非 0 并说明怎么办）。这类拒绝在管理界面里比在采集时炸出来便宜得多。判据按能力而不是按名字数（D38）：链上还剩一个「只报公司行为」的源，市场照样拉不到价格。
- `probe` 失败也返回 0：这条命令的意义是「看它到底怎么坏的」，退出码不该是网络状况的函数。它同时打印实际用的源、取回行数、是否触发降级。
- `kinds` 每个条目带一句 note（市场、上限、不提供什么）。运维在决定「要不要把 tencent 插到 A 股链里」时，需要知道它不提供成交额，而不需要读代码。
- `sams health` 的 `[降级链]` 屏显示**本进程实际会用的那条链**并标注来源（库里的记录 / 配置文件兜底），按能力分别打 ⚠：日线只剩一个源、链上没有任何公司行为源、公司行为只有一个源（D38）。只有一个能力的源在链上标 `(仅公司行为)`，免得看的人以为它兜得住价格。每个源都健康 ≠ 有备源，这两件事在不同屏上，是专家复核时最容易看漏的一类误导。

同一屏还回答「这些数据现在能不能用」（D33）：`[数据]` 段按市场给出最后一根 K 线的日期、成交额覆盖率、负量行数，并在 `corp_actions` 为空且已有日线时明说「库存裸价，跨除权日的区间涨跌幅不可用」（§18 U12）。这些是事实陈述，不改退出码——一个还没采集过的库不该被判成不健康。

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

**节流**：`cooldown_min`（默认 30），去重键为 `(rule_id, market, code, account_id)`——同一只票在两个账户分别破止损必须各报一次，账户为 NULL 的行情类规则退化为 `(rule_id, market, code)`。进程重启从 `alerts` 表恢复冷却状态，不重复推送。防刷屏是监控系统体验的生死线。

**组合级指标按账户计算**：单票浮盈浮亏、距止损距离、仓位占比在账户内算；当日盈亏、最大回撤、板块集中度提供两个口径——分账户 + 跨账户汇总（多币种按 `fx_rates` 当日汇率折 CNY）。跨账户重复持有同一标的需单独提示（合计暴露可能被低估）。

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

**执行约束必须写进告警文案**（否则告警误导决策）。约束由**标的所属市场**决定，不由账户决定——同一账户里的 A 股仍 T+1、美股仍 T+0（见 §5.4）：

- 标的市场为 A 股且 `settlement` 为 T+1 且今日买入：文案标「今日买入，受 T+1 限制无法当日卖出」
- 标的市场为 A 股且跌停封板：文案标「跌停封板，可能无法按预期价执行」
- 账户 `margin_enabled=true`：追加维持担保比例提示（规则本身 M5）
- 金额型阈值与盈亏额一律带币种标注（标的币种 + 报告币种折算值）

**除权除息**：`corp_actions`（不是 `bars_daily.adj_factor`——该列已删，见 §5.5）驱动成本价与止损价换算，避免每次除权误报"跌破止损"——列为 M3 强制验收用例。换算后的序列在读取层现算，库里始终只有 raw。

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

M5 扩展：资金流向类（主力/北向/融资/龙虎榜/大宗）、事件公告类（解禁/除权/停复牌/评级/关键词，公告与新闻由 LLM 分类打标见 D17）、估值分位类、全市场选股扫描类、参照相对类。

**模板数量以 M0 裁剪清单为准**：上表 40 个是能力目录（证明架构撑得住），不是 M3 的实现承诺。M3 只实现被真实使用验证过的子集。

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
- **消息格式**：`[市场][账户名] 名称 代码 | 规则描述 | 当前值/阈值 | 时间 | 阶段`，账户名为空则省略该段——多账户下不带账户名的止损告警无法行动。附操作提示（如"该账户为 T+1，今日买入无法当日卖出"）；各渠道按长度限制裁剪。
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
| 多账户 | 单测 | 同一标的两账户不同成本 → 各自止损价正确且不互相覆盖；节流键含 account_id（A 触发后 B 仍报）；持仓类规则缺 account_id 被拒 |
| 多币种 | 单测（固定汇率 fixture） | 录入不折算（存储值恒为标的币种）；price_pnl 与 fx_pnl 之和等于本币总变动；汇率缺失时标 stale 并在汇总页显示截止日；未标注币种的金额阈值被拒 |
| 采集适配器 | 契约测试（录制真实响应） | 空数据、字段缺失、429 限流、停牌异常值 → 不崩且标记 stale；**回写身份不符整批拒绝**（§7.1 的 `require_echoed_code` / `parse_*_daily(..., symbol=)`：拿 `000001` 的曲线冒充 `600519` 必须在入库前挡住，事后无从查出） |
| 写入守护 | 内存 DuckDB 单测 | 非正/非有限收盘价丢弃并计数、非正 open/high/low 置空、OHLC 不自洽时 high/low 置空（D30）；**负成交量原样保留**——它是来源给的事实，由 `sams health` 露头而不是被静默洗掉 |
| 代码归一 | 纯函数单测 | 同一只票的所有写法收敛成一种落库形态（`HK00700`/`HK.00700`/`00700.HK`/`700` → `00700`）；人民币柜台按**数值区间** 80000–89999 判，不按前缀（D31）；自选股仓库入口即归一，`add` 与 `contains` 不必由调用方对齐 |
| 日历判定 | 单测 + 注入时钟 | 「已覆盖年份的缺失日」第一次就回答休市，不等第二次；覆盖与否按**首末日期跨度**判而非行数（D32）；半截日历一个进程只问来源一次，显式 `refresh` 才算新机会 |
| 仓库层 | 内存 DuckDB 单测 | 幂等 upsert 重跑无重复行；**用户隔离**（查 A 拿不到 B）；非法状态跳转被拒 |
| 指标/估值 | 黄金样例对比 | MA/MACD/RSI 与已知值一致；赔率公式与手算一致；分项 evidence 可追溯 |
| 观点生命周期 | 状态机测试 | 合法/非法跳转；派生规则随参数变更同步；派生规则不可删 |
| 通知器 | mock 发送 | 重试 3 次、渠道降级、脱敏、失败不影响引擎 |
| Web/admin | 冒烟 | 登录、越权访问被拒、admin 只读页可访问 |
| 回测(M5) | 小样本手算对齐 | 胜率/盈亏比统计一致 |
| **分层与系统边界** | AST 扫 import（`tests/test_layering.py`） | 依赖方向与 §3.1 表一致；`core` 不 import 上层；凑齐三层的那一个模块只能是 `bootstrap.py`；第三方库落点受管——`duckdb` 只在 `storage/db.py`、`requests`/`akshare`/`yfinance`/`exchange_calendars` 只在 `datasource/`、`bcrypt` 只在 `storage/repos/users.py`、`pandas` 全库禁止 |
| **时间相关不变式** | 冻结时钟（`core/clock.py` 端口） | 会话在跨日/跨月边界不过期、`revoked_at` 取注入时刻、告警 `ts` 跟随 clock、限速等的正好是配置间隔。**禁止用 `datetime.now()` 直读**——否则这类用例会在真实日历上随机失败 |
| **对抗契约** | 四份 `tests/test_adversarial_*.py`（storage / datasource / services / caliber） | 每轮评审与测试后把「这里有坑」改写成「这里必须是这个行为」；断言只描述已成立的契约，不留 red-xfail 的漏洞存档 |

**不做 E2E 全链路自动化**（依赖真实行情源，不稳定且慢），改为每期结束人工走一遍验收清单。

---

## 14. 里程碑

### M0 对照期（与 M1 并行，不阻塞）

部署 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 到自己和小团队真实使用两周（GitHub Actions 定时 + 企微/PushPlus 推送），产出两份清单：

1. **规则裁剪清单**：把 §10.3 的 40 个模板按"我实际看了/忽略了/想要但没有"分为保留、删除、新增三类，M3 只实现保留 + 新增部分。
2. **能力缺口清单**：DSA 做不到而必需的能力（预期集中在盘中时效、止损执行约束、决策留痕），作为 M3 验收标准的补充来源。

同时按 D15 直接评估其通知模块与自选股导入实现的可移植性，能移植的不排入自研工时。

### M1 地基（详设完成，本 spec 实施起点）

core（配置/事件总线/交易日历/类型）、storage（全部表 + 仓库层 + 用户隔离）、datasource（三市场主备源 + 可插拔注册表与库内路由，D28）、cli（`sync`/`calendar check`/`health`/`source`）、users+权限+admin 后台骨架。

**验收**：`python -m cli sync --market A --code 600519` 拉取入库；重复执行无重复行；A 股全年日历与交易所一致；用户 B 查不到用户 A 的自选股；日历 fixture 单测全绿。

**M1 验收状态（2026-10-17）**：命令名为 `sams sync --market A --code 600519 --start … --end …`（`python -m cli` 已改为 console script `sams`）。重复执行无重复行、用户隔离、日历单测（XHKG/XNYS + fixture）、迁移/校验和/健康三态——**均离线全绿**（`uv run pytest -q` 670 passed，4 个 `live` 用例默认 deselect）。数据源已按 D28 做成可插拔两层（代码实现注册表 + 库内 `data_sources`/`data_source_routes` 配置），三个市场都有主源与备源，`sams source` 提供管理入口。

**真实网络已验证**（U10，本机 `/tmp/sams-live`）：US/AAPL、HK/00700、A/600519 均在主源失败后降级到腾讯 gtimg 取数成功，重复同步行数不变（28/33/33 行）——降级链与幂等不是纸面推论。系统侧的诚实性同样确认：源全挂时 `sams health` 打印 `失败×N · 未恢复` 且退出码 1，不是静默成功；而 `sams source probe` 取数失败仍返回 0，因为试跑是诊断动作，脚本要能接着问下一个源（§7.6）。

### M2 分析与观点（详设 §8 §9）

**M2-A 除权链路已完成（2026-10-18，闭合 U12）**：`corp_actions` 的 A/HK/US 采集器（akshare 分红送配、东财港股分红派息、Yahoo actions）、`corp_actions_coverage`（D36）、按能力路由的两条链（D38）、读取层 hfq 与区间收益的四条拒绝（D34/D35/D37/D39）、`sams sync actions`。`sams sync` 因此从一条命令变成一组子命令：`sams sync daily …`（原来的那条）与 `sams sync actions …`。

指标引擎（含周/月重采样）、基本面与资金流接入、估值合成、thesis CRUD 与生命周期、`cli thesis show` 文本报告、未来函数检查用例。

**验收**：任一自选股输出估值报告——合理区间、买入区、止损、目标、赔率，每个分数能点开看到数据来源。

### M3 监控告警（详设 §10 §11 §12）

规则模板引擎与止损止盈九种（模板集合按 M0 裁剪结果实现，§10.3 的 40 个为能力目录而非承诺数量）、过滤条件组合、三级触发、事件总线、**五个通知渠道**（企微钉钉/邮件/桌面/语音/PushPlus）、按市场调度器、stale 处理、崩溃恢复、admin 系统健康页、**多账户多币种账本**（一个用户多账户、账户内多币种现金池、订单与持仓手动/CSV 录入、汇率日采集与折算、盈亏拆 price_pnl/fx_pnl，供持仓类与止损止盈规则使用；不含回测与 fx_trades）。

**验收**：**挂机运行一个真实 A 股交易日**——自选股无漏报、无同规则 30 分钟内刷屏、非交易时段全静默、除权股无误报、重启后不重复推送；建两个账户（一个持 A 股 CNY、一个同时持港股 HKD 与美股 USD），同一标的两账户设不同成本 → 各自止损按自己成本正确触发、两条告警不互相吞、汇总页本币与报告币种两套数字一致且汇兑贡献单列。

### M4 范围（本 spec 不展开实施细节）

Streamlit 看板（行情/观点/持仓对比/告警历史/admin 全量只读）、**批注系统**（多态挂载、Markdown+标签+截图附件、编辑历史、默认私有可标共享、admin 可配共享策略）、**推送带上下文**（告警附该股最近 3 条批注）、**复盘闭环**（告警标记 ack/acted/dismissed，dismissed 必填原因）。

### M5 范围

backtrader 回测引擎（复用 M3 已有的模拟账本模型，不另建一套）、胜率回填（标注样本数）、止损参数横向比较（8% vs 12% vs trailing vs ATR）、表达式 DSL、资金流/事件公告（含 LLM 分类打标，D17）/估值分位/全市场扫描/指数板块盘后监控（配置开关）、规则忽略率统计、`fx_trades` 换汇流水与现金池对账、信用账户维持担保比例告警、Docker 化与服务器部署、qlib 因子后端可选接入。

---

## 15. 开源借鉴

| 项目 | 借鉴点 |
|---|---|
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | 多渠道推送统一抽象、决策仪表盘输出形态、多源降级、自选股批量导入（CSV/剪贴板）、GitHub Actions 跑盘后批量（免服务器选项） |
| [tick-stock-panel](https://github.com/topics/a-stock) | DuckDB+Polars 选型验证、异动三分类（竞价/盘中/偏移）、语音推送强告警 |
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 多视角分析师分工 + 风险层合议，对应四维打分与赔率约束 |
| [freqtrade](https://www.freqtrade.io/en/stable/strategy-customization/) | dry-run 模拟账本设计、**informative pairs** 参照标的机制（§10.1 过滤条件）、webhook 载荷规范 |
| [qlib](https://github.com/microsoft/qlib) | **PIT 数据理念 → §8.1 反未来函数硬约束**；因子/回测分层 |

不采纳：LLM 黑盒打分参与估值（保持可解释优先）。LLM 仅用于公告/新闻分类打标，见 D17。

### 15.1 与 daily_stock_analysis 的定位差异

DSA 的本质是**每天跑一次的 LLM 自选股报告生成器**（定时触发 + 多源行情/新闻 + 决策仪表盘 + 多渠道推送）；本系统的本质是**常驻的实时监控与风控内核**。

| 维度 | DSA | 本系统 |
|---|---|---|
| 告警时效 | 日频（盘后一份报告） | 10s 级盘中轮询 + pre/hit/urgent 三级；跳空破位在集合竞价即报 |
| 止损止盈 | 指标类告警为主，无账户风险语义 | 九种可叠加策略、T+1 与跌停封板可执行性提示、除权不误报 |
| 决策留痕 | 各日报告相互独立，前后矛盾不提示 | thesis 生命周期、派生规则不可删、参数改动留痕、漂移复检 |
| 可解释性 | LLM 黑盒，同输入不同输出，不可复现 | 每个分数带 evidence，可追溯到输入数据 |
| 可验证性 | 无 PIT 概念，无法回测验证历史结论准确率 | 反未来函数硬约束 + 规则评估纯函数可历史重放 |
| 数据沉淀 | 拉完即分析，历史留存弱 | 日线/资金流/财务入库，可直接做研究查询 |
| 多用户 | 单用户自用配置 | 用户隔离 + admin 后台 + 凭据加密 |

### 15.2 已知不足（相对 DSA）

| 不足 | 影响 | 缓解 |
|---|---|---|
| 从零开发，无社区打磨 | M1–M3 才达到它第一天的可用体验 | M0 对照期；移植其通知与导入实现（D15） |
| 信息吞吐量弱于 LLM | 对公告、新闻、突发事件钝感 | M5 接入 LLM 分类打标（D17） |
| 数据源维护成本自担 | akshare 字段变更期间系统失效 | 多源降级 + 契约测试锁定 + 数据新鲜度页可见（§7.2） |
| 港美股覆盖度初期不足 | M1 仅日线 | M3 补齐基础，M5 扩资金流 |
| 预设策略少（2 套 vs 15 套） | 中文技术分析语境缺失 | style 模板可复制改参；M5 可选接 qlib |
| 无图片/剪贴板导入自选股 | 配置成本高 | M4 移植 DSA 导入实现 |
| 高频监控有噪音风险 | 用户可能关掉通知 | 过滤条件 + 节流 + 分级 + 忽略率统计反推（§10.1） |
| 需本机常驻 | 运维负担高于 GitHub Actions 定时 | M5 Docker 化；盘后批量可考虑迁到 CI 定时 |

### 15.3 协作定位

两者并行而非替代：DSA 负责每日信息量报告，本系统负责盘中风控与决策留痕。不 fork 改造（D16）。

---

## 16. 风险与应对

| 风险 | 应对 |
|---|---|
| 免费数据源限流或字段变更（akshare 频繁调整） | 多源降级 + 契约测试锁定字段 + 原始响应留 Parquet 便于回放；数据质量页可见 |
| 告警噪音导致用户关掉通知 | 过滤条件 + 节流 + 严重级别分级 + 忽略率统计反推关停建议 |
| 胜率数字被误当承诺 | 样本数强制展示、<30 标"样本过少"、不参与排序推荐 |
| 港股临时停市无法预知 | §6.5 的日历裁决口径（只用市场级证据，不从代码级空响应推断）+ 静默；发现不一致走 `SystemAlarm` |
| DuckDB 单写入者限制未来拆分 | 读写全收口 `storage`；A→B 时换库只动一层（D5） |
| 范围偏大导致一期做不完 | M1–M3 为最小可用闭环；先跑通再排 M4/M5 |
| M0 对照期被跳过 | 规则模板将按想象而非真实使用实现，返工概率高；若跳过 M0，M3 模板数上限锁定为 15 个 |

---

## 17. 免责声明

所有估值区间、赔率、胜率、信号与提醒均为**基于历史统计的规则化参考，不构成投资建议**。系统不接入实盘，不代用户做任何交易决策。

---

## 18. 未决口径登记表（有意不修，留待复核）

对抗测试与两轮架构/交易专家评审后，明确**不改**而记录在案的项。每条都是「知道它在这里、知道为什么先不动」，不是遗漏。

| # | 现状 | 为什么不修 | 何时必须处理 |
|---|---|---|---|
| U1 | 负成交量/负成交额原样入库（`close<=0`、非正高低、`high<low` 已在仓库层拦掉，见 D30） | 丢弃会让「源给脏数」与「这天没数据」在库里同形；清洗是采集层职责。**露头已做**：`sams health` 报「N 行成交量为负」 | M2 数据质量页决定要不要改写 |
| U2 | 停牌与日历缺口不交叉核对 | 需要 market-wide 参照（§6.5 第 2 条），M1 无盘中轮询 | M4 |
| U3 | `SystemAlarm` 粒度是**源**，不是源×市场 | 港美股确实已有备源（§7.1），但「这个市场现在还有没有人接」由 `sams health` 的降级链屏回答（§7.6），不必先拆表 | M4 按市场报告降级时 |
| U4 | ~~`corp_actions` 缺 `price`、`ratio` 口径未定义~~ **已闭合**（M2-A）：迁移 0008 补 `price`，D34 定死三个数值列的口径（每股现金 / 股数乘数 / 每股配股价），`list_for()` 的 `until` 是 PIT 读法且写进了文档与用例，同键覆盖由 (market, code, ex_date, kind) 主键承担 | 表尚无生产者；现在加过滤与字段等于给未定的口径下结论。**这一条是 U12 的前置**：没有除权日标记就没有跨除权日的涨跌拒绝 | — |
| U5 | ~~港美股只有 yfinance 单源，无降级目标~~ **已闭合**：HK/US 链为 `yfinance → tencent`，A 股链为 `akshare → eastmoney → tencent → sina`（§7.1） | 当初不补 Stooq 是因为本机到 stooq 网络不通，写了也无法自测；腾讯 gtimg 满足「免费、无 key、三市场」且实测可达 | — |
| U6 | `SAMS_SECRET_KEY` 必填但 M1 没有消费者 | 通道 Fernet 加密在 M2；先把必填做出来，避免 M2 上线时静默回退到默认密钥 | M2（消费者到位） |
| U7 | `quotes_snapshot.is_stale` 与 `task_runs` 有表无写入方 | §5.3 的陈旧标记服务盘中轮询，M1 只有盘后日线 | M4 |
| U8 | 用户域留在 DuckDB、快照留在库里 | DuckDB 单写入者约束；但 M1 是单进程 CLI，读写串行，不构成实际阻塞 | M4 起服务化时评估用户域→SQLite、快照→Parquet |
| U9 | 采集不带 `captured_at`/`ingest_run_id`，无法回放「某次运行的视角」 | 归档为 Parquet + 运行号是运维设施，M1 加它会稀释「幂等 upsert」这条主线验收 | M4 运维工具 |
| U10 | 验收项「真实网络同步 US/HK」**已验证**：`/tmp/sams-live` 上 US/AAPL、HK/00700、A/600519 三条链都跑到了降级并入库，重跑行数不变 | 此前的失败是 yfinance 单源被限流 + 本机无备源，属 U5；U5 闭合后同一条命令就通了 | — |
| U11 | 单列只能带一个时区（Polars 限制）：`tz` 以字符串存、按需转换 | 反压不过库设计；偏移量在 core 侧集中换算 | 不处理 |
| U12 | ~~裸价 + 无除权标记 = 跨除权日的区间涨跌是错的~~ **已闭合**（M2-A，D34–D39）：`sams sync actions` 采集入库、`corp_actions_coverage` 区分「没分红」与「没查过」（D36）、读取层按 D34 现算 hfq、A 股按 D37 做跳变证据守卫、折不出数的记 `unknown` 并拒绝出数（D39）。实测 601088 在 2024-07-08：裸价 −3.8137% vs 复权 +1.2605%（交易所公布 +1.26%） | 修它需要两件事同时到位：`corp_actions` 的采集器（U4 的字段语义）与「跨除权日拒绝算区间涨跌」的读取层守卫。M1 先把**事实**说出口：`sams health` 在 `corp_actions` 为空且有日线时打印「库存裸价，跨除权日的区间涨跌幅不可用」，让用它的人至少看得见这句话 | — |
| U16 | **公司行为的覆盖比日线薄**：HK 两个源（东财港股分红 + Yahoo）、US 只有 Yahoo、A 股只有 akshare 一家，且 A 股的**配股**根本没有来源（东财「分红送配详情」不含配股，另一张全市场配股表按位置重命名列，实测配股价与当日价格对不上） | 单源不是「没做备源」而是没有第二个可信免费域：巨潮（cninfo）的接口要用 `py_mini_racer` 伪造 `Accept-Enckey`，为一个备源引一个 JS 运行时 + 一段随时可能失效的逆向，成本高于收益。配股则是「宁可显式缺失」：漏记的配股会被 D37 的跳变守卫抓住并拒绝出数，而猜一个配股价会一路乘进整段序列。`sams health` 按能力标 ⚠ 公司行为单源（D38） | M3 采集器排程化时：单源失败要有告警；找到按列名给字段且能双票验证的配股来源后补 rights |
| U13 | 腾讯 `qt` 快照里的换手率、流通股本被丢掉（`amount` 同样不给）。§10.3 的换手率模板因此暂时无法启用 | M1 没有快照采集器（`quotes_snapshot` 表有头无尾，见 U7），为一个还没消费的功能去解析响应字段，是把猜测写成事实 | M2 快照采集器落地时（届时一并验 §10.3 的换手模板） |
| U14 | 腾讯日线每行第 7 个元素挂着公司行为对象（回购/派息），`parse_tencent_daily` 按前六个字段取，把它丢了 | 该对象的键名与口径未经第二只票、第二个市场验证；先解出来会造出一批来源不明的 `corp_actions` 行——比空表更糟。截断本身是**显式**的（`row[:len(FIELD_ORDER)]`），并有测试锁住「多余元素不报错也不入库」 | 字段语义已由 U4/D34 定完，作为 A 股公司行为的**第二个源**实现才有意义（见 U16：那一侧现在是单源） |
| U15 | `EmptyResponse` 让链路继续往下走，但「上一个源为什么给空」不入库（只在当次 CLI 输出里） | 停牌、代码写错、区间没数据三种情况在源侧都是空；库里存哪种都是猜 | M4 盘中轮询有市场级参照后可以判（§6.5 第 2 条） |
