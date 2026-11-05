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
from sams.core.errors import ValidationError
from sams.core.fundamentals import ROE_BASIS_ANNUAL, out_of_caliber
from sams.core.screener_fields import FIELDS, Condition, Field, field, validate
from sams.services.technical import TechnicalService
from sams.storage.repos.financials import FinancialRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

#: 财务源不提供披露日的市场（D215/D228）：这些行的 PIT 只能按报告期止日采信。
#: 只在这个集合里放开，A 股仍只认 `published_date <= 快照日`。
UNDATED_FINANCIAL_MARKETS = frozenset({"HK", "US"})

#: 上面那条放宽一旦被条件用到，结果行就必须自己说出来：屏幕上的「年报 ROE」
#: 在这里是「报告期 ≤ 快照日的最新一期年报」，没有公开日可核。两个市场的理由不同：
#: 美股的财年还可以不是 12-31，那句话不写出来，读者会以为它取错了年份。
UNDATED_FINANCIAL_NOTES = {
    "HK": (
        "港股财务源不提供披露日（D215）：年报字段取「报告期 ≤ 快照日」的最新一期，"
        "公开时点无从核对；PEG 的净利同比同此口径"
    ),
    "US": (
        "美股财务源没有可核对的披露日（D228）：年报字段按「报告期 ≤ 快照日」取最新年报，"
        "财年可以不是 12-31（如 9 月末结账），公开时点无从核对；PEG 的净利同比同此口径"
    ),
}

#: 源没给 ROE 时引擎自己估了几只：结果里那些「年报 ROE」不是源值，必须说出来（D228）
DERIVED_ROE_NOTE = (
    "ROE 有 {count} 只不是源给的数，而是按「ROA ÷ (1−资产负债率)」由同一期年报估算"
    "（估算值同样过小数口径闸）"
)

#: 兜底公式缺输入或算出不可信的值时，那只票的原因（同一类缺口只归一组）
ROE_DERIVATION_FAILED = (
    "年报里没有 ROE，也按「ROA ÷ (1−资产负债率)」估不出可信值："
    "要同一期年报里两个数都有、资产负债率 < 100%、且结果不超过 ROE 的小数口径"
)

#: 负 PE / 负 PB 不是「更便宜」，是亏损或净资产为负（R-09）：估值条件默认只比正值，
#: 这些票记「无数」并点名，不静默丢掉。
#: 这句话同时上网页与命令行，所以只讲事实、不指路：网页的勾选框在条件表单里，
#: 命令行的开关由 CLI 自己附加 `next`（D283）——写死任一边就是在说另一个房间的话。
NONPOSITIVE_VALUATION = (
    "估值不为正（亏损或净资产为负）：估值条件默认只比正值，不计入命中"
)


@dataclass(frozen=True)
class FieldCounts:
    field: str
    op: str
    value: float | tuple[float, float]
    passed: int
    failed: int
    missing: int
    #: 因估值不为正被挡在命中之外的票数，已计入 `missing`（R-09）
    nonpositive: int = 0


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
    #: 结果的排列方式，页面原样念给用户听（D224）
    order_note: str = ""
    #: 这次结果用了「没有披露日、按报告期取」的财务口径时，把这件事说出来（D215）
    basis_note: str = ""
    #: （能取到截至快照日年报的票数, 票池），只在条件读到年报列时给：
    #: 同步只覆盖了一小撮票时，「命中 5 只」必须与这个数一起读（D227）
    financial_coverage: tuple[int, int] | None = None
    #: （技术面可算票数, 票池），只在条件读到技术面列时给（S4-A）
    technical_coverage: tuple[int, int] | None = None
    #: 估值字段只比正值（R-09）：关闭后负 PE/PB 也参与比较
    positive_only: bool = True
    #: ST/退市是否参与（R-09）；无论开关如何，剔除数都从 `st_excluded` 读
    include_st: bool = False
    st_excluded: int = 0


