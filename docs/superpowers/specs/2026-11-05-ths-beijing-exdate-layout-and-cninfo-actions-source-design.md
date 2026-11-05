# 同花顺那张没有 A 股除权日的表：读不出 ≠ 源改版 ≠ 查无（#383）

**状态**：已实现（一期 `56106d3`、二期 `c9a79d2`；§7 的单票实机已回填，当晚整趟 `actions/A` 的失败数待今晚读数）
**日期**：2026-11-05
**关联**：#360（港股「从没派息」被读成源改版，同一族错误的另一头）、#364/D279（源冷却）、#368/D283（覆盖缺口排队）、#382/D314（同一条准入判据）、D34（每 10 股口径反查）、D36（「没有」要由源说）、D40（全史不按窗口切）、D43（公司行为的 0 行不是成功取数）
**数据来源**：实机库 `data/sams.duckdb`（11-05 11:34 从运行中的服务复制的只读快照 `tmp/snap_383.duckdb`）、
`tmp/serve.log`（11-05 07:18–11:15 的源连败记录）、以及当日对 akshare 三个接口的实网探测（`tmp/probe_383*.py`）

## 1. 现象：A 股公司行为每晚 13 只失败，报错说的不是这 13 只的问题

#382 之后 `actions/A` 从 `490/505，15 只失败` 变成 `490/503，13 只失败`。这 13 只的报错正文**逐字相同**：

```
A 920289: [registry] A 920289 取不到公司行为。链 [...] 里被跳过的源 [...]；
  真调用过又失败的 （无）；本次未调用的 akshare_ths（冷却中，还有 1105 秒）；
  说查无记录的 akshare（因为 akshare_ths 没答上来，这些「没有」还不能写成确认）
```

「真调用过又失败的：无」。也就是说这 13 只**当晚没有任何一个源真失败**——东财答了「没有」，
而同花顺因为**冷却**根本没被调用。冷却是谁给的：`tmp/serve.log` 里那一条一直在涨的连败——

```
07:18:31 源 akshare_ths 连续失败 29 次: [akshare_ths] 分红送配响应缺少必需列 ['A股除权除息日']，
         实际列 ['报告期','董事会日期','股东大会预案公告日期','实施公告日','分红方案说明',
                'B股股权登记日','B股除权除息日','分红总额','方案进度','股利支付率','税前分红率']
…
11:15:10 源 akshare_ths 连续失败 35 次: （同一句）
```

所以链条是：**北交所那几张表让同花顺被读成「源改版」→ 连败进冷却（D279）→ 冷却是按源名记的、
不分代码（`registry.py:447`）→ 于是全市场每一个「东财说没有」的代码都收不到第二源的答复 →
「没有」写不成覆盖标记（D36）→ 每晚 13 行标红**。

## 2. 实测：那张表没问题，是同花顺对北交所换了列

`akshare.stock_fhps_detail_ths` 自己的实现里就写了这个分支
（`.venv/.../akshare/stock_feature/stock_fhps_ths.py`）：

```python
if "A股股权登记日" in temp_df.columns:      # 主板/创业板/科创板
    ... 转 A股股权登记日 / A股除权除息日
else:                                       # 其余（实测：北交所）
    ... 转 B股股权登记日 / B股除权除息日
```

同花顺的 F10 页对北交所标的**把日期列挂在「B股」那一组名下**（它自己不新增事实，
只是换了列名走 else 分支）。逐只实网探测这 13 只：

| 代码 | 同花顺行数 | A 股除权列 | 有没有「实施方案」行 | 那行写的什么 | 巨潮给的除权日 |
| --- | --- | --- | --- | --- | --- |
| 920289 | 6 | ✗ | ✓ 实施公告日 2024-07-09 | 10转12.5397股 | **2024-07-17** |
| 920093 | 6 | ✗ | ✓ 2024-11-13 | 10派3元(含税) | **2024-11-21** |
| 920079 | 8 | ✗ | ✓ 2024-12-18 | 10转22股 | **2024-12-26** |
| 920238 | 4 | ✗ | ✓ 2025-05-22 | 10派5.2元(含税) | **2025-05-30** |
| 920162 | 5 | ✗ | ✓ 2017-08-25（新三板时期） | 10送15股派1.666667元 | 无记录 |
| 920156 / 920258 / 920285 / 920136 | 4–5 | ✗ | ✗（全是预案/不分配） | — | 无记录 |
| 301666 / 688221 / 688825 | 1–12 | ✓ | ✗ | — | 无记录 |
| 001246 | — | 页面一张表都没有 | — | `ValueError: No tables found` | 无记录 |

