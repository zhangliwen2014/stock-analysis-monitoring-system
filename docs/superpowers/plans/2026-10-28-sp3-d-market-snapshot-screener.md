# SP3 计划 D：全市场快照 + 选股引擎 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `sams screen spot --market A --where "pr_now<=0.5"` 在 5000 多只 A 股里筛出结果，每条件报「通过/不通过/无数」三个数，无数出局的票逐个点名；数据来自每日落库的全市场快照。

**Architecture:** 先在腾讯适配器上加一条横截面能力 `market_snapshot(market, codes)`（纯函数放在 `datasource/spot.py`，D192），服务层从 `symbol_master` 取活跃代码、把快照按 `(market, day, code)` 整日覆盖进 `snapshot_daily`（迁移 0023），再在 `core/screener_fields.py` 立一张字段注册表，`services/screener.py` 只按注册表校验条件、在快照帧上算三数并输出无数分组。盘后 `spot` job 与 `sams sync spot` 调用同一个服务函数。

**Tech Stack:** 腾讯批量报价（GBK）、Polars、DuckDB、既有 `AdapterRegistry`/`CAPABILITIES` 契约、argparse CLI。

**设计出处:** `docs/superpowers/specs/2026-10-28-screener-market-snapshot-design.md`（D172–D192；§3.1 是开工前实测修正）。

**执行约定（房规）:**
- 只在 `main` 上做，不建分支；`uv run` 一律走 uv；**绝不** `ruff format`；lint 命令是
  `uv run ruff check --no-cache src/sams tests`。
- 迁移编号用 **0023**（当前最大 0022；计划 E 再用 0024）。
- 不用 `git add -A`/`git add .`；一次只加本任务涉及的文件。
- 提交信息用现有风格：`feat(screener): …` / `test(screener): …` / `fix(screener): …`。
- 一条行为一轮红绿：先写测试、跑出**正确原因的失败**，再写最小实现。
- 全量测试只由主控跑，不接 `head/tail/grep`。

---

## 文件结构（本计划要动的全部）

| 文件 | 责任 |
| --- | --- |
| `src/sams/core/screener_fields.py` | **新建**。字段注册表、比较符、`Condition`、`validate()`（唯一词表） |
| `src/sams/analysis/fundamentals.py` | 加 `pr_now(pe_dyn, pb) -> PriceProfitRatio`（D177/D190 的口径闸） |
| `src/sams/datasource/spot.py` | **新建**。快照字段位置表、A 股 symbol 前缀、解析器、空帧、探针（纯函数） |
| `src/sams/datasource/tencent.py` | 加 `market_snapshot(market, codes)` + `market_snapshot_markets = {"A"}` |
| `src/sams/datasource/capabilities.py` | 登记 `market_snapshot` 能力 |
| `src/sams/datasource/registry.py` | 加 `MarketSnapshotResult` + `fetch_market_snapshot` + `_call_market_snapshot` |
| `src/sams/storage/repos/symbol_list.py` | 加 `active_codes(market)` |
| `src/sams/storage/migrations/0023_market_snapshot.sql` | **新建**。`snapshot_daily` |
| `src/sams/storage/repos/snapshots.py` | **新建**。整日覆盖、最新日、按日读 |
| `src/sams/services/market_snapshot.py` | **新建**。票池 → 取数 → 定日 → 落库 → 报缺 |
| `src/sams/cli/sync.py` | 加 `sams sync spot` |
| `src/sams/services/scheduler/jobs.py` | 加市场级 `spot` job |
| `src/sams/services/scheduler/types.py` | `JobDeps` 加 `snapshots` 槽位 |
| `src/sams/services/screener.py` | **新建**。三数、命中、无数分组、截断 |
| `src/sams/cli/screen.py` | 加 `sams screen spot` |
| `src/sams/bootstrap.py` | 装配 `SnapshotRepo` / `MarketSnapshotService` / `ScreenerService` |
| `tests/…` | 每个任务各自的测试文件（见任务内） |

---

### Task 1: 字段词表与条件校验

**Files:**
- Create: `src/sams/core/screener_fields.py`
- Modify: `src/sams/analysis/fundamentals.py`（文件末尾追加）
- Test: `tests/core/test_screener_fields.py`、`tests/analysis/test_pr.py`（追加用例）

- [ ] **Step 1: 写失败测试（词表与校验）**

```python
# tests/core/test_screener_fields.py
"""选股词表：一个字段一行，坏条件在引擎之前就被拦下（D179）。

这一层是「屏幕上能选」与「服务端认」之间的唯一通道。每一枚用例钉一条会静默出错的
地方：字段名拼错被当成「没有票通过」、把百分数当小数、between 写反后自动交换、
两条不同口径的 PR 混进同一个篮子（D59）。
"""

from __future__ import annotations

import pytest

from sams.core.errors import ValidationError
from sams.core.screener_fields import FIELDS, Condition, Field, fields_for, validate


def test_unknown_field_lists_the_vocabulary():
    with pytest.raises(ValidationError, match="可选的是"):
        validate("A", [Condition("prr", "<=", 0.5)])


def test_market_without_the_field_is_refused_with_the_reason():
    # pr_now 只供 A 股（D188）：HK/US 那一行位置没法交叉验证，没注册
    with pytest.raises(ValidationError, match="只供"):
        validate("HK", [Condition("pr_now", "<=", 0.5)])


def test_empty_conditions_are_refused():
    # 空条件 = 全市场都通过，那是一条永远为真的筛法，重跑时看起来像「方案坏了」
    with pytest.raises(ValidationError, match="一条条件都没有"):
        validate("A", [])


def test_unknown_op_lists_the_operators():
    with pytest.raises(ValidationError, match="比较符"):
        validate("A", [Condition("price", "≈", 10.0)])


def test_between_takes_two_numbers_in_order():
    assert validate("A", [Condition("price", "between", (1.0, 10.0))])


def test_between_written_backwards_is_refused_not_swapped():
    # D58：静默替调用方改写请求，会让屏幕上那一行写的与跑的不是一回事
    with pytest.raises(ValidationError, match="写反了"):
        validate("A", [Condition("price", "between", (10.0, 1.0))])


def test_non_finite_values_are_refused():
    with pytest.raises(ValidationError, match="有限"):
        validate("A", [Condition("price", "<", float("nan"))])


def test_two_pr_baskets_cannot_share_one_basket():
    """D59/D177：两条市赚率公式算出的数不是同一个量，同篮排序会让「最便宜的」只对组内成立。"""
    registry = (
        *FIELDS,
        Field(
            "pr",
            "市赚率（年报口径）",
            "小数比值（0.5 = 50%）",
            "financials",
            "pr",
            frozenset({"A"}),
            basket="pr",
        ),
    )
    with pytest.raises(ValidationError, match="不同口径"):
        validate(
            "A",
            [Condition("pr_now", "<=", 0.5), Condition("pr", "<=", 0.5)],
            registry=registry,
        )


def test_registry_and_page_vocabulary_are_the_same_object():
    """词表只有一份：`fields_for` 是页面下拉与校验共用的那条路。"""
    names = {f.name for f in fields_for("A")}
    assert {"pr_now", "pe_dyn", "pb", "roe", "turnover_rate"} <= names
    assert "pr_now" not in {f.name for f in fields_for("HK")}
```

- [ ] **Step 2: 写失败测试（现值 PR 的口径闸）**

```python
# 追加到 tests/analysis/test_pr.py 末尾
from sams.analysis.fundamentals import pr_now


def test_pr_now_is_pe_squared_over_pb():
    got = pr_now(18.68, 6.62)
    assert got.value == pytest.approx(18.68**2 / (100 * 6.62))
    assert got.reason is None


def test_pr_now_refuses_a_loss_maker_because_squaring_would_flip_the_sign():
    """PE 为负、平方后变正：不拦的话 `<= 0.5` 会把亏损票筛成「便宜」（D190）。"""
    got = pr_now(-8.0, 1.5)
    assert got.value is None and "亏损" in got.reason


def test_pr_now_refuses_negative_equity():
    got = pr_now(12.0, -0.3)
    assert got.value is None and "净资产为负" in got.reason


def test_pr_now_refuses_an_implied_roe_beyond_the_caliber_bound():
    got = pr_now(0.5, 0.9)  # 推出的 ROE = 1.8
    assert got.value is None and "上界" in got.reason


def test_pr_now_says_which_column_is_missing():
    assert "PE(动)" in pr_now(None, 1.0).reason
    assert "PB" in pr_now(10.0, None).reason
```

- [ ] **Step 3: 跑测试，确认红在正确的地方**

Run: `uv run pytest tests/core/test_screener_fields.py tests/analysis/test_pr.py -q -k "screener or pr_now"`
Expected: `ModuleNotFoundError: No module named 'sams.core.screener_fields'`，随后（第二次跑）
`ImportError: cannot import name 'pr_now'`。

- [ ] **Step 4: 写实现**

```python
# src/sams/core/screener_fields.py
"""选股词表：一个字段一行，页面的下拉、参数校验、覆盖率与列头全从这张表长出来（D179）。

字段只在这里加。写第二份词表的代价不是重复，是「屏幕上能选、服务端不认」——
那类错误在点下按钮之后才出现，而它看起来像一个后端 bug。

两条口径的声明就在这里写完（D58）：`unit` 说清 0.5 与 50 的换算，`basket` 说清哪两条
数不可同篮（D59 的 PR 两支）；校验器与引擎都只读这两格，不再各写一份判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from sams.core.errors import ValidationError

#: 比较符。`=` 配 `低..高` 才是 between——词表里不另造一个 `between` 记号，
#: 否则命令行、表单、JSON 三处各有一份拼法
OPS = (">=", ">", "<=", "<", "=", "between")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    #: 显示单位。D58 的教训：`0.5` 与 `50` 必须在这里说清哪一个
    unit: str
    #: `snapshot` = snapshot_daily 的列；`financials` = 按快照日 PIT 取的那一期；
    #: `derived` = 引擎在同一张帧上算出来的列
    source: str
    #: 工作帧里的列名（引擎负责把三种来源拼成同一张帧）
    column: str
    markets: frozenset[str]
    note: str = ""
    #: 不可同篮的字段共用一个非空值（D59/D177）
    basket: str = ""


_SNAPSHOT_MARKETS = frozenset({"A"})

FIELDS: tuple[Field, ...] = (
    Field("price", "现价", "元", "snapshot", "price", _SNAPSHOT_MARKETS),
    Field("pct_chg", "涨跌幅", "百分数（1.85 = 1.85%）", "snapshot", "pct_chg", _SNAPSHOT_MARKETS),
    Field("amount", "成交额", "元", "snapshot", "amount", _SNAPSHOT_MARKETS),
    Field(
        "turnover_rate",
        "换手率",
        "百分数（0.31 = 0.31%）",
        "snapshot",
        "turnover_rate",
        _SNAPSHOT_MARKETS,
    ),
    Field("volume_ratio", "量比", "倍", "snapshot", "volume_ratio", _SNAPSHOT_MARKETS),
    Field("amplitude", "振幅", "百分数（2.39 = 2.39%）", "snapshot", "amplitude", _SNAPSHOT_MARKETS),
    Field("pe_ttm", "市盈率(TTM)", "倍", "snapshot", "pe_ttm", _SNAPSHOT_MARKETS),
    Field("pe_dyn", "市盈率(动)", "倍", "snapshot", "pe_dyn", _SNAPSHOT_MARKETS),
    Field("pb", "市净率", "倍", "snapshot", "pb", _SNAPSHOT_MARKETS),
    Field("market_cap", "总市值", "亿元", "snapshot", "market_cap", _SNAPSHOT_MARKETS),
    Field("float_cap", "流通市值", "亿元", "snapshot", "float_cap", _SNAPSHOT_MARKETS),
    Field(
        "pr_now",
        "现值市赚率 PR",
        "小数比值（0.5 = 50%）",
        "derived",
        "pr_now",
        _SNAPSHOT_MARKETS,
        note="PE(动)² ÷ (100×PB)：分母是价推出来的 ROE，随价格变，与年报口径的 pr 不同口径",
        basket="pr_now",
    ),
    Field(
        "roe",
        "年报 ROE",
        "小数比值（0.15 = 15%）",
        "financials",
        "roe",
        _SNAPSHOT_MARKETS,
        note="截至快照日已披露的最近一期年报（PIT）；没有就记「无数」",
    ),
    Field(
        "revenue_yoy",
        "营收同比",
        "小数比值（0.10 = 10%）",
        "financials",
        "revenue_yoy",
        _SNAPSHOT_MARKETS,
    ),
    Field(
        "profit_yoy",
        "净利同比",
        "小数比值（0.10 = 10%）",
        "financials",
        "profit_yoy",
        _SNAPSHOT_MARKETS,
    ),
)

_BY_NAME = {item.name: item for item in FIELDS}


def field(name: str, *, registry: tuple[Field, ...] = FIELDS) -> Field:
    by_name = {item.name: item for item in registry}
    try:
        return by_name[name]
    except KeyError:
        raise ValidationError(
            f"不认识的字段 {name!r}：可选的是 {', '.join(by_name)}"
        ) from None


def fields_for(market: str, *, registry: tuple[Field, ...] = FIELDS) -> tuple[Field, ...]:
    return tuple(item for item in registry if market in item.markets)


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: float | tuple[float, float]

    def describe(self) -> str:
        if self.op == "between":
            low, high = self.value  # type: ignore[misc]
            return f"{self.field} between {low:g}..{high:g}"
        return f"{self.field} {self.op} {float(self.value):g}"


def _number(text: object, *, where: str) -> float:
    try:
        number = float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationError(f"{where} 要的是数，收到 {text!r}") from None
    if not isfinite(number):
        raise ValidationError(f"{where} 要的是一个有限的数，收到 {text!r}")
    return number


def validate(
    market: str, conditions: list[Condition], *, registry: tuple[Field, ...] = FIELDS
) -> tuple[Condition, ...]:
    """过一遍词表：字段在市场里有、比较符认识、值是有序的两个有限数、篮子不冲突。

    全部通过才返回（返回的是同一批条件，调用方不必再查一遍）。
    """
    if not conditions:
        raise ValidationError("一条条件都没有：空条件等于「全市场都通过」，那不是一次筛选")
    baskets: dict[str, str] = {}
    checked: list[Condition] = []
    for cond in conditions:
        spec = field(cond.field, registry=registry)
        if market not in spec.markets:
            raise ValidationError(
                f"{market} 没有「{spec.label}」这个字段（它只供 {', '.join(sorted(spec.markets))}）"
            )
        if cond.op not in OPS:
            raise ValidationError(
                f"不认识的比较符 {cond.op!r}：可选的是 {', '.join(OPS)}"
            )
        if cond.op == "between":
            if not isinstance(cond.value, tuple | list) or len(cond.value) != 2:
                raise ValidationError(
                    f"{spec.name} 的 between 要给两个数（低..高），收到 {cond.value!r}"
                )
            low = _number(cond.value[0], where=f"{spec.name} between 的下界")
            high = _number(cond.value[1], where=f"{spec.name} between 的上界")
            if low > high:
                # 不自动交换：屏幕上写的是「10..1」，交换后跑的是另一件事（D58）
                raise ValidationError(
                    f"{spec.name} between 的两个数写反了（{low:g}..{high:g}）："
                    "低的一头放前面，这里不替你交换"
                )
            checked.append(Condition(spec.name, cond.op, (low, high)))
        else:
            value = _number(cond.value, where=f"{spec.name} {cond.op} 的值")
            checked.append(Condition(spec.name, cond.op, value))
        if spec.basket:
            seen = baskets.get(spec.basket)
            if seen is not None and seen != spec.name:
                raise ValidationError(
                    f"「{seen}」与「{spec.name}」是两条不同口径的市赚率，不能在同一篮里混排"
                    "（D59/D177）：请只选一条，范围行会跟着写它的口径名"
                )
            baskets[spec.basket] = spec.name
    return tuple(checked)
```