class ScreenerService:
    def __init__(
        self,
        snapshots: SnapshotRepo,
        symbols: SymbolListRepo,
        financials: FinancialRepo,
        technical: TechnicalService | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.symbols = symbols
        self.financials = financials
        self.technical = technical

    def run(
        self,
        market: str,
        conditions: list[Condition],
        *,
        day: date | None = None,
        limit: int | None = None,
        registry: tuple[Field, ...] = FIELDS,
        positive_only: bool = True,
        include_st: bool = False,
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
                positive_only=positive_only,
                include_st=include_st,
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
                positive_only=positive_only,
                include_st=include_st,
            )
        st_excluded = 0
        if market == "A" and not include_st:
            flagged = _st_mask(universe)
            st_excluded = int(flagged.sum())
            universe = universe.filter(~flagged)
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
                        f"票池里的 {st_excluded} 只全是 ST/退市："
                        "勾选「包含 ST/退市」可把它们放回来"
                    ),
                    positive_only=positive_only,
                    include_st=include_st,
                    st_excluded=st_excluded,
                )
        universe, covered = self._with_financials(
            market, universe, snapshot_day, wanted=set(specs.values())
        )
        technical_coverage: tuple[int, int] | None = None
        technical_reasons: dict[tuple[str, str], str] = {}
        if any(item.source == "technical" for item in specs.values()):
            if self.technical is None:
                raise ValidationError(
                    "技术面字段需要日线与公司行为覆盖：当前装配没有 TechnicalService，"
                    "这一列不能按快照算出来"
                )
            technical = self.technical.snapshot(
                market, universe["code"].to_list(), snapshot_day
            )
            universe = universe.join(technical.values, on="code", how="left")
            technical_reasons = technical.reasons
            technical_coverage = (technical.computable, universe.height)
        derived_roe = 0
        if "roe" in specs:
            universe, derived_roe = _fill_derived_roe(universe, market)
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
        if "peg" in specs:
            # PE 与年报净利同比都为正才有意义：负增速的 PEG 是一个看起来更便宜的负数
            universe = universe.with_columns(
                pl.when((pl.col("pe_ttm") > 0) & (pl.col("profit_yoy") > 0))
                .then(pl.col("pe_ttm") / (pl.col("profit_yoy") * 100))
                .otherwise(None)
                .cast(pl.Float64)
                .alias("peg")
            )
        counts: list[FieldCounts] = []
        masks: list[pl.Series] = []
        for cond in checked:
            spec = specs[cond.field]
            column = universe[spec.column]
            missing = column.is_null() | column.is_nan()
            nonpositive = 0
            if positive_only and spec.positive:
                skipped = (~missing & (column <= 0)).fill_null(False)
                nonpositive = int(skipped.sum())
                missing = missing | skipped
            passed = (_compare(column, cond) & ~missing).fill_null(False)
            counts.append(
                FieldCounts(
                    field=cond.field,
                    op=cond.op,
                    value=cond.value,
                    passed=int(passed.sum()),
                    failed=int((~passed & ~missing).sum()),
                    missing=int(missing.sum()),
                    nonpositive=nonpositive,
                )
            )
            masks.append(passed)
        hit_mask = masks[0]
        for mask in masks[1:]:
            hit_mask = hit_mask & mask
        sort_field = specs[checked[0].field]
        descending = sort_field.prefer == "high"
        ranked = universe.filter(hit_mask).sort(
            [sort_field.column, "code"],
            descending=[descending, False],
            nulls_last=True,
        )
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
        basis_notes: list[str] = []
        if market in UNDATED_FINANCIAL_MARKETS and any(
            item.source == "financials" or item.needs for item in specs.values()
        ):
            basis_notes.append(UNDATED_FINANCIAL_NOTES[market])
        if derived_roe:
            basis_notes.append(DERIVED_ROE_NOTE.format(count=derived_roe))
        if technical_coverage is not None:
            basis_notes.append(
                "技术面字段按快照日复权收盘现算，只覆盖已确认公司行为区间的 "
                f"{technical_coverage[0]}/{technical_coverage[1]} 只；其余记「无数」"
            )
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
            missing=_missing_groups(
                universe,
                checked,
                specs,
                hit_codes,
                kinds,
                market,
                positive_only,
                technical=technical_reasons,
            ),
            # 首批条件决定整个名单的顺序：质量/成长从高到低，估值/负债从低到高（D224）
            order_note=f"{sort_field.label} {'从高到低' if descending else '从低到高'}",
            basis_note="；".join(basis_notes),
            financial_coverage=(
                None if covered is None else (covered, universe.height)
            ),
            technical_coverage=technical_coverage,
            positive_only=positive_only,
            include_st=include_st,
            st_excluded=st_excluded,
        )

    def _with_financials(
        self,
        market: str,
        universe: pl.DataFrame,
        day: date,
        *,
        wanted: set[Field],
    ) -> tuple[pl.DataFrame, int | None]:
        """只看**这次选中的条件**用到哪些财务列：全市场扫描不该为快照列多付一条 join。

        快照帧里没有财务列，所以判据必须是 `Field.source == "financials"`，
        不是「帧里有没有这一列」——后者永远为假，join 永远不会发生。
        推导列（如 PEG）用 `needs` 声明自己依赖的财务列，否则它会在缺少分母的帧上
        算出一列 None，而页面只会说「无数」，看不出是 join 漏了（D222）。

        第二个返回值是这个票池里**取得到截至快照日年报**的票数；没 join 财务时是 None。
        """
        columns = sorted(
            {item.column for item in wanted if item.source == "financials"}
            | {column for item in wanted for column in item.needs}
        )
        if not columns:
            return universe, None
        loaded, _dropped = self.financials.load_as_of(
            market,
            universe["code"].to_list(),
            day,
            basis=ROE_BASIS_ANNUAL,
            include_undated=market in UNDATED_FINANCIAL_MARKETS,
        )
        by_code = {row["code"]: row for row in loaded.iter_rows(named=True)}
        joined = universe.with_columns(
            [
                pl.Series(
                    column,
                    [by_code.get(code, {}).get(column) for code in universe["code"]],
                    dtype=pl.Float64,
                )
                for column in columns
            ]
        )
        return joined, len(by_code)


