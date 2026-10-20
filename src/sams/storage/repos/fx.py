"""`fx_rates` 的读写（spec §5.4 规则 2/3 的数据侧）。

存的时候只存一个方向：`(base, quote)` 表示 1 单位 base = rate 单位 quote。
读的时候**不取倒数**——方向写反的症状是「大约对」，那是最难查的错。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite

from sams.core.errors import ValidationError
from sams.core.funds import FX_STALE_DAYS
from sams.storage.db import Database


@dataclass(frozen=True)
class FxRate:
    day: date
    base: str
    quote: str
    rate: float
    source: str | None
    is_stale: bool
    #: `latest` 才填：距今几天。当天为 0。
    age_days: int = 0


@dataclass(frozen=True)
class FxPair:
    """一个币种对的最后一天：健康页按对报，不按全库 `max(date)` 报（见 `freshness`）。"""

    base: str
    quote: str
    latest: date
    age_days: int
    source: str | None


class FxRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def freshness(self, today: date) -> tuple[FxPair, ...]:
        """每个存在过的币种对各报自己的最后一天与距今几天。

        全库一个 `max(date)` 会说「汇率很新」：USD→CNY 昨天同步过、HKD→CNY 三周没动时，
        港币那笔持仓的换汇影响照样是假数，而那一屏看不出被谁背了书。
        「距今几天」按 `today` 算而不是存下来：陈旧是读出来的，写下的那天它不陈旧。
        """
        rows = self.db.query(
            "SELECT base_ccy, quote_ccy, date, source FROM ("
            "  SELECT base_ccy, quote_ccy, date, source,"
            "         rank() OVER (PARTITION BY base_ccy, quote_ccy ORDER BY date DESC) AS newest"
            "  FROM fx_rates) WHERE newest = 1 ORDER BY base_ccy, quote_ccy",
            [],
        )
        return tuple(
            FxPair(
                base=row["base_ccy"],
                quote=row["quote_ccy"],
                latest=row["date"],
                age_days=(today - row["date"]).days,
                source=row["source"],
            )
            for row in rows.to_dicts()
        )

    def put(
        self,
        day: date,
        base: str,
        quote: str,
        rate: float,
        *,
        source: str | None = None,
        is_stale: bool = False,
    ) -> None:
        if not isfinite(rate) or rate <= 0:
            raise ValidationError(f"汇率必须是正有限小数，收到 {rate!r}")
        if base == quote:
            raise ValidationError(f"{base} 对 {quote} 的汇率不需要存：同币种恒为 1")
        self.db.execute(
            "INSERT OR REPLACE INTO fx_rates"
            " (date, base_ccy, quote_ccy, rate, source, is_stale) VALUES (?, ?, ?, ?, ?, ?)",
            [day, base, quote, rate, source, is_stale],
        )

    def get(self, day: date, base: str, quote: str) -> FxRate | None:
        if base == quote:
            return FxRate(day, base, quote, 1.0, "same", False, 0)
        rows = self.db.query(
            "SELECT * FROM fx_rates WHERE date = ? AND base_ccy = ? AND quote_ccy = ?",
            [day, base, quote],
        )
        return None if rows.is_empty() else _row(rows.to_dicts()[0], 0)

    def latest(
        self, day: date, base: str, quote: str, *, max_age_days: int | None = None
    ) -> FxRate | None:
        """`day` 或之前最近的一条；超过 `max_age_days` 就不给（宁可没有，不要过期）。

        默认 `FX_STALE_DAYS`：周末用周五的汇率是合法的（标的不涨也不跌），
        长假第五天还在用周五那个数是假数。放宽要由调用方明说，不由这里默认放行。
        """
        if base == quote:
            return FxRate(day, base, quote, 1.0, "same", False, 0)
        limit = FX_STALE_DAYS if max_age_days is None else max_age_days
        rows = self.db.query(
            "SELECT * FROM fx_rates WHERE base_ccy = ? AND quote_ccy = ? AND date <= ?"
            " ORDER BY date DESC LIMIT 1",
            [base, quote, day],
        )
        if rows.is_empty():
            return None
        row = rows.to_dicts()[0]
        age = (day - row["date"]).days
        if age > limit:
            return None
        return _row(row, age)


def _row(row: dict, age_days: int) -> FxRate:
    return FxRate(
        day=row["date"],
        base=row["base_ccy"],
        quote=row["quote_ccy"],
        rate=float(row["rate"]),
        source=row["source"],
        #: 陈旧是算出来的，不是存进去的：存进去的那一刻它不陈旧，第二天就陈旧了
        is_stale=bool(row["is_stale"]) or age_days > 0,
        age_days=age_days,
    )
