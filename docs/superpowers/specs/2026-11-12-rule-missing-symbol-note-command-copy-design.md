# 规则缺标的注记去命令（D351）

## 需求

R3-02 点名网页里的命令行话术。规则列表页（`rules.html:39` 原样转述 `row.view.notes`）
在一条 `symbol` 规则的标的不在主档里时，经**服务层字符串**印 `sams sync daily`：

* `services/rules/maintenance.py::_notes`（344-348 行）：`target_type == "symbol"` 且
  `self.data.symbols.get(market, code) is None` 时追加
  `库里没有 {target_ref} 这条标的：读不到现价就不判任何价格类条件，先 \`sams sync daily
  --market {m} --code {c} --start 起 --end 止\`（那条链会顺带把主档这一行补上）`。

**这条注记由 `RuleService.view()` 生成，被两个渠道共用**（与 D350 同形，按 D343 分流）：

* **网页**：`web/pages/rules.py:451 list` → `rules.html:39` 把 `view.notes` 用「；」拼起来印。
* **CLI**：`cli/rule.py::_print_view`（304 行，show/add 逐条印 notes）、`run_list`（369-370 行，
  只印 `notes[0]`）；`--json` 的 `_rule_payload`（250 行）也带 `notes`。
* 规则**表单页**与**试算出口**不转述这条 note（表单页另有 `_line_notes`，试算走引擎那一份
  `RunReport`），不在本次范围。`cli/rule.py::_anchor_line`（275 行）那句 `sams rule edit`
  是 CLI 独有函数、不上网页，也不在范围。

> 范围界定：引擎 `anchor.py` 的除权搬运注记（`sync daily`/`rule edit`/`ledger buy`/
> `thesis revise` 四条命令，经 `report.skipped` 上 `rule_outcome.html` 与 CLI check）是
> 另一条更深的共用链（还连调度器证据），留作 **D352**，本次只动维护面这一条 note。

## 取舍

1. **共享注记改渠道中性：只说事实，不带命令。** 去掉「先 `sams sync daily …`（那条链会顺带
   把主档这一行补上）」那一截，留下 `库里没有 {target_ref} 这条标的：读不到现价就不判任何价格类条件`。
   `RuleService` 不知道本市场有没有日线源、这只票在不在票池，所以**不能**无条件说「盘后会自动补」
   （D336/D347 同一条诚实规矩）；事实本身两个渠道都读得通。
2. **网页那一头不补命令、也不另加链接：中性事实即可。** 规则列表的「对象」列本就带
   `target_url`（`rules.html:42`，`_target_url(rule)` 指到个股页）——网页用户点它就能到个股页，
   那里才有补采三态（D338）。所以网页只需把命令去掉，导航已存在，不承诺自动补、不注入新依赖。
   **模板无需改**：共享层中性后 `rules.html:39` 自动印中性事实。
3. **CLI 那一头补回命令：给 `RuleView` 加一个 `symbol_gap` 标记字段，CLI 据此补命令。**
   `notes` 是混合元组（停用／零对象／缺标的／通道不可用），不像 D343 归因那样有独立的 `skipped`
   字段。为了不动显示顺序与 JSON 形状，缺标的 note **仍留在 `notes` 里**（中性），同时把它的
   原文放进新字段 `RuleView.symbol_gap`（无缺口时为 `""`）。CLI 逐条印 note 时，对
   `note == view.symbol_gap` 的那一条追加完整命令——精确等值匹配（不是前缀猜测），契约干净：
   `symbol_gap` 非空当且仅当缺标的 note 在 `notes` 里，且等于它的原文。命令带 `--market/--code`
   （`--start 起 --end 止` 是日期占位符：上市日未知，不假装能补一个具体区间），market/code 由
   `rule.target_ref` 拆出。`--json` 的 `notes` 保持中性（数据，不是操作指南，D343）。
4. **不新增队列活、不动模板、不动其余 note。** 停用／零对象／通道不可用三条 note 不带命令，
   原样不动；`missing_kind` 之类语义不涉及。

## 契约

* `services/rules/maintenance.py`：
  * `RuleView` 增 `symbol_gap: str = ""`（缺标的 note 的中性原文；无缺口为 `""`）。
  * 新增 `_symbol_gap(self, rule) -> str`：`target_type == "symbol"` 且主档缺该行时回
    `f"库里没有 {rule.target_ref} 这条标的：读不到现价就不判任何价格类条件"`，否则 `""`。
  * `_notes(self, rule, targets, gap)` 增一个 `gap` 入参：缺标的支改为 `if gap: out.append(gap)`
    （中性，无命令）；其余支不变。
  * `view()`：`gap = self._symbol_gap(rule)`，`notes=self._notes(rule, len(targets), gap)`，
    `RuleView(..., symbol_gap=gap)`。gap 只算一次，既进 `notes`（保显示顺序）又进标记字段。
