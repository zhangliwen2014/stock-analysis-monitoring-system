# S1-B 告警证据链与跃迁去重 · 设计

日期：2026-10-31
状态：已实施（2026-10-31）
上游：`docs/superpowers/specs/2026-10-31-expert-review.md`（共识第 7 条、§4 工程项）

## 1. 要解决的问题

专家验收里三条原话：

1. **「同一条规则每天重发一遍」**——冷却窗口（D94）只按分钟挡，窗口一过，同一个
   「跌破止损」每天再响一次。用户把这种告警当噪音，真破位那次反而被淹掉。
2. **「点开告警，看不到它当时看的是哪根 bar」**——详情页有值、有阈值、有报价时刻，
   但没有「这条告警由哪一天的数据判出」。复盘时无法回到现场。
3. **「一只票出问题，五条规则一起响，只能一条条点已读」**——没有单票静音，也没有
   批量已读。

验收标准（评审文档原文）：

- 同一天的同一状态**重跑 0 新增**；
- 告警详情能**跳回触发那根 bar**，并在图上标出来；
- **单票静音**生效：静音后这只票不再推，解除后不补发静音期间的历史。

## 2. 决策

### 2.1 去重键：状态跃迁，不是 bar 时间

`(user_id, rule_id, market, code, account_id)` 上存一个 **`last_state`**：

- `clear`：上一次判定「没到」；
- 或一个阶段名（`pre` / `hit` / `urgent`）。

只有 **`last_state` 变化**时才产生新告警。`bar_ts` 只作证据，不进去重键——
把 `bar_ts` 放进键里等于每天一根新 bar 就重发一次，正是要修的病；而状态在
`bar_ts` 之内漂移（盘中 `pre` → `hit`）又会被键吞掉。

`account_id` 必须在键里：自选/持仓类规则一条展开成几十只票、甚至同一只票挂在
两个账户上，缺了它两个账户会互相盖住状态。`user_id` 也必须在：规则归属于人，
同一条规则换人不应继承上一个人的告警状态。

**`is_test` 不进键**，因为测试演练根本不读也不写状态表：它是「现在按这条规则
演练一条」，与真实去重无关（D131 的测试告警同样不参与 `no_open_alert`）。
测试告警的 `dedupe_key` 列照写，供审计辨认，但不参与判定。

### 2.2 什么时点写状态

| 这一趟的结论 | 写 `last_state`？ | 出口 |
| --- | --- | --- |
| 判过了、没到（`verdict is None`） | 是，置 `clear`（条件更新，已是 clear 不写） | `quiet` |
| 命中，但被过滤器挡下（时段/新鲜度/停牌/已成堆） | **否** | `Suppressed` |
| 命中，被冷却挡下 | **否** | `Suppressed` |
| 命中，与 `last_state` 同档 | 否 | `Suppressed`（理由「状态未变」） |
| 命中，是跃迁，dry-run | 否（dry-run 从不写库） | `Raised("")` |
| 命中，是跃迁，推送成功 | 是，写新阶段 + `bar_ts` + 值 | `Raised(alert_id)` |
| 命中，是跃迁，推送抛 `SamsError` | 否（下一趟重试） | `Skipped` |
| 命中，但该票已静音 | 是，写新阶段 + `bar_ts`（不推送） | `Suppressed`（理由「静音」） |
| `is_test=True` | 否（测试永不读写状态） | 正常投递 |

两个关键取舍：

- **过滤器与冷却不写状态**：时段外的那次读数确实命中了；若把状态写下去，
  开盘后用户就再也收不到这条告警。宁可下一趟再判一次。
- **静音要写状态**：解除静音时若状态还停在静音前的档位，下一次求值会补发一条
  「静音期间已经发生」的告警。写下去 = 静音期间的跃迁算发生过、只是没送达。

### 2.3 `clear` 的写回