```python
# 追加到 src/sams/analysis/fundamentals.py（放在 pr() 之后）
@dataclass(frozen=True)
class PriceProfitRatio:
    """现值 PR 的出数或拒算理由：两个字段恰好有一个是 None。"""

    value: float | None
    reason: str | None = None


def pr_now(pe_dyn: float | None, pb: float | None) -> PriceProfitRatio:
    """现值市赚率 `PE(动)² ÷ (100 × PB)`（D177）。

    与 `pr()` 不是同一件事：这一条的分母是价推出来的 ROE（`PB ÷ PE`），随价格变；
    `pr()` 的分母是已披露那期 ROE，随披露变。两条口径永不混排（D59）。

    亏损票的 PE 为负、平方后变正——不拦的话 `<= 0.5` 会把亏损票筛成「便宜」（D190）。
    PB 为负同理（净资产为负，PR 的方向会反过来）。推出的 ROE 仍走 `out_of_caliber`
    的那条上界，不另写一份判断。
    """
    if pe_dyn is None:
        return PriceProfitRatio(None, "PE(动) 缺失（快照没给这一列）")
    if pe_dyn <= 0:
        return PriceProfitRatio(None, f"PE(动)={pe_dyn:g} 不为正（亏损），平方后会变成假便宜")
    if pb is None:
        return PriceProfitRatio(None, "PB 缺失（快照没给这一列）")
    if pb <= 0:
        return PriceProfitRatio(None, f"PB={pb:g} 不为正（净资产为负），PR 的方向会反过来")
    implied = pb / pe_dyn
    if out_of_caliber("roe", implied):
        return PriceProfitRatio(
            None,
            f"PE(动) 与 PB 推出的 ROE={implied:.3g} 达到口径上界（|ROE| ≥ 1），这个数不可信",
        )
    return PriceProfitRatio(pe_dyn**2 / (100.0 * pb), None)
```

`fundamentals.py` 顶部已有 `from dataclasses import dataclass` 的话不必重复加；若没有，
在 import 区加 `from dataclasses import dataclass`。

- [ ] **Step 5: 跑测试到绿**

Run: `uv run pytest tests/core/test_screener_fields.py tests/analysis/test_pr.py -q`
Expected: 全部 PASS（`tests/analysis/test_pr.py` 原有用例不变）。

- [ ] **Step 6: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/core/screener_fields.py src/sams/analysis/fundamentals.py tests/core/test_screener_fields.py tests/analysis/test_pr.py
git commit -m "feat(screener): 字段词表与条件校验，现值 PR 的口径闸（D179/D190）"
```

---

### Task 2: 快照解析器与腾讯适配器上的 `market_snapshot`

**Files:**
- Create: `src/sams/datasource/spot.py`
- Modify: `src/sams/datasource/tencent.py`
- Test: `tests/datasource/test_spot.py`

- [ ] **Step 1: 写失败测试（字段位置、前缀、分批、缺列）**

```python
# tests/datasource/test_spot.py
"""全市场快照：字段位置是接口的硬事实，位置错一位就是把别的数写进这一列。

腾讯那行 88 格是按四只票（茅台、平安银行、腾讯、苹果）交叉核过的；这里用同一形状的
录制行钉住 38 换手、39 PE(TTM)、43 振幅、44 流通市值、45 总市值、46 PB、49 量比、
52 PE(动)、35 复合字段第三段的成交额。港美股行不测：那一侧不注册（D188）。
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.spot import SPOT_BATCH, parse_spot, spot_symbol
from sams.datasource.tencent import TencentAdapter

ZONE = ZoneInfo("Asia/Shanghai")


def line(key: str, cells: list[str]) -> str:
    return f'v_{key}="{"~".join(cells)}";'


def cells(
    *,
    code: str = "600519",
    name: str = "贵州茅台",
    price: str = "1330.75",
    ts: str = "20261028161454",
    pct: str = "1.85",
    turnover: str = "0.31",
    pe_ttm: str = "20.43",
    amplitude: str = "2.39",
    float_cap: str = "16635.46",
    cap: str = "16635.46",
    pb: str = "6.62",
    volume_ratio: str = "0.91",
    pe_dyn: str = "18.68",
    amount: str = "5187324618",
) -> list[str]:
    row = [""] * 88
    row[0] = "1"
    row[1] = name
    row[2] = code
    row[3] = price
    row[30] = ts
    row[32] = pct
    row[35] = f"{price}/38805/{amount}"
    row[38] = turnover
    row[39] = pe_ttm
    row[43] = amplitude
    row[44] = float_cap
    row[45] = cap
    row[46] = pb
    row[49] = volume_ratio
    row[52] = pe_dyn
    return row


def test_field_positions_are_mapped_to_the_right_columns():
    frame = parse_spot(
        line("sh600519", cells()), market="A", codes=["600519"], zone=ZONE
    )
    row = frame.to_dicts()[0]
    assert row["code"] == "600519" and row["name"] == "贵州茅台"
    assert row["quoted_on"] == date(2026, 10, 28)
    assert row["price"] == 1330.75
    assert row["pct_chg"] == 1.85
    assert row["turnover_rate"] == 0.31
    assert row["pe_ttm"] == 20.43
    assert row["pe_dyn"] == 18.68
    assert row["pb"] == 6.62
    assert row["market_cap"] == 16635.46
    assert row["float_cap"] == 16635.46
    assert row["volume_ratio"] == 0.91
    assert row["amplitude"] == 2.39
    # 成交额在 A 股第 35 格的第三段，单位是元
    assert row["amount"] == 5187324618.0
    assert row["is_st"] is False
    assert row["source"] == "tencent"


def test_st_is_read_from_the_name_not_a_flag_column():
    frame = parse_spot(
        line("sz000004", cells(code="000004", name="*ST国华")),
        market="A",
        codes=["000004"],
        zone=ZONE,
    )
    assert frame.to_dicts()[0]["is_st"] is True


def test_bj_codes_use_the_bj_prefix():
    """公共的 a_share_symbol 会把 920xxx 发成 sz（实测无行，静默缺 353 只）。"""
    assert spot_symbol("A", "920992") == "bj920992"
    assert spot_symbol("A", "600519") == "sh600519"
    assert spot_symbol("A", "000001") == "sz000001"


def test_a_short_row_is_field_drift_not_a_row_of_nulls():
    short = cells()[:40]
    with pytest.raises(FieldDrift, match="字段位置"):
        parse_spot(line("sh600519", short), market="A", codes=["600519"], zone=ZONE)


def test_a_response_without_any_match_is_empty_not_a_frame_of_zeros():
    with pytest.raises(EmptyResponse):
        parse_spot(line("sh600000", cells()), market="A", codes=["600519"], zone=ZONE)
    with pytest.raises(EmptyResponse):
        parse_spot("", market="A", codes=["600519"], zone=ZONE)


def test_adapter_slices_the_code_list_into_batches():
    """批大小是接口的硬边界：实测 880 个代码可、1000 个回 414。"""
    requested: list[list[str]] = []

    def fetch(url, params, *, timeout):
        requested.append(params["q"].split(","))
        code = params["q"].split(",")[0][2:]
        return line(params["q"].split(",")[0], cells(code=code, name=f"票{code}"))

    adapter = TencentAdapter(quote_fetch=fetch)
    codes = [f"{600000 + i}" for i in range(SPOT_BATCH + 3)]
    frame = adapter.market_snapshot("A", codes)
    assert [len(chunk) for chunk in requested] == [SPOT_BATCH, 3]
    assert frame.height == len(codes) - 3  # 第二批只答了第一只（替身行为），正好验合并


def test_adapter_refuses_hk_and_us():
    adapter = TencentAdapter(quote_fetch=lambda *a, **k: "")
    for market in ("HK", "US"):
        with pytest.raises(DatasourceError, match="只供"):
            adapter.market_snapshot(market, ["00700"])


@pytest.mark.live
def test_live_snapshot_answers_real_rows_with_the_key_columns():
    """真接口冒烟：A 股 5 只必须都答得上，且 PE/PB/市值不是整列空。"""
    adapter = TencentAdapter()
    frame = adapter.market_snapshot("A", ["600519", "000001", "920992", "300750", "601088"])
    assert frame.height == 5
    assert frame.filter(frame["price"].is_not_null()).height == 5
    assert frame.filter(frame["pb"].is_not_null()).height >= 4
```

- [ ] **Step 2: 跑测试，确认红**

Run: `uv run pytest tests/datasource/test_spot.py -q -m "not live"`
Expected: `ModuleNotFoundError: No module named 'sams.datasource.spot'`。

- [ ] **Step 3: 写实现（纯函数模块）**

```python
# src/sams/datasource/spot.py
"""全市场快照的纯函数：字段位置、A 股 symbol 前缀、解析器、空帧与探针（D186/D192）。

东财三个 `*_spot_em` 在本机全不通（实测 RemoteDisconnected），腾讯的批量报价是唯一
还活着的通道：GBK 的 JS 赋值语句，一条请求实测最多 880 个代码（1000 个回 414），
所以 `SPOT_BATCH = 800`。

字段位置是按四只票（茅台、平安银行、腾讯、苹果）交叉核对的。港美股行的位置与 A 股不同、
且对不上已知财报，所以这一层不注册它们（D188）——拿不准的口径不出数，比出一个假数便宜。

