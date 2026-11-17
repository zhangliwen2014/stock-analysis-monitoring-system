# 规则出口残留命令的去命令（渠道分流，D353）

## 背景

R3-02：网页用户没有终端，屏幕上不该出现「去敲 `sams …`」这种他们做不到的下一步。
D343 定的做法是**渠道分流**——共享层（服务、JSON、网页）只说事实，命令由 CLI 那一头
从结构化字段补回。D351 处理了「规则缺标的」注记，D352 处理了除权搬运缺口的四条命令。

D352 的设计文档把非 anchor 来源的残余留在「范围外，另案处理」。这一步就是那一案：
`services/rules/` 里还剩三处命令直接拼进了共享层字符串，而这三处都会上网页。

| 位置 | 字符串 | 流向 |
|---|---|---|
| `engine.py:637` `_verdict` 的 `KeyError` 网 | `` `sams rule edit` 改它（…见 `sams rule kinds`） `` | `Skipped.reason` → 网页「判不了」+ `rule check --json` + CLI |
| `filters.py:119` `no_open_alert` | `` 先处理那一条（`sams alert handle`） `` | `Suppressed.reason` → 网页「压住」+ `rule check --json` + CLI |
| `maintenance.py:370` `_why_zero` 的 watchlist 分支 | `` （M3-C 没有 `sams watch` 命令…） `` | `RuleView.notes` → 网页规则详情/试算 + CLI `rule show` |

## 现状：三处各自的出口

1. **`Skipped.reason`**（D352 已加 `fixes` 通道）：网页
   `templates/partials/rule_outcome.html` 的「判不了」只印 `reason`；CLI `_print_check`
   在 reason 之后补 `（…）`；`_check_payload` 只放 `reason`。这一步只需把命令从 reason
   挪进 `fixes`。
2. **`Suppressed.reason`**：网页「压住」与 JSON 都只读 `reason`，CLI `_print_check` 只印
   `reason`。`Suppressed` 目前没有命令携带位，需要补一个与 `Skipped.fixes` 同形的字段；
   过滤器是纯函数，需要把「哪个开关挡下的」连同理由一起交回引擎。
3. **`RuleView.notes`**：网页与 CLI 读同一份字符串，且这句话里的「命令」其实是一句
   **不存在**的说明（M3-C 还没有那个写入口）。它不是「下一步去哪敲」，对网页读者只是
   一句内部黑话；改成中性表述即可，不需要结构化通道。

## 方案

### 1. `_verdict` 的 `KeyError` 网

* `reason` 只留事实与网页读者能做的动作：
  `` 这条规则用不了 'price'：那一行是老模板写的，请按当前模板重填这条规则。 ``
* `fixes=(f"`sams rule edit` 改它（要哪几个、能取哪些值见 `sams rule kinds`）",)`，
  交给 D352 已铺好的 `_skipped(..., fixes=...)`。

### 2. `no_open_alert`

* 新增 `FilterHit`（`filters.py`）：

  ```python
  @dataclass(frozen=True)
  class FilterHit:
      reason: str                     # 中性：进网页、JSON、CLI 正文
      fixes: tuple[str, ...] = ()     # 只给 CLI：下一步命令
  ```

* `run_filters` 返回 `FilterHit`；全过开关时返回 `FilterHit("")`。
* 命令按开关名登记在一张表里，四个过滤器函数本身仍只返回句子（不改四个签名）：

  ```python
  FILTER_FIXES = {"no_open_alert": ("`sams alert handle` 处理掉那一条",)}
  ```

* `filters.no_open_alert` 的句子去掉括号里的命令，收在「先处理那一条」。
* `Suppressed` 增加 `fixes: tuple[str, ...] = ()`（注释同 `Skipped.fixes`：网页/JSON 不读）；
  `engine._suppressed(..., fixes=())` 原样带上；`_one` 用 `hit.reason` 与 `hit.fixes`。
* CLI `_print_check` 的「压住」循环在 reason 之后补 `（{"；".join(item.fixes)}）`。

### 3. `_why_zero` 的 watchlist 分支

括号改成 `（自选清单的维护入口还没上线）`：对网页读者是中性事实，对 CLI 读者也不再
指向一条不存在的命令。无结构化通道。

## 接口与不变式

* 共享层不变式：**凡进网页、进 JSON、进 `RuleView.notes` 的字符串，不含 `` `sams ` ``。**
* CLI 不变式：`rule check` 文本对这两类出口仍带得出 `sams rule edit` / `sams alert handle`；
  `rule check --json` 不新增命令字段。
* `FilterHit` 是值对象；过滤器仍不碰库（`test_the_gate_is_a_value_object_not_a_repo`）。
* `FILTER_FIXES` 的键与 `FILTER_KEYS` 不必一一对应：没有命令的开关缺省为空元组。

## 测试（先红后绿）

* `tests/services/test_rules_filters.py`
  * 测试助手 `run()` 改读 `.reason`；`no_open_alert` 命中时 `hit.fixes` 含 `alert handle`，
    其余开关命中时 `fixes == ()`。
* `tests/services/test_rules_engine.py`
  * `test_a_row_missing_its_param_is_reported_not_a_crash`：reason 不含 `sams `，
    `fixes` 含 `sams rule edit`；
  * `no_open_alert` 压制用例：reason 不含 `sams `，`suppressed[0].fixes` 含 `alert handle`。
* `tests/cli/test_rule_cli.py`
  * `rule check` 文本含 `sams alert handle`；
  * `--json` 的 suppressed reason 不含 `sams `（与 skipped 那条同形）。
* `tests/web/test_rules_web.py`
  * 现有「压住」那一行仍印中性 reason；给假 `Suppressed` 带 `fixes`，断言页面无 `sams `。
* `tests/services/test_rules_maintenance.py`
  * 空自选清单注记不含 `sams `。

## 验证

* 依次串行：`ruff check src tests`；目标五组 pytest；最后全量 `pytest`
  （`faulthandler_timeout` 兜底，观察与本次无关的网络型挂起）。
* 生产库副本 + 临时服务：造一条 `no_open_alert` 压制与一条老模板 `KeyError` 规则
  （只在**副本**上），网页 `/rules/{id}/check` 无 `sams `；CLI 文本带命令、`--json` 中性。
* 生产 `sams serve` 重启后 `/healthz`、`/login` 200（优雅停会因非守护调度线程卡住不放
  DuckDB 锁，必要时 `kill -9` 旧进程再起——D351/D352 同一现象）。

## 范围外

* D352 范围外仍保留：告警历史正文（`alert show`）不在本步补命令。
* `services/rules/*.py` 里只在 docstring/注释出现的 `sams …`（如 `engine.py:115`、
  `filters.py:35`、`anchor.py:78`、`notes.py:111`、`derive.py:76`）不是用户可见文本，不动。
* 其它服务层（非 rules）里可能存在的「共享层带命令」：按同一模式另案扫。

## 回归结果

（待填）
