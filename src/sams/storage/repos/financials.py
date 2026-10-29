"""基本面、按日估值、资金流三张表的写入与读取。

这一层管三件别人管不了的事：

1. **键形态**（D50）。`market`/`code` 在入口归一，与 `bars_daily` 同一条规则。否则
   同一只票在 `financials` 里是 `00700`、在 `valuation_daily` 里是 `700`，市赚率拿
   「今天的 PE」去配「最近一期的 ROE」时配不上——症状是「这只票算不出 PR」，
   而真相是两次采集用了两种写法。

2. **PIT**（D61）。`load_as_of` 只认 `published_date <= as_of` 的行，**披露日为 NULL
   的一律不采信**并单独计数交回调用方。把 NULL 当「早就披露了」等于往回开时间机器：
   一个 3 月就「知道」4 月末才公开的 ROE 的选股结果，回测起来好看得没有意义。

3. **单位**（D58）。比率列越出小数口径的可能范围时置空那一列、留下那一行，并 warn。
   宁可 PR 拒绝出数，也不要一个短两个数量级的「便宜」。

`financials.pe_ttm/pb` 这两列本层不写（D60）：按日的倍数走 `valuation_daily`，
把某一天的 PE 写进某一期的行里，就是拿一个日期冒充一个期间。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from uuid import uuid4

import polars as pl

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import StorageError, ValidationError
from sams.core.fundamentals import (
    FINANCIAL_COLUMNS,
    FLOW_COLUMNS,
    RATIO_BOUNDS,
    ROE_BASIS,
    ROE_BASIS_ANNUAL,
    TEXT_COLUMNS,
    VALUATION_COLUMNS,
    out_of_caliber,
)
from sams.storage.db import Database
from sams.storage.repos._keys import normalize_keys

logger = logging.getLogger("sams.storage.fundamentals")


def _basis_clause(basis: str) -> str:
    """ROE 口径 → SQL 谓词（D62）。认报告期的日历日，不认名字或字段。

    这一句只写在这里，不散进调用方：口径的日历日判据是 D62 的全部内容，
    改一次就要改所有读侧，而漏改一处的症状是「扫出的票数与算出 PR 的票数对不上」。
    打印用的同一句判据是 `core.fundamentals.is_year_end()`（两处一起改）。
    """
    if basis not in ROE_BASIS:
        raise ValidationError(
            f"ROE 口径只认 {' 或 '.join(ROE_BASIS)}（年报 / 最新一期），收到 {basis!r}"
        )
    return (
        "AND month(report_date) = 12 AND day(report_date) = 31"
        if basis == ROE_BASIS_ANNUAL
        else ""
    )


def _prepare(
    df: pl.DataFrame,
    *,
    columns: tuple[str, ...],
    required: tuple[str, ...],
    date_columns: tuple[str, ...],
    key: tuple[str, ...],
    table: str,
) -> pl.DataFrame:
    """补齐缺列、定型、归一键、丢掉键不全的行——三张表共用这一段，理由见模块开头。"""
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise StorageError(f"{table} 缺少必需列: {missing}")
    # 未登记的列过去由 `select(list(columns))` 静默丢掉：加一条采集时写库不报错、
    # 读回来没有，而「静默少一列」比一次 StorageError 贵得多
    unknown = [c for c in df.columns if c not in columns]
    if unknown:
        raise StorageError(
            f"{table} 的列名单里没有 {unknown}：这一列会被丢掉而你以为写成功了。"
            "要么把它加进 `core.fundamentals` 的名单（连同 DDL 与类型），"
            "要么在采集归一那一步就把它去掉"
        )
    out = df
    for column in columns:
        if column not in out.columns:
            out = out.with_columns(
                pl.lit(None, dtype=_dtype(column, date_columns)).alias(column)
            )
    out = out.select(list(columns))
    for column in date_columns:
        if out.schema[column] != pl.Date:
            # cast(pl.Date) 会把带时区的 datetime 一律取 UTC 日历日：A 股 00:30+08:00 存成前一天
            raise StorageError(
                f"{table}.{column} 必须是 pl.Date，收到 {out.schema[column]}；"
                "带时刻请先按该市场时区取 date"
            )
    numeric = [c for c in columns if _dtype(c, date_columns) is pl.Float64]
    out = out.with_columns(*[pl.col(c).cast(pl.Float64) for c in numeric]) if numeric else out
    out = normalize_keys(out, logger=logger, table=table)
    # NaN/Inf 挡不住 is_not_null，而它一旦入库就会覆盖上一期的好数（与 bars 同一条理由）
    bad = out.select(
        pl.sum_horizontal(
            *[~pl.col(c).is_finite() & pl.col(c).is_not_null() for c in numeric]
        ).sum()
    ).item()
    if bad:
        logger.warning("%s 有 %d 个非有限数值（NaN/Inf）已置空", table, bad)
    out = out.with_columns(
        pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c)
        for c in numeric
    )
    # 小数口径越界（多半是百分数没换算，D58 + D66）：置空那一列，留 warn 给人看。
    # 判据逐行走 core 那一个 `out_of_caliber`，不在这里重写 `>=`：仓库拦下而命令行放过的
    # 数，最后会以「同一个数在两侧口径不同」的形状出现在 PR 里
    for column in RATIO_BOUNDS:
        if column not in out.columns:
            continue
        flagged = [out_of_caliber(column, value) for value in out[column].to_list()]
        suspect = sum(flagged)
        if suspect:
            logger.warning(
                "%s 有 %d 行的 %s 越出小数口径的可信范围（|x| >= %g，疑似百分数未 ÷100）已置空",
                table,
                suspect,
                column,
                RATIO_BOUNDS[column],
            )
            mark = f"__{column}_out_of_caliber"
            out = (
                out.with_columns(pl.Series(mark, flagged, dtype=pl.Boolean))
                .with_columns(
                    pl.when(pl.col(mark)).then(None).otherwise(pl.col(column)).alias(column)
                )
                .drop(mark)
            )
    return out.unique(subset=list(key), keep="last").sort(list(key))


def _dtype(column: str, date_columns: tuple[str, ...]) -> pl.DataType:
    """词表里每一列的类型：文本列的名单在 core，日期列由调用方给，其余是数值。

    缺列补 null 时也用这一个判据——补成 Utf8 会把「这一列没给」变成「这一列是文本」，
    越界检查会在 `str` 上做 `abs()` 而当场炸掉。
    """
    if column in date_columns:
        return pl.Date
    if column in TEXT_COLUMNS:
        return pl.Utf8
    return pl.Float64


def _empty(columns: tuple[str, ...], date_columns: tuple[str, ...]) -> pl.DataFrame:
    """空批次的形状与真实读取一致：调用方不必为「一只票都没同步」写分支。"""
    return pl.DataFrame(schema={c: _dtype(c, date_columns) for c in columns})


def _as_date(value: object) -> date | None:
    return value.date() if isinstance(value, datetime) else value  # type: ignore[return-value]


class FinancialRepo:
    """按报告期的财务指标。读取的唯一入口是 `load_as_of`（PIT），没有「取最新一行」这种捷径。"""

    TABLE = "financials"
    KEY = ("market", "code", "report_date")
    DATE_COLUMNS = ("report_date", "published_date")
    REQUIRED = ("market", "code", "report_date", "source")

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        """幂等：同 (market, code, report_date) 后写覆盖先写。

        这里**不**做跨源冲突拒绝（D45 那一套只属于公司行为）：财务指标的修订是常态，
        同一期报表被源改过一次不是两条互相矛盾的事实，而是同一个事实的两次报道——
        谁写的记在 `source` 里，读的人查得到。
        """
        if df.is_empty():
            return 0
        borrowed = [c for c in ("pe_ttm", "pb") if c in df.columns]
        if borrowed:
            raise StorageError(
                f"financials 不收 {borrowed}：这一行的粒度是报告期，而倍数是某一天的数"
                "（D60）。按日的 PE/PB 写 `valuation_daily`——把它们塞进报告期，"
                "市赚率会拿一个月前的 PE 配今天的价，而屏幕上看不出来"
            )
        prepared = _prepare(
            df,
            columns=FINANCIAL_COLUMNS,
            required=self.REQUIRED,
            date_columns=self.DATE_COLUMNS,
            key=self.KEY,
            table=self.TABLE,
        )
        if prepared.is_empty():
            return 0
        view = f"incoming_financials_{uuid4().hex[:8]}"
        self.db.register(view, prepared)
        try:
            self.db.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE}
                    (market, code, report_date, published_date, report_type,
                     roe, revenue_yoy, profit_yoy, dividend_yield, market_cap,
                     revenue, net_profit, eps, bps, ocf_per_share,
                     gross_margin, net_margin, roa, roic, debt_ratio,
                     current_ratio, quick_ratio, inventory_turnover,
                     total_asset_turnover, source)
                SELECT market, code, report_date, published_date, report_type,
                       roe, revenue_yoy, profit_yoy, dividend_yield, market_cap,
                       revenue, net_profit, eps, bps, ocf_per_share,
                       gross_margin, net_margin, roa, roic, debt_ratio,
                       current_ratio, quick_ratio, inventory_turnover,
                       total_asset_turnover, source
                FROM {view}
                """
            )
        finally:
            self.db.unregister(view)
        return prepared.height

    def load_as_of(
        self, market: str, codes: list[str], as_of: date, *, basis: str = ROE_BASIS_ANNUAL
    ) -> tuple[pl.DataFrame, int]:
        """每只票取「`as_of` 那天已经披露的、该口径下最新的一期」，另带回被 NULL 披露日挡掉的行数。

        返回的是两个数不是一个：`dropped` 那一句必须由调用方打印（D61）。只说
        「最新一期是 2024-03-31」而不说「另有 3 期因为没有披露日未被采信」，
        读的人会以为那 3 期不存在，而不是以为我们不知道它们何时公开。

        `basis`（D62）决定「最新一期」允许落在哪些报告期上：默认只认年报，因为源的
        ROE 是该期累计值，而 PR 的分子 PE(TTM) 是十二个月。`dropped` 按同一口径计——
        把季报的 NULL 披露日也算进「年报口径下被挡掉的行数」，那句话就成了一句谎话。
        """
        market = canonical_market(market)
        if as_of is None:
            raise ValidationError("load_as_of 必须给截至日：PIT 读取没有『默认今天』这条路")
        clause = _basis_clause(basis)
        if not codes:
            return _empty(FINANCIAL_COLUMNS, self.DATE_COLUMNS), 0
        wanted = [canonical_code(market, c) for c in codes]
        frame = self.db.query(
            f"""
            SELECT * EXCLUDE (rnk) FROM (
                SELECT *,
                       rank() OVER (PARTITION BY code ORDER BY report_date DESC) AS rnk
                FROM financials
                WHERE market = ? AND code = ANY(?)
                  AND published_date IS NOT NULL AND published_date <= ?
                  {clause}
            ) WHERE rnk = 1
            ORDER BY code
            """,
            [market, wanted, as_of],
        )
        dropped = int(
            self.db.scalar(
                f"SELECT count(*) FROM financials"
                f" WHERE market = ? AND code = ANY(?) AND published_date IS NULL {clause}",
                [market, wanted],
            )
            or 0
        )
        return frame, dropped

    def history(
        self, market: str, code: str, *, as_of: date | None = None, limit: int | None = None
    ) -> pl.DataFrame:
        """已披露的各期，报告期倒序。M2-D 的「ROE 趋势」与屏上那几行补充期数用这个。"""
        sql = [
            "SELECT * FROM financials WHERE market = ? AND code = ?"
            " AND published_date IS NOT NULL"
        ]
        params: list = [canonical_market(market), canonical_code(market, code)]
        if as_of is not None:
            sql.append("AND published_date <= ?")
            params.append(as_of)
        sql.append("ORDER BY report_date DESC")
        if limit is not None:
            sql.append(f"LIMIT {int(limit)}")
        return self.db.query(" ".join(sql), params)

    def recent(self, market: str, code: str, *, limit: int = 8) -> pl.DataFrame:
        """展示用的最近几期：**含披露日为 NULL 的行**，报告期倒序。

        只给个股页看数（D215）。它不是第二个 `load_as_of`：这里没有截至日闸门，
        也不代表这些期「已披露」——港股指标源不给披露日，拿这些行参与 PR 或选股，
        等于把未来的信息塞进回测。
        """
        return self.db.query(
            "SELECT * FROM financials WHERE market = ? AND code = ?"
            f" ORDER BY report_date DESC LIMIT {int(limit)}",
            [canonical_market(market), canonical_code(market, code)],
        )

    def periods(self, market: str, code: str) -> pl.DataFrame:
        """这只票在库里的**每一**期，含披露日为 NULL 的那些，报告期升序。

        唯一问得出「历史分位那段曲线每天能采信哪一期」的入口：`load_as_of` 与 `history`
        都把 NULL 披露日滤掉了，而分位要**逐日**做 PIT（每一天的截至日不一样），
        并且还要把那些没披露日的期**数出来**（D61）。在仓库侧先滤掉，分析层就会把
        「我们不知道它何时公开」读成「库里没有那一期」，`unpublished_periods` 恒为 0。

        这里不设 `as_of`：那条曲线的一天一天各自带闸门，在本方法上加一个总上界会把
        窗口尾部的那些天一起削掉，而分位问的正是尾部。
        """
        return self.db.query(
            "SELECT report_date, published_date, roe FROM financials"
            " WHERE market = ? AND code = ? ORDER BY report_date",
            [canonical_market(market), canonical_code(market, code)],
        )

    def synced_codes(self, market: str) -> list[str]:
        """库里**有财务行**的全部代码，不看披露日、不看口径、不看截至日。

        选股的 universe 用它而不是「某口径下有已披露一期」（D64 + D66）：一只披露日全为
        NULL 的票确实在库里，按「有已披露一期」划样本会让它从「已同步基本面的 N 只」那句
        话里静默消失——而它正是「我们没数据」被读成「压根没同步」的那一只。它必须进
        universe，然后作为排除被点名。
        """
        rows = self.db.query(
            "SELECT DISTINCT code FROM financials WHERE market = ? ORDER BY code",
            [canonical_market(market)],
        )
        return list(rows["code"].to_list())

    def publication_state(self, market: str, codes: list[str], as_of: date) -> pl.DataFrame:
        """每只票的「为什么这一期用不上」：有几期、其中几期没披露日、最早的一期哪天公开。

        这一问是**库的性状**，不是口径的性状：`load_as_of` 只回答「有什么可用」，而一句
        「没有可用期」要区分三件不同的事，各自的下一步命令不同（D61 + D64）：

        * 有 `unpublished` 期 → 「我们不知道它何时公开」，下一步是重采；
        * 有 `next_published` 晚于 `as_of` → 「那天之前它还不存在」，下一步是换截至日；
        * 两样都没有 → 可用期落在口径之外（季报/半年报），那句由调用方按口径说。

        `as_of` 不给就拒（M2-A 那条同名的红线）：那个 `min` 没有闸门时答的是「任何时候都行」。
        """
        if as_of is None:
            raise ValidationError("publication_state 必须给截至日：那个 min 不设闸门就没有意义")
        market = canonical_market(market)
        if not codes:
            return pl.DataFrame(
                schema={
                    "code": pl.Utf8,
                    "periods": pl.Int64,
                    "unpublished": pl.Int64,
                    "next_published": pl.Date,
                }
            )
        wanted = [canonical_code(market, c) for c in codes]
        return self.db.query(
            """
            SELECT code,
                   count(*) AS periods,
                   count(*) FILTER (WHERE published_date IS NULL) AS unpublished,
                   min(published_date) FILTER (WHERE published_date > ?) AS next_published
            FROM financials
            WHERE market = ? AND code = ANY(?)
            GROUP BY code
            ORDER BY code
            """,
            [as_of, market, wanted],
        )

    def count(self, market: str | None = None) -> int:
        sql = "SELECT count(*) FROM financials"
        params: list = []
        if market:
            sql += " WHERE market = ?"
            params.append(canonical_market(market))
        return int(self.db.scalar(sql, params) or 0)

    def distinct_codes(self, market: str) -> int:
        return int(
            self.db.scalar(
                "SELECT count(DISTINCT code) FROM financials WHERE market = ?",
                [canonical_market(market)],
            )
            or 0
        )

    def latest_published(self, market: str) -> date | None:
        """这个市场最近一次「某只票的某一期被公开」的日期——健康页问的就是这个。"""
        return _as_date(
            self.db.scalar(
                "SELECT max(published_date) FROM financials WHERE market = ?",
                [canonical_market(market)],
            )
        )


