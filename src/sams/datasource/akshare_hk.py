"""港股公司行为：东财「港股分红派息」→ 标准列。纯解析，网络调用留在适配器里。

这个源只有公司行为与代码表两种能力，没有日线（分层上它是独立的一个 `@source`，不挂到
akshare A 股适配器上：`markets` 一旦并集，HK 的日线链就会多出一个只会失败的成员）。

三条口径，每条错了都不会报错：

1. **金额必须是港币**。`分红方案` 常写成「每股派美元0.1元(相当于港币0.784088元)」，
   而 K 线是港币报价：拿美元数去除港币价格，因子会错一个汇率倍数，跨那天的涨跌看着正常。
   没有港币等价的非港币派息 ⇒ 折不出数，按 D39 记 `unknown`（不猜汇率）。
2. **「派送」的宾语可能是权证而不是股票**。实测 00016 有「每12股派送1股红利认股权证」——
   那是行权价另算的期权，不是白送的股。乘进股数乘数会把不该有的除权算进去，
   所以带「权证」字样的段一律记 `unknown`。
3. **认不出来的段落一律记 `unknown` 而不是丢掉**（D39）。港股没有涨跌停证据规则（D37 只对
   A 股生效），漏一次除权在图上什么都不留下；留下一条 `unknown` 会让跨那天的区间收益拒绝
   出数——拒绝是可恢复的，错数是不可恢复的。

`除净日` 为空的行（预案未定）由 `normalize_actions` 计数后丢弃，那是源里的常态噪声。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.actions import empty_actions, normalize_actions
from sams.datasource.base import classify_error, to_date_expr
from sams.datasource.sources import source
from sams.datasource.symbol_list import normalize_symbol_list

logger = logging.getLogger("sams.datasource")

#: 本表按代码取全历史，接口不带日期参数（所以适配器标 `corp_actions_full_history`）
_EX_DATE = "除净日"
_SCHEME = "分红方案"
_TYPE = "分配类型"
#: 币种出现在「每股派〈币种〉〈金额〉元」的间隙里，实测有 港币 / 美元 / 人民币 三种
_CASH = re.compile(r"每股派\s*(.*?)\s*([0-9]+(?:\.[0-9]+)?)\s*元")
_HKD_EQUIV = re.compile(r"相当于港币\s*([0-9]+(?:\.[0-9]+)?)\s*元")
#: 「每10股派送8股」是真送股；分子是每 N 股送 M 股，乘数 = 1 + M/N
_BONUS = re.compile(r"每\s*([0-9]+(?:\.[0-9]+)?)\s*股派送\s*([0-9]+(?:\.[0-9]+)?)\s*股")
_TRANSFER = re.compile(r"每\s*([0-9]+(?:\.[0-9]+)?)\s*股转(?:增)?\s*([0-9]+(?:\.[0-9]+)?)\s*股")
_WARRANT_HINTS = ("权证", "认股权")
#: 一个方案里多个事实用逗号分隔（实测 ASCII 与全角都出现）
_SEGMENTS = re.compile(r"[，,]")


def required_columns(frame: pl.DataFrame) -> None:
    """缺列就是改版：这个接口是 akshare 按位置重命名的，列名会变且不会报错。"""
    absent = [c for c in (_EX_DATE, _SCHEME) if c not in frame.columns]
    if absent:
        raise FieldDrift(
            "akshare_hk", f"港股分红响应缺少必需列 {absent}，实际列 {list(frame.columns)}"
        )


def _per_share(amount: float, base: float) -> float:
    """每 base 股送/转 amount 股 ⇒ 股数乘数。舍入到 10 位：1+8/10 不该写成 1.8000000000000002"""
    return round(1.0 + amount / base, 10)


def parse_scheme(text: str | None) -> list[tuple[str, dict[str, object]]]:
    """一条 `分红方案` → 若干个 (kind, 数值) 事实。

    返回空列表表示「这段文本里没有任何可折数的东西」，调用方据此记 `unknown`。
    """
    if not (text or "").strip():
        return []
    out: list[tuple[str, dict[str, object]]] = []
    for segment in (s.strip() for s in _SEGMENTS.split(text)):
        if not segment:
            continue
        if any(hint in segment for hint in _WARRANT_HINTS):
            # 认股权证/以股代息以外的权证：它不是股数也不是现金，不能假装算得出来
            out.append(("unknown", {"note": segment}))
            continue
        recognized = False
        cash = _CASH.search(segment)
        if cash:
            currency, amount = cash.group(1).strip(), float(cash.group(2))
            hkd = _HKD_EQUIV.search(segment)
            if hkd:
                out.append(("dividend", {"cash_per_share": float(hkd.group(1)), "note": segment}))
            elif "港币" in currency:
                out.append(("dividend", {"cash_per_share": amount, "note": segment}))
            else:
                # 「每股派美元0.1元」没有港币等价：本系统不存汇率，折不出这一笔
                out.append(("unknown", {"note": f"{segment}（无港币等价）"}))
            recognized = True
        bonus = _BONUS.search(segment)
        if bonus:
            base, given = float(bonus.group(1)), float(bonus.group(2))
            if base and given:
                out.append(("bonus", {"ratio": _per_share(given, base), "note": segment}))
        transfer = _TRANSFER.search(segment)
        if transfer:
            base, converted = float(transfer.group(1)), float(transfer.group(2))
            if base and converted:
                out.append(("transfer", {"ratio": _per_share(converted, base), "note": segment}))
        if not recognized and not bonus and not transfer:
            out.append(("unknown", {"note": segment}))
    return out


def expand_payout(raw: pl.DataFrame) -> pl.DataFrame:
    """逐条方案 → 标准形状（同一天的同类事实合并成一条，note 用 `/` 串起原文）。"""
    required_columns(raw)
    has_type = _TYPE in raw.columns
    merged: dict[tuple[object, str], dict[str, object]] = {}
    blank = 0
    for row in raw.iter_rows(named=True):
        facts = parse_scheme(row.get(_SCHEME))
        if not facts:
            # 方案文本本身是空的：那不是一个事件，是源里的一行占位
            blank += 1
            continue
        for kind, values in facts:
            key = (row[_EX_DATE], kind)
            slot = merged.setdefault(
                key,
                {
                    "ex_date": row[_EX_DATE],
                    "kind": kind,
                    "cash_per_share": None,
                    "ratio": None,
                    "price": None,
                },
            )
            note = "｜".join(
                part
                for part in (
                    (str(row[_TYPE]).strip() if has_type and row.get(_TYPE) else ""),
                    str(values.get("note") or "").strip(),
                )
                if part
            )
            slot["note"] = f'{slot["note"]} / {note}' if slot.get("note") else note
            if kind == "dividend":
                slot["cash_per_share"] = round(
                    (slot["cash_per_share"] or 0.0) + float(values["cash_per_share"]), 10
                )
            elif kind in ("bonus", "transfer"):
                # 同日两笔送转：按「新增比例相加」合并乘数（D34），不是两个乘数相乘
                slot["ratio"] = round((slot["ratio"] or 1.0) + float(values["ratio"]) - 1.0, 10)
    if blank:
        logger.warning("港股分红：%d 行的分红方案为空，按占位行丢弃", blank)
    if not merged:
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
    rows = sorted(merged.values(), key=lambda r: (str(r["ex_date"]), r["kind"]))
    return pl.DataFrame(rows).with_columns(to_date_expr(pl.col("ex_date")))


def akshare_hk_payout(symbol: str) -> pl.DataFrame:
    """真实网络调用：某只港股的分红派息全历史。"""
    import akshare as ak

    frame = ak.stock_hk_dividend_payout_em(symbol=symbol)
    if frame is None or len(frame) == 0:
        # 一次都没派息是一家真实存在的公司的正常状态（在韩/内房股里常见），不是故障
        return pl.DataFrame()
    return pl.from_pandas(frame)


def akshare_hk_symbol_list() -> pl.DataFrame:
    """真实网络调用：港股实时快照，全市场代码表只取它的 代码/名称 两列（D148）。"""
    import akshare as ak

    frame = ak.stock_hk_spot_em()
    if frame is None or len(frame) == 0:
        raise EmptyResponse("akshare_hk", "港股代码表返回空")
    return pl.from_pandas(frame)


def _to_polars(frame) -> pl.DataFrame:
    return frame if isinstance(frame, pl.DataFrame) else pl.from_pandas(frame)


class AkshareHkAdapter:
    name = "akshare_hk"
    markets = frozenset({"HK"})
    corp_actions_full_history = True

    def __init__(
        self,
        payout_fetch: Callable[[str], pl.DataFrame] = akshare_hk_payout,
        list_fetch: Callable[[], pl.DataFrame] = akshare_hk_symbol_list,
    ) -> None:
        self._payout_fetch = payout_fetch
        self._list_fetch = list_fetch

    def corp_actions(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        symbol = canonical_code(market, code)
        try:
            expanded = expand_payout(_to_polars(self._payout_fetch(symbol)))
            if expanded.is_empty():
                return empty_actions()
            # 全历史源不按窗口切片：切掉的那些行是源已经给到的事实，而服务层照样会写
            # 「确认至今天的全历史覆盖」——库与覆盖标记说反话时，守卫就会放行裸价
            actions = normalize_actions(expanded, market=market, code=code, source=self.name)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return actions

    def symbol_list(self, market: str) -> pl.DataFrame:
        """港股全市场代码表（东财港股快照的 代码/名称 列，D148）。

        非 HK 抛 `EmptyResponse` 而不是 `ValueError`：这句是答复不是故障，
        降级链要继续问下一个源，健康表也不该因此记一笔。
        """
        if market not in self.markets:
            raise EmptyResponse(self.name, f"港股代码表不覆盖 {market} 市场")
        try:
            return normalize_symbol_list("HK", _to_polars(self._list_fetch()))
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


source("akshare_hk", note="东财港股分红派息 + 港股代码表（没有日线）")(AkshareHkAdapter)
