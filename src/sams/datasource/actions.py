"""公司行为采集的公共契约（D38）：能力协议 + 标准列规范化。

一个源可以只给日线、只给公司行为，或两者都给。能力由实现类自己声明，registry 按能力
路由，缺能力的源**显式跳过并说明原因**。静默跳过的症状是「这个市场从没分过红」——
它和「从没查过」在库里长得一模一样，而裸价跨除权日算出来的涨跌是一个看起来完全正常的
错数（601088 的 −3.81% vs 交易所口径 +1.26%）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol

import polars as pl

from sams.core.actions import ACTION_COLUMNS, ACTION_KINDS
from sams.core.codes import canonical_code
from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.base import to_date_expr

logger = logging.getLogger("sams.datasource")

#: 一条公司行为至少要有一个数才能推导因子；三个都空是源里的噪声行（不分红的报告期）
_NUMERIC = ("cash_per_share", "ratio", "price")


class CorpActionAdapter(Protocol):
    """能给公司行为的源实现这个协议。日线能力（`BarAdapter`）与它是两条独立的能力线：
    有日线的源不一定有除权记录，反过来也成立。"""

    name: str
    #: 接口不带日期参数、一次给全史的源置 True（见 `answers_full_history`）
    corp_actions_full_history: bool

    def corp_actions(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """返回 ACTION_COLUMNS（D34 口径：每股现金、股数乘数、每股配股价）。

        「这个区间没有除权」返回空帧而不是抛 `EmptyResponse`（见 `empty_actions`）；
        只有源真的答不上来（改版、限流）才抛异常。
        """
        ...


def has_actions(obj: Any) -> bool:
    """按能力判断，不按类判断。用 `isinstance(..., CorpActionAdapter)` 会把协议写成名义类型，
    测试里的桩就得继承实现类——那等于取消了「能力可单独实现」这条设计。"""
    return callable(getattr(obj, "corp_actions", None))


def answers_full_history(obj: Any) -> bool:
    """这个源给的是不是「这只票的全部历史公司行为」（D36 的覆盖标记要看这个）。

    默认按否处理：没声明过的源只能确认它被请求的那段区间。把没说过的事当成说过，
    覆盖守卫就会在最窄的一次取数上放行最宽的历史——那正是 U12 要挡的错法。
    """
    return bool(getattr(obj, "corp_actions_full_history", False))


def empty_actions() -> pl.DataFrame:
    """源干净地回答「这个区间没有公司行为」——一个带正确 schema 的空帧。

    这不是 `EmptyResponse`：一家二十年只分红三次的股票，同步 2024 年 1—2 月拿到空结果是
    **正确**的，而 EmptyResponse 在路由里既不算成功也不算故障（D25），覆盖标记就写不下去——
    于是「查过、没有」与「从没查过」又混回一团（D36 要拆的正是这一对）。
    """
    typed = {
        "ex_date": pl.Date,
        "cash_per_share": pl.Float64,
        "ratio": pl.Float64,
        "price": pl.Float64,
    }
    return pl.DataFrame(schema={c: typed.get(c, pl.Utf8) for c in ACTION_COLUMNS})


def normalize_actions(
    raw: pl.DataFrame,
    *,
    market: str,
    code: str,
    source: str,
    column_map: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """把源侧的公司行为帧规范成 ACTION_COLUMNS。

    与 `normalize_bars` 的取舍不同之处在于**不静默丢行**：
      * `kind` 出现不认识的值 ⇒ 整批失败（FieldDrift）。少认一种类型就是少乘一次除权，
        而序列上看不出任何异常；改版导致的新类型必须当场响。
      * 数值出现负数 ⇒ 整批失败。负派息不存在，出现就是解析错位，乘进因子会漂一整段。
      * 缺 `ex_date`、缺 `kind`、三个数值全空 ⇒ 丢掉并计数告警。这些是源里的常态噪声
        （预案未定、不分红的报告期），不是改版。
    """
    column_map = dict(column_map or {c: c for c in ACTION_COLUMNS})
    absent = [column_map[k] for k in ("ex_date", "kind") if column_map.get(k) not in raw.columns]
    if absent:
        raise FieldDrift(source, f"{market} {code} 缺少必需列 {absent}，实际列 {list(raw.columns)}")
    if not any(column_map.get(n) in raw.columns for n in _NUMERIC):
        raise FieldDrift(
            source, f"{market} {code} 的响应里现金、股数、配股价三类数值列全缺，推不出任何因子"
        )

    out = raw.rename(
        {src: std for std, src in column_map.items() if src in raw.columns and src != std}
    )
    for column in _NUMERIC:
        if column not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
        out = out.with_columns(pl.col(column).cast(pl.Float64, strict=False))
    if "note" not in out.columns:
        out = out.with_columns(pl.lit(None, dtype=pl.Utf8).alias("note"))
    out = out.with_columns(
        to_date_expr(pl.col("ex_date")),
        pl.col("kind").cast(pl.Utf8, strict=False).str.strip_chars().str.to_lowercase(),
    )

    unknown = sorted({k for k in out["kind"].to_list() if k is not None and k not in ACTION_KINDS})
    if unknown:
        raise FieldDrift(
            source,
            f"{market} {code} 出现未知的公司行为类型 {unknown}，可选 {sorted(ACTION_KINDS)}："
            "认不出就不入库，漏乘一次除权在图上看不出来",
        )
    negative = out.filter(
        (pl.col("cash_per_share") < 0) | (pl.col("ratio") < 0) | (pl.col("price") < 0)
    )
    if not negative.is_empty():
        raise FieldDrift(
            source,
            f"{market} {code} 有 {negative.height} 行给出负数（除权日 "
            f"{negative['ex_date'].head(3).to_list()}），数值不可能为负，按解析错位处理",
        )
    out = out.with_columns(
        pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in _NUMERIC
    )

    before = out.height
    out = out.filter(pl.col("ex_date").is_not_null() & pl.col("kind").is_not_null())
    no_date = before - out.height
    before = out.height
    out = out.filter(
        pl.col("cash_per_share").is_not_null()
        | pl.col("ratio").is_not_null()
        | pl.col("price").is_not_null()
    )
    no_number = before - out.height
    if no_date or no_number:
        logger.warning(
            "丢弃 %s %s 的公司行为 %d 行（缺除权日/类型 %d，三个数值全空 %d）",
            market,
            code,
            no_date + no_number,
            no_date,
            no_number,
        )
    if out.is_empty():
        raise EmptyResponse(source, f"{market} {code} 区间内没有可推导的公司行为")

    return (
        out.with_columns(
            pl.lit(canonical_code(market, code)).alias("code"),
            pl.lit(market).alias("market"),
            pl.lit(source).alias("src"),
        )
        .select(ACTION_COLUMNS)
        .unique(subset=["ex_date", "kind"], keep="last")
        .sort(["ex_date", "kind"])
    )
