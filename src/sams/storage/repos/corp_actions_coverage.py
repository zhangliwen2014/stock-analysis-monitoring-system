"""公司行为覆盖区间仓库（spec D36 / 迁移 0009）。

这张表存在的理由只有一条：`corp_actions` 里 0 行同时意味着「这只票没分过红」和
「没人查过这只票」，而区间收益对这两者的处理完全相反——前者可以照实用裸价算，
后者必须拒绝。没有「谁在什么时候确认过多大区间」这个事实，读取层的守卫只能在
永远放行和永远拒绝之间二选一。

一个 (market, code, src) 只保存**一段连续的**已确认区间：两段互不相干的窗口拼起来
像一个更大的窗口，而中间那段其实没人看过。宁可把区间缩窄并 warn，也不虚报覆盖面。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import ValidationError
from sams.storage.db import Database

logger = logging.getLogger("sams.storage.corp_actions")

#: 相邻到几天以内视为同一段连续区间？除权日是离散的，两周的空档足以漏掉一次年度分红，
#: 而周与月之间的周末本就该被吞掉——取 7 天，既不把周末当成缺口，也不把半个月当成连着。
ADJOIN_WINDOW_DAYS = 7


class CoverageRecord:
    def __init__(
        self,
        market: str,
        code: str,
        covered_from: date,
        covered_to: date,
        full_history: bool,
        src: str,
        fetched_at: datetime | None = None,
    ) -> None:
        self.market = market
        self.code = code
        self.covered_from = covered_from
        self.covered_to = covered_to
        self.full_history = full_history
        self.src = src
        self.fetched_at = fetched_at


def _norm(market: str, code: str) -> tuple[str, str]:
    """和 watchlists 同一个理由：键的形态必须和 bars 一致，否则查不到又不报错。

    确认记录的小写市场名比行情的小写键更坏：读取层按 `A` 查不到任何确认，
    于是那条「这个源的区间已被完整确认过」的事实永远放行不了任何区间（D46 的下沉）。
    """
    if not market or not code:
        raise ValidationError("market 与 code 不能为空")
    try:
        cleaned = canonical_market(market)
        return cleaned, canonical_code(cleaned, code)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


class CorpActionsCoverageRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        market: str,
        code: str,
        *,
        covered_from: date,
        covered_to: date,
        full_history: bool,
        src: str,
    ) -> None:
        """写入一次确认。已有区间时：相接则合并成一段，断开则缩窄为新的一段。"""
        market, code = _norm(market, code)
        if not src:
            raise ValidationError("src 不能为空：覆盖是谁确认的，必须留下名字")
        if covered_from > covered_to:
            raise ValidationError(f"覆盖区间反了：{covered_from} > {covered_to}")
        existing = self.get(market, code, src=src)
        if existing is not None and _touches(existing, covered_from, covered_to):
            covered_from = min(covered_from, existing.covered_from)
            covered_to = max(covered_to, existing.covered_to)
            # 「拿到全历史」是采集者说过的话，说过就不该因为后来拉了个小区间被忘掉
            full_history = full_history or existing.full_history
        elif existing is not None:
            logger.warning(
                "%s/%s 的 %s 覆盖区间 [%s, %s] 与新窗口 [%s, %s] 不相连，缩窄为新区间",
                market,
                code,
                src,
                existing.covered_from,
                existing.covered_to,
                covered_from,
                covered_to,
            )
        self.db.execute(
            """
            INSERT OR REPLACE INTO corp_actions_coverage
                (market, code, src, covered_from, covered_to, full_history, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, now())
            """,
            [market, code, src, covered_from, covered_to, full_history],
        )

    def get(self, market: str, code: str, *, src: str) -> CoverageRecord | None:
        market, code = _norm(market, code)
        rows = self.db.query(
            "SELECT * FROM corp_actions_coverage WHERE market = ? AND code = ? AND src = ?",
            [market, code, src],
        ).to_dicts()
        if not rows:
            return None
        row = rows[0]
        return CoverageRecord(
            market=row["market"],
            code=row["code"],
            covered_from=_as_date(row["covered_from"]),
            covered_to=_as_date(row["covered_to"]),
            full_history=bool(row["full_history"]),
            src=row["src"],
            fetched_at=row["fetched_at"],
        )

    def for_code(self, market: str, code: str) -> list[CoverageRecord]:
        """这只票的所有确认记录（各源一条）。判定归 analysis 层，这里只给事实。"""
        market, code = _norm(market, code)
        return [
            CoverageRecord(
                market=row["market"],
                code=row["code"],
                covered_from=_as_date(row["covered_from"]),
                covered_to=_as_date(row["covered_to"]),
                full_history=bool(row["full_history"]),
                src=row["src"],
                fetched_at=row["fetched_at"],
            )
            for row in self.db.query(
                "SELECT * FROM corp_actions_coverage WHERE market = ? AND code = ? "
                "ORDER BY src",
                [market, code],
            ).to_dicts()
        ]

    def count(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM corp_actions_coverage") or 0)


def _touches(existing: CoverageRecord, start: date, end: date) -> bool:
    """新窗口是否与已确认区间相接或重叠。"""
    slack = timedelta(days=ADJOIN_WINDOW_DAYS)
    return start - existing.covered_to <= slack and existing.covered_from - end <= slack


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value