def _fill_derived_roe(universe: pl.DataFrame, market: str) -> tuple[pl.DataFrame, int]:
    """源没给 ROE 时，用同一期年报的 `ROA ÷ (1−资产负债率)` 估一个（D228）。

    三条守卫写在逐行循环里，各有理由：

    * **源值优先**：估算是缺口兜底，不是对源值的第二次解释；
    * **资产负债率必须 < 100%**：否则分母 ≤ 0，公式给出的负数会让「高杠杆」看起来
      像「股东亏钱」；
    * **结果过同一个口径闸**（`out_of_caliber`）：在这里重写 `abs(x) >= 1` 会让美股
      100%+ 的真实 ROE 在兜底这条路上被静默置空——闸的数值只有一个出处（D66/D228）。

    返回的第二个数是这次**真的估出来**的行数：它要进 `basis_note`，否则读者分不清
    屏幕上的「年报 ROE」哪一个是源值、哪一个是推算值。
    """
    values: list[float | None] = []
    derived = 0
    for roe, roa, debt in universe.select("roe", "roa", "debt_ratio").iter_rows():
        if roe is not None:
            values.append(roe)
            continue
        candidate = None
        if roa is not None and debt is not None and debt < 1.0:
            candidate = roa / (1.0 - debt)
        if candidate is not None and out_of_caliber("roe", candidate, market=market):
            candidate = None
        if candidate is not None:
            derived += 1
        values.append(candidate)
    return universe.with_columns(pl.Series("roe", values, dtype=pl.Float64)), derived


