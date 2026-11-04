# S2 基本面深度设计：行业归一、同业对比、估值分位、核心业务

> 状态：设计（待实施）
> 依据：`docs/superpowers/specs/2026-10-31-expert-review.md` §3 S2、`docs/superpowers/specs/2026-10-31-product-platform-gap-analysis.md` §5.4 / §11.2 Phase 2
> 范围：A 股为主；港股/美股的行业与同业在 v0 明确「未覆盖」，不猜
> 决策：D236–D242（见 §9）

## 1. 要回答的问题

用户原话里的这半句，是 S2 的全部目标：

> 查看股票的基本面，不仅仅有基础信息，还有公司的核心业务、该核心业务在行业中的地位、行业的趋势，同类公司和上市公司的情况、估值对比等。

拆成四个可验收的问题：

1. **它靠什么赚钱？** —— 核心业务六字段，系统给草稿，用户确认。
2. **它在行业里排第几？** —— v0 只给**代理指标**：营收规模在行业内的分位，明确写「不是市占率」。
3. **同行贵不贵？** —— 3–5 家同业，中位 PE/PB/ROE，每个中位带样本数与 as-of。
4. **它自己现在贵不贵？** —— PR/PE/PB 的五年历史分位条，20/50/80 分位刻度，样本 ≥250 才出数。

验收口径（来自评审与差距分析）：

- 同业表给中位 PE/PB/ROE、样本数与 as-of；缺样本时说「无法计算」，不补零。
- 分位条给 20/50/80 分位与样本 ≥250；少于地板只出原因，不出条。
- 核心业务六字段可录入、可确认，每个值带来源与来源日期。
- 缺失统一四级：不适用 / 未披露 / 无法计算 / 未覆盖；行级「—」加悬浮原因。

## 2. 设计原则

1. **复用已有数据，不建行业表。** 差距分析已定：Phase 2「复用 `symbols.sector`/`company_profiles.industry`，不建行业表」。本设计不新增行业源、不新增行业表；只做归一与回填。
2. **不猜。** 行业口径对不上、同业不足、估值行过期、PE 为负，都明说缺失，不拿相邻行业或 0 顶上。
3. **每个数带出处。** 分位带窗口与样本数，同业带 as-of 与排除计数，核心业务带来源、来源日期、确认人。
4. **PIT 不放松。** 分位与同业 ROE 都只认「披露日 ≤ 截至日」的最新一期年报；没有披露日的期数被计数，不被静默丢掉（D61）。
5. **代理指标必须自称代理。** 行业地位用营收分位代替市占率，屏幕上写「代理指标，不是市占率」。
6. **共享事实，留痕确认。** 核心业务是公司事实，不是个人笔记：全库一份，`confirmed_by` 记录谁确认的；不做每用户一份。

## 3. 数据现状（实机库，2026-10-30）

这些数字决定了设计能落地，不是假设：

| 项 | 现状 | 设计含义 |
| --- | --- | --- |
| `company_profiles`（A） | 503 行，`industry` 全覆盖，格式 `食品饮料-饮料-白酒` | 行业与同业的现成来源 |
| `symbols.sector` | 0 行有值 | 回填的落点 |
| `valuation_daily`（A） | 502 只，2024-10-28 ~ 2026-10-30，约 490 个交易日 | 五年分位窗口内样本 ≥250，可分位；不足五年要标注 |
| `financials`（A） | 34,878 行；`revenue` 34,850 行；`market_cap` 0 行 | 营收分位可用；市值不能用 `financials.market_cap` |
| `snapshot_daily`（A） | 2026-10-30 共 5,577 行，`is_st` 197 行；`market_cap` 单位亿元 | ST 过滤与同业市值来源 |
| 三级行业组 | 152 组；组内 ≥4 家（即有 ≥3 家同行）的 49 组 | 一级、二级放宽是常见路径，不是边角 |
| 示例：贵州茅台 | `食品饮料-饮料-白酒` 6 家；PE/PB/ROE/营收齐全 | 验收主样本：5 家同业、中位可算、营收第 1 |