三条要点：

1. **B 股那一组列全是 `NaT`**（实测 920289 的 `B股除权除息日` 五个值都是 `NaT`）。同花顺对
   北交所压根没给除权日，连借列名读的机会都没有。
2. **这五只（920289/920093/920079/920238/920162）表里躺着真实发生过、但定不了位的方案**。
   920289 那条是 10 转 12.5397：把它当「没分红」写进覆盖标记，等于让守卫相信这只票的历史序列
   不需要复权，而实际那一年价格该除以 2.25。这正是 D36/D43 挡的那类错。
3. **库里的现状**（`corp_actions` / `corp_actions_coverage`）：这 13 只一根公司行为都没有；
   只有 301666/688221/688825 三只早在同花顺还没冷却时写下了双源「查无」的确认。
   在册北交所代码 354 只，`corp_actions` 里有行的只有 23 只。

`bars_daily` 的第一根都落在 2026 年（920289 从 2026-09-04 起），所以上表那些 2024/2025 的除权日
**暂时还没进到我们要出数的窗口里**——今天错的不是曲线，是「这条链看不见北交所的任何一次除权」
这件事本身：北交所下一次派息就会落进窗口，而我们现在既读不出、又给不出覆盖标记。

## 3. 根因：一句 `FieldDrift` 同时说了三件不同的事

`akshare_ths.required_columns()`（`akshare_ths.py:67`）把 `_REQUIRED = (A股除权除息日, 方案进度, 分红方案说明)`
当成一个整体检查，缺任何一个都抛 `FieldDrift`。而这个类名在系统里的含义是明确的
（`errors.py:90`、`registry.py:73` 的判据）：**源改版了，重试不会让缺掉的字段长回来，要人出声**。

按这个含义回头看，缺列至少有三种完全不同的事实：

| 事实 | 该怎么对待 | 现在被当成 |
| --- | --- | --- |
| 这张表还是分红送配表，只是这个板块不写 A 股除权日 | 逐只、永久成立，重试无用；**不该算这个源挂了** | 源故障 → 连败 → 全局冷却 |
| 表里躺着定不了位的已实施方案 | **不能说「查无」**（会写下假的覆盖确认） | — |
| 表连「方案进度/分红方案说明」这两列都没了 | 真的改版，要报警 | 与上两条混在一起 |

第二列那条同时解释了为什么**不能简单地「缺列就返回空帧当查无」**：那会把
920289 的 10 转 12.5397 洗成「这只票从没分红」——#360 修的是把「没有」读成「改版」，
这一次是反方向把「有但看不见」读成「没有」，两边都是让覆盖标记撒谎。

## 4. 决策

### D316 — 分红送配表分三态：有、没有、读不出

`akshare_ths` 的必需列收缩成**这张表是不是分红送配表**的两列（`方案进度`、`分红方案说明`）；
缺它们才抛 `FieldDrift`。`A股除权除息日` 不再参与身份判定，改为在 `expand_ths` 的逐行处理里定三态：

- **有**：能定位的已实施方案 → 标准行，照旧。
- **没有**：一张表读完，**没有任何一行「实施方案」**（全是预案/不分配不转增）→ 返回空帧。
  这是源自己说的一句「这里没有发生过的除权」，可以被写成覆盖确认（与 920156/920258/920285/920136
  的实测形状一致）。
