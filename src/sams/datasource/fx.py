"""汇率采集的契约与规整（spec §5.4 规则 2 的数据来源）。

这一层只把「外面怎么写」翻译成「库里怎么写」，翻译之外不决定任何事。两件事在这里定死：

* **方向**（§5.4 规则 2）。`(base, quote)` 读作「1 单位 base = rate 单位 quote」，与
  `fx_rates` 那一列同义。源给反方向时取倒数会得到一个「大约对」的数：它对到足以让市值、
  成本、合计全都看着正常，又错到足以让每一笔跨币种盈亏都差几个百分点，而且库里查不出
  来。所以方向不对就当没答上，绝不换一个数。
* **`EmptyResponse` 在这里算一句答复**（与日线同侧、与基本面反侧，D43）。汇率没有
  「这张表里没这只票」那种 0 行形态：接口正常回空就是「这段日子没有报价」。而**挂了不算**：
  一家说没有、另一家超时，那句「没有」还不能写成确认。
"""

from __future__ import annotations

import logging
from datetime import date
from math import isfinite
from typing import Any

import polars as pl

from sams.core.errors import EmptyResponse, FieldDrift

logger = logging.getLogger("sams.datasource.fx")

#: 标准列集：`source` 是「这一行的数来自哪一家」（D54），手工覆盖时它是唯一的线索
FX_COLUMNS = ("date", "base", "quote", "rate", "source")

_SCHEMA = {
    "date": pl.Date,
    "base": pl.Utf8,
    "quote": pl.Utf8,
    "rate": pl.Float64,
    "source": pl.Utf8,
}


def has_fx(obj: Any) -> bool:
    """能力判据只看方法在不在（D38），与 `actions.has_actions` 同一条写法。"""
    return callable(getattr(obj, "fx_rate", None))


def empty_fx() -> pl.DataFrame:
    """全链都说「这段没有」时返回的形状：列集与真帧一致，调用方无需分支。"""
    return pl.DataFrame(schema=_SCHEMA)


def normalize_fx(
    raw: pl.DataFrame, *, base: str, quote: str, source: str = "unknown"
) -> pl.DataFrame:
    """校验方向、解析日期、补来源列。缺列 / 空框 / 方向反了都抛，不返回空框。

    「非有限」的那几行（NaN、None）丢掉并记一行警告：那是 pandas 侧的「这一格没值」，
    与「这一天的汇率是 0」不是一件事——后者会被仓库层的闸门当场拒收，那是应当出声的错。
    """
    missing = [column for column in ("date", "rate") if column not in raw.columns]
    if missing or raw.is_empty():
        raise EmptyResponse(source, f"汇率响应缺列或为空：{missing or '空框'}")
    rows: list[dict[str, Any]] = []
    dropped = 0
    for row in raw.to_dicts():
        value = _finite(row.get("rate"))
        if value is None:
            dropped += 1
            continue
        got_base = str(row.get("base") or base)
        got_quote = str(row.get("quote") or quote)
        if (got_base, got_quote) != (base, quote):
            raise EmptyResponse(
                source,
                f"汇率方向不对：要 {base}→{quote}，源给的是 {got_base}→{got_quote}"
                "（取倒数是「大约对」，方向写反查不出来）",
            )
        try:
            day = date.fromisoformat(str(row["date"])[:10])
        except ValueError:
            # 日期列换了写法是改版，不是「这段没数据」：把它答成「没有」会让降级链
            # 收下这句确认，然后库里从此缺这一段
            raise FieldDrift(
                source, f"汇率日期 {row['date']!r} 读不出是哪一天（要 YYYY-MM-DD 打头）"
            ) from None
        rows.append({"date": day, "base": base, "quote": quote, "rate": value, "source": source})
    if not rows:
        logger.warning("%s→%s 有 %d 行汇率读不出数，已丢弃", base, quote, dropped)
        raise EmptyResponse(source, f"{base}→{quote} 这一段没有可读出的汇率")
    return pl.DataFrame(rows, schema=_SCHEMA).sort("date")


def _finite(value: Any) -> float | None:
    """读不出一个有限数时给 None：0 是一个数，`None` 才是「这一格没值」。"""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if isfinite(out) else None
