"""报价适配器契约与三个源共用的解析件。

报价与日线是两种取数：没有区间、一次问一批、结果以「只」为单位。把它塞进
`BarAdapter.daily_bars` 的形状里，适配器就要假装每只票都有一根 K 线，而失败会以
「这个区间没有数据」的样子出现——那正是 D43 说过的「两种 0 行不能混成一个结论」。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import ValidationError
from sams.core.quotes import QuoteBatch, as_utc

#: 一次请求问多少只。免费接口对 URL 长度与 QPS 都有脾气，50 是实测能稳过的量级
QUOTE_BATCH = 50


class QuoteAdapter(Protocol):
    name: str
    markets: frozenset[str]

    def quotes(self, market: str, codes: list[str]) -> QuoteBatch:
        """返回这一批里能读到的报价；缺的代码进 `skipped`，读不懂的进 `errors`。"""
        ...


def has_quotes(obj: object) -> bool:
    """按能力判断，不按类判断（D38 的同一条约定）。"""
    return callable(getattr(obj, "quotes", None))


def chunks(items: list[str], size: int = QUOTE_BATCH) -> list[list[str]]:
    if size <= 0:
        raise ValueError(f"size 必须为正: {size}")
    return [items[i : i + size] for i in range(0, len(items), size)]


def echo_matches(market: str, requested: str, echoed: str) -> bool:
    """源回显的代码是不是我们问的那一只。

    美股回显带交易所后缀（问 `AAPL` 回 `AAPL.OQ`），所以认「与请求一致，或恰好多一个
    尾部段」；不能按点前的裸代码比——`BRK.A` 与 `BRK.B` 是同一公司的两类股，裸代码
    相同而语义不同，按裸代码比会把 B 类股的价格写进 A 类的键下。A/HK 用
    `canonical_code` 归一（`sh600519` 与 `600519` 同一只）。回显不符必须整只拒绝：
    错一位就是把别家公司的价格写进这只票的键下。
    """
    text = (echoed or "").strip()
    if not text:
        return False
    if canonical_market(market) == "US":
        req = requested.strip().upper()
        text = text.upper()
        return text == req or (text.startswith(req + ".") and text.count(".") == req.count(".") + 1)
    try:
        return canonical_code(market, text) == canonical_code(market, requested)
    except ValidationError:
        return False


def field_float(fields: Sequence[str], index: int) -> float | None:
    """按位置取一个数：空、缺位、`N/A`、NaN 一律 None，绝不折成 0。
    取不到数的调用方必须把它记进批次的 errors，不能只进 skipped——少了谁要让值班的人看见。
    """
    try:
        text = fields[index]
    except IndexError:
        return None
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_moment(raw: str, formats: Sequence[str], zone: ZoneInfo) -> datetime | None:
    """按源给的本地时刻解析并转 UTC；读不懂就返回 None，由调用方决定怎么兜底。"""
    text = (raw or "").strip()
    for fmt in formats:
        try:
            return as_utc(datetime.strptime(text, fmt), zone)
        except ValueError:
            continue
    return None
