"""A 股公司行为：东财「分红送配详情」→ 标准列。纯解析，网络调用留在适配器里。

三条口径是这个文件存在的全部理由，每一条错了都不会报错：

1. **单位是「每 10 股」**。`10派2.26元` 是每股 2.26 元吗？不是，是每 10 股 2.26 元
   （神华 2024 年真实值：10派22.60元 ⇒ 每股 2.26 元）。直接当每股用，因子会大一个数量级，
   而价格序列照样是单调的、图上看不出问题。所以每条派息都拿描述列
   （`现金分红-现金分红比例描述`，实测形如「10派22.60元(含税,扣税后20.34元)」）反查一次，
   数字与文本对不上就整批失败。
2. **同一行里派息与送转是两个独立事实**，必须拆成多行：`corp_actions` 的主键是
   (market, code, ex_date, kind)，塞成一行只能选一种语义（D34）。
3. **只有「实施分配」才是发生过的事实**。预披露/董事会决议通过/股东大会决议通过的行
   带不了除权日（实测 2026-06-30 报告期：760 行实施分配全部有除权日，166+68+49 行
   未实施的没有），但「以后可能除权」不是「已经除权」——把没发生的除权乘进序列，
   比漏一次更难被发现。未实施的行跳过并计数告警，等它变成实施的那次同步再收。

配股（rights）不在这个接口里，东财另有全市场配股表，但它是按**位置**重命名列的
（akshare 里 `big_df.columns = [...]`），实测有行的「配股比例/配股价」与当日价格对不上。
宁可显式缺失：漏记的配股会被 D37 的证据守卫抓住（跳变超过该板块最宽涨跌幅 ⇒ 拒绝出数），
而不是被一个错数顶掉。spec §18 记为已知缺口。
"""

from __future__ import annotations

import logging
import re

import polars as pl

from sams.core.errors import FieldDrift

logger = logging.getLogger("sams.datasource")

SOURCE = "akshare"
#: 只有这一种进度算「已发生」。其余进度见模块文档
IMPLEMENTED = "实施分配"
#: 源口径是「每 10 股」，标准列口径是「每股」（D34）
_PER_SHARE = 10.0

_CASH = "现金分红-现金分红比例"
_CASH_DESC = "现金分红-现金分红比例描述"
_BONUS = "送转股份-送股比例"
_TRANSFER = "送转股份-转股比例"
_COMBINED = "送转股份-送转总比例"
_PROGRESS = "方案进度"
_EX_DATE = "除权除息日"
_REPORT_DATE = "报告期"

#: 描述列里派息的写法：金额跟在「派」与「元」之间。实测两种开头都会出现——
#: 「10派22.60元(含税,扣税后20.34元)」与「10转2.00派0.60元(含税,扣税后0.54元)」，
#: 所以锚点取「派…元」而不是「10派」：后者在带转增的行上整列匹配不上，会把好数据判成改版。
#: 取的是**税前**金额——交易所的除权参考价按税前派息计算，用扣税后会把参考价算高。
_CASH_IN_DESC = re.compile(r"派\s*([0-9]+(?:\.[0-9]+)?)\s*元")
#: 送/转的写法多变（`10送3转增2股`、`10转2.00派0.60元`），只做「解析出来了就得一致」
#: 的反查，解析不出来不失败：文本格式不是数值口径的载体
_BONUS_IN_DESC = re.compile(r"送\s*([0-9]+(?:\.[0-9]+)?)")
_TRANSFER_IN_DESC = re.compile(r"转(?:增)?\s*([0-9]+(?:\.[0-9]+)?)")

#: 描述与数值允许的分位差：源把金额统一格式化到分，四舍五入会造成半分以内的偏差
_TOLERANCE = 0.005


def required_columns(frame: pl.DataFrame) -> None:
    """必需的列整列消失 = 源改版，当场失败。

    这些都是按位置重命名出来的列（`big_df.columns = [...]`），接口换字段顺序时它们会
    静默错位：`除权除息日` 里装着「每股收益」，日期解析失败 → 整只票「没有除权记录」。
    """
    needed = (_EX_DATE, _PROGRESS, _CASH, _CASH_DESC, _COMBINED)
    absent = [c for c in needed if c not in frame.columns]
    if absent:
        raise FieldDrift(SOURCE, f"分红送配响应缺少必需列 {absent}，实际列 {list(frame.columns)}")


def _number(value: object) -> float | None:
    """NaN/None/空串都算「源没给这个数」，不是 0。"""
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN != NaN


def _per_share(per_ten: float) -> float:
    """每 10 股 → 每股。除以 10 在二进制下不闭合：22.6/10 = 2.2600000000000002，
    这个尾巴会一路乘进后复权序列，而源的精度本来只到分。取 10 位小数远小于任何
    有意义的金额差，却能让同一条记录每次算出同一个数。"""
    return round(per_ten / _PER_SHARE, 10)


def _desc_amount(text: str | None, pattern: re.Pattern[str]) -> float | None:
    if not text:
        return None
    match = pattern.search(text)
    return None if match is None else float(match.group(1))