## 4. 数据层

### 4.1 只新增一张表：`company_business`

迁移 `0036_company_business.sql`。行业不建表；核心业务按「一票 × 一字段」存，来源与日期跟着每个字段走。

```sql
-- 0036_company_business.sql — 核心业务六字段（S2）
--
-- 键是 (market, code, field)：同一只票的六项各自带来源与日期，
-- 不用「一票一行六列」——那样一个字段的来源日期会污染另外五个。
-- 这是公司事实，不是个人笔记：不带 user_id，confirmed_by 只记录谁确认的。

CREATE TABLE IF NOT EXISTS company_business (
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    field        VARCHAR NOT NULL,
    value        VARCHAR NOT NULL,
    source       VARCHAR NOT NULL,
    as_of        DATE NOT NULL,
    confirmed_by VARCHAR,
    confirmed_at TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code, field)
);
```

- `value` 是纯文本（保留换行），最长 2000 字符；空值不落行——「没有」由缺行表达。
- `source`：`company_profiles.summary` / `computed` / `user`。`system` 不单独存在：草稿确认后保留原来源。
- `as_of` 是**来源日期**（概况抓取日 / 计算日 / 用户录入日），`confirmed_at` 是**确认时刻**。两者分开，屏幕才能写「来源：公司概况 2026-10-20；确认：alice 2026-10-31」。
- 不做编辑历史表（非目标）。最后一次写入生效，`confirmed_by` 留痕。

### 4.2 行业归一（`core/industry.py`）

只做**同一分类体系内的字符串归一**，不跨体系映射（D236）：

```python
@dataclass(frozen=True)
class IndustryPath:
    levels: tuple[str, ...]          # 至少一级
    @property
    def path(self) -> str: ...       # "食品饮料-饮料-白酒"
    def key(self, count: int) -> str: ...  # 取最后 count 级
    def match_keys(self) -> tuple[str, ...]: ...  # 三级 → 二级 → 一级

def parse_industry(raw: str | None) -> IndustryPath | None: ...
def normalize_industry(raw: str | None) -> str: ...  # 空/缺失 → ""
```

规则：

- `unicodedata.normalize("NFKC", raw)`；拆全角空格。
- 分隔符统一：`-`、`－`、`—`、`/`、`／`、`>`、`＞` 都当层级分隔。
- 去空段、去首尾空白；连续分隔符折叠。
- 超过三级取最后三级（更具体的层级在后）；不足三级按实际级数。
- **不做别名表**：申万、证监会、GICS 的字符串不互相翻译。口径对不上时同业说「未覆盖」，不说「大概同类」。
- 合法字符不限制（中文、字母、数字、括号都保留）；只做结构归一，不改写业务词。

回填：

- `CompanyProfileRepo.list_market(market)` 新增：`SELECT market, code, name, industry, fetched_at FROM company_profiles WHERE market=? ORDER BY code`。
- `SymbolRepo.set_sector(market, code, sector) -> bool`：只写 `sectors.sector` 与 `updated_at`；返回是否改变了值。
- `IndustryService.backfill(market)`：
  - 逐行归一 `profile.industry`；非空且与 `symbols.sector` 不同 → 写入。
  - 概况行业为空：**保留旧 sector**，计入 `kept_stale`；不自动清空（清空会让持仓页的行业分组凭空消失）。
  - 概况有、主档无这一行：计入 `missing_symbol`，不建主档（主档只由代码表建）。
  - 返回 `IndustryBackfill(scanned, changed, unchanged, missing_industry, kept_stale, missing_symbol)`。
  - 幂等：第二次执行 `changed=0`。
- `run_industry`：`rows_affected=changed`，`note` 写「概况 N 只，回填 M 只，未变 K 只，缺行业 X 只，主档缺行 Y 只」。
- 调度：**每日**，排在数据链（含 `profile`）之后、`spot` 之前。它不联网，读的是刚同步完的概况。
- CLI：`sams sync industry --market A` 调同一个服务，便于首次回填与验收。

