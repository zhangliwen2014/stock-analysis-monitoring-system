"""同业对比 v0：同一层行业里摆 3–5 家、三个中位、一句代理地位（S2 / D236–D241）。

这一层不新抓任何数据：行业来自 `company_profiles` / `symbols.sector`，市值来自
`snapshot_daily`，倍数来自 `valuation_daily`，ROE 与营收来自 `financials` 的 PIT 读法。
它只决定「谁算同业、哪个数进中位、缺的那一格属于哪一档」，四个容易装作没发生的坑：

* **市值只有一个单位**。`snapshot_daily.market_cap` 是亿元，`valuation_daily.market_cap`
  是元，两者只差 1e8（D239）。混用不会报错，只会让选同业静默错位——所以这一层只读快照，
  快照过期就当没有市值，绝不拿估值行顶上。
* **中位不含自己**。自己摆第一行是为了对照，不是为了让中位多一个样本；同业不足 3 家时
  中位拒绝，而不是「2 家也算一个行业」。
* **放宽层级要出声**。三级只有 2 家时用二级，并且把那句话写在 notes 里——读的人要能
  看出这一屏的中位是更大一层行业的数。
* **行业地位是代理**。营收规模排名不是市占率；每一步拒绝都带 `MISSING_*`，
  屏幕上才分得清「不适用」「未披露」「无法计算」「未覆盖」（D241）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.fundamentals import ROE_BASIS_ANNUAL
from sams.core.industry import IndustryPath, normalize_industry, parse_industry
from sams.core.missing import (
    NOT_APPLICABLE,
    NOT_COVERED,
    NOT_DISCLOSED,
    UNCOMPUTABLE,
)
from sams.storage.repos.f10 import CompanyProfileRepo
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbols import SymbolRepo

#: 选行业层级与出中位的地板：不含自己。少于它时中位拒绝，而不是拿两家假装「行业中位」
MIN_PEERS = 3
#: 一屏最多摆几家同业（不含自己）
MAX_PEERS = 5
#: 中位数要的有效同业数
MIN_MEDIAN_SAMPLES = 3
#: 行业地位代理要的行业成员数（含自己）
MIN_POSITION_SAMPLES = 5
#: 估值行超过这么多天不算当期
PEER_STALE_DAYS = 30
#: 快照超过这么多天，市值与 ST 标记都不再当当期
SNAPSHOT_STALE_DAYS = 7

_LEVEL_LABELS = {3: "三级", 2: "二级", 1: "一级"}

_NOT_A_REASON = "同业对比 v0 只覆盖 A 股；港股/美股的行业分类与一致预期需要付费源"
_NOT_A_NOTE = "同业对比 v0 只覆盖 A 股；港股/美股的行业分类与一致预期需要付费源"


@dataclass(frozen=True)
class PeerRow:
    """表里的一行。`*_missing` 非空时，那一格的值一定是 None——没有「有数而说不出出处」的格。"""

    market: str
    code: str
    name: str
    is_self: bool
    #: 亿元，只来自 `snapshot_daily`（D239）
    market_cap: float | None
    pe_ttm: float | None
    pb: float | None
    #: 小数口径（D58）
    roe: float | None
    #: 元，最近年报 PIT 行
    revenue: float | None
    valuation_on: date | None
    roe_period: date | None
    revenue_period: date | None
    pe_missing: str = ""
    pb_missing: str = ""
    roe_missing: str = ""
    #: 行级说明：估值行超过 30 天等
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PeerMetric:
    """一个中位。`value` 为 None 时 `missing_kind` 与 `reason` 一定非空。"""

    value: float | None
    samples: int
    as_of: date
    missing_kind: str = ""
    reason: str = ""
    #: 有行但那个值不能计入中位的家数（PE≤0、PB≤0）
    excluded: int = 0


@dataclass(frozen=True)
class PeerComparison:
    market: str
    code: str
    #: 这一卡的**计算时间**，不是任何一个数的数据日（D288）：卡上不拿它替来源说话
    as_of: date
    #: 归一后的行业路径
    industry: str
    #: "三级" / "二级" / "一级"；未覆盖/未披露时为空
    level_used: str
    #: 自己在前，同业按市值降序
    rows: tuple[PeerRow, ...]
    pe: PeerMetric
    pb: PeerMetric
    roe: PeerMetric
    #: 行业地位代理那句话；拒绝时说明是哪一档拒绝
    position: str
    position_kind: str = ""
    position_missing: str = ""
    notes: tuple[str, ...] = ()
    #: ok / thin / not_covered / not_disclosed
    state: str = "ok"
    #: 市值那一列来自哪一天的快照；快照不新鲜时为 `None`，与「市值不出数」同一条判断
    snapshot_day: date | None = None
    #: 本卡各行 PE/PB 的估值日（去重、降序）：中位是几天并起来的，只有这里能看出来
    valuation_days: tuple[date, ...] = ()
    #: 本卡各行 ROE/营收所属的年报期（去重、降序）
    financial_periods: tuple[date, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    code: str
    name: str
    path: IndustryPath


class PeerService:
    def __init__(
        self,
        profiles: CompanyProfileRepo,
        symbols: SymbolRepo,
        valuations: ValuationRepo,
        financials: FinancialRepo,
        snapshots: SnapshotRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.profiles = profiles
        self.symbols = symbols
        self.valuations = valuations
        self.financials = financials
        self.snapshots = snapshots
        self.clock = clock

    def compare(
        self, market: str, code: str, *, as_of: date | None = None
    ) -> PeerComparison:
        """一次同业对比。港美直接 `not_covered`：不查任何表，也不摆一张空表。"""
        market = canonical_market(market)
        code = canonical_code(market, code)
        until = as_of if as_of is not None else self.clock.today()
        if market != "A":
            return _not_covered(market, code, until)

        profiles = {
            row["code"]: row for row in self.profiles.list_market("A").to_dicts()
        }
        symbols = {row["code"]: row for row in self.symbols.list_market("A").to_dicts()}

        self_industry = _industry_of(profiles.get(code), symbols.get(code))
        self_path = parse_industry(self_industry)
        if self_path is None:
            # 没有行业就没有「同一层」，也就没有可比的对象：先补概况
            return _not_disclosed(market, code, until)

        snapshot_day = self.snapshots.latest_until("A", until)
        snapshot_fresh = (
            snapshot_day is not None
            and (until - snapshot_day).days <= SNAPSHOT_STALE_DAYS
        )
        snap = (
            {
                canonical_code("A", row["code"]): row
                for row in self.snapshots.load_day("A", snapshot_day).to_dicts()
            }
            if snapshot_fresh and snapshot_day is not None
            else {}
        )
        caps = {key: row.get("market_cap") for key, row in snap.items()}

        candidates: list[_Candidate] = []
        st_skipped = no_industry = 0
        for peer_code, profile in profiles.items():
            if peer_code == code:
                continue
            symbol = symbols.get(peer_code)
            if symbol is not None and symbol.get("list_status") not in (None, "L"):
                continue
            name = profile.get("name") or (symbol or {}).get("name") or peer_code
            if _looks_st(name) or bool(snap.get(peer_code, {}).get("is_st")):
                st_skipped += 1
                continue
            path = parse_industry(_industry_of(profile, symbol))
            if path is None:
                no_industry += 1
                continue
            candidates.append(_Candidate(peer_code, name, path))

        level, level_key, level_count, widened = _pick_level(self_path, candidates)
        members = [
            candidate
            for candidate in candidates
            if candidate.path.key(level) == level_key
        ]
        deepest, deepest_count = (
            widened if widened is not None else (level, level_count)
        )

        all_codes = [code] + [member.code for member in members]
        val = self.valuations.latest(
            "A", all_codes, until, within_days=PEER_STALE_DAYS
        )
        val_map = {row["code"]: row for row in val.to_dicts()}
        fin, dropped = self.financials.load_as_of(
            "A", all_codes, until, basis=ROE_BASIS_ANNUAL
        )
        fin_map = {row["code"]: row for row in fin.to_dicts()}

        fresh = [member for member in members if member.code in val_map]
        stale = len(members) - len(fresh)
        selected = _select_peers(
            fresh,
            self_code=code,
            self_cap=caps.get(code) if snapshot_fresh else None,
            self_revenue=_revenue(fin_map.get(code)),
            caps=caps if snapshot_fresh else {},
            fin=fin_map,
        )
        fallback = sum(
            1
            for peer in selected
            if (peer.code not in caps) and _revenue(fin_map.get(peer.code)) is None
        )

        rows: list[PeerRow] = [
            _row(
                code,
                name=_name_of(code, profiles, symbols),
                is_self=True,
                val=val_map.get(code),
                fin=fin_map.get(code),
                cap=caps.get(code) if snapshot_fresh else None,
            )
        ]
        peers = [
            _row(
                peer.code,
                name=peer.name,
                is_self=False,
                val=val_map.get(peer.code),
                fin=fin_map.get(peer.code),
                cap=caps.get(peer.code) if snapshot_fresh else None,
            )
            for peer in selected
        ]
        peers.sort(key=lambda row: (row.market_cap is None, -(row.market_cap or 0.0), row.code))
        rows.extend(peers)

        pe = _metric(
            [val_map[peer.code].get("pe_ttm") for peer in selected],
            until,
            digits=2,
        )
        pb = _metric(
            [val_map[peer.code].get("pb") for peer in selected],
            until,
            digits=2,
        )
        roe = _metric(
            [_roe(fin_map.get(peer.code)) for peer in selected],
            until,
            digits=4,
            exclude_non_positive=False,
        )

        position, position_kind, position_missing = _position(
            self_code=code,
            level=level,
            level_key=level_key,
            members=members,
            fin=fin_map,
            snap=caps if snapshot_fresh else {},
            snapshot_day=snapshot_day if snapshot_fresh else None,
        )

        notes: list[str] = []
        if widened is not None:
            notes.append(
                f"{_LEVEL_LABELS[deepest]}行业只有 {deepest_count} 家，"
                f"已放宽到{_LEVEL_LABELS[level]}「{level_key}」"
            )
        notes.extend(
            _exclusion_notes(
                st=st_skipped,
                stale=stale,
                no_industry=no_industry,
                pe_excluded=pe.excluded,
                pb_excluded=pb.excluded,
            )
        )
        if dropped:
            notes.append(f"财务行里 {dropped} 期因为没有发布日而全程未被采信")
        if not snapshot_fresh:
            seen = snapshot_day if snapshot_day is not None else "无"
            notes.append(
                f"最新快照 {seen} 距截至日超过 {SNAPSHOT_STALE_DAYS} 天或缺失："
                "市值一列不出数（先跑 `sams sync spot`）"
            )
        if fallback:
            notes.append(f"{fallback} 家同业缺市值与营收，选同业时按市值降序兜底")

        state = "thin" if len(selected) < MIN_PEERS else "ok"
        return PeerComparison(
            market=market,
            code=code,
            as_of=until,
            snapshot_day=snapshot_day if snapshot_fresh else None,
            valuation_days=_distinct_day(row.valuation_on for row in rows),
            financial_periods=_distinct_day(row.roe_period for row in rows),
            industry=self_path.path,
            level_used=_LEVEL_LABELS[level],
            rows=tuple(rows),
            pe=pe,
            pb=pb,
            roe=roe,
            position=position,
            position_kind=position_kind,
            position_missing=position_missing,
            notes=tuple(notes),
            state=state,
        )


# --- 选层级与选同业 -----------------------------------------------------------------------


def _pick_level(
    self_path: IndustryPath, candidates: Sequence[_Candidate]
) -> tuple[int, str, int, tuple[int, int] | None]:
    """从三级往一级找第一个「不含自己 ≥ MIN_PEERS 家」的层级。

    返回 (层级, 层级键, 该层家数, 放宽来源)；放宽来源 `(更深的层级, 其家数)` 只在这一层
    比最深一层更宽时非空——「放宽了」不能是读者自己猜出来的。一个都不够时返回家数最多的
    那一层（同样多取更具体的），并在放宽来源里带上最深一层，说明为什么没停在那一层。
    """
    deepest = len(self_path.levels)
    keys = {n: self_path.key(n) for n in range(deepest, 0, -1)}
    counts = {
        n: sum(1 for candidate in candidates if candidate.path.key(n) == keys[n])
        for n in keys
    }
    for n in range(deepest, 0, -1):
        if counts[n] >= MIN_PEERS:
            widened = None if n == deepest else (deepest, counts[deepest])
            return n, keys[n], counts[n], widened
    # 都不够：用家数最多的那一层；并列取更具体的
    level = max(counts, key=lambda n: (counts[n], n))
    widened = None if level == deepest else (deepest, counts[deepest])
    return level, keys[level], counts[level], widened


def _select_peers(
    peers: Sequence[_Candidate],
    *,
    self_code: str,
    self_cap: float | None,
    self_revenue: float | None,
    caps: dict[str, float | None],
    fin: dict[str, dict],
) -> list[_Candidate]:
    """按「与自己的距离」选 3–5 家：有市值比市值，没市值比营收，都没有按市值降序兜底。"""

    def distance(peer: _Candidate) -> tuple[int, float]:
        cap = caps.get(peer.code)
        revenue = _revenue(fin.get(peer.code))
        if self_cap and cap and self_cap > 0 and cap > 0:
            return 0, abs(math.log(cap / self_cap))
        if self_revenue and revenue and self_revenue > 0 and revenue > 0:
            return 1, abs(math.log(revenue / self_revenue))
        cap_sort = -(cap if cap is not None else 0.0)
        return 2, cap_sort

    return sorted(peers, key=distance)[:MAX_PEERS]


# --- 中位与行业地位 -----------------------------------------------------------------------


def _metric(
    values: Sequence[float | None],
    as_of: date,
    *,
    digits: int,
    exclude_non_positive: bool = True,
) -> PeerMetric:
    """一个中位：只算同业、每项独立；不足地板时拒绝，`excluded` 记有值但不计入的家数。"""
    usable: list[float] = []
    excluded = 0
    for value in values:
        if value is None:
            continue
        if exclude_non_positive and value <= 0:
            excluded += 1
            continue
        usable.append(value)
    samples = len(usable)
    if samples < MIN_MEDIAN_SAMPLES:
        return PeerMetric(
            None,
            samples,
            as_of,
            missing_kind=UNCOMPUTABLE,
            reason=f"样本不足（n={samples} < {MIN_MEDIAN_SAMPLES}）",
            excluded=excluded,
        )
    return PeerMetric(round(_median(usable), digits), samples, as_of, excluded=excluded)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _position(
    *,
    self_code: str,
    level: int,
    level_key: str,
    members: Sequence[_Candidate],
    fin: dict[str, dict],
    snap: dict[str, float | None],
    snapshot_day: date | None,
) -> tuple[str, str, str]:
    """行业地位代理：先看营收，缺了就退到市值；两者都没有 / 样本不足时明确拒绝。"""
    label = _LEVEL_LABELS[level]
    self_revenue = _revenue(fin.get(self_code))
    self_period = _report_period(fin.get(self_code))
    if self_revenue is not None:
        values = [self_revenue] + [
            revenue
            for revenue in (_revenue(fin.get(member.code)) for member in members)
            if revenue is not None
        ]
        if len(values) < MIN_POSITION_SAMPLES:
            return (
                f"行业样本不足（n={len(values)} < {MIN_POSITION_SAMPLES}）：营收分位不出数",
                "revenue",
                UNCOMPUTABLE,
            )
        rank = 1 + sum(1 for value in values[1:] if value > self_revenue)
        return (
            f"营收规模位于{label}行业「{level_key}」{len(values)} 家中的第 {rank} 位"
            f"（超过 {_share(rank, len(values))}% 的同行）；"
            f"口径：最近年报（{self_period}）；代理指标，不是市占率",
            "revenue",
            "",
        )

    self_cap = snap.get(self_code)
    if self_cap is not None and snapshot_day is not None:
        values = [self_cap] + [
            cap
            for cap in (snap.get(member.code) for member in members)
            if cap is not None
        ]
        if len(values) < MIN_POSITION_SAMPLES:
            return (
                f"行业样本不足（n={len(values)} < {MIN_POSITION_SAMPLES}）：市值分位不出数",
                "market_cap",
                UNCOMPUTABLE,
            )
        rank = 1 + sum(1 for value in values[1:] if value > self_cap)
        return (
            f"市值位于{label}行业「{level_key}」{len(values)} 家中的第 {rank} 位"
            f"（超过 {_share(rank, len(values))}% 的同行）；"
            f"口径：{snapshot_day} 的快照市值（营收缺失）；代理指标，不是市占率",
            "market_cap",
            "",
        )

    return (
        "这只票的营收与市值都没有：行业地位无法计算"
        "（先 `sams sync financials` / `sams sync spot`）",
        "",
        NOT_DISCLOSED,
    )


def _share(rank: int, total: int) -> int:
    return round((total - rank) * 100 / total)


# --- 行与说明 -----------------------------------------------------------------------------


def _distinct_day(days: Sequence[date | None]) -> tuple[date, ...]:
    """去重并按新到旧排：卡头那句「估值 11-03 ~ 11-02」用的就是这一份。"""
    return tuple(sorted({day for day in days if day is not None}, reverse=True))


def _row(
    code: str,
    *,
    name: str,
    is_self: bool,
    val: dict | None,
    fin: dict | None,
    cap: float | None,
) -> PeerRow:
    pe = val.get("pe_ttm") if val else None
    pb = val.get("pb") if val else None
    pe_missing = _multiples_missing(pe)
    pb_missing = _multiples_missing(pb)
    roe = _roe(fin)
    flags = () if val else ("估值行超过 30 天或缺失：PE/PB 不出数",)
    return PeerRow(
        market="A",
        code=code,
        name=name,
        is_self=is_self,
        market_cap=cap,
        pe_ttm=pe if pe_missing == "" else None,
        pb=pb if pb_missing == "" else None,
        roe=roe,
        revenue=_revenue(fin),
        valuation_on=val.get("date") if val else None,
        roe_period=_report_period(fin),
        revenue_period=_report_period(fin),
        pe_missing=pe_missing,
        pb_missing=pb_missing,
        roe_missing="" if roe is not None else NOT_DISCLOSED,
        flags=flags,
    )


def _multiples_missing(value: float | None) -> str:
    if value is None:
        return NOT_DISCLOSED
    if value <= 0:
        return NOT_APPLICABLE
    return ""


def _roe(fin: dict | None) -> float | None:
    return fin.get("roe") if fin else None


def _revenue(fin: dict | None) -> float | None:
    return fin.get("revenue") if fin else None


def _report_period(fin: dict | None) -> date | None:
    return fin.get("report_date") if fin else None


def _industry_of(profile: dict | None, symbol: dict | None) -> str | None:
    """行业：概况优先，主档 `sector` 兜底（D236；不回填时也能工作）。"""
    for source in (profile, symbol):
        if not source:
            continue
        raw = source.get("industry") if source is profile else source.get("sector")
        normalized = normalize_industry(raw)
        if normalized:
            return normalized
    return None


def _looks_st(name: str) -> bool:
    return "ST" in name.upper() or "退" in name


def _name_of(
    code: str,
    profiles: dict[str, dict],
    symbols: dict[str, dict],
) -> str:
    profile = profiles.get(code)
    if profile and profile.get("name"):
        return profile["name"]
    symbol = symbols.get(code)
    if symbol and symbol.get("name"):
        return symbol["name"]
    return code


def _exclusion_notes(
    *,
    st: int,
    stale: int,
    no_industry: int,
    pe_excluded: int,
    pb_excluded: int,
) -> list[str]:
    parts: list[str] = []
    if st:
        parts.append(f"ST {st} 家")
    if stale:
        parts.append(f"估值行超过 {PEER_STALE_DAYS} 天或缺失 {stale} 家（已排除出同业）")
    if no_industry:
        parts.append(f"无行业 {no_industry} 家")
    if pe_excluded:
        parts.append(f"PE 不为正 {pe_excluded} 家（未计入中位）")
    if pb_excluded:
        parts.append(f"PB 不为正 {pb_excluded} 家（未计入中位）")
    return ["排除：" + "；".join(parts)] if parts else []


# --- 不查表的两条 -------------------------------------------------------------------------


def _empty_metric(as_of: date, kind: str, reason: str) -> PeerMetric:
    return PeerMetric(None, 0, as_of, missing_kind=kind, reason=reason)


def _not_covered(market: str, code: str, as_of: date) -> PeerComparison:
    return PeerComparison(
        market=market,
        code=code,
        as_of=as_of,
        industry="",
        level_used="",
        rows=(),
        pe=_empty_metric(as_of, NOT_COVERED, _NOT_A_REASON),
        pb=_empty_metric(as_of, NOT_COVERED, _NOT_A_REASON),
        roe=_empty_metric(as_of, NOT_COVERED, _NOT_A_REASON),
        position="",
        position_missing=NOT_COVERED,
        notes=(_NOT_A_NOTE,),
        state="not_covered",
    )


def _not_disclosed(market: str, code: str, as_of: date) -> PeerComparison:
    reason = "这只票没有行业（公司概况与主档都是空的）：先同步公司概况（`sams sync profile`）"
    return PeerComparison(
        market=market,
        code=code,
        as_of=as_of,
        industry="",
        level_used="",
        rows=(),
        pe=_empty_metric(as_of, NOT_DISCLOSED, reason),
        pb=_empty_metric(as_of, NOT_DISCLOSED, reason),
        roe=_empty_metric(as_of, NOT_DISCLOSED, reason),
        position=reason,
        position_missing=NOT_DISCLOSED,
        notes=(reason,),
        state="not_disclosed",
    )
