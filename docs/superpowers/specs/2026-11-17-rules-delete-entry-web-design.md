# 规则列表的删除入口（R3-12，D354）

## 背景

R3-12：专家在 `/rules` 上找不到删除入口。列表每行只有「试算 / 判一次」与「停用 /
启用」；试 `/rules/{id}/delete` 得到 404，而真实端点是不带 GET 的
`POST /rules/{id}/remove`——那一条原先只给「示例结果页」的收尾按钮用。

这不是能力缺失，是**入口缺失**：`RuleRepo.remove` 与 `rule_service.remove` 一直都在，
CLI `sams rule remove --yes` 也一直能删。缺的是网页上一条人找得到的路，以及删除前的
一次当面确认（修订史会跟着没有，见 §9.2 的取舍）。

同时，`RuleRepo.remove` 对派生规则的拒绝句里直接写了 `` `sams rule disable` ``。这句话
现在会出现在网页（列表页 400 的 `error`）与 CLI 的 JSON 拒绝里——R3-02 的渠道分流
（D343/D351/D352/D353）要求共享层只讲事实，命令由 CLI 自己补回。

## 现状

| 位置 | 现在 | 问题 |
|---|---|---|
| `templates/rules.html` 行动作 | 试算/判一次、停用/启用 | 无删除入口 |
| `GET /rules/{id}/delete` | 未注册，404 | 那是人凭直觉会先试的地址 |
| `POST /rules/{id}/remove` | 已存在，CSRF 保护 | 只被示例结果页调用；无确认屏 |
| `RuleRepo.remove` 的派生拒绝 | 「…只能停用（`sams rule disable`）：…」 | 命令进了网页与 JSON |
| `rule_check.html` 示例收尾 | 直接 POST remove | 示例规则刚建出来、已停用，保留直达 |

## 方案

### 1. 列表加「删除」，先过一屏确认

* `rules.html` 每行动作后补一个链接：
  `/rules/{{ row.rule.id }}/delete`，弱化样式，与表单按钮并排。
* 新增 `GET /rules/{rule_id}/delete` → `rule_delete_page`：
  * `require_user` + `_own_view`（不存在与不是你的都 404，与编辑页同一口径）；
  * 渲染新模板 `rule_delete.html`（`card narrow`，与 `watchlist_purge.html` 同形）：
    名称、条件、对象、当前启停状态；一句后果：「删除之后修订史一起消失，此后没人能
    回答『这条线曾经在哪』」；
  * 普通规则：一个带 CSRF 的表单 `POST /rules/{rule_id}/remove`，按钮「删除这条规则」，
    外加「先不删，回规则列表」。
* 既有的 `POST /rules/{rule_id}/remove` 不变（303 `/rules?notice=removed`；拒绝时
  列表页 400 + `error`）。示例结果页那个直达按钮保持原样：它删的是刚建出来、明确
  标着「示例」且已停用的临时规则，多一屏确认只是摩擦。

### 2. 派生规则：确认屏只解释，不给删除按钮

派生规则删不掉是 §9.2 的刻意设计（「悄悄关掉止损提醒」必须留下能查到的记录）。
确认屏对它们不动用「拒绝后跳回列表」那一路，而是**进门就把话说清**：

* 标题与事实照旧，正文换成中性的一句：
  「这条规则是从一条观点派生出来的，不能删除，只能停用：『悄悄关掉止损提醒』
  必须留下一条能查到的记录。」
* 下一句给出网页读者做得到的动作：「要让它不再响，回列表点『停用』即可。」
* 不渲染 remove 表单，只留返回链接。

### 3. 拒绝句去命令，CLI 文本补回

* `RuleRepo.remove`：删掉 `（`sams rule disable`）`，保留 §9.2 的理由。于是网页与
  `rule remove --json` 读到的都是中性事实。
* `cli/rule.py` 的 `run_remove`：在 `remove` 调用外面单独包一层 `SamsError`。当
  `view.rule.thesis_id` 为真且**不是** `--json` 时，把
  `` `sams rule disable {rule_id}` `` 补在这句拒绝后面再交给 `refused`；JSON 走原句。
  `--yes` 那道拒绝不动：它本来就只在 CLI 里生成，且已经指明了 disable。

