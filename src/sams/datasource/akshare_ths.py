"""A 股公司行为的第二个源：同花顺「分红送配」→ 标准列。纯解析，网络调用留在适配器里。

为什么要有这个文件（交易员试用报告 P1）：东财那张 `RPT_SHAREBONUS_DET` 对 002450、600636
这类票直接回「返回数据为空」，而同花顺同一只票有 8~28 条除权记录。A 股此前是单源，
源说「没有」就是没有——于是这些票的覆盖标记永远给不出来，跨除权日的区间一路拒绝或退回裸价。

同花顺与东财的口径差别决定了解析的形状：

1. **它只给一句方案说明**，没有数值列可以反查。所以样式必须逐个实测过（用例里的文本全是
   真实响应里抄的），认不出来的一律按 D39 记 `unknown`，而不是按「大概是每 10 股」猜一个数。
2. **基数写在文本里**（`10派1.8元`、`20派2元`、`每10股派1.5元`），换算按写出来的基数除，
   不把 10 当常量——除以错误的基数会让因子差一倍而序列照样单调。
3. **含税与扣税后写在同一个括号里**（`10派22.60元(含税,扣税后20.34元)`）：取**税前**。
   交易所的除权参考价按税前派息计算，取扣税后会把参考价算低、因子算高。
4. **AB 股同一家公司的方案分行写**（`A股10送3.5股派1.5元,B股10送2.426股派1.04元`）：
   取标了 A股 的那段。取错段乘进去的是一个差 30% 的比例，两个数都看着正常。
5. **只有「实施」进度是发生过的事实**（实测取值：实施方案 284 / 董事会预案 228 /
   股东大会预案 57 / 取消分红 1）。

配股不在这个接口里（与东财那张一样，见 spec §18 U16），`price` 列恒空。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, FieldDrift
from sams.datasource.actions import empty_actions, normalize_actions
from sams.datasource.base import classify_error
from sams.datasource.sources import source

logger = logging.getLogger("sams.datasource")

_EX_DATE = "A股除权除息日"
_SCHEME = "分红方案说明"
_PROGRESS = "方案进度"
_REPORT_DATE = "报告期"
_REQUIRED = (_EX_DATE, _PROGRESS, _SCHEME)

#: 同花顺的进度写法与东财不同（实施方案 vs 实施分配），共同点是都得含「实施」才算已发生
_IMPLEMENTED = re.compile(r"实施")
#: 「不分配不转增」是一句完整答复，不是一个折不出数的事件：留下 unknown 会让整只票拒绝出数
_NOTHING = re.compile(r"不(分配|派发|转增|送转)")
_MARKET_PREFIX = re.compile(r"^([ABH])股")
#: 基数：`10派…` / `10股派…` / `每10股派…` / `20派…`
_BASE = re.compile(r"^每?\s*(\d+(?:\.\d+)?)\s*股?")
_CASH = re.compile(r"(?:派|分红)\s*(\d+(?:\.\d+)?)\s*元")
_BONUS = re.compile(r"送\s*(\d+(?:\.\d+)?)\s*股?")
_TRANSFER = re.compile(r"转\s*增?\s*(\d+(?:\.\d+)?)\s*股?")

_SCHEMA = {
    "ex_date": pl.Date,
    "kind": pl.Utf8,
    "cash_per_share": pl.Float64,
    "ratio": pl.Float64,
    "price": pl.Float64,
    "note": pl.Utf8,
}


def required_columns(frame: pl.DataFrame) -> None:
    needed = [c for c in _REQUIRED if c not in frame.columns]
    if needed:
        raise FieldDrift(
            "akshare_ths", f"分红送配响应缺少必需列 {needed}，实际列 {list(frame.columns)}"
        )


def _number(text: str, pattern: re.Pattern[str]) -> float | None:
    match = pattern.search(text)
    return None if match is None else float(match.group(1))


def _a_share_clause(text: str) -> str | None:
    """取属于 A 股的那一段方案。

    AB 股合写时两段用逗号分开且各带市场前缀；没前缀的段就是这只票自己的方案。
    一段都归不上时返回 None——那说明这张表里这条记录不是给 A 股股东的。
    """
    head = re.split(r"[（(]", text, maxsplit=1)[0].strip()
    clauses = [c.strip() for c in re.split(r"[,，]", head) if c.strip()]
    for clause in clauses:
        prefix = _MARKET_PREFIX.match(clause)
        if prefix is None or prefix.group(1) == "A":
            return _MARKET_PREFIX.sub("", clause, count=1)
    return None


def _per_share(amount: float, base: float) -> float:
    """按文本里写出的基数换算成每股。

    `10派22.60元` 的 22.60 是每 10 股的钱（神华 2024 真实值 ⇒ 每股 2.26）。除以 10 在二进制下
    不闭合，取 10 位小数：远小于任何有意义的金额差，但能让同一条记录每次算出同一个数。
    """
    return round(amount / base, 10)


def parse_scheme(text: str | None) -> list[tuple[str, dict[str, object]]]:
    """一句方案说明 → 若干条事实 `(kind, 数值)`。

    返回空列表表示「这句话里没有公司行为」（不分配不转增、或整段属于 B/H 股）；返回
    `[("unknown", {})]` 表示「这句话说了有一件事，但折不出数」——两者在 D39 上完全不同。
    """
    if not text:
        return [("unknown", {})]
    clause = _a_share_clause(text)
    if clause is None:
        return []
    if _NOTHING.search(clause):
        return []
    base_match = _BASE.match(clause)
    if base_match is None:
        # 没有基数就没法换算：按 10 猜会让因子差一个数量级，而序列照样单调
        return [("unknown", {})]
    base = float(base_match.group(1))
    if not base:
        return [("unknown", {})]

    facts: list[tuple[str, dict[str, object]]] = []
    cash = _number(clause, _CASH)
    if cash:
        facts.append(("dividend", {"cash_per_share": _per_share(cash, base)}))
    for pattern, kind in ((_BONUS, "bonus"), (_TRANSFER, "transfer")):
        added = _number(clause, pattern)
        if added:
            facts.append((kind, {"ratio": 1.0 + _per_share(added, base)}))
    return facts or [("unknown", {})]


def _to_date(value: object) -> date | None:
    """源给的除权日实测有四种形态：`date`、`datetime`、`'2011-06-10'`、pandas 的 `NaT`。

    认不出来就当「没有日期」而不是抛错：这一行会被计数后丢掉（主键需要日期，猜一天
    等于替公司编一次除权），而同花顺表里其余的行照样能用。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10] if value is not None else ""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def expand_ths(raw: pl.DataFrame) -> pl.DataFrame:
    """同花顺分红送配明细 → 标准公司行为列（一事件一行，单位已换成每股）。"""
    if raw.is_empty():
        return pl.DataFrame(schema=_SCHEMA)
    required_columns(raw)

    rows: list[dict] = []
    skipped: dict[str, int] = {}
    undated = 0
    for row in raw.iter_rows(named=True):
        progress = (row.get(_PROGRESS) or "").strip()
        if not _IMPLEMENTED.search(progress):
            skipped[progress or "(空)"] = skipped.get(progress, 0) + 1
            continue
        ex_date = _to_date(row.get(_EX_DATE))
        if ex_date is None:
            # 进度说已实施却没有除权日（实测 600585 有一行）：主键需要日期，猜一天=编一次除权
            undated += 1
            continue
        text = row.get(_SCHEME)
        period = row.get(_REPORT_DATE)
        note = "；".join(part for part in (f"报告期 {period}" if period else None, text) if part)
        for kind, values in parse_scheme(text):
            rows.append(
                {
                    "ex_date": ex_date,
                    "kind": kind,
                    "cash_per_share": values.get("cash_per_share"),
                    "ratio": values.get("ratio"),
                    "price": values.get("price"),
                    "note": note or str(text),
                }
            )

    if skipped:
        logger.warning(
            "同花顺跳过未实施的公司行为 %d 行（进度 %s），实施后下次同步会收到",
            sum(skipped.values()),
            skipped,
        )
    if undated:
        logger.warning("同花顺有 %d 行「实施方案」没有除权日，无法定位到序列上的某一天", undated)
    out = pl.DataFrame(rows, schema=_SCHEMA)
    # 同一方案在两个公告阶段各出现一次时只能算一次：D41 的相加针对的是不同的事实
    out = out.unique(subset=["ex_date", "kind", "cash_per_share", "ratio"], keep="first")
    return out.sort(["ex_date", "kind"])