class ValuationRepo:
    """按交易日的估值倍数（PE/PB/股息率/市值）。市赚率里「今天的 PE」只能来自这里（D60）。"""

    TABLE = "valuation_daily"
    KEY = ("market", "code", "date")
    DATE_COLUMNS = ("date",)
    REQUIRED = ("market", "code", "date", "source")

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        if df.is_empty():
            return 0
        # 反向的粒度守卫（D60）：`financials` 拒 pe_ttm/pb，这一侧拒比率。
        # `dividend_yield` 不在名单里——按日那个是源当日给的「股息率(TTM)」，粒度对得上
        borrowed = [c for c in ("roe", "revenue_yoy", "profit_yoy") if c in df.columns]
        if borrowed:
            raise StorageError(
                f"valuation_daily 不收 {borrowed}：这一行的粒度是交易日，"
                "而比率是某一期的数（D60）。按报告期的比率写 `financials`——"
                "把它们塞进按日那一行，市赚率会拿今天的 ROE 去配任何一天的 PE"
            )
        prepared = _prepare(
            df,
            columns=VALUATION_COLUMNS,
            required=self.REQUIRED,
            date_columns=self.DATE_COLUMNS,
            key=self.KEY,
            table=self.TABLE,
        )
        if prepared.is_empty():
            return 0
        view = f"incoming_valuation_{uuid4().hex[:8]}"
        self.db.register(view, prepared)
        try:
            self.db.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE}
                    (market, code, date, pe_ttm, pb, dividend_yield, market_cap, source,
                     updated_at)
                SELECT market, code, date, pe_ttm, pb, dividend_yield, market_cap, source,
                       now()
                FROM {view}
                """
            )
        finally:
            self.db.unregister(view)
        return prepared.height

    def latest(
        self, market: str, codes: list[str], as_of: date, *, within_days: int | None = None
    ) -> pl.DataFrame:
        """每只票 `date <= as_of` 里最新的一行。

        `within_days` 是给选股用的：一只停牌两个月的票仍然「有 PE」，但那个 PE 属于
        两个月前的那一天，拿它去和今天的价比出来的 PR 不是同一天的一篮子。给了它就
        只要 `as_of - within_days` 之内还有行的票——被排除的票数由调用方说出口。
        """
        market = canonical_market(market)
        if not codes:
            return _empty(VALUATION_COLUMNS, self.DATE_COLUMNS)
        wanted = [canonical_code(market, c) for c in codes]
        sql = """
            SELECT * EXCLUDE (rnk) FROM (
                SELECT *, rank() OVER (PARTITION BY code ORDER BY date DESC) AS rnk
                FROM valuation_daily
                WHERE market = ? AND code = ANY(?) AND date <= ?
        """
        params: list = [market, wanted, as_of]
        if within_days is not None:
            sql += " AND date >= ?"
            params.append(_days_before(as_of, within_days))
        sql += "            ) WHERE rnk = 1 ORDER BY code"
        return self.db.query(sql, params)

    def series(
        self, market: str, code: str, start: date | None = None, end: date | None = None
    ) -> pl.DataFrame:
        """按日的倍数序列。M2-D 的「PE-PB 历史分位」问的就是这个。"""
        sql = ["SELECT * FROM valuation_daily WHERE market = ? AND code = ?"]
        params: list = [canonical_market(market), canonical_code(market, code)]
        if start is not None:
            sql.append("AND date >= ?")
            params.append(start)
        if end is not None:
            sql.append("AND date <= ?")
            params.append(end)
        return self.db.query(" ".join(sql) + " ORDER BY date", params)

    def covered_codes(self, market: str, *, as_of: date, within_days: int = 7) -> list[str]:
        rows = self.db.query(
            """
            SELECT code FROM valuation_daily
            WHERE market = ? AND date <= ? AND date >= ?
            GROUP BY code ORDER BY code
            """,
            [canonical_market(market), as_of, _days_before(as_of, within_days)],
        )
        return list(rows["code"].to_list())

    def latest_date(self, market: str, code: str) -> date | None:
        return _as_date(
            self.db.scalar(
                "SELECT max(date) FROM valuation_daily WHERE market = ? AND code = ?",
                [canonical_market(market), canonical_code(market, code)],
            )
        )

    def count(self, market: str | None = None) -> int:
        sql = "SELECT count(*) FROM valuation_daily"
        params: list = []
        if market:
            sql += " WHERE market = ?"
            params.append(canonical_market(market))
        return int(self.db.scalar(sql, params) or 0)

    def distinct_codes(self, market: str) -> int:
        return int(
            self.db.scalar(
                "SELECT count(DISTINCT code) FROM valuation_daily WHERE market = ?",
                [canonical_market(market)],
            )
            or 0
        )


class FundingFlowRepo:
    """按日的资金流（主力净流入、北向、融资余额）。港美股没有这一列的可靠免费源：缺就是缺。"""

    TABLE = "funding_flow"
    KEY = ("market", "code", "date")
    DATE_COLUMNS = ("date",)
    REQUIRED = ("market", "code", "date", "source")

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        """列名单里没有 `updated_at`：`funding_flow` 建表时就没有这一列（0001），
        而资金流的读取只问「哪一天」，不问「这行是什么时候写的」。"""
        if df.is_empty():
            return 0
        prepared = _prepare(
            df,
            columns=FLOW_COLUMNS,
            required=self.REQUIRED,
            date_columns=self.DATE_COLUMNS,
            key=self.KEY,
            table=self.TABLE,
        )
        if prepared.is_empty():
            return 0
        view = f"incoming_flow_{uuid4().hex[:8]}"
        self.db.register(view, prepared)
        try:
            self.db.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE}
                    (market, code, date, main_net_in, north_net, north_hold, margin_bal,
                     source)
                SELECT market, code, date, main_net_in, north_net, north_hold, margin_bal,
                       source
                FROM {view}
                """
            )
        finally:
            self.db.unregister(view)
        return prepared.height

    def load_range(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        return self.db.query(
            "SELECT * FROM funding_flow WHERE market = ? AND code = ?"
            " AND date >= ? AND date <= ? ORDER BY date",
            [canonical_market(market), canonical_code(market, code), start, end],
        )

    def latest_date(self, market: str, code: str) -> date | None:
        return _as_date(
            self.db.scalar(
                "SELECT max(date) FROM funding_flow WHERE market = ? AND code = ?",
                [canonical_market(market), canonical_code(market, code)],
            )
        )

    def count(self, market: str | None = None) -> int:
        sql = "SELECT count(*) FROM funding_flow"
        params: list = []
        if market:
            sql += " WHERE market = ?"
            params.append(canonical_market(market))
        return int(self.db.scalar(sql, params) or 0)


def _days_before(day: date, days: int) -> date:
    return day - timedelta(days=days)
