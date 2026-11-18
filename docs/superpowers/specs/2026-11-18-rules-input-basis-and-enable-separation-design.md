# 规则试算标注输入基、保存与启用分离（D358）

**日期**：2026-11-18
**关联**：R3-14（第三轮专家实测：试算读的是最新日线收盘而非盘中报价，且保存即启用）、
D231（判一次 / 示例演练）、D283（网页不出现命令行）、D357（均线基准落表单）
**范围**：`src/sams/web/pages/rules.py`、`src/sams/web/templates/rule_form.html`、
`src/sams/web/templates/rule_check.html`（若需要）、两处测试文件。不改引擎。

## 背景

第三轮实机验收里，波段与风控两位都报了 R3-14：

* 试算（`/rules/{id}/check`、`/rules/preview`）与盘中真判停不同基——试算读的是
  库里最新一根日线收盘，调度器盘中读的是实时报价，页面没有把这件事说出来；
* 新建规则一保存就是启用状态：人还在改数、还没确认，调度器已经开始看它了。

两句都在「同一件事两个说法」的清单里，改法是让页面如实说、让启用成为单独的一步。

## 现状与约束

* 引擎 `preview()` 把 `dry_run=True` 写死，且「走收盘那一趟的口径（`intraday=False`、
  不给行情板）」（`engine.py` 的 docstring）。网页 `/rules/{id}/check` 调
  `check()` 时也不传 `quotes`，因此**两个网页入口都是收盘口径**。
* `_preview` 已经带一句 lead：「这次试算读的是最新一根日线收盘，没读盘中报价：
  保存后调度器会按实时报价继续看。」但它没说保存后默认不启用，措辞也与检查页不一致。
* `rule_check.html` 只有「试运行 / 真跑」那一句（`partials/rule_outcome.html`），
  **没有**输入基说明。
* `RuleService.add` 的 `enabled` 默认是 `True`；`rule_create` 不传，
  所以网页保存即启用。示例（`/rules/demo`）显式传 `enabled=False`，是既有反例。
* 编辑页（`rule_edit`）不动启停；列表已有 `POST /rules/{id}/toggle` 与
  `_NOTICES` 的 `on` / `off` / `created` / `updated` / `removed`。白名单之外的一律不上屏。
* 逐对象的报价日已经在命中的那一行显示（`hit.data_on` → 「数据 YYYY-MM-DD」）；
  未命中的对象没有逐条日期可读，这一屏不编一个。

## 方案

### 1. 输入基：一句话，两个入口

在 `pages/rules.py` 里落两条模块常量（中文、无命令）：

* `_CHECK_BASIS`（检查页：试算 / 判一次）：
  「这一趟按收盘口径判：读每只标的库里最新一根日线收盘，没读盘中报价。
  启用中的规则在盘中由调度器按实时报价判。」
* `_PREVIEW_BASIS`（保存前试算）：
  「这次试算按收盘口径判：读库里最新一根日线收盘，没读盘中报价。
  保存后规则默认不启用；启用后调度器会在盘中按实时报价继续看。」

`_check_page` 把它交给 `_outcome_rows(..., lead=_CHECK_BASIS)`；
`_preview` 改用 `_PREVIEW_BASIS`。两句都出现在既有 partial 的 lead 位置
（「试运行 / 真跑」那一行下面），两个入口同源、只差后半句。

不把具体 bar 日期塞进 lead：命中的行已经有「数据」那一格，未命中的对象没有逐条
日期可读，写「评估日」会被误解成报价日（R3-04 的教训）。

### 2. 保存与启用分离

* `rule_form.html`：新建态（`rule_id` 为空）在提交按钮前多一格复选框
  `name="enabled" value="1"`，文案「保存后立即启用（不勾：先存成停用，确认后再启用）」，
  **默认不勾**。编辑态不渲染这一格——编辑从来不动启停，启停只在列表那一行的
  「启用 / 停用」按钮上。
* `rule_create`：`enabled=one(data, "enabled") == "1"` 传给 `RuleService.add`。
* 303 的 notice 分两支：
  * 勾了 → `notice=created`：既有「规则已保存并启用。」；
  * 没勾 → `notice=created_off`：「规则已保存，尚未启用：在列表那一行点『启用』。」
* `_NOTICES` 增 `created_off`；两个出口都带 `#{id}` 锚点，落在那一行。
* 示例（`/rules/demo`）、命令行 `sams rule add`、编辑页的启停语义都不变。

## 接口与不变式

1. `POST /rules/new` 不带 `enabled` ⇒ 新建规则 `enabled=False`；带 `enabled=1`
   ⇒ `enabled=True`。编辑不改启停。
2. 两种保存出口都在 `/rules` 上给出对应 notice，且 URL 带 `#{id}`。
3. `/rules/{id}/check`（试算与真跑）与 `/rules/preview` 都出现输入基说明，
   包含「最新一根日线收盘」与「没读盘中报价」；预览那句另外说明保存后默认不启用。
4. 页面全文不含 `sams `（D283）。
5. 示例规则仍以停用状态建出（既有合同）。
6. `_NOTICES` 仍只认白名单，query 原文不上屏。

## 测试（先红后绿）

`tests/web/test_rules_web.py`：

* 新建页渲染 `name="enabled"` 的复选框，且不带 `checked`；
* `POST /rules/new`（不带这一格）→ 库里那条 `enabled is False`，列表页出现
  「规则已保存，尚未启用」；
* 同一屏带 `enabled=on` 提交 → `enabled is True`，列表页出现「规则已保存并启用」；
* 既有 `test_saving_says_so_on_the_list_it_returns_to` 改成上面第一支的期望；
* `/rules/{id}/check`（试算）页面出现「最新一根日线收盘」「没读盘中报价」。

`tests/web/test_rules_form_price.py`：

* `/rules/preview` 页面出现「最新一根日线收盘」「没读盘中报价」与「保存后规则默认不启用」。

## 验证

* 串行：目标两组（rules_web、rules_form_price）、ruff、全量 pytest 按目录分段
  （理由同 D356/D357：单进程长跑会踩到 polars / DuckDB 的退出期问题）。
* 生产库副本（`tmp/d358` 复制 `data/sams.duckdb` + `.wal`，`sams serve --port 8766`）：
  * POST `/rules/new` 不勾启用 → 303 落在「尚未启用」，列表页该行在「已停用」组；
  * 再建一条勾启用 → 列表页在「启用」组；
  * `/rules/{id}/check` 试算页有输入基那两句；
  * 各页 `sams ` 出现次数为 0。
* 生产重启后 `/healthz` 200、`/rules` 未登录 303。

## 范围外

* R3-25 数字格式化、R3-26 乱码存量：T7 其余两项，另案。
* 命令行 `sams rule add` 的默认启用语义：本轮只改网页（命令行用户有显式开关）。
* 在预览里读盘中行情板（页面拿板会把「还没轮到轮询」读成「这只票没价」，
  `preview()` 的既有取舍）。
* 逐对象的输入日进入报告（`RunReport` 只带 `as_of` 与命中行的 `data_on`）。

## 回归结果

（实现与验证完成后填写。）