def expand_fhps(raw: pl.DataFrame) -> pl.DataFrame:
    """东财分红送配明细 → 标准公司行为列（一事件一行，单位已换成每股）。

    输出还没有 market/code/src，那是 `normalize_actions` 的事：这里只做「一行方案 →
    若干条事实」的展开与单位换算，市场归属留给采集边界，两层各自可测。
    """
    if raw.is_empty():
        return pl.DataFrame(
            schema={
                "ex_date": pl.Date,
                "kind": pl.Utf8,
                "cash_per_share": pl.Float64,
                "ratio": pl.Float64,
                "price": pl.Float64,
                "note": pl.Utf8,
            }
        )
    required_columns(raw)

    rows: list[dict] = []
    skipped: dict[str, int] = {}
    for row in raw.iter_rows(named=True):
        progress = (row.get(_PROGRESS) or "").strip()
        if progress != IMPLEMENTED:
            skipped[progress or "(空)"] = skipped.get(progress, 0) + 1
            continue
        ex_date = row.get(_EX_DATE)
        if ex_date is None:
            # 进度说已实施、却没有除权日：这两件事不可能同时为真，按改版处理而不是猜一天
            raise FieldDrift(SOURCE, f"{_PROGRESS}={IMPLEMENTED} 但 {_EX_DATE} 为空")
        note = _note(row)
        cash = _number(row.get(_CASH))
        description = row.get(_CASH_DESC)
        if cash is not None and cash > 0:
            if description and "每股" in description:
                raise FieldDrift(
                    SOURCE,
                    f"除权日 {ex_date} 的描述写的是每股口径（{description!r}），与列名给的"
                    "「每10股」矛盾：单位换算的反查依据本身变了，继续算会把因子差十倍",
                )
            described = _desc_amount(description, _CASH_IN_DESC)
            if described is None:
                raise FieldDrift(
                    SOURCE,
                    f"除权日 {ex_date} 的派息 {_CASH}={cash} 无法与描述列核对（描述为 "
                    f"{description!r}）：每10股→每股的换算失去交叉验证，不敢乘进因子",
                )
            if abs(described - cash) > _TOLERANCE:
                raise FieldDrift(
                    SOURCE,
                    f"除权日 {ex_date} 的派息数值 {cash} 与描述里的 {described} 不一致，"
                    "按源侧口径漂移处理",
                )
            rows.append(
                {
                    "ex_date": ex_date,
                    "kind": "dividend",
                    "cash_per_share": _per_share(cash),
                    "ratio": None,
                    "price": None,
                    "note": note,
                }
            )

        bonus = _number(row.get(_BONUS))
        transfer = _number(row.get(_TRANSFER))
        combined = _number(row.get(_COMBINED))
        for per_share, kind in _share_parts(
            bonus, transfer, combined, ex_date=ex_date, description=row.get(_CASH_DESC)
        ):
            rows.append(
                {
                    "ex_date": ex_date,
                    "kind": kind,
                    "cash_per_share": None,
                    "ratio": 1.0 + _per_share(per_share),
                    "price": None,
                    "note": note,
                }
            )

    if skipped:
        logger.warning(
            "跳过未实施的公司行为 %d 行（进度 %s），实施后下次同步会收到",
            sum(skipped.values()),
            skipped,
        )
    out = pl.DataFrame(
        rows,
        schema={
            "ex_date": pl.Date,
            "kind": pl.Utf8,
            "cash_per_share": pl.Float64,
            "ratio": pl.Float64,
            "price": pl.Float64,
            "note": pl.Utf8,
        },
    )
    return out.sort(["ex_date", "kind"])


def _share_parts(
    bonus: float | None,
    transfer: float | None,
    combined: float | None,
    *,
    ex_date,
    description: str | None,
) -> list[tuple[float, str]]:
    """每 10 股新增的股数，按送股/转增分别成一条。

    分成两条不是为了分类好看：主键含 kind，同日两条在推导因子时相加（D34），
    合成一条就只能选一种语义，而 `ratio` 这一列一旦同时装「送几股」和「乘数」，
    因子公式无论怎么写都有一半的行是错的。
    """
    parts = [(value, kind) for value, kind in ((bonus, "bonus"), (transfer, "transfer")) if value]
    if not parts:
        # 只给总数、没区分送/转：两者对价格因子的作用完全相同，按 bonus 记录，
        # note 里留着源自己的描述供人核对。把它当成 0 会让因子少乘一次
        return [] if not combined else [(combined, "bonus")]
    total = sum(value for value, _ in parts)
    if combined and abs(total - combined) > _TOLERANCE:
        raise FieldDrift(
            SOURCE,
            f"除权日 {ex_date} 的送转总比例 {combined} 与送股 {bonus}+转增 {transfer} 不等",
        )
    described = _desc_amount(description, _BONUS_IN_DESC)
    if bonus and described is not None and abs(bonus - described) > _TOLERANCE:
        raise FieldDrift(
            SOURCE, f"除权日 {ex_date} 的送股比例 {bonus} 与描述里的 {described} 不一致"
        )
    return parts


def _note(row) -> str:
    parts = []
    if row.get(_REPORT_DATE):
        parts.append(f"报告期 {row[_REPORT_DATE]}")
    if row.get(_CASH_DESC):
        parts.append(str(row[_CASH_DESC]))
    return "；".join(parts) or IMPLEMENTED