### 4.3 缺失四级（`core/missing.py`）

统一词表，所有 S2 屏幕共用（D241）：

```python
NOT_APPLICABLE = "not_applicable"   # 不适用
NOT_DISCLOSED  = "not_disclosed"    # 未披露
UNCOMPUTABLE   = "uncomputable"     # 无法计算
NOT_COVERED    = "not_covered"      # 未覆盖

MISSING_LABELS = {...}
def missing_label(kind: str) -> str: ...
```

使用边界：

| 档 | 用在哪 | 例 |
| --- | --- | --- |
| 不适用 | 口径本身不成立 | PE ≤ 0 时的 PE 分位/PE 中位；PR 的 PE 支 |
| 未披露 | 源/公司没给这个数 | 没有已披露年报 ROE；概况没有公司简介；核心业务字段为空 |
| 无法计算 | 数有，但样本/口径不够 | 分位样本 <250；同业中位 n<3；行业样本 n<5 |
| 未覆盖 | 这个市场/数据种类还没接 | 港美同业；本地没有任何估值行；没有行业信息 |

模板里只通过 `missing_label(kind)` 出中文，不在模板里写第二份映射。

## 5. 估值分位条

### 5.1 分析层扩展

`analysis/valuation/percentile.py` 的 `Percentile` 加三个带默认值的字段，避免破坏现有构造：

```python
@dataclass(frozen=True)
class Percentile:
    ...
    q20: float | None = None       # 历史 20 分位对应的值
    q50: float | None = None
    q80: float | None = None
    missing_kind: str = ""         # 拒绝时填 MISSING_*；成功时为空
```

- `pr_percentile` / `metric_percentile` 在通过 250 天地板后，用线性插值算 `q20/q50/q80` 并填进读数；拒绝时三个 q 都是 None。
- 拒绝点补 `missing_kind`：
  - 窗口内没有任何估值行 → `not_covered`
  - 今天 PE 不为正 / PR 的 PE 支不成立 → `not_applicable`
  - 今天没有已披露且为正的 ROE → `not_disclosed`
  - 样本 < `MIN_SAMPLES` → `uncomputable`
  - 倍数分位没有可用正数 → `not_covered`（全是缺失）或 `uncomputable`（有行但无可用）
- `_rank` 的口径不变：不高于现在、等值算入、今天不投票。

### 5.2 读取用例

`ValuationService.percentiles(market, code, *, as_of=None, window_years=5, basis=ROE_BASIS_ANNUAL)`：

```python
@dataclass(frozen=True)
class ValuationPercentiles:
    market: str
    code: str
    as_of: date
    window_years: int
    rows: tuple[Percentile, ...]    # 固定 pr、pe_ttm、pb 三行
    coverage_note: str              # 本地序列起点与不足五年的说明
```

- 复用 `_valuation_days`、`_disclosed_periods`、`pr_percentile`、`metric_percentile`；不读日线、不算合成分、不建交易计划。
- PR 用年报口径（与 PR 分位、选股一致）；PE/PB 用各自历史。
- `coverage_note`：库里最早一行晚于 `window_from` 时写「本地估值序列从 2024-10-28 开始（约 2.0 年），窗口仍按过去五年计算，窗口内样本 490 天」；序列覆盖满五年时为空。
- 港美没有估值行时三行都 `not_covered`，不抛异常。

### 5.3 屏幕

新异步局部 `/api/partials/valuation?market=&code=`，一张卡：