`verdict is None`（判过了、没到）时调用 `states.clear(key)`，SQL 是条件更新：
`UPDATE ... SET state='clear' WHERE ... AND state <> 'clear'`。绝大多数时候命中
0 行，成本是一次主键查询；换来的是「跌回去又跌破」能再响一次。

`Skipped`（缺数、搬不动、送不出）**不**清状态：没有结论，不能拿它证明条件已解除。

### 2.4 证据：`trigger_bar_ts`

新列 `alerts.trigger_bar_ts DATE`，取自 `Snapshot.price_on`（这个价属于哪一天）；
状态型命中没有价格（如 `stop.thesis_drift`）时回落到本次求值的本地日 `day`。

详情页新增一行「触发 K 线」，指向
`/symbol/{market}/{code}?focus=YYYY-MM-DD&back=/alerts/{id}#chart`：

- 个股页把 `focus` 透传给图表局部，局部把 `data-focus` 挂在 `#chart` 上；
- `app.js` 的 `klineChart` 若在 `payload.dates` 里找到这一天，就在主图加一条
  竖线 `markLine`，并把 `dataZoom` 的 `startValue/endValue` 收到该 bar 前后
  各 30 根；
- 找不到（默认窗口只有 180 天）就在图注里明说「告警那天不在当前窗口里」，
  不假装标上了。

`alerts.dedupe_key VARCHAR` 同步落库，格式
`{rule_id}|{market}|{code}|{account_id or '-'}`，与正文里那行「去重键」逐字一致。
它不参与判定，只让「这条告警当时按哪个键判的」在复盘时可见、可查。

## 3. 存储

迁移 `0034_alert_evidence_states.sql`：

```sql
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS trigger_bar_ts DATE;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR;

-- 一条去重键的当前状态。account_key 用空串代表「不挂账户」：
-- DuckDB 的主键不接受 NULL，而 `account_id IS NULL` 是自选/价格类规则的常态。
CREATE TABLE IF NOT EXISTS alert_states (
    user_id     VARCHAR NOT NULL,
    rule_id     VARCHAR NOT NULL,
    market      VARCHAR NOT NULL,
    code        VARCHAR NOT NULL,
    account_key VARCHAR NOT NULL,
    state       VARCHAR NOT NULL,
    bar_ts      DATE,
    value_text  VARCHAR,
    updated_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, rule_id, market, code, account_key)
);

-- 单票静音。键是（用户, 市场, 代码），不带规则：静音讲的是「这只票别再烦我」。
CREATE TABLE IF NOT EXISTS alert_mutes (
    user_id    VARCHAR NOT NULL,
    market     VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    note       VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, market, code)
);
```

不回填历史：`alert_states` 为空时，第一次命中的规则会各发一条（初始状态），
之后进入跃迁逻辑。这是可接受的一次性成本，也避免拿历史告警猜一个假状态。

`AlertRepo.create` 增两个可选参数 `trigger_bar_ts: date | None`、
`dedupe_key: str | None`；`_ALERT_COLUMNS` 与 `Alert` dataclass 同步加列。

## 4. 仓库

### `alert_states.py`

```python
@dataclass(frozen=True)
class AlertState:
    user_id: str; rule_id: str; market: str; code: str
    account_id: str | None; state: str; bar_ts: date | None
    value_text: str | None; updated_at: datetime

class AlertStateRepo:
    def get(self, *, user_id, rule_id, market, code, account_id) -> AlertState | None
    def set(self, *, user_id, rule_id, market, code, account_id,
            state, bar_ts, value_text, now) -> AlertState
    def clear(self, *, user_id, rule_id, market, code, account_id, now) -> None
```

`set` 走 `INSERT ... ON CONFLICT (...) DO UPDATE`（与 `symbols`/`chart_prefs`
同一姿势）；`clear` 是条件更新，不动 `bar_ts`（它仍是最后一根判过的 bar）。

### `alert_mutes.py`