- **读不出**：表里有 ≥1 行「实施方案」，但这张表没有 A 股除权日列 → 抛 `EmptyResponse`
  （`_walk` 的 `no_answer` 通道，`registry.py:582`）。这句话的含义现成且正好：
  「源连『没有』这句话都没说清，不能替它写成查过」（D36）。
  它同时满足两个要求：**不给覆盖标记**（那五行里躺着真方案）、
  **不计进源连败**（`_call` 在 `registry.py:1091` 对 `EmptyResponse` 先 `raise`，
  不经过 `_mark_failure`）——北交所的版式因此不再能把同花顺整源冷却掉。

不新增异常类：需要的语义 `EmptyResponse` 已经全部具备，新类只会多一个要人记住的名字。

### D317 — 页面一张表都没有，同属「读不出」，不属「查无」

`001246` 那类 `ValueError: No tables found`（同花顺 F10 页 200、标题是「分红融资」，
但 `<table>` 数为 0）在适配器边界转成 `EmptyResponse`。
理由是它有两副脸：新股从没分红会这样，同花顺改版也会这样，而我们**没有**任何一句「这家公司没分过红」
的正面陈述可依（实测该页里「暂无」出现 1 次，但 600519 那页也出现 1 次——它不是空态标记）。
判成「查无」= 可能写假确认；判成源故障 = 继续烧冷却。判成「读不出」才是实话。

代价要说清：只要链上有一个源「读不出」，覆盖确认就给不出去（`empties and not unanswered`，
`registry.py:611`），所以 001246、920162 会**一直**留在那一晚的失败里，直到 §5 的第二个改动
或有人给出正面答复。这是刻意的保守：宁可每晚两只标红，也不给一只做过 10 转 12.5 的票写「从没分红」。

### D318 — 给 A 股公司行为补第三个源：巨潮历史分红

同花顺看不见北交所的除权日，但**交易所自己的披露平台看得见**。
`akshare.stock_dividend_cninfo`（巨潮 `p_sysapi1139`，逐只全史）实测：

- 上表五只里除 920162 外全部给回**除权日**（920289 2024-07-17 / 920093 2024-11-21 /
  920079 2024-12-26 / 920238 2025-05-30），另有 `股权登记日`、`送股比例`、`转增比例`、`派息比例`。
- 口径与东财一致：**每 10 股**。600519 实测 31 行、`除权日` 无一为空，末行
  「10派280.2423元(含税)」对应 `派息比例 280.2423`。数值列与说明文本可以互查（D34）。
- 无记录的票返回 `records: []`（HTTP 200、`resultcode 200`、`resultmsg: success`），
  而 akshare 的列重命名对空帧不生效，随后在 `temp_df["实施方案公告日期"]` 上抛
  `KeyError`——**与东财那个 `TypeError` 是同一个坑**（#360 记过一次），照
  `akshare_cn.eastmoney_says_no_records`（`akshare_cn.py:152`）的既有解法处理：
  只在失败路径上多打一次请求，读到 `records == []` 才认作「源答了：没有」，否则原样上抛。
- 每只 0.3–0.8 秒；只有前两源都答「没有/读不出」时才走到它（排在链尾）。
  当晚实测量级：13 只里最多 9 只走到这里。

**它不能替掉同花顺**：`002450`、`600636`（同花顺当初被引进来的那两只，东财空答复、同花顺有
8–28 条）在巨潮同样是无记录。三个源是三家上游，谁都不是全集——这正是 D36 要的形状。

链路位置：`data_source_routes` 加 `('A', 'akshare_cninfo', 6)`（迁移 `0047_cninfo_actions.sql`），
能力只给公司行为、标 `corp_actions_full_history`（与同花顺同样不挂日线链，D38）。

## 5. 改动清单

### 一期：分类（D316 + D317）

| 文件 | 改什么 |
| --- | --- |
| `src/sams/datasource/akshare_ths.py` | `_REQUIRED` 收缩为 `(方案进度, 分红方案说明)`；`expand_ths` 里按有没有 A 股除权列分三态，读不出抛 `EmptyResponse` 并带上「哪几行、实施公告日各是哪天」；`akshare_ths_plans` 之外在适配器 `corp_actions` 的边界把 `No tables found` 归入 `EmptyResponse` |
| `src/sams/datasource/registry.py` | 不改。`EmptyResponse` 的两条既有性质（不 `_mark_failure`、走 `no_answer`）正是需要的，本决策**只使用**它们，一处都不动 |
| `tests/datasource/test_akshare_ths.py` | 见 §6 |