```
估值位置（as-of 2026-10-30；窗口 2021-10-30 ~ 2026-10-30）

PR（PE(TTM) ÷ 已披露年报 ROE）
0 ─────────●──────────────── 100
现在 0.42 · 分位 18% · 样本 490 天
历史 20/50/80 分位 = 0.35 / 0.58 / 0.91
[PE 缺失或不为正 12 天（那一支要预估 ROE，历史里没有）；…]

PE(TTM)
0 ────────────●───────────── 100
现在 21.37 · 分位 35% · 样本 490 天
历史 20/50/80 分位 = 14.2 / 18.9 / 25.6

PB
0 ──────────────●─────────── 100
现在 6.93 · 分位 41% · 样本 490 天
历史 20/50/80 分位 = 4.1 / 6.2 / 8.8

说明：分位 = 过去五年里不高于现在的交易日占比；今天不投票；等值算「不高于」。
```

- 条用纯 HTML/CSS，不引图表库：底条 + 20/50/80 三个刻度 + 当前 marker（`left: {{ value }}%`）。
- 数值格式：PR 两位小数，PE/PB 两位小数；样本与窗口跟着每一行。
- 拒绝时不出条：显示 `{{ missing_label(kind) }}` + `reason`；若 `current` 有值，仍显示「现在 0.42」，让用户知道数有、只是分位不给。
- 悬停 `title` 放被丢弃行的分类计数（`Percentile.evidence` 已有）。
- 五年前的窗口与库里实际序列起点分开说；不要把「只有两年数据」说成「五年分位」。

## 6. 同业对比

### 6.1 服务（`services/peers.py`）

```python
MIN_PEERS = 3          # 选行业层级的地板：不含自己
MAX_PEERS = 5          # 最多摆 5 家同业
MIN_MEDIAN_SAMPLES = 3 # 中位数的地板
MIN_POSITION_SAMPLES = 5  # 行业地位代理的地板
PEER_STALE_DAYS = 30   # 估值行超过这么多天不算当期
SNAPSHOT_STALE_DAYS = 7

@dataclass(frozen=True)
class PeerRow:
    market: str
    code: str
    name: str
    is_self: bool
    market_cap: float | None       # 亿元，来自 snapshot_daily
    pe_ttm: float | None
    pb: float | None
    roe: float | None
    revenue: float | None          # 元，来自 financials
    valuation_on: date | None
    roe_period: date | None
    revenue_period: date | None
    pe_missing: str = ""           # MISSING_*
    pb_missing: str = ""
    roe_missing: str = ""
    flags: tuple[str, ...] = ()    # 行级说明：估值行超过 30 天等

@dataclass(frozen=True)
class PeerMetric:
    value: float | None
    samples: int
    as_of: date
    missing_kind: str = ""
    reason: str = ""
    excluded: int = 0              # 有值但不计入（PE≤0 等）

@dataclass(frozen=True)
class PeerComparison:
    market: str
    code: str
    as_of: date
    industry: str                  # 归一后的路径
    level_used: str                # "三级" / "二级" / "一级"
    rows: tuple[PeerRow, ...]      # 自己在前，同业按市值降序
    pe: PeerMetric
    pb: PeerMetric
    roe: PeerMetric
    position: str                  # 行业地位代理那句话
    position_kind: str = ""        # "revenue" / "market_cap" / ""
    position_missing: str = ""
    notes: tuple[str, ...] = ()    # 放宽层级、排除计数、PIT 丢弃期数
    state: str = "ok"              # ok / thin / not_covered / not_disclosed
```

### 6.2 选行业与选同业

1. **自己的行业**：`company_profiles.industry` 优先，主档 `symbols.sector` 兜底；`parse_industry` 归一。没有 → `not_disclosed`，提示「先同步公司概况」。
2. **候选池**：`company_profiles` 的全部 A 股（不是自选股池），合并 `symbols` 的名称与 `list_status`。这是同业能跨出自选范围的原因。
3. **排除**：
   - `list_status != 'L'`；名称含 `ST`（大小写不敏感）或 `退`；最新 `snapshot_daily.is_st = TRUE`。
   - 没有行业、行业无法归一。
   - 估值行距离 `as_of` 超过 `PEER_STALE_DAYS`（仍列在表里？不；先排除出候选，计数说明）。