```python
@dataclass(frozen=True)
class Mute:
    user_id: str; market: str; code: str; note: str; created_at: datetime

class MuteRepo:
    def get(self, user_id: str, market: str, code: str) -> Mute | None
    def add(self, user_id: str, market: str, code: str, *, note: str = "",
            now: datetime) -> Mute
    def remove(self, user_id: str, market: str, code: str) -> bool
    def list_for(self, user_id: str) -> list[Mute]
```

`add` 幂等（重复静音不报错、保留第一次的时刻）；`remove` 返回是否真删掉一行。

### 批量已读

`AlertRepo.acknowledge_many(user_id, alert_ids) -> tuple[int, int]`：

- 一个事务里逐条做与 `acknowledge` 相同的条件更新；
- 不属于该用户、已不是 `new`、或不存在的 id **跳过**，不抛错——
  收件箱勾选里夹着一条刚被别人处置的，不该让整批失败；
- 返回 `(已标已读, 跳过)`；每成功一条照旧写 `alert_events`。

## 5. 引擎接线

`RuleEngine.__init__` 增 `states: AlertStateRepo`、`mutes: MuteRepo`。

`_one` 的顺序改为：

```
verdict = ...
if verdict is None:
    非 dry_run 且非 is_test → states.clear(...)
    return None
reason = run_filters(...)
if reason: return Suppressed(...)
if is_test:   # 测试演练绕过状态与静音
    return dry_run ? Raised("") : _dispatch(...)
if mutes.get(user, market, code):
    states.set(state=verdict.stage, bar_ts=..., ...)   # 记下但不推
    return Suppressed("静音：这只票已静音，状态记为 {stage}，解除后不补发")
cooled = _cooldown(...)
if cooled: return Suppressed(...)
last = states.get(...)
if last is not None and last.state == verdict.stage:
    return Suppressed("状态未变：上一趟已是 {stage}，同一状态不重复推送")
if dry_run: return Raised("")
try:
    raised = _dispatch(...)          # 成功后写状态
except SamsError as exc:
    return Skipped(...)
states.set(...)
return raised
```

`_dispatch` 把 `trigger_bar_ts` 与 `dedupe_key` 放进 `AlertMessage`；
`NotifyService.record` 落到新列；`_message_from` 从 `Alert` 读回，保证重发时
证据不丢。

静音与状态写入都在 `_dispatch` 前后做，**不放进** `NotifyService`：去重是判定
语义，属于引擎；通知层只管把已有的一行送出去。

### 装配

`bootstrap.py` 在造 `RuleEngine` 处加两个仓库；`ServiceContext` 增
`alert_states`、`alert_mutes` 两格（网页静音要用）。测试脚手架
（`tests/support_rules.py` 的 `Harness.engine()`、`tests/services/test_rules_ex_rights.py`
的 `Run`）同步补齐，否则构造参数缺失会在全量回归里炸出一片。

## 6. Web

### 6.1 详情页：触发 bar

`alert.html` 在「现值 / 阈值」后加一行：

```
触发 K 线 | 2026-10-30（查看当天走势）
```

链接带 `focus` 与返回地址；没有 `trigger_bar_ts` 的历史行显示「—」，不猜。

### 6.2 个股页与图

- `symbol_page` 读 `focus`（`YYYY-MM-DD`，形状不对就当没给，不 400——它只是
  一个视图参数）；
- `chart_partial` 增 `focus: str = ""` 参数，透传进模板；周期切换/指标切换的
  htmx 链接保留 `focus`；
- `partials/chart.html` 的 `#chart` 加 `data-focus="{{ focus }}"`；
- `app.js` 的 `klineChart`：找到日期 → `markLine` + `dataZoom` 收窗；
  找不到 → 图注追加一句。

### 6.3 单票静音

- 详情页「闭环」卡加一个表单：`POST /alerts/{id}/mute`（可选备注），
  已静音时改为 `POST /alerts/{id}/unmute` 并显示静音时刻/备注；
