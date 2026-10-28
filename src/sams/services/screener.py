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

import hashlib
import json
from dataclasses import dataclass
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
        universe = (
            frame.filter(pl.col("code").is_in(sorted(active))) if active else frame.head(0)
        )
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
        universe = self._with_financials(
            market, universe, snapshot_day, wanted=set(specs.values())
        )
        kinds: dict[str, str] = {}
        if "pr_now" in specs:
            values = []
            for code, pe_dyn, pb in universe.select("code", "pe_dyn", "pb").iter_rows():
                outcome = pr_now(pe_dyn, pb)
                values.append(outcome.value)
                if outcome.value is None:
                    kinds[code] = outcome.kind or "现值 PR 没有数"
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
            ScreenHit(
                code=row["code"],
                name=row["name"],
                # 现价不属于任何条件，但每一行都要拿它读「当时贵不贵」（D181）
                values={name: row[specs[name].column] for name in labels}
                | {"price": row["price"]},
            )
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
            missing=_missing_groups(universe, checked, specs, hit_codes, kinds, market),
        )

    def _with_financials(
        self,
        market: str,
        universe: pl.DataFrame,
        day: date,
        *,
        wanted: set[Field],
    ) -> pl.DataFrame:
        """只看**这次选中的条件**用到哪些财务列：全市场扫描不该为快照列多付一条 join。

        快照帧里没有财务列，所以判据必须是 `Field.source == "financials"`，
        不是「帧里有没有这一列」——后者永远为假，join 永远不会发生。
        """
        columns = sorted({item.column for item in wanted if item.source == "financials"})
        if not columns:
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
                for column in columns
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
    kinds: dict[str, str],
    market: str,
) -> tuple[MissingGroup, ...]:
    """一只票只记**第一条**让它出局的条件：同一个缺口记两次会让「无数 N 只」重复计人。

    pr_now 的组键用 `kind`（亏损 / 净资产为负 / …），不用逐票的 reason：后者带着
    这只票自己的 PE 或 PB，真实快照上 1459 只无数票会散成 1394 组。
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in universe.iter_rows(named=True):
        if row["code"] in hit_codes:
            continue
        for cond in checked:
            value = row[specs[cond.field].column]
            if value is None or (isinstance(value, float) and isnan(value)):
                if cond.field == "pr_now":
                    reason = kinds.get(row["code"], "现值 PR 没有数")
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


def preset_strategy_id(name: str, conditions) -> str:
    """方案重跑写进 `scan_results` 的键：名字 + 条件哈希（D183）。

    条件一改就是另一句话：旧结果仍按旧哈希查得到，而方案那一行本身不随时间变。
    """
    payload = json.dumps(
        [[item.field, item.op, item.value] for item in conditions],
        ensure_ascii=False,
        default=list,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"screen:{name}:{digest}"