### 二期：补源（D318）

| 文件 | 改什么 |
| --- | --- |
| `src/sams/datasource/akshare_cninfo.py`（新） | 纯解析 `expand_cninfo`（每 10 股 → 每股，送/转/派拆三行 D34，派息与说明文本反查）+ `AkshareCninfoAdapter`（`markets={"A"}`、`corp_actions_full_history=True`） |
| `src/sams/storage/migrations/0047_cninfo_actions.sql`（新） | 注册源与 A 市场 priority 6 路由，注释写清「同花顺对北交所不写 A 股除权日」这个引进理由 |
| `tests/datasource/test_akshare_cninfo.py`（新） | 录制 payload 的解析用例 + 空记录判定 |
| `tests/datasource/test_registry.py` | 三源链上「前两个读不出/查无、第三个有行」的收敛次序 |

## 6. 测试（先红后绿，逐条一件事）

一期（`tests/datasource/test_akshare_ths.py`）：

1. `test_the_beijing_layout_with_nothing_implemented_answers_no_records`
   喂 §2 里 920289 的真实列集 + 只有预案的行 → 期望返回空帧（**不抛** FieldDrift）。
2. `test_an_implemented_scheme_without_a_datable_ex_date_is_unanswerable`
   同一列集、把一行进度改成「实施方案」→ 期望 `EmptyResponse`，正文含「实施方案」与 1。
3. `test_a_response_missing_the_scheme_columns_is_still_a_redesign`
   缺 `分红方案说明` → 仍抛 `FieldDrift`（真改版的报警能力不能因为一期被削掉）。
4. `test_a_page_with_no_table_at_all_is_unanswerable_not_a_failure` /
   `test_a_real_source_failure_is_still_a_real_failure`（D317）
   注入的 fetch 抛 `ValueError: No tables found` → `EmptyResponse`；抛别的 → 仍按故障归类。
5. `test_an_unanswerable_code_does_not_cool_the_source_down`（`test_registry.py`，registry 级）
   同花顺对代码甲答「读不出」后，代码乙仍被真调用（12 轮 12 次、`consecutive_failures` 仍 0），
   并带一个对照组：同一只链上换成真故障，12 轮只调 10 次——钉住 D316 要解决的放大效应本身。

二期（`tests/datasource/test_akshare_cninfo.py`，25 条；行内数据全部来自 11-05 实网探测）：

- 单位与拆行：`test_cninfo_ratios_are_per_ten_shares`（600519 末行 280.2423 → 每股 28.02423）、
  `test_a_cash_row_is_converted_from_per_ten_to_per_share`、
  `test_a_transfer_and_a_dividend_on_one_row_are_two_facts`、
  `test_bonus_shares_are_a_different_fact_from_transferred_ones`、
  `test_the_ex_rights_date_is_the_anchor_not_the_registration_date`。
- 反查失败要响：`test_a_number_that_disagrees_with_the_written_scheme_fails_loudly`、
  `test_a_scheme_written_per_share_is_a_unit_drift`。
- 三态：`test_an_empty_response_frame_answers_that_there_was_nothing`、
  `test_a_table_where_no_row_can_be_dated_answers_nothing`、
  `test_a_dated_row_with_no_numbers_is_kept_as_unknown`（D39）、
  `test_a_scheme_that_says_no_distribution_is_a_complete_answer`、
  `test_a_frame_without_the_ratio_columns_is_a_redesign`。
- 「查无记录」那句要问得出来：`test_empty_records_from_cninfo_answer_nothing_not_a_crash`、
  `test_a_crash_that_is_not_a_no_records_answer_still_raises`，以及信封四判
  `test_the_no_records_verdict_comes_from_the_envelope_not_the_crash` /
  `test_records_present_means_the_crash_was_not_a_no_records_answer` /
  `test_an_unreadable_envelope_is_not_a_no_records_answer` /
  `test_a_probe_that_raises_is_not_a_no_records_answer`（#360 同一族）。
