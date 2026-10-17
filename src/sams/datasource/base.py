"""采集层公共件：适配器协议、标准列集、规范化。

设计意图：每个数据源只做「外部接口 → 标准列集」这一件事，
其余（限速、重试、降级、入库）都发生在适配器之外。
适配器因此可以在完全不联网的情况下被测试——测试喂录制 payload 给纯解析函数。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Protocol

import polars as pl

from sams.core.errors import (
    DatasourceError,
    EmptyResponse,
    FieldDrift,
    RateLimited,
    TransientError,
)

logger = logging.getLogger("sams.datasource")

_RATE_HINTS = (
    "429",
    "too many requests",
    "rate limit",
    "frequency",
    "请求过于频繁",
    "抱歉，您发出的请求过频",
)
_TRANSIENT_HINTS = ("timeout", "timed out", "connection", "reset by peer", "try again")


def classify_error(source: str, exc: Exception) -> DatasourceError:
    """把第三方库五花八门的异常归入本域异常类型。

    路由器依赖这个分类决定「退避重试」还是「立即换源」：
    对字段漂移做退避重试是纯浪费，对 429 立即换源则会白白打爆备源。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint in text for hint in _RATE_HINTS):
        return RateLimited(source, str(exc))
    if isinstance(exc, (TimeoutError, ConnectionError)) or any(
        hint in text for hint in _TRANSIENT_HINTS
    ):
        return TransientError(source, str(exc))
    return DatasourceError(source, f"{type(exc).__name__}: {exc}")

BAR_COLUMNS = [
    "market",
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
]

#: A 股两个源的成交量都给「手」，港美股给「股」。入库统一为股。
LOTS_TO_SHARES = 100.0

#: 市场 → 把用户输入的代码翻译成数据源所需的 symbol
def code_in_market(market: str, code: str) -> str:
    cleaned = (code or "").strip().upper()
    if not cleaned:
        raise ValueError("code 不能为空")
    if market == "HK":
        digits = cleaned.removeprefix("HK")
        return f"{int(digits):05d}.HK" if digits.isdigit() else cleaned
    return cleaned


class BarAdapter(Protocol):
    """数据源适配器契约。健康状态不在这里：由 registry 在路由时统一记录，
    因为只有它看得见「这次失败有没有被备源救回来」。"""

    name: str
    markets: frozenset[str]

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """返回标准列集（BAR_COLUMNS 的子集 + 必需列）。无数据抛 EmptyResponse。"""
        ...


def normalize_bars(
    raw: pl.DataFrame,
    *,
    market: str,
    code: str,
    source: str,
    column_map: dict[str, str],
    required: tuple[str, ...] = ("date", "close"),
    volume_scale: float = 1.0,
) -> pl.DataFrame:
    """把源侧 DataFrame 规范化为标准列集。

    column_map: 标准列名 → 源列名。
    只有 `required`（默认日期与收盘价）缺列才算字段漂移并失败；
    其余列缺失填 null —— 免费源少给一列成交额是常态，不该因此丢整天数据。
    丢弃规则：无收盘价或无日期的行丢弃并记警告，存成 close=0 会污染后续全部指标。

    volume_scale: 将源侧成交量换算成「股」的系数——A 股两个源给的是「手」（=100），
    港美股给的是「股」（=1）。不换算的话，`放量 > 5 日均量 x 倍`、换手率、
    成交额阈值会在三个市场之间各差 100 倍，且没有任何一处会报错。
    """
    absent_required = [
        column_map[name] for name in required if column_map.get(name) not in raw.columns
    ]
    if absent_required:
        raise FieldDrift(
            source, f"{market} {code} 缺少必需列 {absent_required}，实际列 {list(raw.columns)}"
        )

    renames = {
        src: std for std, src in column_map.items() if src in raw.columns and src != std
    }
    out = raw.rename(renames)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
        out = out.with_columns(pl.col(column).cast(pl.Float64, strict=False))
    if volume_scale != 1.0:
        out = out.with_columns(pl.col("volume") * pl.lit(volume_scale))

    out = out.with_columns(_to_date_expr(pl.col("date")))

    before = out.height
    out = out.filter(pl.col("close").is_not_null() & pl.col("date").is_not_null())
    dropped = before - out.height
    if dropped:
        logger.warning("丢弃 %s %s 的 %d 行（无收盘价或无日期）", market, code, dropped)

    if out.is_empty():
        raise EmptyResponse(source, f"{market} {code} 无有效日线数据")

    return (
        out.with_columns(
            pl.lit(market).alias("market"),
            pl.lit(code).alias("code"),
            pl.lit(source).alias("source"),
        )
        .select(BAR_COLUMNS)
        .unique(subset=["date"], keep="last")
        .sort("date")
    )


def _to_date_expr(col: pl.Expr) -> pl.Expr:
    """源侧日期形态不一：date、datetime、'YYYY-MM-DD'、'YYYYMMDD' 都出现过。

    统一先转成字符串再逐个格式尝试：Polars 2.0 起不允许把字符串列直接 cast 成 Date
    （strict=False 也会抛错），所以只能在 strptime 这条路上走到底。
    最后一档取前 10 个字符，兜住 '2024-01-02 00:00:00 CST'、'...T00:00:00.000' 这类
    带后缀的形态——它们前 10 位仍是合法日期。
    """
    text = col.cast(pl.Utf8, strict=False)
    return pl.coalesce(
        text.str.strptime(pl.Date, "%Y-%m-%d", strict=False),
        text.str.strptime(pl.Date, "%Y-%m-%d %H:%M:%S", strict=False),
        text.str.strptime(pl.Date, "%Y-%m-%dT%H:%M:%S", strict=False),
        text.str.strptime(pl.Date, "%Y%m%d", strict=False),
        text.str.slice(0, 10).str.strptime(pl.Date, "%Y-%m-%d", strict=False),
    )