这里只放纯函数（与 `quote.py`/`minute.py` 同一位置）：适配器方法在 `tencent.py` 上，
因为腾讯已经在 A 股的降级链里、报价通道与限速全是现成的（D192）。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.quote import echo_matches, field_float, parse_moment

#: 与 `tencent._TENCENT_LINE` 同一形状。那边的是私有名，跨模块引用私有名比多三行正则更糟
_LINE = re.compile(r'v_([^=]+)="([^"]*)"')

#: 一条请求最多带多少个代码（实测边界：880 可、1000 回 414）
SPOT_BATCH = 800

#: 三个市场的时间格式各不相同；本期只注册 A 股，其余留在这里等实测（D188）
_TS_FORMATS = {"A": ("%Y%m%d%H%M%S",)}

#: A 股行里按位置取的列。键是工作帧列名，值是**接口行**的下标
SPOT_INDEX = {
    "name": 1,
    "price": 3,
    "pct_chg": 32,
    "turnover_rate": 38,
    "pe_ttm": 39,
    "amplitude": 43,
    "float_cap": 44,
    "market_cap": 45,
    "pb": 46,
    "volume_ratio": 49,
    "pe_dyn": 52,
}
#: 行至少要有这么多格：最后一格是 pe_dyn(52)
SPOT_MIN_FIELDS = 53
#: A 股成交额藏在第 35 个复合字段（价/量/额）的第三段，单位元
_AMOUNT_SLOT = 35
#: 北交所的代码形态：腾讯只认 `bj` 前缀
_BJ_PREFIX = "920"
_SH_FIRST = "569"

SPOT_COLUMNS = [
    "market",
    "code",
    "name",
    "quoted_on",
    "price",
    "pct_chg",
    "amount",
    "turnover_rate",
    "volume_ratio",
    "amplitude",
    "pe_ttm",
    "pe_dyn",
    "pb",
    "market_cap",
    "float_cap",
    "is_st",
    "source",
]
SPOT_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "name": pl.Utf8,
    "quoted_on": pl.Date,
    "price": pl.Float64,
    "pct_chg": pl.Float64,
    "amount": pl.Float64,
    "turnover_rate": pl.Float64,
    "volume_ratio": pl.Float64,
    "amplitude": pl.Float64,
    "pe_ttm": pl.Float64,
    "pe_dyn": pl.Float64,
    "pb": pl.Float64,
    "market_cap": pl.Float64,
    "float_cap": pl.Float64,
    "is_st": pl.Boolean,
    "source": pl.Utf8,
}


def has_market_snapshot(obj: Any) -> bool:
    return callable(getattr(obj, "market_snapshot", None))


def empty_snapshot() -> pl.DataFrame:
    return pl.DataFrame({name: pl.Series(name, [], dtype=SPOT_SCHEMA[name]) for name in SPOT_COLUMNS})


def spot_symbol(market: str, code: str) -> str:
    """本系统代码 → 腾讯快照 symbol（`sh600519` / `sz000001` / `bj920992`）。

    北交所走 `bj`：公共的 `a_share_symbol` 只认 sh/sz，把 920xxx 发成 `sz` 会拿到空行，
    症状是「代码表里 353 只票整批消失」而不是一条报错（D186）。
    """
    if market != "A":
        raise ValueError(f"腾讯快照只供 A 股，收到 {market}")
    symbol = canonical_code(market, code)
    if symbol.startswith(_BJ_PREFIX):
        return f"bj{symbol}"
    return f"{'sh' if symbol[0] in _SH_FIRST else 'sz'}{symbol}"


def parse_spot(
    text: str, *, market: str, codes: list[str], zone: ZoneInfo
) -> pl.DataFrame:
    """腾讯批量响应 → 快照帧。行短于 `SPOT_MIN_FIELDS` 当场 `FieldDrift`（D185）。

    缺列读成 null 的后果是那一列在全市场无一例外变成「无数」，而屏幕上写的是
    「这一条没票通过」——所以宁可整批失败，让采集 job 出声。
    """
    if not (text or "").strip():
        raise EmptyResponse("spot", f"{market} 快照响应为空")
    parsed: list[list[str]] = []
    for raw in text.splitlines():
        match = _LINE.search(raw)
        if match is not None and match.group(2).strip():
            parsed.append(match.group(2).split("~"))
    if not parsed:
        raise EmptyResponse("spot", f"{market} 快照响应里没有可读的行")

    rows: list[dict] = []
    for code in codes:
        canonical = canonical_code(market, code)
        row = next(
            (
                fields
                for fields in parsed
                if len(fields) > 2 and echo_matches(market, canonical, fields[2])
            ),
            None,
        )
        if row is None:
            continue
        if len(row) < SPOT_MIN_FIELDS:
            raise FieldDrift(
                "spot",
                f"{market} 快照行只有 {len(row)} 格（要 {SPOT_MIN_FIELDS}）：字段位置变了，"
                f"{canonical} 这批不写库",
            )
        moment = parse_moment(row[30], _TS_FORMATS[market], zone)
        name = row[SPOT_INDEX["name"]].strip()
        rows.append(
            {
                "market": market,
                "code": canonical,
                "name": name or None,
                "quoted_on": moment.astimezone(zone).date() if moment else None,
                "price": field_float(row, SPOT_INDEX["price"]),
                "pct_chg": field_float(row, SPOT_INDEX["pct_chg"]),
                "amount": field_float(row[_AMOUNT_SLOT].split("/"), 2),
                "turnover_rate": field_float(row, SPOT_INDEX["turnover_rate"]),
                "volume_ratio": field_float(row, SPOT_INDEX["volume_ratio"]),
                "amplitude": field_float(row, SPOT_INDEX["amplitude"]),
                "pe_ttm": field_float(row, SPOT_INDEX["pe_ttm"]),
                "pe_dyn": field_float(row, SPOT_INDEX["pe_dyn"]),
                "pb": field_float(row, SPOT_INDEX["pb"]),
                "market_cap": field_float(row, SPOT_INDEX["market_cap"]),
                "float_cap": field_float(row, SPOT_INDEX["float_cap"]),
                # A 股没有独立的 ST 标记列：名字里的 ST 就是交易所的口径
                "is_st": "ST" in name,
                "source": "tencent",
            }
        )
    if not rows:
        raise EmptyResponse("spot", f"{market} 快照里一行都没对上请求的代码")
    return pl.DataFrame(rows, schema=SPOT_SCHEMA)
```

- [ ] **Step 4: 跑测试，确认解析器绿、适配器仍红**

Run: `uv run pytest tests/datasource/test_spot.py -q -m "not live"`
Expected: 解析器 4 条 PASS；`test_adapter_*` 两条 FAIL（`TencentAdapter` 没有
`market_snapshot`，`AttributeError`）。

- [ ] **Step 5: 在腾讯适配器上加方法与意愿表**

在 `tencent.py` 的 import 区加：

```python
from sams.datasource.spot import SPOT_BATCH, parse_spot, spot_symbol
```

在 `TencentAdapter` 的类属性区（`minute_bars_markets` 之后）加：

```python
    #: 全市场快照只答 A 股：港美股行的 PE/PB 位置没法交叉验证（D188）
    market_snapshot_markets = frozenset({"A"})
```

在 `quotes` 方法之后加：

```python
    def market_snapshot(self, market: str, codes: list[str]) -> pl.DataFrame:
        """一个市场的一次横截面（D173）：批大小是接口的硬边界，切片在适配器里做。

        一片失败就是整市场失败——按票重试同一个请求没有意义（D173），让 job 出声、
        保留上一次那天的数据，比写进一半更像一次采集。
        """
        if market not in self.market_snapshot_markets:
            raise DatasourceError(
                self.name,
                f"腾讯快照只供 {sorted(self.market_snapshot_markets)}，收到 {market}",
            )
        wanted = list(dict.fromkeys(canonical_code(market, code) for code in codes))
        if not wanted:
            raise EmptyResponse(self.name, "快照请求里没有代码")
        frames: list[pl.DataFrame] = []
        for start in range(0, len(wanted), SPOT_BATCH):
            chunk = wanted[start : start + SPOT_BATCH]
            symbols = [spot_symbol(market, code) for code in chunk]
            try:
                text = self._quote_fetch(
                    QUOTE_URL, {"q": ",".join(symbols)}, timeout=self._timeout
                )
                frames.append(
                    parse_spot(text, market=market, codes=chunk, zone=MARKET_ZONES[market])
                )
            except DatasourceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise classify_error(self.name, exc) from exc
        frame = pl.concat(frames)
        if frame.is_empty():
            raise EmptyResponse(self.name, f"{market} 快照返回 0 行")
        return frame
```

- [ ] **Step 6: 跑测试到绿**

Run: `uv run pytest tests/datasource/test_spot.py tests/datasource/test_tencent.py tests/datasource/test_tencent_quotes.py -q -m "not live"`
Expected: 全部 PASS。

- [ ] **Step 7: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/datasource/spot.py src/sams/datasource/tencent.py tests/datasource/test_spot.py
git commit -m "feat(datasource): 腾讯全市场快照——字段位置、bj 前缀、分批与缺列（D186）"
```

---

### Task 3: 能力登记 `market_snapshot`

**Files:**
- Modify: `src/sams/datasource/capabilities.py`
- Test: `tests/datasource/test_capabilities.py`（追加用例）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/datasource/test_capabilities.py
def test_market_snapshot_is_registered_under_its_method_name():
    cap = capability("market_snapshot")
    assert cap.label == "全市场快照"
    assert "快照" in cap.lacking
    assert "market_snapshot" in CAPABILITIES


def test_tencent_answers_market_snapshot_only_for_a():
    from sams.datasource.minute import answers
    from sams.datasource.tencent import TencentAdapter

    adapter = TencentAdapter(quote_fetch=lambda *a, **k: "")
    assert "market_snapshot" in provided_by(TencentAdapter)
    assert answers(adapter, "market_snapshot", "A") is True
    assert answers(adapter, "market_snapshot", "HK") is False
```

（若文件里没有 import `CAPABILITIES`/`capability`/`provided_by`，在顶部补上。）

- [ ] **Step 2: 跑测试确认红**

Run: `uv run pytest tests/datasource/test_capabilities.py -q -k market_snapshot`
Expected: `KeyError: 'market_snapshot'`。

- [ ] **Step 3: 实现**

在 `capabilities.py` 的 import 区加：

```python
from sams.datasource.spot import has_market_snapshot
```

在 `CAPABILITIES` 字典末尾（`minute_bars` 之后）加：

```python
    # 横截面：一次答一个市场，不按票路由；仍登记在这里，好让「这个市场有没有快照」
    # 与「这条链有没有源」共用一个答案（D173）
    "market_snapshot": Capability(
        label="全市场快照", lacking="不提供全市场快照", probe=has_market_snapshot
    ),
```

- [ ] **Step 4: 跑测试到绿**

Run: `uv run pytest tests/datasource/test_capabilities.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/datasource/capabilities.py tests/datasource/test_capabilities.py
git commit -m "feat(datasource): 登记 market_snapshot 能力（D173）"
```

---

### Task 4: 注册表的快照链与代码表取活跃代码

**Files:**
- Modify: `src/sams/datasource/registry.py`
- Modify: `src/sams/storage/repos/symbol_list.py`
- Test: `tests/datasource/test_registry.py`（追加）、`tests/storage/test_symbol_list.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/datasource/test_registry.py
class SnapshotStub:
    """只有快照能力的源：与腾讯同签名，答哪一批由 `answers` 决定。"""

    name = "snap"
    markets = frozenset({"A"})
    market_snapshot_markets = frozenset({"A"})

    def __init__(self, rows=None, outcome=None):
        self.rows = rows if rows is not None else spot_rows()
        self.outcome = outcome
        self.calls = 0

    def market_snapshot(self, market, codes):
        self.calls += 1
        if self.outcome is not None:
            raise self.outcome
        return self.rows


def spot_rows(codes=("600519",)):
    import polars as pl
    from sams.datasource.spot import SPOT_SCHEMA

    return pl.DataFrame(
        {
            "market": ["A"] * len(codes),
            "code": list(codes),
            "name": ["票"] * len(codes),
            "quoted_on": [date(2026, 10, 28)] * len(codes),
            "price": [10.0] * len(codes),
            "pct_chg": [1.0] * len(codes),
            "amount": [1.0e8] * len(codes),
            "turnover_rate": [1.0] * len(codes),
            "volume_ratio": [1.0] * len(codes),
            "amplitude": [1.0] * len(codes),
            "pe_ttm": [10.0] * len(codes),
            "pe_dyn": [9.0] * len(codes),
            "pb": [1.2] * len(codes),
            "market_cap": [100.0] * len(codes),
            "float_cap": [90.0] * len(codes),
            "is_st": [False] * len(codes),
            "source": ["tencent"] * len(codes),
        },
        schema=SPOT_SCHEMA,
    )