4. **层级放宽**：按 `path.match_keys()` 从三级到一级，选第一个「不含自己 ≥ `MIN_PEERS` 家」的层级。都没有 → 用一级（或最具体可用层级）的现有家数，`state='thin'`，中位逐项拒绝为「无法计算（样本不足 n=2 < 3）」。`
5. **选 3–5 家**：按市值与自己的距离选最近的最多 5 家（`abs(log(cap/self_cap))`）；市值缺失时用营收距离；两者都缺失时按市值降序取前 5，并在 `notes` 说明。选完后按市值降序摆，自己在第一行。
6. **市值单位**：一律用 `snapshot_daily.market_cap`，单位**亿元**；不与 `valuation_daily.market_cap`（元）混用（D239）。两者只差 1e8，但混用会让选同业静默错位。
7. **PE/PB**：`valuation_daily` 在 `as_of` 之前、`PEER_STALE_DAYS` 内的最新一行；PE/PB ≤0 或缺失 → 该格 `not_applicable` / `not_disclosed`，不计入中位。
8. **ROE**：`FinancialRepo.load_as_of(basis=ROE_BASIS_ANNUAL)`，一手查回所有候选；没披露年报 → `not_disclosed`；`dropped`（无披露日的期数）写进 `notes`。
9. **营收**：同一份年报 PIT 行的 `revenue`（元）；用于营收分位与（缺市值时的）选同业。

### 6.3 中位与行业地位

- 中位只算**同业**，不算自己；每项独立：
  - `samples = 有效同业数`；`samples < 3` → `value=None, missing_kind=uncomputable, reason="样本不足（n=2 < 3）"`。
  - 有效值排序取中位；偶数个取中间两个平均；保留两位小数。
  - `excluded` 记「有行但不计入」的家数（PE≤0、PB≤0）。
- `as_of` 统一是 `PeerComparison.as_of`；每行另带自己的 `valuation_on` / `roe_period`，屏幕上能对上。
- **行业地位代理**（D241 的「代理指标」）：
  - 用最近年报营收（PIT）在**所选层级全部成员**（含自己）里排名；
  - `rank = 1 + 营收高于自己的家数`；`n = 有营收的成员数`；
  - `position = "营收规模位于三级行业「食品饮料-饮料-白酒」6 家中的第 1 位（超过 83% 的同行）；口径：最近年报（2025-12-31）；代理指标，不是市占率"`；
  - 自己营收缺失 → 用市值分位，`position_kind='market_cap'`，话里写「市值分位（营收缺失）」；
  - 两者都缺失 → `position_missing=not_disclosed`；
  - `n < 5` → `position_missing=uncomputable`（「行业样本不足（n=4 < 5）」）。
- 港美：`state='not_covered'`，`notes=("同业对比 v0 只覆盖 A 股；港股/美股的行业分类与一致预期需要付费源",)`。不查任何表。

### 6.4 屏幕

新异步局部 `/api/partials/peers?market=&code=`：

```
同业对比（A 股 · 三级行业「食品饮料-饮料-白酒」· 5 家 · as-of 2026-10-30）
行业地位：营收规模位于三级行业 6 家中的第 1 位（超过 83% 的同行）；
          口径：最近年报（2025-12-31）；代理指标，不是市占率

公司        市值(亿元)   PE(TTM)   PB    ROE(年报)   估值日       报告期
贵州茅台    17,401.76    21.37    6.93   34.5%       2026-10-30   2025-12-31  ← 自己
山西汾酒    15,404.49    16.78    3.85   33.1%       2026-10-30   2025-12-31
...
中位（同业）             17.36    2.41   24.1%
                         n=5      n=5    n=5

排除：ST 2 家；估值行超过 30 天 3 家；PE 不为正 1 家（未计入中位）；无行业 1 家
说明：中位只算同业，不含自己；PE/PB 是估值行当日，ROE 是最近年报 PIT；
      估值行最旧 2026-10-02。