def akshare_ths_plans(symbol: str) -> pl.DataFrame:
    """真实网络调用：某只 A 股的分红送配**全历史**（同花顺明细页）。

    接口不带日期参数，一次给全史 ⇒ 适配器标 `corp_actions_full_history`（D36/D40）。
    """
    import akshare as ak

    frame = ak.stock_fhps_detail_ths(symbol=symbol)
    if frame is None or len(frame) == 0:
        return pl.DataFrame()
    return pl.from_pandas(frame)


def _to_polars(frame) -> pl.DataFrame:
    return frame if isinstance(frame, pl.DataFrame) else pl.DataFrame(frame)


class AkshareThsAdapter:
    name = "akshare_ths"
    markets = frozenset({"A"})
    #: 只有公司行为能力，没有日线：日线链上它会被显式跳过（D38），不算进备源计数
    corp_actions_full_history = True

    def __init__(self, plans_fetch: Callable[[str], pl.DataFrame] = akshare_ths_plans) -> None:
        self._plans_fetch = plans_fetch

    def corp_actions(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """全史交回去，**不按窗口切**（D40）：切掉的行是源已经给到的事实，而服务层照样会写
        「全历史，确认至今天」——库与覆盖标记说反话时守卫就放行裸价。
        """
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        symbol = canonical_code(market, code)
        try:
            expanded = expand_ths(_to_polars(self._plans_fetch(symbol)))
            if expanded.is_empty():
                return empty_actions()
            actions = normalize_actions(expanded, market=market, code=code, source=self.name)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001 — 边界处统一归类，见 classify_error
            raise classify_error(self.name, exc) from exc
        return actions


source("akshare_ths", note="同花顺 A 股分红送配（只有公司行为，没有日线）")(AkshareThsAdapter)