* `cli/rule.py`：
  * 新增 `_gap_command(rule) -> str`：从 `rule.target_ref` 拆 market/code，回
    `f"（先 \`sams sync daily --market {market} --code {code} --start 起 --end 止\`，那条链会顺带把主档这一行补上）"`。
  * `_print_view`（304 行）：`for note in view.notes:` 印
    `note + (_gap_command(rule) if note == view.symbol_gap else "")`。
  * `run_list`（369-370 行）：`first = view.notes[0]`，印
    `first + (_gap_command(rule) if first == view.symbol_gap else "")`。
  * `_rule_payload`（250 行）不动（JSON `notes` 中性）。
* 模板、`web/pages/rules.py` 均无需改（共享层中性后网页自动去命令；导航 `target_url` 已在）。

## 测试

* `tests/services/test_rules_maintenance.py::test_a_symbol_that_is_not_in_the_library_is_flagged`
  （199 行）：保留 `assert any("库里没有" in note …)`，增
  `assert all("sams sync daily" not in note for note in view.notes)` 与
  `assert view.symbol_gap and "库里没有" in view.symbol_gap`（中性事实进标记字段）。
* `tests/cli/test_rule_cli.py`：新增
  `test_show_reappends_the_sync_daily_command_for_a_missing_symbol`——`BAD_CODE` 那条跑
  `rule show`（文本）：含 `sams sync daily --market A --code {BAD_CODE}`；并新增
  `test_list_reappends_the_sync_daily_command_for_a_missing_symbol`（`rule list` 文本同断言）。
  既有 `test_list_counts…`/`test_show_prints…`（313/323 行）只查「库里没有」，不动仍过。
* `tests/cli/test_rule_cli.py`：新增 `test_the_json_gap_note_stays_neutral`——`rule show --json`
  的 `rule.notes` 含「库里没有」、不含 `sams sync daily`。
* `tests/web/test_rules_web.py`：新增 `test_the_rules_list_gap_note_carries_no_command`——
  经服务建一条标的不在主档的 `symbol` 规则，`GET /rules`：文本含「库里没有」、不含
  `sams sync daily`（既有 986 行那条测的是 demo 路径，不覆盖列表缺口 note）。

## 验证

* 串行 pytest（`test_rules_maintenance.py` + `test_rule_cli.py` + `test_rules_web.py` +
  `test_rules_form_price.py`）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：建一条标的不在主档的 symbol 规则，`GET /rules` 那行 note 中性、
  无命令，「对象」列仍链到个股页。停临时服务释放写锁后对同规则跑 `sams rule show`/`rule list`：
  缺标的 note 带完整 `--market/--code` 命令，`--json` 的 notes 中性。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。

## 回归结果

* **全量串行 pytest**：`5924 passed, 21 deselected, 4767 warnings in 3880.69s (1:04:40)`，
  退出码 0，无看门狗击杀、无 faulthandler dump。D351 的目标用例（`test_rules_maintenance` /
  `test_rule_cli` / `test_rules_web` / `test_version_bumps`）都在这 5924 里全绿。
* **ruff**：`ruff check src tests` → All checks passed。
* **生产库副本真数据**（`tmp/d351_real.duckdb`，555 条主档）：库里本就有两条缺标的规则
  （`A/999999` 规则 `92614d5e…`、`ZZ/600519`），正好是真实缺口。
  * `sams rule show 92614d5e…` 文本：`已停用…` 那条不带命令；`库里没有 A/999999 这条标的…`
    那条尾部补回完整 `（先 \`sams sync daily --market A --code 999999 --start 起 --end 止\`…）`。
  * `sams rule show … --json`：`rule.notes = ['已停用…', '库里没有 A/999999 这条标的：读不到现价就不判任何价格类条件']`，
    整个 JSON 里 `sams sync daily` 出现 0 次（`HAS_CMD: False`）。网页打印的就是这同一份 `view.notes`，
    故真数据上网页也带不出命令；「对象」列既有的 `/symbol` 链接不变。
* **旁注（非 D351 引入）**：本次全量运行前，曾有两次在一条非 live、依赖网络的 TLS 用例上挂住
  （`_ssl__SSLSocket_do_handshake_impl`，与本次改动无关，D351 只动规则 note 文案）；网络正常后
  一次跑完全绿，未 kill 任何用例。