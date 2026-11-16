# 除权搬运缺口的说明去命令（渠道分流，D352）

## 背景

R3-02：网页用户没有终端，屏幕上不该出现「去敲 `sams …`」这种他们做不到的下一步。
D343 定下的做法是**渠道分流**——共享层（服务、JSON、网页）只说事实，命令由 CLI 那一头
据一个结构化标记补回（D351 已按此法把「规则缺标的」注记拆开）。

这一枚处理同一类问题的另一处：`services/rules/anchor.py` 在「人填的价搬不动」时，把
**下一步命令**直接拼进了要上屏的那句话里。四条命令：

| 位置（`anchor.py`） | 命令 | 何时用 |
|---|---|---|
| `carry()` 里 `on < floor` 分支（约 128 行） | `sams sync daily --market M --code C` | 那个价属于库里第一根 K 线之前 |
| 参数重填（约 166 行） | `sams rule edit` | 参数那一行没有日期戳 |
| 成本重记（约 178 行） | `sams ledger buy --on 成交日` | 建仓成本没有日期戳 |
| 论上的止损/目标（约 184/190 行） | `sams thesis revise` | 论上那条线/目标没有日期戳 |

## 现状：这些句子流到哪

`move_prices` 把每条「搬不动」写进两个出口，二者都**带着命令**：

1. **判据要用它**（`Template.judged`）⇒ `Relocation.blocked`（`tuple[str, …]`，句子之间
   用「；」拼）。`engine._one` 见 `placed.blocked` 非空就 `_skipped(reason="；".join(…))`，
   于是落进 `RunReport.skipped[].reason`：
   * 网页 `rule_check.html` 的「判不了」那几行（`web/pages/rules.py:546` 取 `item.reason`）——**要中性**；
   * CLI `rule check` 文本（`cli/rule.py:631` 直接印 `item.reason`）——**要保留命令**；
   * CLI `rule check --json`（`_check_payload` 的 `reason`）——**要中性**（与 D351 一致）。
2. **只是显示那一列**（非 `judged`）⇒ `Relocation.snapshot.missing`（`tuple[str, …]`）。
   `engine._lines`（约 905 行）把它拼进告警正文。这份正文是**一份字符串**，同时被：
   * 网页告警页 `alert.html:42`（`{{ body }}`）——**要中性**；
   * 外部通知器（钉钉/邮件/push/企微）——**要中性**（读的人也没有终端）；
   * CLI `alert show`（读库里的历史正文）——历史正文已落库为中性；
   所以告警正文这一路只能整体中性，不能在正文里塞命令再指望按渠道去掉。

调度器（`services/scheduler/jobs.py:800` 一带）只**计数** `skipped`，不印理由，因此无需改。

## 方案：一句话事实 + 结构化缺口

沿用 D343/D351 的分流，把「原因」与「修它的命令」分成两件东西：

1. `anchor.py` 里，`consequence()` 只产出**中性句子**（丢掉 `{fix}` 尾巴）。`carry()` 仍
   返回「修它的那一步」（命令）与「原因」，但两者不再拼在一起。
2. 新增一个小结构，把「这句话属于哪种缺口、下一步是哪条命令」带出去：

   ```python
   @dataclass(frozen=True)
   class Gap:
       sentence: str   # 上屏/入库的中性事实（不含任何 `sams …`）
       fix: str        # 只给 CLI 的下一步命令（判据要用它时才有意义）
   ```

   `Relocation.blocked` 由 `tuple[str, …]` 改为 `tuple[Gap, …]`；`.blocked_sentences`
   供需要纯文本处（若还有）拼接。
3. `engine._one`：`_skipped(..., "；".join(g.sentence for g in placed.blocked))`，并把
   `tuple(g.fix for g in placed.blocked if g.fix)` 交给一个**只供 CLI**的携带位。
   `Skipped` 增加 `gaps: tuple[str, …] = ()`（命令，网页/JSON 不读）。
4. CLI `rule check` 文本：印 `item.reason` 后，若有 `item.gaps`，再补
   `（{"；".join(item.gaps)}）`；`--json` 的 `reason` 保持中性，不新增命令字段。