```

- 每格缺失用 `—` + `title="{{ missing_label(kind) }}：原因"`。
- 中位行三列各自带 `n`；样本不足显示 `— 无法计算（n=2）`。
- 行业层级放宽时 `notes` 写「三级行业只有 2 家，已放宽到二级「食品饮料-饮料」」。
- 港美显示「未覆盖」整卡说明，不摆空表；行业缺失显示「未披露」+ 同步命令。
- 同业不足 3 家：表照列 1–2 家，中位逐项拒绝；不拿这两家假装「行业中位」。

## 7. 核心业务六字段

### 7.1 字段（`core/business.py`）

| key | 屏幕标签 | 说明 | v0 来源 |
| --- | --- | --- | --- |
| `business` | 核心业务描述 | 靠什么赚钱：主要产品/服务与收入模式 | 系统草稿（`company_profiles.summary` 前 200 字）+ 用户确认 |
| `revenue_mix` | 收入构成 | 各业务/产品的收入占比与增速（年报口径） | 人工 |
| `customers` | 主要客户 | 前五大客户、集中度、渠道结构 | 人工 |
| `capacity` | 产能与供给 | 产能、产量、在建与利用率 | 人工 |
| `position` | 行业地位 | 市占率/排名；v0 用营收分位代理 | 系统计算草稿 + 用户确认 |
| `trend` | 行业趋势 | 行业增速、渗透率、政策与供需 | 人工 |

```python
@dataclass(frozen=True)
class BusinessFieldSpec:
    key: str
    label: str
    hint: str
    max_chars: int = 2000

BUSINESS_FIELDS = (...)
BUSINESS_FIELD_KEYS = tuple(spec.key for spec in BUSINESS_FIELDS)
```

`v0 来源`是**默认草稿**，不是限制：任何字段都可以人工填写/覆盖。

### 7.2 读取（`services/business.py`）

```python
@dataclass(frozen=True)
class BusinessField:
    spec: BusinessFieldSpec
    value: str | None
    source: str                  # company_profiles.summary / computed / user
    source_label: str            # 公司概况 / 系统计算 / 用户录入
    as_of: date | None           # 来源日期
    confirmed: bool
    confirmed_by: str | None
    confirmed_at: datetime | None
    draft: bool                  # True = 系统草稿，尚未被确认
    missing_kind: str = ""
    missing_reason: str = ""

@dataclass(frozen=True)
class BusinessRead:
    market: str
    code: str
    fields: tuple[BusinessField, ...]
    notes: tuple[str, ...] = ()
```

优先级：**已确认/已保存的行** > **系统草稿** > **缺失**。

- `business` 草稿：`company_profiles.summary` 截前 200 字（按句号切，尽量不截断句子），`source='company_profiles.summary'`，`as_of=profile.fetched_at.date()`。概况为空 → `not_disclosed`（「公司概况没有公司简介」）。
- `position` 草稿：`PeerService.compare` 的 `position` 文本，`source='computed'`，`as_of=today`。港美/样本不足 → 对应的 `not_covered` / `uncomputable`。
- `revenue_mix/customers/capacity/trend`：没有持久化行就是 `not_disclosed`（「尚未录入」）。
- 已保存行即使来源是 `company_profiles.summary`，也照常显示 `confirmed=True`；草稿与已确认在屏幕上必须是两种样子。

### 7.3 写入

`BusinessProfileService.save(market, code, values: Mapping[str, str], user_id: str)`：

- `market/code` 归一；`field` 必须在 `BUSINESS_FIELD_KEYS`（否则 `ValidationError` 带字段中文名）。
- `value = value.strip()`；空 → 删除该字段的行（回到草稿/缺失）。
- 非空：超 `max_chars` → `ValidationError`（「『收入构成』最多 2000 字」）。
- 来源判定：
  - 提交值与**当前草稿/已存值**逐字相同 → 保留原来源与 `as_of`（确认草稿不改来源）。
  - 不同 → `source='user'`，`as_of=today`。
- 写 `confirmed_by=user_id`、`confirmed_at=now()`。
- 一次保存是六字段的整组覆盖：空字段删除，非空字段 upsert。DuckDB 单写者，不需要额外锁。
- 任何登录用户都可以确认/修改；`confirmed_by` 是审计线索。全库共享，不做 user_id 分区（D240）。

### 7.4 屏幕

新异步局部 `/api/partials/business?market=&code=`，卡片放在资料块之后：

```
核心业务（来源：公司概况 + 用户确认）