- 适配器：`test_the_adapter_is_a_full_history_actions_only_source`、
  `test_the_adapter_gives_back_the_whole_history_not_the_window`（D40）、
  `test_the_adapter_names_the_code_it_was_asked_about`、
  `test_an_unknown_row_survives_the_adapter_rather_than_becoming_nothing`、
  `test_an_unsupported_market_is_refused_before_the_network`。
- 链：`test_a_third_source_answers_when_the_first_two_cannot`（`test_registry.py`，
  东财「查无」→ 同花顺「读不出」→ 巨潮有行，且 `confirmed_empty_by` 为空）。
- 接线：`test_sources_pluggable.py` 的模块清单与能力断言、`test_bootstrap.py` 的 A 链
  `("akshare", "akshare_ths", "akshare_cninfo")`、`test_data_sources.py` 的种子数 11→12。

**落地时对 D316 的一处收细**：单行缺除权日 ⇒ 计数 + `logger.warning` 后丢弃（与一期同花顺那侧
已审的口径一致，`akshare_ths.py:195` 同理），只有**一行都定不了位**时才抛 `EmptyResponse`。
整批失败会把一只票 30 条除权换成一条都不入库；而「有记录却一行都读不出」必须抛，因为交回空帧
会被 `corp_actions` 读成一句「这只票没除权过」并写下假覆盖确认。


## 7. 实机验证（上线后读什么）

### 单票实机（11-05 13:39，serve 停机期间走 CLI，库为 `data/sams.duckdb`）

`sams sync actions --market A --code 920289` 的实际读数：

```
920289     源=akshare_cninfo 取回=1 入库=1 确认至=2026-11-05（全历史：取回与入库不受请求窗口约束）
           [降级: akshare✓ → akshare_ths✗ → akshare_cninfo✓]
同花顺跳过未实施的公司行为 5 行（进度 {'董事会预案': 3, '股东大会预案': 2}）
```

- `corp_actions`：`A / 920289 / 2024-07-17 / transfer / ratio 2.25397 / src=akshare_cninfo`
  —— 与 §2 探测到的原始行逐字对上（`转增比例 12.5397` ⇒ 每股 1.25397 ⇒ 乘数 2.25397）。
- `corp_actions_coverage`：`920289 / src=akshare_cninfo / full_history=True /
  covered_from 2024-07-17 / covered_to 2026-11-05`。同花顺当初就是这里给不出确认。
- 同花顺那一侧按 D316 答的是「读不出」，正文带着定位不了的行：
  `这张表里没有「A股除权除息日」列，读不出 1 行「实施方案」定位不到除权日：2024-07-09 10转12.5397股`。
- **一条本轮踩到的装配次序**：`build_core` 在读 `data_source_routes` 时把降级链固化成 registry
  的一份快照，而迁移在其后才跑（`cli/__init__.py:99-103` 先建容器再 `container.migrate()`；
  `ServeParts.prepare()`（`bootstrap.py:1271`）同样在 `build_serve` 之后）。于是**加迁移的那一趟
  看不到新源**：13:39 的第一次同步里链还是 7 个源、没有 `akshare_cninfo`，第二次才有。
  当前进程（13:41 启动）已带上它。这条不改代码，记在 §8。

### 当晚整趟（11-05 23:06–23:47 四趟，读数取 23:55 的库副本）

- `task_runs`（`job_name='actions' AND market='A'`，最近四趟 23:06 / 23:20 / 23:33 / 23:46）
  全部同一句：**`501/503 只公司行为，2 只失败，3 条顺延的补采重新排队`**。
  失败数从一期的 6 掉到 **2**，与预测一致；分母仍是 `N/503`。
  `status` 仍是 `failed`——这 2 只把整趟标成失败，是既有口径（不是新问题）。