- 静音键取告警上的 `(market, code)`，页面上写明「静音的是这只票的全部规则」；
- 收件箱分组标题显示「已静音」徽标（`MuteRepo.list_for` 一次取全，页内比对，
  不逐组查库）；
- 端点全部走 `csrf_protect`、归属闸 `_require_own`、`audit(...)`。

### 6.4 批量已读

- 收件箱每行 `new` 状态前加 checkbox（`name="ids"`），表头加全选；
- 页面底部或筛选表单下方一个 `POST /alerts/ack` 表单，隐藏 `next` 保存当前
  筛选 URL；
- 端点 `acknowledge_many`，303 回 `next`（只接受以 `/alerts` 开头的本站路径，
  其余回 `/alerts`）；回显「已标已读 N 条，跳过 M 条」；
- 无 checkbox 的已读/终态行不提交，也不显示复选框。

## 7. 测试计划（TDD，红→绿）

存储层：

- 迁移后 `alerts` 有新列、两张新表存在；
- `AlertStateRepo.set` 重复写是更新不是插两行；`clear` 只在非 clear 时写、
  保留 `bar_ts`；
- `MuteRepo.add` 幂等、`remove` 返回真假、`list_for` 只列本人；
- `acknowledge_many` 全成功、部分跳过（已处置/别人的 id）、写事件历史。

引擎层（用现有 `Harness`）：

- 首次命中 → `raised` 一条，状态表有该 stage；
- 同一天再跑 → `raised` 为空、`suppressed` 理由含「状态未变」；
- 命中→没到→再命中 → 第二条照发（`clear` 写回生效）;
- 过滤器挡下的命中不写状态（时段外跑一次，窗口内再跑仍能发）;
- 静音后命中 → 不推、状态更新；解除后同 stage 不补发；再跃迁才发；
- `is_test=True` → 照发且不写状态；
- `dry_run=True` → 不写状态、不写 clear；
- 发出的告警行 `trigger_bar_ts`/`dedupe_key` 正确；
- 推送抛错 → 状态不变，下一趟还能发。

Web 层：

- 详情页有触发 bar 链接（无证据时是「—」）；
- 个股页 `?focus=...` 渲染 `data-focus`，切换周期的链接仍带 focus；
- 静音 POST 后详情页出现「取消静音」，收件箱分组有徽标；
- 批量已读：勾两条只 ack 一条是 new 的，回显数字对，跳转回原筛选；
- 非本人/不存在 id 不被 ack。

回归：目标测试全绿 → `ruff check src tests` → 全量 pytest → 实机重启后验证
「重跑 0 新增 / 跳回 bar / 静音生效」。

## 8. 明确不做

- **趋势规则**（连续 N 期方向、距阈值 <X%）只进结论与摘要列，不在本次实现；
- 静音的**到期时间**：本次只有开/关；
- 静音期间**补发摘要**：解除后不补；
- `alerts` 历史行的 `trigger_bar_ts` 回填：老行没有可靠来源，留空；
- 收件箱按「静音票」筛选/折叠。

## 9. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 首次上线每条命中的规则各发一条 | 可接受的一次性成本；文档写明，且状态表为空正是「初始」语义 |
| `clear` 写回变成每对象一写 | 条件更新 `WHERE state <> 'clear'`，绝大多数命中 0 行；单进程同步一趟 |
| 状态键漏了 `account_id` 导致互相覆盖 | 键里含 `account_key`，测试专门钉「同票两账户各一条」 |
| 批量已读重放（刷新重提交） | POST → 303，回显数字；幂等跳过已读行 |
| `focus` 被当成任意日期注入 | 只做形状校验，原样进 `data-*`；模板自动转义；图注只显示字符串 |
| 静音让用户漏掉真破位 | 详情页明写「静音的是这只票的全部规则」；解除后状态照常跃迁；`strong` 级仍受 DND 既有规则保护（静音是显式动作，与 DND 不同） |
