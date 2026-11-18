# 数字直排：选股结果按字段口径印、证据句里的小数不再原样带出（D359）

**日期**：2026-11-18
**关联**：R3-25（第三轮专家实测：数字直排）、D58（入库只有小数口径）、
D308（价格按源精度显示）
**范围**：`src/sams/web/display.py`、`src/sams/web/pages/screen.py`、
`src/sams/analysis/valuation/compose.py`、两处测试文件。
不改引擎、不改库、不改条件输入框的口径。

## 背景

第三轮实机验收里，价值/散户/量化三位都报了 R3-25：

* 估值证据句里直排机器精度：「分位 87.0902%」「91.3934%」「0.00929805」；
* `/screen` 的结果格按引擎原值印：股息率 `0.0536673`、ROE `0.2315`，
  而同一页的字段下拉与条件摘要都写着「小数比值（0.15 = 15%）」——
  页面自己说了口径，表格却不照它印。

两句是同一件事：数字的显示精度没有归口。引擎给的是原始读数，屏幕该按
**这一格自述的单位**决定怎么排。

## 现状与约束

* `core/screener_fields.Field.unit` 已经逐字段声明口径：
  * `小数比值（0.15 = 15%）`：`pr_now`、`pr_annual`、`roe`、`revenue_yoy`、
    `profit_yoy`、`dividend_yield`、`gross_margin`、`net_margin`、`roa`、`roic`、
    `debt_ratio`、`ma20_gap`、`ma60_gap`、`ma250_gap`、`pos_52w`；
  * `百分数（1.85 = 1.85%）`：`pct_chg`、`turnover_rate`、`amplitude`；
  * `倍`、`元`、`亿元`、`0–100`：其余字段。
* `web/display.py` 已有 `ratio_pct`（全站唯一乘 100 的出口）与 `price`、`money`。
  这两个助手只被模板用，路由里拼字符串的地方（`screen._hit_row`、
  `compose._fmt` 与证据句）绕过了它们。
* `compose.py` 的 `_fmt` 与各条证据句用的是 `f"{value:g}"`：`g` 默认 6 位有效数字，
  0.00929805 会原样印出；分位这一类 0–100 的百分数也是 `:g`，于是出现
  「分位 87.0902%」。
* 条件输入框（`screen._text`、`_url`）里的是**人填的原值**：字段口径写明
  「0.15 = 15%」，预填 0.15 是对的，不能在这里乘 100——否则同一条条件
  每次回填都会差一百倍。
* `/screen` 的价格那一格已经走 `price()`（D308），不归这一次改。

## 方案

### 1. 选股结果格：按字段单位印

在 `web/display.py` 增加一个只看单位字符串的纯函数：

```python
def screen_value(unit: str, value: float | None) -> str:
    """选股结果格按字段自述的口径印（R3-25）。"""
    if value is None:
        return "—"
    if unit.startswith("小数比值"):
        return ratio_pct(value, 2)
    if unit.startswith("百分数"):
        return f"{value:.2f}%"
    return f"{value:g}"
```

* 小数比值 → 乘 100、两位、带 `%`（`0.2315` → `23.15%`，`0.00929805` → `0.93%`）；
* 百分数 → 两位、带 `%`（`1.85` → `1.85%`）；
* 倍 / 元 / 亿元 / 0–100 保持 `:g`：这些字段没有「页面自述另一套口径」的问题，
  这一次不顺手改它们的排版。

`pages/screen.py` 的 `_hit_row` 改成接收字段表（模块级
`_SPECS = {item.name: item for item in FIELDS}`），每一格：

```python
"cells": [
    screen_value(_SPECS[item.field].unit, hit.values.get(item.field))
    for item in conditions
],
```

`_text` 与 `_url` 不动：它们写的是条件输入框里的原值。

### 2. 证据句里的小数：最多 4 位、百分比一位

`compose.py` 的 `_fmt` 改成「4 位小数、去尾零」：

```python
def _fmt(value: float | None) -> str:
    """句子里的数最多 4 位小数（R3-25）：`:g` 会把 0.00929805 原样印出来。"""
    if value is None:
        return "无"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text
```

证据句里所有 `{xxx:g}` 改成 `{_fmt(xxx)}`：合理区间、买入区、止损、目标、赔率的
式子与理由。整数与常见的两位价不会变（`_fmt(10)` → `10`，`_fmt(27.6)` → `27.6`），
0.00929805 变成 `0.0093`。

分位与闸门是「位置/门槛」而不是价格：

* `分位 {pr_percentile:.1f}%`（87.0902 → 87.1，91.3934 → 91.4）；
* `闸门 {percentile_entry_max:.0f}%`（20 → 20）；
* 带宽已有 `:.0%`，不动。

`scoring.py` 的 evidence 已用 `:.4g` / `:.1f`，`fundamentals.py` 已用 `:.4g}`，
都在界内，不动。

## 接口与不变式

1. 引擎读数、库内存储、条件输入框与 URL 里的值**一个都不变**；只变屏幕（与 CLI
   共用的 evidence 字符串）上的写法。
2. 乘 100 仍然只经 `ratio_pct`：`screen_value` 不自己乘。
3. 未声明为比率的字段不会被悄悄换成百分数；`倍/元/亿元` 的排版保持现状。
4. 证据句不再出现超过 4 位的小数；分位一位、闸门整数。
5. 页面不出现命令行（D283）：这次不新增任何命令提示。

## 测试（先红后绿）

`tests/web/test_screen_web.py`：

* `test_result_cells_follow_the_unit_each_field_declares`：
  条件 `roe >= 0.1`、`debt_ratio <= 0.9`、`pct_chg >= 0`，断言结果格是
  `20.00%` / `30.00%` / `1.00%`，不是 `0.2` / `0.3` / `1`。

`tests/analysis/test_valuation_compose.py`：

* `test_the_percentile_in_evidence_is_rounded_to_a_readable_position`：
  `entry_zone(pr_percentile=87.0902, percentile_entry_max=20)`，
  证据含 `分位 87.1%`，不含 `87.0902`。
* `test_a_penny_fair_price_is_not_printed_with_machine_precision`：
  `fair_band(price=1.0, pr=53.85, fair_pr_low=0.5, fair_pr_high=1.0)`，
  证据含 `0.0093`，不含 `0.00929805`。

## 验证

* targeted：`tests/web/test_screen_web.py`、`tests/analysis/test_valuation_compose.py`；
* 全量分批（storage/cli/services 各自一段，web 逐文件）；
* 生产库副本：`tmp/d359` 起 8766，登录后看 `/screen` 的 ROE/股息率结果格、
  `/symbol/A/600318` 一类估值证据句；确认没有 `sams ` 命令串。

## 范围外

* R3-26（存量乱码备注/标题）另行处理。
* 价格、金额、赔率的既有排法（`price`/`money`/`odds_text`）不动。
* CLI 表格的列宽与 `sams screen` 的纯文本输出不动；只共享 evidence 字符串。

## 回归结果

（验证后填）