核心业务描述   贵州茅台酒股份有限公司主要业务是茅台酒及系列酒的生产与销售…… 
               公司概况 · 2026-10-20 · 待确认            [确认并保存]
收入构成       — 未披露                                  未披露：尚未录入
主要客户       — 未披露                                  未披露：尚未录入
产能与供给     — 未披露                                  未披露：尚未录入
行业地位       营收规模位于三级行业 6 家中的第 1 位……     系统计算 · 2026-10-31 · 待确认
行业趋势       — 未披露                                  未披露：尚未录入

[编辑六项]  ← <details> 内是六个 textarea，一次提交
```

- 已确认行：`用户录入 · 2026-10-31 · 已确认（alice 2026-10-31）`。
- 草稿行：`公司概况 · 2026-10-20 · 待确认`，值用普通文本，不用高亮冒充事实。
- 缺失行：`—` + `missing_label(kind)` + 一句原因；不老写「未披露」四个字。
- 编辑表单：`<details>` 收起；六个 `textarea` 带 `maxlength`；提示「留空 = 清除这一项；保存即确认」；隐藏 `csrf`；提交到 `POST /symbol/{market}/{code}/business`，303 回 `?back=...#business`。
- 编码：表单读取必须走 `web.forms.form_data`（S0-B 的 `form_data` 双路修复），不直接读 `request.form()`。

## 8. 接线、调度与验收

### 8.1 装配

- `ServiceContext` 新增：`company_business_store`、`industry`、`peers`、`business`。
- `JobDeps` 新增 `industry: IndustryService | None = None`。
- `build_jobs`：数据链之后、`spot` 之前登记 `industry`；港股/美股也登记（回填 0 行是合法结果，健康页看得见）。
- 个股页加载顺序：资料（F10）→ 核心业务 → 同业 → 估值位置。四块各自异步，任何一块慢/失败不挡 K 线。
- 持仓页 `_industry` 的兜底也走 `normalize_industry`，与回填后的 `symbols.sector` 同一口径。

### 8.2 测试（红→绿，中文名）

| 文件 | 关键行为 |
| --- | --- |
| `tests/core/test_industry.py` | 三级原样；全角/斜杠归一；>3 级取末三级；空 → None；不跨体系猜 |
| `tests/core/test_missing.py` | 四档中文名与未知档不静默 |
| `tests/core/test_business.py` | 六字段 key/标签/长度；`BUSINESS_FIELD_KEYS` 唯一 |
| `tests/storage/test_business_repo.py` | 幂等 upsert；空值删除；键归一；字段名非法拒绝 |
| `tests/services/test_industry_service.py` | 概况→sector 回填；重复跑 0 写入；空行业保留旧值；主档缺行计数 |
| `tests/services/test_valuation_percentiles.py` | 500 样本出三分位 + q20/50/80；249 样本拒绝 `uncomputable`；无估值行 `not_covered`；PE 为负 → PR `not_applicable` 而 PE/PB 仍出；无年报 → PR `not_disclosed`；今天不投票 |
| `tests/services/test_peers_service.py` | 6 家白酒 → 5 家同业 + 中位与 n；三级不足 → 二级并说明；样本 <3 → 中位拒绝；ST/过期估值/PE 为负的排除计数；港美 `not_covered`；行业缺失 `not_disclosed`；营收分位 rank/percentile；无披露日期数计数 |
| `tests/services/test_business_profile.py` | 无记录 → 六项缺失 + business/position 草稿；保存六项读回来源/日期/确认人；草稿原样确认保留来源；改过 → `user`；空值清除；超长拒绝 |
| `tests/web/test_symbol_fundamentals.py` | 个股页三个 loader；三个局部 200 且含中位/分位/六字段；POST 需 CSRF/登录；保存后 303 并读回；港美同业未覆盖 |
| `tests/test_scheduler_acceptance.py` | job 列表含 `industry@A/HK/US`，位置在 `announcements@` 之后、`spot@` 之前 |
| `tests/test_layering.py` | 新模块不越层（services 不 import monitor） |
| `tests/cli/test_sync_industry.py` | `sams sync industry --market A` 输出回填计数，二次 0 |