## 接口与不变式

* 新增 `GET /rules/{rule_id}/delete`（HTML 确认页）；`POST /rules/{rule_id}/remove`
  仍是唯一执行删除的端点。
* 共享层不变式（延续 D343）：`RuleRepo.remove` 与网页响应里的字符串不含 `` `sams ` ``。
* CLI 文本不变式：派生规则的 `rule remove --yes` 拒绝里带得出
  `` `sams rule disable <id>` ``；`--json` 的 `reason` 保持中性。
* 删除仍需要 CSRF；GET 确认页只读，不改任何行。

## 测试（先红后绿）

* **网页** `tests/web/test_rules_web.py`：
  * 列表页含 `/rules/{id}/delete` 链接；
  * GET 确认页 200，含规则名、条件、`action="/rules/{id}/remove"`、CSRF 与返回链接；
  * POST remove（`follow_redirects=False`）→ 303 `/rules?notice=removed`，随后列表里
    没有这条规则；
  * 派生规则（`thesis_id` 非空）的确认页 200：含「不能删除」，**不含** `sams `，
    也不渲染 remove 表单；
  * 直接 POST 派生规则的 remove → 400，`error` 中性（不含 `sams `）。
* **服务层** `tests/services/test_rules_derive.py`：既有的「不能删除」用例补一句
  `"sams " not in str(exc.value)`。
* **CLI** `tests/cli/test_rule_cli.py`：
  * 原有派生拒绝的 JSON 用例改为断言 `reason` 中性（不含 `sams `、含「不能删除」）；
  * 新增文本用例：`rule remove --yes` 的拒绝文本含
    `` `sams rule disable <id>` ``，退出码 2。

## 验证

* 依次串行：`ruff check src tests`；目标三组 pytest（web rules / CLI rule / services
  derive）；最后全量 `pytest`。
* 生产库副本 + 临时服务：
  * `/rules` 每行有删除链接；打开一条普通规则的确认页、取消返回；
  * 在**副本**上建一条临时规则走完确认页删除，列表不再出现；
  * 找一条派生规则（或按需在副本上造一条）确认页只解释、不带命令、不给按钮；
  * CLI：派生规则 `rule remove --yes` 文本带 disable 命令、`--json` 中性。
* 生产 `sams serve` 重启后 `/healthz`、`/login` 200。

## 范围外

* 批量删除、软删除/回收站、删除后撤销——都不在 R3-12 的问题里。
* 示例结果页那个直达删除按钮不在本步改道，理由见「方案 1」。
* `/rules/{id}/delete` 的 POST 别名：只保留 R3-12 提到的直觉地址作 GET 确认屏，
  执行端点继续只有 `/remove` 一个。

## 回归结果

* **全量串行 pytest**：`5936 passed, 21 deselected, 4785 warnings in 3779.66s (1:02:59)`，
  退出码 0，无挂起/看门狗击杀。
* **ruff**：`ruff check src tests` → All checks passed。
* **目标子集**（web rules / CLI rule / services derive）：285 passed。
* **真数据**（生产库副本 `tmp/d354`，`SAMS_DB_PATH` 指向副本，临时服务 19524/19665）：
  * `/rules` 每行出现「删除」链接；打开普通规则的确认页 200，含规则名、条件、
    POST `/rules/{id}/remove`、csrf 与取消链接，页面无任何写操作的 GET 语义。
  * 副本上造一条临时规则走完确认页删除：303 → `/rules?notice=removed`，列表中消失。
  * 派生规则确认页 200：只有「不能删除，只能停用」的解释与（启用时的）「回列表点停用」，
    没有删除按钮/表单，文本不含 `` `sams ``。
  * 对派生规则直接 POST `/remove`：400，理由中性（含「不能删除」，无 `` `sams ``）。
  * CLI：派生规则 `rule remove --yes` 文本在拒绝句后补
    `` （`sams rule disable <id>`） ``，退出码 2；`--json` 的 `reason` 不含 `` `sams ``。
* **生产重启**：`sams serve` 重启后 `/healthz`、`/login` 均 200。