5. 非判据（显示列）那一路：`snapshot.missing` 只收中性句子；命令不再写入告警正文
   （显示列缺口不挡判定，中性那句话已经把「按原值显示、可能偏高」说清了，D55 的「下一步」
   在这里指向的是重填参数这个动作本身，而不是一句网页读者敲不了的命令）。

## 接口与不变式

* 共享层不变式：**凡进网页、进告警正文、进 JSON 的字符串，不含 `` `sams ` ``。**
* CLI 不变式：`rule check` 文本对**判据类**缺口仍带得出完整命令（`rule edit` /
  `ledger buy` / `thesis revise` / `sync daily` 按缺口种类）。
* `Gap.fix` 为空串表示「这个缺口没有单独的命令要给 CLI」（极少；主要出现在非判据路）。

## 测试（先红后绿）

* **服务层** `tests/services/…`（anchor / engine）：
  * 构造一个有除权、锚在 `bars_from` 之前（或没有日期戳）的判据类缺口，断言
    `RunReport.skipped[].reason` **不含** `sams `，且 `skipped[].gaps` 里是 `sams sync daily …`
    / `sams rule edit` / `sams ledger buy` / `sams thesis revise` 之一。
  * 非判据缺口：`snapshot.missing` 顶部句子中性。
* **CLI** `tests/cli/test_rule_cli.py`：
  * `rule check` 文本含对应命令；
  * `rule check --json` 的 `reason` 不含 `sams `。
* **网页** `tests/web/`：
  * `rule_check.html`（经试算）那行 reason 不含 `sams `；
  * 告警正文（`alert.html` 的 `body`）不含 `sams `。
* **调度器**：现有计数用例不变即证没被带歪。

## 验证

* 依次串行：`ruff check src tests`；目标四组 pytest；最后全量 `pytest`（观察是否再遇到
  与本次无关的网络型非 live 用例挂起；用 `faulthandler_timeout` 兜底）。
* 生产库副本 + 临时服务：找一条会触发搬运缺口的规则（或按需在**副本**上造一条），
  `GET /rules/{id}/check` 那几行无 `sams `；CLI `rule check` 文本带命令、`--json` 中性。
* 生产 `sams serve` 重启后 `/healthz`、`/login` 200。（重启注意：优雅停会因非守护调度线程
  卡住不放 DuckDB 锁，必要时 `kill -9` 旧进程再起——见 D351 部署记录。）

## 范围外（留给后续）

* 告警正文/`alert show` 这一历史文本：本步只保证它**中性**；若将来要在 CLI `alert show`
  里补回命令，需要把缺口结构随告警一起入库，另起一步。
* `_verdict` 的 `KeyError` 网、`_no_impl` 等**非 anchor 来源**、但同样含命令的 skipped
  理由，不在本步；它们各自有自己的「下一步」措辞，另案处理。

## 回归结果

* **全量串行 pytest**：`5926 passed, 21 deselected, 4768 warnings in 4055.19s (1:07:35)`，
  退出码 0，无挂起/看门狗击杀。
* **ruff**：`ruff check src tests` → All checks passed。
* **目标子集**（anchor / engine / ex-rights / intraday / web rules / CLI rule）：194 passed。
* **真数据**（生产库副本 + `sams rule check --dry-run --user sp2expert4`）：4 条规则、判了
  4 个对象、0 skipped，端到端跑通不崩；真实规则都盖了日期戳，故未触发搬运缺口（那条路由
  anchor/engine 的单测覆盖）。
* **渠道分流落地**：
  * CLI `rule check` 文本在 reason 后补 `（…）` 命令，`--json` 与网页/告警正文只印中性
    reason（新增 `test_check_json_keeps_the_gap_reason_neutral`、web fake `fixes` 不渲染、
    engine `test_a_display_only_gap_keeps_its_command_off_the_alert_body` 佐证）。
  * 告警正文由 `snapshot.missing` 拼出，已整体中性：网页告警页与外部通知器都不再带命令。
* **旁注**：收尾处有 `nanobind` 引用计数警告，是依赖的环境噪声，不影响退出码（D351 同样出现）。