- 那两只是 **920162、001246**，日志原文（23:46 那一趟）：
  ```
  23:46:16,975 公司行为 akshare_ths 回答无数据（A 920162），换源
  23:46:17,436 巨潮历史分红对 920162 回「无记录」，换下一个源（'实施方案公告日期'）
  23:46:17,439 公司行为 akshare_cninfo 说 A 920162 查无记录，继续问下一个源
  ```
  001246 是同样三句（23:46:15 / 23:46:16）。**订正本节上一版的预测**：它们今晚的形状
  不是「同花顺读不出除权日」，而是「akshare 答空、巨潮答查无记录、同花顺回空
  （`no_answer`）」——三家都开了口，但有一家没把「没有」说清，D36 就不发覆盖确认，
  于是它们既没有事实也没有确认，继续挂在失败里。结论不变：这两只票在任何一张表里
  都没有可定住的除权事件，差的不是我们的解析，是源。
- `source_health`：**`akshare_ths` 的 `consecutive_failures` 已归 0、`ok=True`**——
  上一版记的那个 39 到此收口（同花顺后来真的答成功过，连败被清零，D317 的
  「读不出不计连败」也不再是唯一解释）。`akshare_cninfo` 是 `consecutive_failures=1`、
  `ok=False`：今晚它答的绝大多数是「查无记录」（不算失败），这一次连败来自一次真失败，
  下一条成功答复就会清零。
- `corp_actions` 里 `src='akshare_cninfo'` 的 A 股行仍是 **4** 条
  （920079 / 920093 / 920238 / 920289 各 1 行）；`corp_actions_coverage` 里 **11** 条全史确认
  （上面 4 只 + 920136 / 920156 / 920258 / 920285 / 688221 / 688266 / 688825）。
  920162、001246 两张表里都没有行，与 §1 的预测一致。
- **健康页那句话没有按原预测落地**：`/admin/health` 要 admin 会话（`sp1check` 是普通用户，
  23:56 实测 403），而页面这一格本来就是一个**数**、不是一句话——
  `HealthService._codes_without_coverage()` 的同一口径今晚读到
  **A 19 只（有 K 线的 534 只里）没有任何除权覆盖确认**，HK 2/9、US 2/2。
  所以「不说这只票没分过红、不给手工命令」这条只在数据层被验证（没有确认就没有那句
  「没分过红」）；屏幕文案留给下一次带 admin 会话的实机核对，本节不再留占位。


## 8. 已知缺口与非目标

- **同一个谎言在 A 股列那条路上也存在**：表里有 `A股除权除息日`、但某一行是 `NaT`
  （实测 600585 有一行）时，`expand_ths` 只计数丢弃（`akshare_ths.py:195`），剩下的行照样让
  这个源答成「全史」——被丢掉的那一行因此被写成了「没有」。巨潮那侧本轮采用了同一口径
  （`test_an_undated_row_does_not_discard_the_dated_ones` 把它钉成了显式决定，不是疏漏）。
  本期不修：要修的是「给覆盖标记带上一个下限日」，动的服务层是 `coverage` 的写入形状
  （`covered_from`/`full_history` 是现成的列，但把「部分确认」这件事说清需要单独一轮）。
  本期先把北交所这条最宽的口子扎住。
- **新注册的源要重启两次才上岗**：`build_core` 把 `data_source_routes` 读成 registry 的一份
  链快照（`bootstrap.py:501-502`），而迁移排在其后——CLI 是 `cli/__init__.py:99` 先建容器再
  `container.migrate()`，`serve` 是 `ServeParts.prepare()`（`bootstrap.py:1271`）在 `build_serve`
  之后。11-05 实测：加 0047 的那一趟同步里链还是 7 个源、没有 `akshare_cninfo`，第二次才有。
  本期不改（动的是装配次序：要么把迁移提到 `build_core` 之前，要么让 registry 每次取链时重读），
  因为**改错了会让每次采集多一轮全表读**，而这条只在「加源当天」咬人——按上面的口径，
  加完迁移再重启一次 serve 即可。
- 不改冷却键的粒度（按源名、不按代码）。一期之后「读不出」不再计入连败，剩下的
  「一只票真把源打挂 → 全市场换下一个源」是 D279 本来的取舍。
- 不动 #365（孤源链整夜撞墙），仍在等用户定的范围里。#370（T1a 话术）已在 11-06 单独修掉：
  共享原因串只讲事实，网页不再读到 `--include-negatives`，命令行的开关提示由 CLI 自己附加（D283）。