### 8.3 全量回归与实机验收

1. `sams db migrate` → 0036；实机重启还带上了 0037（心跳停止标记，见 `2026-10-31-expert-review.md` §7 说明）。
2. `sams sync industry --market A` → 503 只回填；再跑一次 0 写入。
3. 重启 `sams serve`（新的 `SAMS_SECRET_KEY`，会话失效属预期）。
4. 登录后逐个核对：
   - **600519 贵州茅台**：同业 5 家（山西汾酒/泸州老窖/古井贡酒/今世缘/迎驾贡酒），中位 PE/PB/ROE 带 n；行业地位第 1/6（83%）；PR/PE/PB 分位条各 490 天左右，20/50/80 刻度和当前 marker 对得上。
   - **000001 平安银行**：银行同业；PE/PB 中位与 ROE 中位不为 0；估值分位正常。
   - **一个三级行业不足 4 家的票**：表照列、层级放宽说明、中位拒绝「无法求解（n=2）」。
   - **AAPL**：同业整卡「未覆盖（v0 仅 A 股）」；核心业务若有概况摘要则出草稿，否则未披露。
   - **核心业务**：在 600519 上把六项填齐并保存；刷新后来源/日期/确认人都在；再清空一项，回到「未披露」。
5. `pytest` 全量 + `ruff check src tests`。
6. 更新 `docs/superpowers/specs/2026-10-31-expert-review.md` §7：S2 行写证据（测试数、实机样本、截图/路径）。

## 9. 决策记录

- **D236**：行业归一复用 `symbols.sector` + `company_profiles.industry`，不建行业表、不接新行业源；只做同一分类体系内的字符串归一，不跨申万/证监会/GICS 映射。口径对不上时同业「未覆盖」。
- **D237**：同业 v0 仅 A 股；按三级→二级→一级逐级放宽，不含自己 ≥3 家才用该层；最多摆 5 家；不足不补零。
- **D238**：中位与分位各自带样本数与 as-of；中位 n<3、分位样本<250、行业地位 n<5 时拒绝出数并说明下一步。
- **D239**：同业市值一律用 `snapshot_daily.market_cap`（亿元）；不与 `valuation_daily.market_cap`（元）混用。
- **D240**：核心业务六字段全库共享，带 `source/as_of/confirmed_by/confirmed_at`；系统只抽公司简介草稿与营收分位代理，其余人工；不做编辑历史。
- **D241**：缺失统一四级（不适用/未披露/无法计算/未覆盖），行级「—」+原因，不补零；行业地位标明「代理指标，不是市占率」。
- **D242**：分位条展示历史 20/50/80 分位对应的值；分位口径仍是「不高于现在的交易日占比」，今天不投票，等值算「不高于」。

## 10. 明确不做（S2 范围内）

- 不接全市场行业/同业付费源，不做 GICS 映射，不做港股/美股同业。
- 不抓年报 PDF/文本做 LLM 抽取；「系统抽」只指公司简介草稿与营收分位代理。
- 不做营收构成、客户、产能的自动抽取；v0 人工录入。
- 不做行业趋势的自动数据源；v0 人工填写。
- 不做 DCF 单一目标价、市占率采购、分析师一致预期。
- 不做核心业务编辑历史/审批流；`confirmed_by` + `confirmed_at` 是 v0 的全部留痕。
- 不做 `nav_daily`、技术面选股字段、舆情（属于 S3/S4）。
