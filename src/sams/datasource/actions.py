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

        `corp_actions_full_history` 为真的源**不受 `start/end` 约束**：它的接口本来就一次
        给全史，切掉窗口外的行等于让库里少几行事实，而服务层照样会写「全历史，确认至今
        天」——库与覆盖标记说反话时，守卫就对老日子放行了裸价。按区间拉的源必须切。
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
      * 缺 `ex_date`、缺 `kind`、非 unknown 却三个数值全空 ⇒ 丢掉并计数告警。这些是源里的
        常态噪声（预案未定、不分红的报告期），不是改版。
      * `kind = unknown` 允许三个数值全空：它记的是「这天确实有件事，但我折不出数」（D39），
        读取层见到它就拒绝，而不是把它当成没发生过。
      * 同一 (除权日, 类型) 的多行 ⇒ **相加合成一行**（`merge_same_day`）。库的主键就是
        (market, code, ex_date, kind)，同日同类第二行无处可存，吞掉它就是少乘一截因子。

    空输入不要送到这里来：`EmptyResponse` 在路由里的意思是「这个源没答上」，而「答了、
    这区间没有」必须用 `empty_actions()` 表达（D36 的覆盖标记只认后者）。
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
    # `unknown` 带着空数值是**有意**的：那是「这一天确实动了东西，但我折算不出数」的记录（D39），
    # 读取层见到它就拒绝出数。其余类型三个数值全空才是源里的噪声行（未定预案、不分红的报告期）
    out = out.filter(
        (pl.col("kind") == "unknown")
        | pl.col("cash_per_share").is_not_null()
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

    return merge_same_day(
        out.with_columns(
            pl.lit(canonical_code(market, code)).alias("code"),
            pl.lit(market).alias("market"),
            pl.lit(source).alias("src"),
        )
        .select(ACTION_COLUMNS)
        # 完全一样的重复行是同一份方案的重复披露，不是两件事
        .unique()
    ).sort(["ex_date", "kind"])


def merge_same_day(frame: pl.DataFrame) -> pl.DataFrame:
    """同一 (除权日, 类型) 的多行合成一行：现金相加、新增股数相加、配股价按新增股数加权。

    为什么是相加而不是「留最新一条」：`corp_actions` 的主键是 (market, code, ex_date, kind)，
    同日同类的第二行在库里根本没有容身之处——年度分红与特别分红同日除权是真实存在的
    事件，吞掉一条就是因子少乘一截，而价格序列照样单调、图上看不出任何异常。合成口径与
    推导端逐字一致（`analysis.adjust.group_by_ex_date` 对同一天的多条行为就是相加），
    所以「这里一行、那里两行」不会改变算出来的因子。

    `price` 取加权平均而不是任意一条：推导端算的是 `price × (ratio − 1)`，把两笔配股合成
    一笔时只有按新增股数加权才能保住那个乘积的总和。
    """
    merged: dict[tuple[object, object], dict[str, object]] = {}
    for row in frame.iter_rows(named=True):
        key = (row["ex_date"], row["kind"])
        left = merged.get(key)
        if left is None:
            merged[key] = dict(row)
            continue
        _accumulate(left, row)
    rows = list(merged.values())
    if len(rows) == frame.height:
        return frame
    schema = {column: frame.schema[column] for column in ACTION_COLUMNS}
    return pl.DataFrame(rows, schema=schema).select(ACTION_COLUMNS)


def _accumulate(left: dict[str, object], row: dict[str, object]) -> None:
    """把 `row` 这一笔并进已经存在的 `left`。"""

    def number(value: object) -> float:
        return 0.0 if value is None else float(value)  # type: ignore[arg-type]

    def added_shares(value: object) -> float:
        # `ratio` 为空是「这一笔不动股数」，不是乘数 1.0 更不是 −1 回收
        return number(value) - 1 if value is not None else 0.0

    cash = number(left["cash_per_share"]) + number(row["cash_per_share"])
    extra = added_shares(left["ratio"]) + added_shares(row["ratio"])
    # 配股付的钱 = 每股新股的价钱 × 新股数；缩股不付钱，所以只数正向的那部分
    paid = (
        number(left["price"]) * max(added_shares(left["ratio"]), 0.0)
        + number(row["price"]) * max(added_shares(row["ratio"]), 0.0)
    )
    left["cash_per_share"] = cash if cash else None
    left["ratio"] = 1.0 + extra if extra else None
    if extra:
        left["price"] = paid / extra
    elif left["price"] is None:
        left["price"] = row["price"]
    notes = [n for n in (left.get("note"), row.get("note")) if n]
    left["note"] = "；".join(dict.fromkeys(notes)) if notes else None
