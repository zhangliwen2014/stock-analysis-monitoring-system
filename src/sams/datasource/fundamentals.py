"""基本面、按日估值、资金流三种取数的采集契约：能力协议 + 标准列归一 + 空答复的形状。

这一层把「外面怎么写」翻译成「库里怎么写」，翻译之外什么都不决定。三件必须在这里定死的
事，每一件都对应一种不会报错的错法：

* **单位**（D58）。akshare 的 ROE/增速/股息率按百分数给（20.0 = 20%），yfinance 按小数给
  （0.20 = 20%）。库里只许小数，所以 `percent_columns` 是**必填参数而不是默认值**——
  默认值会让「忘了声明」也成一种声明，而忘掉的后果是市赚率算出 PR=0.01，
  屏幕上它长得和「极度低估」一模一样。
* **键形态**（D50）。`market`/`code` 在出口就归一，与仓库层同一条规则。两侧都归一是
  有意的冗余（这条规则幂等），而「采集时不归一、入库时看调用方记得不记得」会让同一只票
  在 `financials` 里是 `700`、在 `valuation_daily` 里是 `00700`——市赚率配不上对，
  症状是「这只票算不出 PR」，真相是两次采集用了两种写法。
* **空答复的形状**（D43）。「这只票在那张表里没有记录」返回带完整 schema 的 0 行帧；
  「源答不上来」（接口回空、列名改了、日期解析不出）抛 `EmptyResponse`。这两件事在
  覆盖标记上后果完全不同（D36）：前者是可以写成确认的一句话，后者不能替任何人发确认。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol

import polars as pl

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import EmptyResponse, FieldDrift
from sams.core.fundamentals import (
    FINANCIAL_COLUMNS,
    FLOW_COLUMNS,
    PERCENT_DIVISOR,
    TEXT_COLUMNS,
    VALUATION_COLUMNS,
)
from sams.datasource.base import to_date_expr

logger = logging.getLogger("sams.datasource.fundamentals")


class FinancialAdapter(Protocol):
    """能给「按报告期」的财务指标的源。粒度是期不是日（D60）：按日的倍数走 `ValuationAdapter`。"""

    name: str

    def financials(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """返回 FINANCIAL_COLUMNS（比率列为小数口径）。

        「这只票在这段区间没有记录」返回 0 行帧（见 `empty_financials`）；只有源真的答不上来
        才抛 `EmptyResponse`。前者是可以继续问的一句话，后者是不能替任何人发覆盖确认的沉默。
        """
        ...


class ValuationAdapter(Protocol):
    """能给「按交易日」的估值倍数的源。市赚率要的「今天的 PE」只能来自这里。"""

    name: str

    def valuation(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """返回 VALUATION_COLUMNS；亏损票的负 PE 是**一个真数**、不许置空（D59 靠它分叉）。"""
        ...


class FundingFlowAdapter(Protocol):
    """能给按日资金流的源。港美股没有可靠的免费主力净流入源：缺就缺，不拿 0 充数。"""

    name: str

    def funding_flow(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """返回 FLOW_COLUMNS。"""
        ...


def has_financials(obj: Any) -> bool:
    """按方法存在与否判断，与 `has_bars`/`has_actions` 同一约定（D38）。"""
    return callable(getattr(obj, "financials", None))


def has_valuation(obj: Any) -> bool:
    return callable(getattr(obj, "valuation", None))


def has_flow(obj: Any) -> bool:
    return callable(getattr(obj, "funding_flow", None))


def _schema(columns: tuple[str, ...], date_columns: tuple[str, ...]) -> dict[str, pl.DataType]:
    """列 → 类型。「哪些列是文本」的名单在 `core.fundamentals`，这里只补 polars 那一侧的拼写。"""
    return {
        column: (
            pl.Date
            if column in date_columns
            else pl.Utf8
            if column in TEXT_COLUMNS
            else pl.Float64
        )
        for column in columns
    }


def empty_financials() -> pl.DataFrame:
    return pl.DataFrame(schema=_schema(FINANCIAL_COLUMNS, ("report_date", "published_date")))


def empty_valuation() -> pl.DataFrame:
    return pl.DataFrame(schema=_schema(VALUATION_COLUMNS, ("date",)))


def empty_flow() -> pl.DataFrame:
    return pl.DataFrame(schema=_schema(FLOW_COLUMNS, ("date",)))


def normalize_rows(
    raw: pl.DataFrame,
    *,
    market: str,
    code: str,
    source: str,
    columns: tuple[str, ...],
    column_map: Mapping[str, str],
    required: tuple[str, ...],
    date_columns: tuple[str, ...],
    key_date: str,
    percent_columns: tuple[str, ...],
    table: str,
) -> pl.DataFrame:
    """三张表共用的那一段翻译：重命名 → 补列 → 定日期 → 换单位 → 归一键 → 同键去重。

    为什么是一段而不是三份：三种数据的差别只在**列名与粒度**，而会出错的地方完全相同
    （百分数没换算、日期列装着索引、键两种写法）。写三份等于把同一个 bug 抄三遍，
    而第三遍会抄上前两遍里抄错的那一行。
    """
    absent = [column_map[name] for name in required if column_map.get(name) not in raw.columns]
    if absent:
        raise FieldDrift(
            source,
            f"{market} {code} {table} 缺少必需列 {absent}，实际列 {list(raw.columns)}",
        )
    out = raw.rename(
        {src: std for std, src in column_map.items() if src in raw.columns and src != std}
    )
    vanished = [
        src for std, src in column_map.items() if std not in required and src not in raw.columns
    ]
    if vanished and not raw.is_empty():
        # 声明了却整列消失：填 null 是取舍，静默地填不是。披露日整列没了的后果是 PIT 读取
        # 把这一批全部挡掉（D61），而屏幕上只会说「这只票没有已披露的财务数据」——方向反了
        logger.warning(
            "%s %s %s 的 %s 缺少列 %s，按 NULL 入库", source, market, code, table, vanished
        )
    schema = _schema(columns, date_columns)
    for column in columns:
        if column not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=schema[column]).alias(column))
    out = out.select(list(columns))
    for column in date_columns:
        out = out.with_columns(to_date_expr(pl.col(column)).alias(column))
    values = [c for c in columns if c not in (*TEXT_COLUMNS, *date_columns)]
    out = out.with_columns(pl.col(c).cast(pl.Float64, strict=False) for c in values)
    # ÷100 只在这一处发生（D58）：仓库层与服务层再出现一个 ÷100，就是两处口径各活一次
    out = out.with_columns(
        (pl.col(c) / PERCENT_DIVISOR).alias(c) for c in percent_columns if c in out.columns
    )

    before = out.height
    out = out.filter(pl.col(key_date).is_not_null())
    if out.height < before:
        # 主键的一半为空不是「缺一个字段」，而是「这一行永远读不到」：留下它只会涨行数
        logger.warning(
            "%s %s %s 丢弃 %d 行（%s 解析不出日期）",
            source,
            market,
            code,
            before - out.height,
            column_map.get(key_date, key_date),
        )
    if out.is_empty() and before:
        # 源给了行却一行都不可用：那是答不上来，不是「这只票没有记录」，不许写成空答复
        raise EmptyResponse(source, f"{market} {code} 的 {table} 没有一行能解析出日期")

    canonical = canonical_market(market)
    return (
        out.with_columns(
            pl.lit(canonical).alias("market"),
            pl.lit(canonical_code(canonical, code)).alias("code"),
            pl.lit(source).alias("source"),
        )
        .select(list(columns))
        .unique(subset=[key_date], keep="last")
        .sort(key_date)
    )


def normalize_financials(
    raw: pl.DataFrame,
    *,
    market: str,
    code: str,
    source: str,
    column_map: Mapping[str, str],
    percent_columns: tuple[str, ...],
) -> pl.DataFrame:
    """按报告期的财务指标 → `FINANCIAL_COLUMNS`。"""
    return normalize_rows(
        raw,
        market=market,
        code=code,
        source=source,
        columns=FINANCIAL_COLUMNS,
        column_map=column_map,
        required=("report_date",),
        date_columns=("report_date", "published_date"),
        key_date="report_date",
        percent_columns=percent_columns,
        table="financials",
    )


def normalize_valuation(
    raw: pl.DataFrame,
    *,
    market: str,
    code: str,
    source: str,
    column_map: Mapping[str, str],
    percent_columns: tuple[str, ...],
) -> pl.DataFrame:
    """按交易日的估值倍数 → `VALUATION_COLUMNS`。"""
    return normalize_rows(
        raw,
        market=market,
        code=code,
        source=source,
        columns=VALUATION_COLUMNS,
        column_map=column_map,
        required=("date",),
        date_columns=("date",),
        key_date="date",
        percent_columns=percent_columns,
        table="valuation_daily",
    )


def normalize_flow(
    raw: pl.DataFrame,
    *,
    market: str,
    code: str,
    source: str,
    column_map: Mapping[str, str],
    percent_columns: tuple[str, ...],
) -> pl.DataFrame:
    """按交易日的资金流 → `FLOW_COLUMNS`。资金流全是金额，`percent_columns` 一般是空 tuple。"""
    return normalize_rows(
        raw,
        market=market,
        code=code,
        source=source,
        columns=FLOW_COLUMNS,
        column_map=column_map,
        required=("date",),
        date_columns=("date",),
        key_date="date",
        percent_columns=percent_columns,
        table="funding_flow",
    )