def registry_with(*adapters, market_sources=None):
    return AdapterRegistry(
        adapters={a.name: a for a in adapters},
        market_sources=market_sources or {"A": tuple(a.name for a in adapters)},
        sleep=lambda _s: None,
    )


def test_market_snapshot_skips_sources_without_the_capability():
    stub = SnapshotStub()
    plain = StubAdapter("akshare", ["A"], spot_rows())
    reg = registry_with(stub, plain, market_sources={"A": ("akshare", "snap")})
    result = reg.fetch_market_snapshot("A", ["600519"])
    assert result.source == "snap"
    assert ("akshare", "不提供全市场快照") in result.skipped


def test_market_snapshot_respects_the_willingness_table():
    stub = SnapshotStub()
    reg = registry_with(stub, market_sources={"A": ("snap",)})
    stub.market_snapshot_markets = frozenset({"HK"})
    result = reg.fetch_market_snapshot("A", ["600519"])
    assert result.frame.is_empty() or result.empty_everywhere
    assert result.skipped and "不供" in result.skipped[0][1]


def test_market_snapshot_fails_loudly_when_every_source_fails():
    stub = SnapshotStub(outcome=DatasourceError("snap", "boom"))
    reg = registry_with(stub)
    with pytest.raises(DatasourceError, match="快照"):
        reg.fetch_market_snapshot("A", ["600519"])
```

```python
# 追加到 tests/storage/test_symbol_list.py
def test_active_codes_returns_the_ordered_live_list(repo):
    assert repo.active_codes("A") == ["000001", "600001", "600519"]
    repo.deactivate_missing("A", ["600519"])
    assert repo.active_codes("A") == ["000001", "600001"]
    assert repo.active_codes("HK") == ["00700"]
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run pytest tests/datasource/test_registry.py tests/storage/test_symbol_list.py -q -k "market_snapshot or active_codes"`
Expected: `AttributeError: 'AdapterRegistry' object has no attribute 'fetch_market_snapshot'`；`AttributeError: ... 'active_codes'`。

- [ ] **Step 3: 实现（仓库方法）**

在 `symbol_list.py` 的 `count` 之前加：

```python
    def active_codes(self, market: str) -> list[str]:
        """在市的全部代码，按代码升序：选股票池与快照请求都用这一份。"""
        frame = self.db.query(
            "SELECT code FROM symbol_master WHERE market = ? AND is_active ORDER BY code",
            [market],
        )
        return frame["code"].to_list()