def _st_mask(universe: pl.DataFrame) -> pl.Series:
    """A 股没有独立的 ST 标记：「名字里有 ST」是交易所口径，退市整理期看「退」。"""
    name = universe["name"].fill_null("")
    return (
        universe["is_st"].fill_null(False)
        | name.str.contains("ST")
        | name.str.contains("退")
    ).fill_null(False)


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


def _annual_missing_reason(spec: Field, market: str) -> str:
    """「没有这一列」的下一步按市场的披露规则说（D215/D228）。

    A 股那一行确实要用披露日挡，说「已披露」是实话；港股/美股没有披露日可核，
    同一句话在那里就成了另一句无法验证的话。
    """
    if market in UNDATED_FINANCIAL_MARKETS:
        return (
            f"没有截至快照日能取到的年报数据（{spec.label}）："
            f"`sams sync financials --market {market} --code <代码>`"
        )
    return (
        f"没有已披露年报数据（{spec.label}）："
        f"`sams sync financials --market {market} --code <代码>`"
    )


def _missing_groups(
    universe: pl.DataFrame,
    checked: tuple[Condition, ...],
    specs: dict[str, Field],
    hit_codes: set[str],
    kinds: dict[str, str],
    market: str,
    positive_only: bool,
    *,
    technical: dict[tuple[str, str], str] | None = None,
) -> tuple[MissingGroup, ...]:
    """一只票只记**第一条**让它出局的条件：同一个缺口记两次会让「无数 N 只」重复计人。

    pr_now 的组键用 `kind`（亏损 / 净资产为负 / …），不用逐票的 reason：后者带着
    这只票自己的 PE 或 PB，真实快照上 1459 只无数票会散成 1394 组。

    估值字段的负值也在这一组里点名（R-09）：它们没有参与比较，但必须和「真的无数」
    一样有名字，不能因为默认过滤而消失。
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in universe.iter_rows(named=True):
        if row["code"] in hit_codes:
            continue
        for cond in checked:
            spec = specs[cond.field]
            value = row[spec.column]
            if value is None or (isinstance(value, float) and isnan(value)):
                if cond.field == "pr_now":
                    reason = kinds.get(row["code"], "现值 PR 没有数")
                elif spec.source == "technical":
                    # 原因由 TechnicalService 逐字段给：覆盖、跳变、日线根数不是同一件事（S4-A）
                    reason = (technical or {}).get(
                        (cond.field, row["code"]), "技术面读数没有数"
                    )
                elif spec.missing_note:
                    reason = spec.missing_note
                elif spec.source == "financials":
                    # 补数命令按代码跑，但理由只说一次：逐票把代码拼进去，几千只票就是几千组
                    reason = _annual_missing_reason(spec, market)
                    if cond.field == "roe" and (
                        row.get("roa") is not None or row.get("debt_ratio") is not None
                    ):
                        # 有年报、但兜底公式的输入不齐或结果不可信：这时叫卖「去同步」
                        # 是错的下一步——那一期已经在库里，缺的是能用的 ROE
                        reason = ROE_DERIVATION_FAILED
                else:
                    reason = "快照这一行缺这一列"
                grouped.setdefault((cond.field, reason), []).append(row["code"])
                break
            if positive_only and spec.positive and value <= 0:
                grouped.setdefault((cond.field, NONPOSITIVE_VALUATION), []).append(
                    row["code"]
                )
                break
    return tuple(
        MissingGroup(field=name, reason=reason, codes=tuple(sorted(codes)))
        for (name, reason), codes in sorted(grouped.items())
    )


def preset_strategy_id(preset_id: str, conditions) -> str:
    """方案重跑写进 `scan_results` 的键：方案 id + 条件哈希（D183/D257）。

    名字不在键里：改名不该抹掉「上次跑」，而名字曾在键里，两个用户的同名方案
    会互相覆盖。条件一改就是另一句话，旧结果仍按旧哈希查得到。
    """
    payload = json.dumps(
        [[item.field, item.op, item.value] for item in conditions],
        ensure_ascii=False,
        default=list,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"preset:{preset_id}:{digest}"