```

- [ ] **Step 4: 实现（注册表）**

在 `registry.py` 顶部 import 区加：

```python
from sams.datasource.minute import answers
from sams.datasource.spot import empty_snapshot, has_market_snapshot
```

在 `SymbolListResult` 之后加：

```python
@dataclass(frozen=True)
class MarketSnapshotResult:
    """全市场快照链的返回：按市场路由、一次答一屏，既有 `code` 也没有窗口。

    缺答的代码不在这里点名——那要看「服务层问了哪些」，所以由服务层拿请求清单去比。
    """

    market: str
    source: str
    frame: pl.DataFrame
    attempts: tuple[Attempt, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    empty_everywhere: bool = False
```

在 `fetch_symbol_list` 之后加（整段照它的形状，只换成快照的三种归类）：

```python
    def fetch_market_snapshot(self, market: str, codes: list[str]) -> MarketSnapshotResult:
        """快照链：一次答一个市场，按市场路由、不按票重试（D173）。

        「这个源答不答这个市场」看意愿表（`answers`），与目录、置灰共用同一份判据
        （D165）——链上挂着 `market_snapshot` 方法不代表它愿意答港美股。
        """
        cap = capability("market_snapshot")
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        empties: list[str] = []
        failed: list[tuple[str, str, Exception | None]] = []
        for name in self.sources_for(market):
            adapter = self.adapter(name)
            if not has_market_snapshot(adapter):
                logger.info("快照 跳过 %s（%s 链上没有这个能力）", name, market)
                skipped.append((name, cap.lacking))
                continue
            if not answers(adapter, "market_snapshot", market):
                logger.info("快照 跳过 %s（不供 %s）", name, market)
                skipped.append((name, f"不供{market}"))
                continue
            try:
                frame = self._call_market_snapshot(name, market, codes)
            except _SourceFailed as exc:
                attempts.append(Attempt(name, False, exc.tries, exc.error))
                failed.append((name, exc.error, exc.cause))
                continue
            except EmptyResponse as exc:
                attempts.append(Attempt(name, True, 1, f"无数据: {exc}"))
                empties.append(name)
                logger.info("快照 %s 说 %s 这张表是空的，换源", name, market)
                continue
            self._mark_ok(name)
            attempts.append(Attempt(name, True))
            return MarketSnapshotResult(market, name, frame, tuple(attempts), tuple(skipped))

        if empties and not failed:
            return MarketSnapshotResult(
                market,
                empties[0],
                empty_snapshot(),
                tuple(attempts),
                tuple(skipped),
                empty_everywhere=True,
            )

        detail = "；".join(f"{name}: {error}" for name, error, _ in failed) or "（无）"
        if not attempts:
            raise DatasourceError(
                "registry",
                f"{market} 没有能取全市场快照的源：链 {list(self.sources_for(market))}",
            )
        causes = [cause for _, _, cause in failed]
        raise _terminal_error(f"{market} 所有源均取快照失败 → {detail}", causes)

    def _call_market_snapshot(self, name: str, market: str, codes: list[str]) -> pl.DataFrame:
        """对单个源的一次快照取数：限速、退避、归类，与 `_call_symbol_list` 同一形状。

        一次方法内部会切成多片 HTTP 请求（批大小是接口的硬边界），所以限速桶按片数
        领令牌——只领一次等于把真实请求量报小了一个数量级。
        """
        adapter = self.adapter(name)
        if self.bucket is not None:
            for _ in range(max(1, ceil(len(codes) / SPOT_BATCH))):
                self.bucket.acquire()
        calls = 0

        def invoke() -> pl.DataFrame:
            nonlocal calls
            calls += 1
            return adapter.market_snapshot(market, codes)

        try:
            frame = call_with_backoff(
                invoke,
                attempts=self.attempts_per_source,
                retryable=RETRYABLE,
                sleep=self.sleep,
                label=f"{name}:market_snapshot {market}",
            )
        except EmptyResponse:
            raise
        except DatasourceError as exc:
            self._mark_failure(name, exc)
            raise _SourceFailed(name, calls, str(exc), exc) from exc
        except Exception as exc:  # noqa: BLE001
            wrapped = DatasourceError(name, f"{type(exc).__name__}: {exc}")
            self._mark_failure(name, wrapped)
            raise _SourceFailed(name, calls, str(wrapped), exc) from exc
        out = frame if isinstance(frame, pl.DataFrame) else pl.DataFrame(frame)
        if out.is_empty():
            raise EmptyResponse(name, f"{market} 快照返回 0 行")
        return out
```

顶部还要加 `from math import ceil` 与 `from sams.datasource.spot import SPOT_BATCH`。

- [ ] **Step 5: 跑测试到绿**

Run: `uv run pytest tests/datasource/test_registry.py tests/storage/test_symbol_list.py -q`
Expected: 全部 PASS。

- [ ] **Step 6: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/datasource/registry.py src/sams/storage/repos/symbol_list.py tests/datasource/test_registry.py tests/storage/test_symbol_list.py
git commit -m "feat(datasource): 快照链按市场路由、意愿表与限速按片领令牌（D173）"
```

---

### Task 5: `snapshot_daily` 迁移与仓库

**Files:**
- Create: `src/sams/storage/migrations/0023_market_snapshot.sql`
- Create: `src/sams/storage/repos/snapshots.py`
- Test: `tests/storage/test_snapshots.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/storage/test_snapshots.py
"""快照仓库：同一天重采是整日覆盖，按日读回的是那一份完整的横截面。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.snapshots import SnapshotRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def frame(*codes: str, day: date = DAY) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A"] * len(codes),
            "code": list(codes),
            "name": [f"票{code}" for code in codes],
            "quoted_on": [day] * len(codes),
            "price": [10.0] * len(codes),
            "pct_chg": [1.0] * len(codes),
            "amount": [1.0e8] * len(codes),
            "turnover_rate": [1.0] * len(codes),
            "volume_ratio": [1.0] * len(codes),
            "amplitude": [1.0] * len(codes),
            "pe_ttm": [10.0] * len(codes),
            "pe_dyn": [9.0] * len(codes),
            "pb": [1.2] * len(codes),
            "market_cap": [100.0] * len(codes),
            "float_cap": [90.0] * len(codes),
            "is_st": [False] * len(codes),
            "source": ["tencent"] * len(codes),
        },
        schema=SPOT_SCHEMA,
    )


def test_replace_day_writes_and_reads_back(db):
    repo = SnapshotRepo(db)
    assert repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519", "000001")) == 2
    read = repo.load_day("A", DAY)
    assert read.height == 2
    assert set(read["code"]) == {"600519", "000001"}
    assert read["taken_at"].item().replace(tzinfo=UTC) == TAKEN


def test_recollecting_the_same_day_overwrites_instead_of_adding(db):
    """同一天两个答案比没有答案更糟：页面上那句「今天这一篮」没法追溯。"""
    repo = SnapshotRepo(db)
    repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519", "000001"))
    repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519", "300750"))
    read = repo.load_day("A", DAY)
    assert set(read["code"]) == {"600519", "300750"}


def test_latest_day_picks_the_newest_and_says_none_when_empty(db):
    repo = SnapshotRepo(db)
    assert repo.latest_day("A") is None
    repo.replace_day(market="A", day=date(2026, 10, 27), taken_at=TAKEN, frame=frame("600519"))
    repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519"))
    assert repo.latest_day("A") == DAY
    assert repo.latest_day("HK") is None


def test_an_empty_frame_does_not_erase_history(db):
    repo = SnapshotRepo(db)
    repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519"))
    assert repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame()) == 0
    assert repo.load_day("A", DAY).height == 1
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run pytest tests/storage/test_snapshots.py -q`
Expected: `ModuleNotFoundError: No module named 'sams.storage.repos.snapshots'`（迁移未建时
`load_day` 会先报表不存在，同样是红）。

- [ ] **Step 3: 写迁移**

```sql
-- 0023：全市场快照（D175/D186）。主键 (market, day, code)：同一天重采是整日覆盖
CREATE TABLE IF NOT EXISTS snapshot_daily (
    market        TEXT NOT NULL,
    day           DATE NOT NULL,
    taken_at      TIMESTAMPTZ NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    price         DOUBLE,
    pct_chg       DOUBLE,
    amount        DOUBLE,
    turnover_rate DOUBLE,
    volume_ratio  DOUBLE,
    amplitude     DOUBLE,
    pe_ttm        DOUBLE,
    pe_dyn        DOUBLE,
    pb            DOUBLE,
    market_cap    DOUBLE,
    float_cap     DOUBLE,
    is_st         BOOLEAN,
    source        TEXT NOT NULL,
    PRIMARY KEY (market, day, code)
);
```

- [ ] **Step 4: 写仓库**

```python
# src/sams/storage/repos/snapshots.py
"""`snapshot_daily` 的读写：一次采集几千行，读端只按 `(market, day)` 取一屏。

整日覆盖而不是逐行 upsert：同一天重采时，源答不出的那几只必须从这一天消失——
留着旧行等于让「今天这一篮」里混进一只今天没有报价的票，而页面上没有任何一处
能看出它是昨天的数（D181）。
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl

from sams.core.codes import canonical_market
from sams.storage.db import Database

#: 写入的列（顺序与迁移一致）。market/day/taken_at 由 SQL 侧给，不进帧
_COLUMNS = (
    "code",
    "name",
    "price",
    "pct_chg",
    "amount",
    "turnover_rate",
    "volume_ratio",
    "amplitude",
    "pe_ttm",
    "pe_dyn",
    "pb",
    "market_cap",
    "float_cap",
    "is_st",
    "source",
)
_VIEW = "incoming_snapshot_daily"


class SnapshotRepo:
    TABLE = "snapshot_daily"

    def __init__(self, db: Database) -> None:
        self.db = db

    def replace_day(
        self, *, market: str, day: date, taken_at: datetime, frame: pl.DataFrame
    ) -> int:
        """整日覆盖；空帧不动表（一次空答复不该删掉昨天已经采到的历史）。"""
        if frame.is_empty():
            return 0
        market = canonical_market(market)
        incoming = frame.select(list(_COLUMNS)).unique(subset=["code"], keep="last")
        self.db.register(_VIEW, incoming)
        try:
            with self.db.transaction():
                self.db.execute(
                    f"DELETE FROM {self.TABLE} WHERE market = ? AND day = ?", [market, day]
                )
                self.db.execute(
                    f"INSERT INTO {self.TABLE} (market, day, taken_at, {', '.join(_COLUMNS)})"
                    f" SELECT ?, ?, ?, {', '.join(_COLUMNS)} FROM {_VIEW}",
                    [market, day, taken_at],
                )
        finally:
            self.db.unregister(_VIEW)
        return incoming.height

    def latest_day(self, market: str) -> date | None:
        value = self.db.scalar(
            f"SELECT max(day) FROM {self.TABLE} WHERE market = ?",
            [canonical_market(market)],
        )
        return value if isinstance(value, date) else None

    def load_day(self, market: str, day: date) -> pl.DataFrame:
        return self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE market = ? AND day = ? ORDER BY code",
            [canonical_market(market), day],
        )

    def count(self) -> int:
        return int(self.db.scalar(f"SELECT count(*) FROM {self.TABLE}") or 0)
```

- [ ] **Step 5: 跑测试到绿**

Run: `uv run pytest tests/storage/test_snapshots.py tests/storage/test_migrations.py -q`
Expected: PASS。若 `tests/storage/test_migrations.py` 钉了「最大编号」或迁移数量，
按实际断言更新（该文件若只断言 `schema_migrations` 行数，加一条即可）。

- [ ] **Step 6: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/storage/migrations/0023_market_snapshot.sql src/sams/storage/repos/snapshots.py tests/storage/test_snapshots.py
git commit -m "feat(storage): snapshot_daily 按日整表覆盖（D175）"
```

---

### Task 6: 快照服务（票池 → 取数 → 定日 → 落库 → 报缺）与装配

**Files:**
- Create: `src/sams/services/market_snapshot.py`
- Modify: `src/sams/bootstrap.py`
- Test: `tests/services/test_market_snapshot.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/services/test_market_snapshot.py
"""快照服务：票池来自代码表，源答不出的逐个点名，日从报价时刻来。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.datasource.registry import AdapterRegistry
from sams.services.market_snapshot import MarketSnapshotService
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

NOW = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def spot_rows(*codes: str, quoted: date | None = date(2026, 10, 28)):
    import polars as pl
    from sams.datasource.spot import SPOT_SCHEMA

    return pl.DataFrame(
        {
            "market": ["A"] * len(codes),
            "code": list(codes),
            "name": [f"票{c}" for c in codes],
            "quoted_on": [quoted] * len(codes),
            "price": [10.0] * len(codes),
            "pct_chg": [1.0] * len(codes),
            "amount": [1e8] * len(codes),
            "turnover_rate": [1.0] * len(codes),
            "volume_ratio": [1.0] * len(codes),
            "amplitude": [1.0] * len(codes),
            "pe_ttm": [10.0] * len(codes),
            "pe_dyn": [9.0] * len(codes),
            "pb": [1.2] * len(codes),
            "market_cap": [100.0] * len(codes),
            "float_cap": [90.0] * len(codes),
            "is_st": [False] * len(codes),
            "source": ["tencent"] * len(codes),
        },
        schema=SPOT_SCHEMA,
    )


class SnapStub:
    name = "tencent"
    markets = frozenset({"A"})
    market_snapshot_markets = frozenset({"A"})

    def __init__(self, frame):
        self.frame = frame

    def market_snapshot(self, market, codes):
        return self.frame


def service(db, frame):
    registry = AdapterRegistry(
        adapters={"tencent": SnapStub(frame)},
        market_sources={"A": ("tencent",)},
        sleep=lambda _s: None,
    )
    return MarketSnapshotService(
        registry,
        SymbolListRepo(db),
        SnapshotRepo(db),
        clock=FrozenClock(NOW),
    )


def seed_codes(db, *codes):
    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in codes
        ],
    )


def test_universe_comes_from_the_code_table_and_missing_rows_are_named(db):
    seed_codes(db, "600519", "000001", "300750")
    sync = service(db, spot_rows("600519", "300750")).sync("A")
    assert sync.universe_size == 3
    assert sync.rows == 2
    assert sync.skipped == ("000001",)
    assert sync.day == date(2026, 10, 28)


def test_the_day_comes_from_the_quote_moments_not_from_the_wall_clock(db):
    """节假日补采时，今天没有报价：日要落在最近那个有报价的交易日。"""
    seed_codes(db, "600519")
    sync = service(db, spot_rows("600519", quoted=date(2026, 10, 23))).sync("A")
    assert sync.day == date(2026, 10, 23)


def test_an_empty_code_table_is_a_refusal_with_the_next_command(db):
    with pytest.raises(ValidationError, match="symbol_master"):
        service(db, spot_rows()).sync("A")


def test_nothing_is_written_when_every_source_confirms_empty(db):
    seed_codes(db, "600519")
    clock = FrozenClock(NOW)
    registry = AdapterRegistry(
        adapters={"tencent": FramelessStub()},
        market_sources={"A": ("tencent",)},
        sleep=lambda _s: None,
    )
    from sams.core.errors import EmptyResponse

    class FramelessStub(SnapStub):
        def market_snapshot(self, market, codes):
            raise EmptyResponse("tencent", "空")

    sync = MarketSnapshotService(
        registry, SymbolListRepo(db), SnapshotRepo(db), clock=clock
    ).sync("A")
    assert sync.rows == 0 and sync.empty_everywhere
    assert SnapshotRepo(db).latest_day("A") is None
```

（`FramelessStub` 要定义在用例之前；把类提到 `test_nothing_is_written…` 上方。）

- [ ] **Step 2: 跑测试确认红**

Run: `uv run pytest tests/services/test_market_snapshot.py -q`
Expected: `ModuleNotFoundError` 或 `ImportError: MarketSnapshotService`。

- [ ] **Step 3: 写服务**

```python
# src/sams/services/market_snapshot.py
"""全市场快照：票池 → 取数 → 定日 → 整日落库 → 把源答不出的票点名。

票池来自 `symbol_master` 的活跃代码（D187）：不用 `financials.synced_codes`，那是
「同步过财务的票」，与一个市场的几千只差三个数量级——用它当票池，屏幕上那句范围
就在替人做一次没做过的普查。

日从**这一批报价时刻的众数**来，不从墙钟来：节假日补采时今天没有报价，落在今天
等于把上周五的价格写成今天的快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import ValidationError
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo


@dataclass(frozen=True)
class SnapshotSync:
    market: str
    day: date
    taken_at: datetime
    rows: int
    universe_size: int
    #: 请求了、源没有回的代码（不是「没有数」，是「这次没问到」——两者下一步不同）
    skipped: tuple[str, ...] = ()
    source: str = ""
    empty_everywhere: bool = False


class MarketSnapshotService:
    def __init__(
        self,
        registry,
        symbols: SymbolListRepo,
        snapshots: SnapshotRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.registry = registry
        self.symbols = symbols
        self.snapshots = snapshots
        self.clock = clock

    def sync(
        self, market: str, *, codes: list[str] | None = None, day: date | None = None
    ) -> SnapshotSync:
        market = canonical_market(market)
        wanted = (
            [canonical_code(market, code) for code in codes]
            if codes is not None
            else self.symbols.active_codes(market)
        )
        if not wanted:
            raise ValidationError(
                f"{market} 的代码表里没有在市代码：先跑 "
                f"`sams run once --job symbol_master --market {market}`"
            )
        result = self.registry.fetch_market_snapshot(market, wanted)
        taken_at = self.clock.now_utc()
        if result.empty_everywhere:
            return SnapshotSync(
                market=market,
                day=day or self.clock.today(),
                taken_at=taken_at,
                rows=0,
                universe_size=len(wanted),
                source=result.source,
                empty_everywhere=True,
            )
        frame = result.frame
        stamped = _modal_day(frame["quoted_on"])
        actual = day or stamped or self.clock.today()
        written = self.snapshots.replace_day(
            market=market, day=actual, taken_at=taken_at, frame=frame
        )
        answered = set(frame["code"])
        return SnapshotSync(
            market=market,
            day=actual,
            taken_at=taken_at,
            rows=written,
            universe_size=len(wanted),
            skipped=tuple(code for code in wanted if code not in answered),
            source=result.source,
        )


def _modal_day(column: pl.Series) -> date | None:
    """众数日子：一只停牌票的旧时刻不改写这一天（D186）。平票取较晚的那天。"""
    days = column.drop_nulls().to_list()
    if not days:
        return None
    counts: dict[date, int] = {}
    for value in days:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts.values())
    return max(day for day, count in counts.items() if count == best)
```

- [ ] **Step 4: 装配（bootstrap）**

在 import 区加：

```python
from sams.datasource.spot import ...  # 不需要
from sams.services.market_snapshot import MarketSnapshotService
from sams.storage.repos.snapshots import SnapshotRepo
```

`ServiceContext` 里在 `scan_store` 之后加两格：

```python
    snapshot_store: SnapshotRepo
    snapshots: MarketSnapshotService
```

在 `_context_on` 里、`scan_store = ScanResultRepo(database)` 之后加：

```python
    snapshot_store = SnapshotRepo(database)
```

在 `symbol_master_sync = SymbolMasterSyncService(...)` 之后加：

```python
    market_snapshot_sync = MarketSnapshotService(
        registry, symbol_list_store, snapshot_store, clock=clock
    )
```

在 `JobDeps(...)` 里加（见 Task 8 的槽位；本任务先加也能编译）：

```python
        snapshots=market_snapshot_sync,
```

在 `return ServiceContext(...)` 里、`scan_store=scan_store,` 之后加：

```python
        snapshot_store=snapshot_store,
        snapshots=market_snapshot_sync,
```

- [ ] **Step 5: 跑测试到绿**

Run: `uv run pytest tests/services/test_market_snapshot.py tests/test_bootstrap.py -q`
Expected: PASS。

- [ ] **Step 6: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/services/market_snapshot.py src/sams/bootstrap.py tests/services/test_market_snapshot.py
git commit -m "feat(screener): 快照服务——票池来自代码表、日取报价众数、缺答点名（D174-D187）"
```

---

### Task 7: `sams sync spot` 手动补采

**Files:**
- Modify: `src/sams/cli/sync.py`
- Test: `tests/cli/test_sync_spot_cli.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/cli/test_sync_spot_cli.py
"""手动补采快照：输出第一句是范围与日，源没答的票要说数。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
from support import FrozenClock

import sams.cli as cli
from sams.bootstrap import build_container
from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.symbol_list import SymbolListRepo


class SnapStub:
    name = "tencent"
    markets = frozenset({"A"})
    market_snapshot_markets = frozenset({"A"})

    def __init__(self):
        self.codes: list[str] = []

    def market_snapshot(self, market, codes):
        self.codes = list(codes)
        keep = [c for c in codes if c != "000001"]
        return pl.DataFrame(
            {
                "market": ["A"] * len(keep),
                "code": keep,
                "name": ["票"] * len(keep),
                "quoted_on": [date(2026, 10, 28)] * len(keep),
                "price": [10.0] * len(keep),
                "pct_chg": [1.0] * len(keep),
                "amount": [1e8] * len(keep),
                "turnover_rate": [1.0] * len(keep),
                "volume_ratio": [1.0] * len(keep),
                "amplitude": [1.0] * len(keep),
                "pe_ttm": [10.0] * len(keep),
                "pe_dyn": [9.0] * len(keep),
                "pb": [1.2] * len(keep),
                "market_cap": [100.0] * len(keep),
                "float_cap": [90.0] * len(keep),
                "is_st": [False] * len(keep),
                "source": ["tencent"] * len(keep),
            },
            schema=SPOT_SCHEMA,
        )


def container_for(db, settings):
    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in ("600519", "000001", "300750")
        ],
    )
    built = build_container(
        settings,
        db=db,
        clock=FrozenClock(datetime(2026, 10, 28, 8, 41, tzinfo=UTC)),
        adapters={"tencent": SnapStub()},
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv):
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def test_sync_spot_reports_range_and_day(db, settings, capsys):
    container = container_for(db, settings)
    capsys.readouterr()
    assert run(container, ["sync", "spot", "--market", "A"]) == 0
    out = capsys.readouterr().out
    assert "快照 2026-10-28" in out
    assert "2 行" in out and "请求 3 只" in out
    assert "000001" in out, "源没答的票要点名，不能让「少了 1 只」混进「这个市场就这些票」"


def test_sync_spot_json_carries_the_same_numbers(db, settings, capsys):
    container = container_for(db, settings)
    capsys.readouterr()
    run(container, ["sync", "spot", "--market", "A", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 2 and payload["universe_size"] == 3
    assert payload["skipped"] == ["000001"]
    assert payload["day"] == "2026-10-28"
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run pytest tests/cli/test_sync_spot_cli.py -q`
Expected: argparse `SystemExit: 2`（`invalid choice: 'spot'`）。

- [ ] **Step 3: 实现**

在 `sync.py` 的 `add_to` 里、`fx` 之前加：

```python
    spot = actions.add_parser(
        "spot",
        help="全市场快照（一个市场的一次横截面；盘后自动跑的那条同源）",
    )
    spot.add_argument("--market", required=True, help="A（本期只接 A 股）")
    spot.add_argument("--day", help="YYYY-MM-DD，默认按报价时刻的众数日")
    spot.add_argument(
        "--code", action="append", help="只采这几只（试跑用；默认=代码表里全部在市）"
    )
    spot.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    spot.set_defaults(handler=run_spot)
```

在文件末尾（`run_fx` 之后）加：

```python
def run_spot(container: Container, args: argparse.Namespace) -> int:
    """补采与盘后 job 调同一个服务函数；这里只负责把范围、日与缺答写成一句话。"""
    try:
        report = container.snapshots.sync(
            args.market,
            codes=args.code or None,
            day=iso_date(args.day) if args.day else None,
        )
    except ValidationError as exc:
        if args.json:
            _emit({"status": "refused", "reason": str(exc), "exit": 2})
        else:
            print(f"参数有误：{exc}")
        return 2
    code = 0 if report.rows else 1
    if args.json:
        _emit(
            {
                "status": "empty" if not report.rows else "ok",
                "exit": code,
                "market": report.market,
                "day": str(report.day),
                "taken_at": report.taken_at.isoformat(),
                "rows": report.rows,
                "universe_size": report.universe_size,
                "skipped": list(report.skipped),
                "source": report.source,
                "empty_everywhere": report.empty_everywhere,
            }
        )
        return code
    print(
        f"{report.market} 股  快照 {report.day}  请求 {report.universe_size} 只  "
        f"{report.rows} 行  来源 {report.source or '–'}  采集 {stamp(report.taken_at)}"
    )
    if report.empty_everywhere:
        print("⇒ 源回了一张空表：旧数据保留，未写入任何行（先看健康页的源状态）")
    elif report.skipped:
        print(
            f"⇒ 源没答 {len(report.skipped)} 只（代码表里有、这次响应里没有）："
            f"{'、'.join(report.skipped[:20])}"
            + ("…（--json 拿全表）" if len(report.skipped) > 20 else "")
        )
    return code
```

`sync.py` 顶部 import 区补：

```python
from sams.cli.common import ... stamp  # 已有 group/iso_date/emit 等，名字按实际补齐
```

（若 `_emit` 在本模块不存在，用 `print(json.dumps(..., ensure_ascii=False))` 并与
`sams/cli/screen.py` 的 `_emit` 同形。）

- [ ] **Step 4: 跑测试到绿**

Run: `uv run pytest tests/cli/test_sync_spot_cli.py tests/cli/test_cli.py -q`
Expected: PASS。

- [ ] **Step 5: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/cli/sync.py tests/cli/test_sync_spot_cli.py
git commit -m "feat(cli): sams sync spot 手动补采全市场快照"
```

---

### Task 8: 盘后 `spot` job

**Files:**
- Modify: `src/sams/services/scheduler/types.py`
- Modify: `src/sams/services/scheduler/jobs.py`
- Modify: `src/sams/bootstrap.py`
- Test: `tests/services/test_scheduler_jobs_data.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/services/test_scheduler_jobs_data.py
from sams.services.market_snapshot import SnapshotSync
from sams.services.scheduler.jobs import run_spot


class SnapshotServiceStub:
    """市场级快照服务的替身：与 `MarketSnapshotService.sync` 同签名。"""

    def __init__(self, report=None, error=None):
        self.report = report
        self.error = error
        self.calls: list[str] = []

    def sync(self, market, *, codes=None, day=None):
        self.calls.append(market)
        if self.error is not None:
            raise self.error
        return self.report


def snapshot_report(**over):
    base = dict(
        market="A",
        day=AS_OF,
        taken_at=datetime(2024, 10, 25, 8, 30, tzinfo=UTC),
        rows=5000,
        universe_size=5100,
        skipped=("000001",),
        source="tencent",
    )
    base.update(over)
    return SnapshotSync(**base)


def test_spot_job_writes_what_the_service_answers(db, settings):
    stub = SnapshotServiceStub(snapshot_report())
    outcome = run_spot(replace(deps(db, settings), snapshots=stub), ctx())
    assert stub.calls == ["A"]
    assert outcome.ok and outcome.rows_affected == 5000
    assert "快照 2024-10-25 5000 行" in outcome.note
    assert "1 只" in outcome.note


def test_spot_job_failure_is_one_market_named_error(db, settings):
    stub = SnapshotServiceStub(error=RateLimited("tencent", "429"))
    outcome = run_spot(replace(deps(db, settings), snapshots=stub), ctx())
    assert not outcome.ok and "A" in outcome.errors[0]


def test_spot_job_is_registered_only_when_both_halves_exist(db, settings):
    plain = deps(db, settings)
    assert ("spot", "A") not in titles(plain)
    with_capability = replace(plain, registry=AdapterRegistry(
        adapters={"akshare": SnapshotCapable()},
        market_sources={"A": ("akshare",)},
        sleep=lambda _s: None,
    ))
    assert ("spot", "A") not in titles(with_capability)  # 服务没装
    assert ("spot", "A") in titles(replace(with_capability, snapshots=SnapshotServiceStub()))


class SnapshotCapable(RecordingBarsAdapter):
    """A 股日线源顺带提供快照：注册表只问「方法在不在」。"""

    def market_snapshot(self, market, codes):
        raise NotImplementedError
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run pytest tests/services/test_scheduler_jobs_data.py -q -k spot`
Expected: `ImportError: cannot import name 'run_spot'`。

- [ ] **Step 3: 实现**

`types.py` 的 `JobDeps` 末尾（`symbol_master` 之后）加：

```python
    #: 全市场快照（D186）：市场级、不按票隔离；没装这一层的部署照旧构造
    snapshots: MarketSnapshotService | None = None
```

import 区加 `from sams.services.market_snapshot import MarketSnapshotService`。

`jobs.py` 的 `run_indices` 之前加：

```python
def run_spot(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """全市场快照：单元是整个市场的一张横截面（拆不成按只隔离）。

    失败只说一句带市场名的话：job 失败时保留上一次那天的数据，页面照旧能读，
    只是范围行会写「这是哪天的快照」——**绝不**渲染成「这个市场没有便宜票」。
    """
    assert ctx.market is not None
    assert deps.snapshots is not None  # 登记前查过注入，这里只兜编程错误
    try:
        report = deps.snapshots.sync(ctx.market)
    except SamsError as exc:
        return JobOutcome(note=f"{ctx.market} 全市场快照失败", errors=(f"{ctx.market}: {exc}",))
    if report.empty_everywhere:
        return JobOutcome(note=f"{ctx.market} 快照空答复，保留旧表")
    note = f"{ctx.market} 快照 {report.day} {report.rows} 行"
    if report.skipped:
        note += f"，源没答 {len(report.skipped)} 只"
    return JobOutcome(rows_affected=report.rows, note=note)
```

`build_jobs` 里，在数据链那条 `for method, name, run in _DATA_JOBS` 之后、`indices` 之前加：

```python
        # 快照排在数据链之后：它读的是当天收盘价，与日线那批互不依赖，但都在盘后完成
        if deps.snapshots is not None and _has(deps, market, "market_snapshot"):
            jobs.append(JobSpec("spot", DAILY, market, partial(run_spot, deps)))
```

- [ ] **Step 4: 跑测试到绿**

Run: `uv run pytest tests/services/test_scheduler_jobs_data.py tests/cli/test_run_cli.py -q`
Expected: PASS。

- [ ] **Step 5: 更新 `sams run once --job` 的帮助词**

`cli/schedule.py` 里那句 `help="只跑这个 job（bars/actions/financials/valuation/flow/recheck/rules/fx/calendar）"`
改成包含 `spot`：

```
help="只跑这个 job（bars/actions/financials/valuation/flow/spot/recheck/rules/fx/calendar）"
```

- [ ] **Step 6: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/services/scheduler/types.py src/sams/services/scheduler/jobs.py src/sams/cli/schedule.py tests/services/test_scheduler_jobs_data.py
git commit -m "feat(scheduler): 盘后 spot job（DAILY，市场级）（D176）"
```

---

### Task 9: 选股引擎（三数、命中、无数分组、截断）与装配

**Files:**
- Create: `src/sams/services/screener.py`
- Modify: `src/sams/bootstrap.py`
- Test: `tests/services/test_screener.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/services/test_screener.py
"""选股引擎：每条件三个数、三者相加等于票池、无数出局的票带原因点名（D180）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support_fundamentals import financial_rows  # 若签名不同，改用本文件内的 _fin

from sams.core.errors import ValidationError
from sams.core.screener_fields import Condition
from sams.services.screener import ScreenerService
from sams.storage.repos.financials import FinancialRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def snap(code, *, pe_dyn=10.0, pb=1.0, turnover=1.0, price=10.0, quoted=DAY):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": quoted,
        "price": price,
        "pct_chg": 1.0,
        "amount": 1e8,
        "turnover_rate": turnover,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": pe_dyn,
        "pe_dyn": pe_dyn,
        "pb": pb,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


def seed(db, *rows, codes=("600519",)):
    from sams.datasource.spot import SPOT_SCHEMA

    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in codes
        ],
    )
    data = [{"market": "A", "taken_at": TAKEN, **row} for row in rows]
    frame = pl.DataFrame(data, schema={"market": pl.Utf8, "taken_at": pl.Datetime("us", "UTC"), **SPOT_SCHEMA})
    SnapshotRepo(db).replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame)


def service(db):
    return ScreenerService(SnapshotRepo(db), SymbolListRepo(db), FinancialRepo(db))


def test_every_condition_reports_passed_failed_missing_that_sum_to_the_universe(db):
    seed(
        db,
        snap("600519", pe_dyn=10.0, pb=1.0),   # PR 0.10 通过
        snap("000001", pe_dyn=20.0, pb=1.0),   # PR 0.40 通过
        snap("300750", pe_dyn=40.0, pb=1.0),   # PR 1.60 不通过
        snap("600000", pe_dyn=-5.0, pb=1.0),   # 亏损 → 无数
        codes=("600519", "000001", "300750", "600000"),
    )
    result = service(db).run("A", [Condition("pr_now", "<=", 0.5)])
    counts = result.fields[0]
    assert (counts.passed, counts.failed, counts.missing) == (2, 1, 1)
    assert counts.passed + counts.failed + counts.missing == result.universe_size == 4
    assert [hit.code for hit in result.hits] == ["600519", "000001"]


def test_hits_carry_the_values_of_the_condition_that_picked_them(db):
    seed(db, snap("600519", pe_dyn=10.0, pb=1.0))
    hit = service(db).run("A", [Condition("pr_now", "<=", 0.5)]).hits[0]
    assert hit.values["pr_now"] == pytest.approx(0.1)
    assert hit.values["price"] == 10.0


def test_nothing_missing_means_no_missing_groups(db):
    seed(db, snap("600519"))
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.missing == () and result.fields[0].missing == 0


def test_missing_codes_are_grouped_with_a_reason_and_a_next_step(db):
    seed(db, snap("600519", pe_dyn=-5.0, pb=1.0))
    result = service(db).run("A", [Condition("pr_now", "<=", 0.5)])
    (group,) = result.missing
    assert group.field == "pr_now" and group.codes == ("600519",)
    assert "亏损" in group.reason


def test_financial_fields_join_as_of_the_snapshot_day_and_report_the_gap(db):
    seed(db, snap("600519"), snap("000001"), codes=("600519", "000001"))
    FinancialRepo(db).upsert(financial_rows(codes=("600519",), roe=0.2))
    result = service(db).run("A", [Condition("roe", ">=", 0.1)])
    assert result.fields[0].passed == 1
    (group,) = result.missing
    assert group.codes == ("000001",) and "financials" in group.reason


def test_snapshot_only_and_list_only_are_two_separate_numbers(db):
    """D174：快照里多出来的、代码表里缺的，两个数分开报，不合并成一个「有效票池」。"""
    seed(db, snap("600519"), snap("999999"), codes=("600519", "000001"))
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.snapshot_only == 1  # 999999 不在代码表
    assert result.list_only == 1      # 000001 没有快照
    assert result.universe_size == 1


def test_no_snapshot_returns_a_named_empty_result_not_an_exception(db):
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.universe_size == 0 and result.day is None
    assert "sams sync spot" in result.note


def test_limit_truncates_but_the_total_stays_visible(db):
    seed(db, *(snap(c) for c in ("600519", "000001", "300750")), codes=("600519", "000001", "300750"))
    result = service(db).run("A", [Condition("price", ">=", 1.0)], limit=2)
    assert len(result.hits) == 2 and result.total_hits == 3 and result.truncated == 1


def test_two_pr_fields_are_refused_before_any_query(db):
    """D59：两条公式的数不能同篮——拦在引擎入口，不是「按其中一条排序」。"""
    with pytest.raises(ValidationError):
        service(db).run("A", [Condition("pr_now", "<=", 0.5)])
        # 注：v1 只有一条 PR 词条，混选由词表校验拦（Task 1 已钉）；
        # 这一枚留作回归位：将来注册第二条时，这条用例要改成两条同时出现
```

最后一个用例需要改成真实的混选（v1 只有 pr_now，无法混选）。改为：断言空条件与未知字段在此层被拒（校验前移）：

```python
def test_validation_happens_before_the_database_is_read(db):
    with pytest.raises(ValidationError):
        service(db).run("A", [Condition("unknown", "<=", 0.5)])
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run pytest tests/services/test_screener.py -q`
Expected: `ImportError: cannot import name 'ScreenerService'`。

- [ ] **Step 3: 写引擎**

```python
# src/sams/services/screener.py
"""选股引擎：在一天的全市场快照上跑一组 AND 条件（D178/D180）。

三件事决定这份实现的形状：

* **票池 = 当日快照 ∩ 代码表**（D174）。快照里多出来的、代码表里缺的，两个数分开报——
  合成一个「有效票池 N 只」会让「少了 50 只」永远是零。
* **每条条件报三个数**：通过 / 不通过 / 无数，三者相加等于票池大小。`PR ≤ 0.5` 最想
  找的困境反转与周期底部恰恰最容易算不出数（亏损、净资产为负），静默丢掉它们，
  结果看起来完整，实则把这条规则最该抓的东西筛掉了（D43/D60/D180）。
* **行上带的是被筛出来时那一份数**（D181）：不再打一次实时报价——那会让人对着一张
  昨天的 PR 做今天要不要买的决定，而屏幕上没有一处说清两者差了一天。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from math import isnan

import polars as pl

from sams.analysis.fundamentals import pr_now
from sams.core.codes import canonical_market
from sams.core.fundamentals import ROE_BASIS_ANNUAL
from sams.core.screener_fields import FIELDS, Condition, Field, field, validate
from sams.storage.repos.financials import FinancialRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

_FINANCIAL_COLUMNS = ("roe", "revenue_yoy", "profit_yoy")


@dataclass(frozen=True)
class FieldCounts:
    field: str
    op: str
    value: float | tuple[float, float]
    passed: int
    failed: int
    missing: int


@dataclass(frozen=True)
class ScreenHit:
    code: str
    name: str | None
    values: dict[str, float | None]


@dataclass(frozen=True)
class MissingGroup:
    field: str
    reason: str
    codes: tuple[str, ...]


@dataclass(frozen=True)
class ScreenResult:
    market: str
    day: date | None
    taken_at: datetime | None
    universe_size: int
    market_size: int
    snapshot_only: int
    list_only: int
    fields: tuple[FieldCounts, ...] = ()
    hits: tuple[ScreenHit, ...] = ()
    total_hits: int = 0
    truncated: int = 0
    missing: tuple[MissingGroup, ...] = ()
    note: str = ""


class ScreenerService:
    def __init__(
        self,
        snapshots: SnapshotRepo,
        symbols: SymbolListRepo,
        financials: FinancialRepo,
    ) -> None:
        self.snapshots = snapshots
        self.symbols = symbols
        self.financials = financials

    def run(
        self,
        market: str,
        conditions: list[Condition],
        *,
        day: date | None = None,
        limit: int | None = None,
        registry: tuple[Field, ...] = FIELDS,
    ) -> ScreenResult:
        market = canonical_market(market)
        checked = validate(market, conditions, registry=registry)
        specs = {cond.field: field(cond.field, registry=registry) for cond in checked}
        active = set(self.symbols.active_codes(market))
        snapshot_day = day if day is not None else self.snapshots.latest_day(market)
        frame = (
            self.snapshots.load_day(market, snapshot_day)
            if snapshot_day is not None
            else pl.DataFrame()
        )
        if frame.is_empty():
            note = (
                f"{market} 在 {snapshot_day} 没有快照"
                if snapshot_day is not None
                else f"{market} 还没有任何快照（下一步 `sams sync spot --market {market}`）"
            )
            return ScreenResult(
                market=market,
                day=snapshot_day,
                taken_at=None,
                universe_size=0,
                market_size=len(active),
                snapshot_only=0,
                list_only=len(active),
                note=note,
            )
        taken_at = frame["taken_at"].max()
        codes = set(frame["code"])
        snapshot_only = len(codes - active)
        list_only = len(active - codes)
        universe = frame.filter(pl.col("code").is_in(sorted(active))) if active else frame.head(0)
        if universe.is_empty():
            return ScreenResult(
                market=market,
                day=snapshot_day,
                taken_at=taken_at,
                universe_size=0,
                market_size=len(active),
                snapshot_only=snapshot_only,
                list_only=list_only,
                note=(
                    f"快照里没有一只在代码表里：先跑 "
                    f"`sams run once --job symbol_master --market {market}`"
                ),
            )
        universe = self._with_financials(market, universe, snapshot_day)
        reasons: dict[str, str] = {}
        if "pr_now" in specs:
            values = []
            for code, pe_dyn, pb in universe.select("code", "pe_dyn", "pb").iter_rows():
                outcome = pr_now(pe_dyn, pb)
                values.append(outcome.value)
                if outcome.value is None:
                    reasons[code] = outcome.reason or ""
            universe = universe.with_columns(
                pl.Series("pr_now", values, dtype=pl.Float64)
            )
        counts: list[FieldCounts] = []
        masks: list[pl.Series] = []
        for cond in checked:
            column = universe[specs[cond.field].column]
            missing = column.is_null() | column.is_nan()
            passed = (_compare(column, cond) & ~missing).fill_null(False)
            counts.append(
                FieldCounts(
                    field=cond.field,
                    op=cond.op,
                    value=cond.value,
                    passed=int(passed.sum()),
                    failed=int((~passed & ~missing).sum()),
                    missing=int(missing.sum()),
                )
            )
            masks.append(passed)
        hit_mask = masks[0]
        for mask in masks[1:]:
            hit_mask = hit_mask & mask
        sort_column = specs[checked[0].field].column
        ranked = universe.filter(hit_mask).sort([sort_column, "code"], nulls_last=True)
        total = ranked.height
        limited = ranked.head(limit) if limit is not None else ranked
        labels = [cond.field for cond in checked]
        hits = tuple(
            ScreenHit(code=row["code"], name=row["name"], values={name: row[specs[name].column] for name in labels})
            for row in limited.iter_rows(named=True)
        )
        hit_codes = set(limited["code"])
        return ScreenResult(
            market=market,
            day=snapshot_day,
            taken_at=taken_at,
            universe_size=universe.height,
            market_size=len(active),
            snapshot_only=snapshot_only,
            list_only=list_only,
            fields=tuple(counts),
            hits=hits,
            total_hits=total,
            truncated=total - limited.height,
            missing=_missing_groups(universe, checked, specs, hit_codes, reasons, market),
        )

    def _with_financials(self, market: str, universe: pl.DataFrame, day: date) -> pl.DataFrame:
        """只用到了财务字段才去问库：一次全市场扫描不该为快照列多付一条 join。"""
        wanted = {"roe", "revenue_yoy", "profit_yoy"}
        if not any(column in universe.columns for column in wanted):
            return universe
        loaded, _dropped = self.financials.load_as_of(
            market, universe["code"].to_list(), day, basis=ROE_BASIS_ANNUAL
        )
        by_code = {row["code"]: row for row in loaded.iter_rows(named=True)}
        return universe.with_columns(
            [
                pl.Series(
                    column,
                    [by_code.get(code, {}).get(column) for code in universe["code"]],
                    dtype=pl.Float64,
                )
                for column in _FINANCIAL_COLUMNS
            ]
        )


def _compare(column: pl.Series, cond: Condition) -> pl.Series:
    if cond.op == "between":
        low, high = cond.value  # type: ignore[misc]
        return (column >= low) & (column <= high)
    if cond.op == ">=":
        return column >= cond.value  # type: ignore[operator]
    if cond.op == ">":
        return column > cond.value  # type: ignore[operator]
    if cond.op == "<=":
        return column <= cond.value  # type: ignore[operator]
    if cond.op == "<":
        return column < cond.value  # type: ignore[operator]
    return column == cond.value  # type: ignore[operator]


def _missing_groups(
    universe: pl.DataFrame,
    checked: tuple[Condition, ...],
    specs: dict[str, Field],
    hit_codes: set[str],
    reasons: dict[str, str],
    market: str,
) -> tuple[MissingGroup, ...]:
    """一只票只记**第一条**让它出局的条件：同一个缺口记两次会让「无数 N 只」重复计人。"""
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in universe.iter_rows(named=True):
        if row["code"] in hit_codes:
            continue
        for cond in checked:
            value = row[specs[cond.field].column]
            if value is None or (isinstance(value, float) and isnan(value)):
                if cond.field == "pr_now":
                    reason = reasons.get(row["code"], "现值 PR 没有数")
                elif specs[cond.field].source == "financials":
                    reason = (
                        f"没有已披露年报里的{specs[cond.field].label}："
                        f"`sams sync financials --market {market} --code {row['code']}`"
                    )
                else:
                    reason = "快照这一行缺这一列"
                grouped.setdefault((cond.field, reason), []).append(row["code"])
                break
    return tuple(
        MissingGroup(field=name, reason=reason, codes=tuple(sorted(codes)))
        for (name, reason), codes in sorted(grouped.items())
    )
```

注意：`_with_financials` 的判断要看**选中的条件**用了哪些字段，而不是帧里有没有列
（快照帧没有财务列，所以这里的 `any(column in universe.columns)` 永远 False）。
改成由 `run` 传入 `wanted = {spec.column for spec in specs.values()}`：

```python
        universe = self._with_financials(market, universe, snapshot_day, wanted=set(specs.values()))
```

```python
    def _with_financials(
        self, market: str, universe: pl.DataFrame, day: date, *, wanted: set[Field]
    ) -> pl.DataFrame:
        columns = sorted({item.column for item in wanted if item.source == "financials"})
        if not columns:
            return universe
        ...
        return universe.with_columns([... for column in columns])
```
（`_FINANCIAL_COLUMNS` 常量随之删掉。）

- [ ] **Step 4: 装配（bootstrap）**

在 import 区加 `from sams.services.screener import ScreenerService`。

`ServiceContext` 里在 `screen: ScreenService` 之后加：

```python
    screener: ScreenerService
```

`return ServiceContext(...)` 里在 `screen=ScreenService(...)` 之后加：

```python
        screener=ScreenerService(snapshot_store, symbol_list_store, financials_store),
```

- [ ] **Step 5: 跑测试到绿**

Run: `uv run pytest tests/services/test_screener.py tests/test_bootstrap.py -q`
Expected: PASS（若 `tests/support_fundamentals.py` 的 `financial_rows` 签名不同，按它的
实际参数改写测试里的那一行，不要改支持文件）。

- [ ] **Step 6: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/services/screener.py src/sams/bootstrap.py tests/services/test_screener.py
git commit -m "feat(screener): 选股引擎——票池双缺口、每条件三数、无数分组（D174/D180）"
```

---

### Task 10: `sams screen spot` 命令行

**Files:**
- Modify: `src/sams/cli/screen.py`
- Test: `tests/cli/test_screen_spot_cli.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/cli/test_screen_spot_cli.py
"""`sams screen spot`：第一行是范围，每条件三个数，无数出局的票逐个点名。

这一屏最容易撒的谎不是算错 PR，是「无数」被当成「不通过」静默丢掉。所以断言分三段读：
范围行、条件行、无数段；命中与无数各自成段，同一只票只能出现在其中一段。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock

import sams.cli as cli
from sams.bootstrap import build_container
from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def snap(code, *, pe_dyn=10.0, pb=1.0, turnover=1.0):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": DAY,
        "price": 10.0,
        "pct_chg": 1.0,
        "amount": 1e8,
        "turnover_rate": turnover,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": pe_dyn,
        "pe_dyn": pe_dyn,
        "pb": pb,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


def container_for(db, settings, *rows):
    codes = tuple(row["code"] for row in rows)
    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in codes
        ],
    )
    frame = pl.DataFrame(
        [{"market": "A", "taken_at": TAKEN, **row} for row in rows],
        schema={"market": pl.Utf8, "taken_at": pl.Datetime("us", "UTC"), **SPOT_SCHEMA},
    )
    SnapshotRepo(db).replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame)
    built = build_container(
        settings,
        db=db,
        clock=FrozenClock(datetime(2026, 10, 28, 8, 41, tzinfo=UTC)),
        adapters={},
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv):
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def test_first_line_is_the_range_then_one_line_per_condition(db, settings, capsys):
    container = container_for(
        db,
        settings,
        snap("600519", pe_dyn=10.0),
        snap("000001", pe_dyn=20.0),
        snap("300750", pe_dyn=40.0),
    )
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "pr_now<=0.5"]) == 0
    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert "A 股" in first and "快照 2026-10-28" in first
    assert "票池 3 只" in out and "代码表 3 只" in out
    assert "pr_now <= 0.5：通过 2 / 不通过 1 / 无数 0" in out
    assert "600519" in out and "000001" in out
    assert "300750" in out.split("不通过")[0] or "300750" not in out.split("命中")[-1]


def test_missing_codes_are_named_in_their_own_section(db, settings, capsys):
    container = container_for(db, settings, snap("600519", pe_dyn=-5.0), snap("000001"))
    capsys.readouterr()
    run(container, ["screen", "spot", "--market", "A", "--where", "pr_now<=0.5"])
    out = capsys.readouterr().out
    missing = out.split("无数 1 只")[-1]
    assert "600519" in missing and "亏损" in missing


def test_no_snapshot_exits_1_with_the_next_command(db, settings, capsys):
    container = build_container(
        settings, db=db, clock=FrozenClock(datetime(2026, 10, 28, 8, 41, tzinfo=UTC)), adapters={}
    )
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "price>=1"]) == 1
    out = capsys.readouterr().out
    assert "sams sync spot" in out


def test_unknown_field_is_a_parameter_error_with_the_vocabulary(db, settings, capsys):
    container = container_for(db, settings, snap("600519"))
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "prr<=0.5"]) == 2
    assert "可选的是" in capsys.readouterr().out


def test_between_is_written_as_a_range_and_backwards_is_refused(db, settings, capsys):
    container = container_for(db, settings, snap("600519", turnover=2.0), snap("000001", turnover=9.0))
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "turnover_rate=1..5"]) == 0
    assert "600519" in capsys.readouterr().out
    assert run(container, ["screen", "spot", "--market", "A", "--where", "turnover_rate=5..1"]) == 2


def test_json_carries_three_counts_and_the_missing_groups(db, settings, capsys):
    container = container_for(db, settings, snap("600519", pe_dyn=10.0), snap("000001", pe_dyn=-5.0))
    capsys.readouterr()
    run(container, ["screen", "spot", "--market", "A", "--where", "pr_now<=0.5", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit"] == 0
    assert payload["fields"][0]["passed"] == 1
    assert payload["fields"][0]["missing"] == 1
    assert payload["missing"][0]["codes"] == ["000001"]
    assert payload["hits"][0]["code"] == "600519"
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run pytest tests/cli/test_screen_spot_cli.py -q`
Expected: argparse `SystemExit: 2`（`invalid choice: 'spot'`）。

- [ ] **Step 3: 实现**

`screen.py` 的 import 区补：

```python
import re

from sams.core.screener_fields import Condition, fields_for
```

在 `add_to` 的 `pr` 之后加：

```python
    spot = screen.add_parser(
        "spot",
        help="全市场选股：在一天的全市场快照上跑一组 AND 条件，无数出局的票逐个点名",
    )
    spot.add_argument("--market", required=True, help="A")
    spot.add_argument(
        "--where",
        required=True,
        action="append",
        help=(
            "一条条件，可多次传入（AND）：`pr_now<=0.5`、`turnover_rate>=1`、"
            "`pb=0.5..3`（写成 between）"
        ),
    )
    spot.add_argument("--day", help="YYYY-MM-DD，默认最近一天的快照")
    spot.add_argument("--limit", type=int, help="命中最多列这么多行（总数照报）")
    spot.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    spot.set_defaults(handler=run_spot_screen)
```

在 `run_pr` 之后加：

```python
_WHERE = re.compile(r"^\s*([a-z_]+)\s*(>=|<=|==|>|<|=)\s*(.+?)\s*$")


def parse_where(text: str) -> Condition:
    """`field OP value`；`=` 配 `低..高` 就是 between。

    不吃自由表达式：比较符之外的东西一律拒绝，好让「写错一个字符」在这里就有名字，
    而不是在结果里变成一条永远为假的筛选。
    """
    match = _WHERE.match(text or "")
    if match is None:
        raise ValidationError(
            f"--where {text!r} 读不出来：写法是 `字段 比较符 值`，例如 `pr_now<=0.5`"
        )
    name, op, raw = match.groups()
    op = "=" if op == "==" else op
    if op == "=" and ".." in raw:
        low, high = raw.split("..", 1)
        return Condition(name, "between", (_where_number(low, text), _where_number(high, text)))
    return Condition(name, op, _where_number(raw, text))


def _where_number(text: str, whole: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValidationError(f"--where {whole!r} 里的 {text!r} 不是一个数") from None


def run_spot_screen(container: Container, args: argparse.Namespace) -> int:
    try:
        conditions = [parse_where(text) for text in args.where]
        result = container.screener.run(
            args.market,
            conditions,
            day=iso_date(args.day) if args.day else None,
            limit=args.limit,
        )
    except ValidationError as exc:
        if args.json:
            _emit({"status": "refused", "reason": str(exc), "exit": 2})
        else:
            print(f"参数有误：{exc}")
        return 2
    code = 1 if result.universe_size == 0 else 0
    if args.json:
        _emit(_spot_payload(result, code))
        return code
    return _print_spot(result, code)


def _print_spot(result, code: int) -> int:
    if result.day is None or not result.universe_size:
        print(
            f"{result.market} 股  全市场选股  票池 0 只"
            f"（代码表 {result.market_size} 只）"
        )
        print(f"⇒ {result.note}")
        return code
    print(
        f"{result.market} 股  全市场选股  快照 {result.day}  采集 {stamp(result.taken_at)}"
    )
    print(
        f"票池 {result.universe_size} 只（代码表 {result.market_size} 只；"
        f"快照里不在代码表 {result.snapshot_only} 只；代码表里快照缺 {result.list_only} 只）"
    )
    specs = {item.name: item for item in fields_for(result.market)}
    for counts in result.fields:
        spec = specs[counts.field]
        print(
            f"条件 {counts.field} {counts.op} {_spot_value(counts.value)}"
            f"（{spec.label}，{spec.unit}）：通过 {counts.passed} / "
            f"不通过 {counts.failed} / 无数 {counts.missing}"
        )
    print(f"命中 {result.total_hits} 只（按 {result.fields[0].field} 升序）：")
    for hit in result.hits:
        values = "  ".join(
            f"{specs[name].label} {_cell(hit.values.get(name))}" for name in hit.values
        )
        print(f"  {hit.code}  {hit.name or '–'}  {values}")
    if result.truncated:
        print(f"  …还有 {result.truncated} 只未列（--limit 截的；总数照报）")
    for group in result.missing:
        print(f"无数 {len(group.codes)} 只  {group.field}：{group.reason}")
        shown = group.codes[:20]
        print(f"  {'、'.join(shown)}" + ("…（--json 拿全表）" if len(group.codes) > 20 else ""))
    print(
        "口径：比率列一律小数（0.20 = 20%）；PR 现值 = PE(动)² ÷ (100×PB)，"
        "亏损 / 净资产为负 / 推出来的 ROE 过界都记「无数」，不算落选"
    )
    return code
```

在 `_emit` 之前加：

```python
def _spot_value(value) -> str:
    if isinstance(value, tuple):
        low, high = value
        return f"{low:g}..{high:g}"
    return f"{value:g}"


def _cell(value) -> str:
    return "–" if value is None else f"{value:.4g}"


def _spot_payload(result, code: int) -> dict:
    return {
        "status": "empty" if not result.universe_size else "ok",
        "exit": code,
        "market": result.market,
        "day": str(result.day) if result.day else None,
        "taken_at": result.taken_at.isoformat() if result.taken_at else None,
        "universe_size": result.universe_size,
        "market_size": result.market_size,
        "snapshot_only": result.snapshot_only,
        "list_only": result.list_only,
        "fields": [
            {
                "field": item.field,
                "op": item.op,
                "value": list(item.value) if isinstance(item.value, tuple) else item.value,
                "passed": item.passed,
                "failed": item.failed,
                "missing": item.missing,
            }
            for item in result.fields
        ],
        "hits": [
            {"code": hit.code, "name": hit.name, "values": hit.values}
            for hit in result.hits
        ],
        "total_hits": result.total_hits,
        "truncated": result.truncated,
        "missing": [
            {"field": group.field, "reason": group.reason, "codes": list(group.codes)}
            for group in result.missing
        ],
        "note": result.note,
    }
```

- [ ] **Step 4: 跑测试到绿**

Run: `uv run pytest tests/cli/test_screen_spot_cli.py tests/cli/test_screen_cli.py -q`
Expected: PASS。

- [ ] **Step 5: lint + 提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/cli/screen.py tests/cli/test_screen_spot_cli.py
git commit -m "feat(cli): sams screen spot——范围行、三数、无数点名（D178-D181）"
```

---

### Task 11: 真接口验收 + 全量

**Files:**
- 只跑，不改（除非真接口暴露问题，按红绿补一条测试再改）

- [ ] **Step 1: 真接口冒烟（live）**

```bash
uv run pytest tests/datasource/test_spot.py -q -m live
```

Expected: PASS。若 `pb` 缺失的票多于 1 只，把那条断言的阈值按实测改，并在提交信息里写清
是哪几只——**不要**把断言放宽到「不崩就行」。

- [ ] **Step 2: 全量测试（主控跑，不加管道）**

```bash
uv run pytest -q
```

Expected: 0 failed；总数 ≈ 4267 + 本计划新增（约 60）。

- [ ] **Step 3: 真实采集（先停 serve，DuckDB 单写者）**

```bash
sams serve 正在跑的话先停掉（PID 查 `lsof data/sams.duckdb`）
uv run sams sync spot --market A
uv run sams screen spot --market A --where "pr_now<=0.5" --limit 20
uv run sams screen spot --market A --where "pr_now<=0.5" --where "turnover_rate>=1"
```

Expected:
- 快照行数在 5000 上下（代码表 5577，缺的应是新股/退市/源不认的少数）；
- 票池 = 快照 ∩ 代码表，两个缺口数单独出门；
- 第一条条件 `pr_now<=0.5` 的通过数在几百量级（银行/地产/周期），无数里能看到亏损票的
  理由；
- 结果行上的 PE/PB 与行情软件一致（抽 2 只人工核对）。

- [ ] **Step 4: 重启 serve 并确认页面不受影响**

```bash
uv run sams serve
curl -s -o /dev/null -w '%{http_code}\n' localhost:8765/healthz
```

Expected: `200`。

- [ ] **Step 5: 把实测数字写回 spec §3.1（一行）并提交**

```markdown
- 2026-10-28 验收实测：A 股快照 5xxx 行 / 票池 5xxx 只；`pr_now <= 0.5` 通过 xxx 只、
  无数 xx 只（原因：亏损 / 净资产为负 / PE 缺失）。
```

```bash
git add docs/superpowers/specs/2026-10-28-screener-market-snapshot-design.md
git commit -m "docs(screener): 计划 D 验收实测数字"
```

---

## 完成标准

- `sams sync spot --market A` 在真库落进几千行，`sams screen spot` 能按多条件组合筛出结果；
- 每条条件三个数、三者相加 = 票池；无数出局的票带原因与下一步命令；
- 快照同一天重采是整日覆盖；代码表/快照两个缺口分开报；
- `pr_now` 在亏损、净资产为负、ROE 过界上是「无数」，不参与比较；
- `spot` job 排在盘后链里，市场级失败带市场名；
- 全量测试绿，live 冒烟绿。
