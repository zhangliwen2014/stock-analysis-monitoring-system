"""全市场快照的纯函数：字段位置、A 股 symbol 前缀、解析器、空帧与探针（D186/D192）。

东财三个 `*_spot_em` 在本机全不通（实测 RemoteDisconnected），腾讯的批量报价是唯一
还活着的通道：GBK 的 JS 赋值语句，一条请求实测最多 880 个代码（1000 个回 414），
所以 `SPOT_BATCH = 800`。

字段位置是按四只票（茅台、平安银行、腾讯、苹果）交叉核对的。港美股行的位置与 A 股不同、
且对不上已知财报，所以这一层不注册它们（D188）——拿不准的口径不出数，比出一个假数便宜。

这里只放纯函数（与 `quote.py`/`minute.py` 同一位置）：适配器方法在 `tencent.py` 上，
因为腾讯已经在 A 股的降级链里、报价通道与限速全是现成的（D192）。
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.quote import echo_matches, field_float, parse_moment

#: 与 `tencent._TENCENT_LINE` 同一形状。那边的是私有名，跨模块引用私有名比多三行正则更糟
_LINE = re.compile(r'v_([^=]+)="([^"]*)"')

#: 一条请求最多带多少个代码（实测边界：880 可、1000 回 414）
SPOT_BATCH = 800

#: 三个市场的时间格式各不相同；本期只注册 A 股，其余留在这里等实测（D188）
_TS_FORMATS = {"A": ("%Y%m%d%H%M%S",)}

#: A 股行里按位置取的列。键是工作帧列名，值是**接口行**的下标
SPOT_INDEX = {
    "name": 1,
    "price": 3,
    "pct_chg": 32,
    "turnover_rate": 38,
    "pe_ttm": 39,
    "amplitude": 43,
    "float_cap": 44,
    "market_cap": 45,
    "pb": 46,
    "volume_ratio": 49,
    "pe_dyn": 52,
}
#: 行至少要有这么多格：最后一格是 pe_dyn(52)
SPOT_MIN_FIELDS = 53
#: A 股成交额藏在第 35 个复合字段（价/量/额）的第三段，单位元
_AMOUNT_SLOT = 35
#: 北交所的代码形态：腾讯只认 `bj` 前缀
_BJ_PREFIX = "920"
_SH_FIRST = "569"

SPOT_COLUMNS = [
    "market",
    "code",
    "name",
    "quoted_on",
    "price",
    "pct_chg",
    "amount",
    "turnover_rate",
    "volume_ratio",
    "amplitude",
    "pe_ttm",
    "pe_dyn",
    "pb",
    "market_cap",
    "float_cap",
    "is_st",
    "source",
]
SPOT_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "name": pl.Utf8,
    "quoted_on": pl.Date,
    "price": pl.Float64,
    "pct_chg": pl.Float64,
    "amount": pl.Float64,
    "turnover_rate": pl.Float64,
    "volume_ratio": pl.Float64,
    "amplitude": pl.Float64,
    "pe_ttm": pl.Float64,
    "pe_dyn": pl.Float64,
    "pb": pl.Float64,
    "market_cap": pl.Float64,
    "float_cap": pl.Float64,
    "is_st": pl.Boolean,
    "source": pl.Utf8,
}


def has_market_snapshot(obj: Any) -> bool:
    return callable(getattr(obj, "market_snapshot", None))


def empty_snapshot() -> pl.DataFrame:
    return pl.DataFrame(
        {name: pl.Series(name, [], dtype=SPOT_SCHEMA[name]) for name in SPOT_COLUMNS}
    )


def spot_symbol(market: str, code: str) -> str:
    """本系统代码 → 腾讯快照 symbol（`sh600519` / `sz000001` / `bj920992`）。

    北交所走 `bj`：公共的 `a_share_symbol` 只认 sh/sz，把 920xxx 发成 `sz` 会拿到空行，
    症状是「代码表里 353 只票整批消失」而不是一条报错（D186）。
    """
    if market != "A":
        raise ValueError(f"腾讯快照只供 A 股，收到 {market}")
    symbol = canonical_code(market, code)
    if symbol.startswith(_BJ_PREFIX):
        return f"bj{symbol}"
    return f"{'sh' if symbol[0] in _SH_FIRST else 'sz'}{symbol}"


def parse_spot(
    text: str, *, market: str, codes: list[str], zone: ZoneInfo
) -> pl.DataFrame:
    """腾讯批量响应 → 快照帧。行短于 `SPOT_MIN_FIELDS` 当场 `FieldDrift`（D185）。

    缺列读成 null 的后果是那一列在全市场无一例外变成「无数」，而屏幕上写的是
    「这一条没票通过」——所以宁可整批失败，让采集 job 出声。
    """
    if not (text or "").strip():
        raise EmptyResponse("spot", f"{market} 快照响应为空")
    parsed: list[list[str]] = []
    for raw in text.splitlines():
        match = _LINE.search(raw)
        if match is not None and match.group(2).strip():
            parsed.append(match.group(2).split("~"))
    if not parsed:
        raise EmptyResponse("spot", f"{market} 快照响应里没有可读的行")

    rows: list[dict] = []
    for code in codes:
        canonical = canonical_code(market, code)
        row = next(
            (
                fields
                for fields in parsed
                if len(fields) > 2 and echo_matches(market, canonical, fields[2])
            ),
            None,
        )
        if row is None:
            continue
        if len(row) < SPOT_MIN_FIELDS:
            raise FieldDrift(
                "spot",
                f"{market} 快照行只有 {len(row)} 格（要 {SPOT_MIN_FIELDS}）：字段位置变了，"
                f"{canonical} 这批不写库",
            )
        moment = parse_moment(row[30], _TS_FORMATS[market], zone)
        name = row[SPOT_INDEX["name"]].strip()
        rows.append(
            {
                "market": market,
                "code": canonical,
                "name": name or None,
                "quoted_on": moment.astimezone(zone).date() if moment else None,
                "price": field_float(row, SPOT_INDEX["price"]),
                "pct_chg": field_float(row, SPOT_INDEX["pct_chg"]),
                "amount": field_float(row[_AMOUNT_SLOT].split("/"), 2),
                "turnover_rate": field_float(row, SPOT_INDEX["turnover_rate"]),
                "volume_ratio": field_float(row, SPOT_INDEX["volume_ratio"]),
                "amplitude": field_float(row, SPOT_INDEX["amplitude"]),
                "pe_ttm": field_float(row, SPOT_INDEX["pe_ttm"]),
                "pe_dyn": field_float(row, SPOT_INDEX["pe_dyn"]),
                "pb": field_float(row, SPOT_INDEX["pb"]),
                "market_cap": field_float(row, SPOT_INDEX["market_cap"]),
                "float_cap": field_float(row, SPOT_INDEX["float_cap"]),
                # A 股没有独立的 ST 标记列：名字里的 ST 就是交易所的口径
                "is_st": "ST" in name,
                "source": "tencent",
            }
        )
    if not rows:
        raise EmptyResponse("spot", f"{market} 快照里一行都没对上请求的代码")
    return pl.DataFrame(rows, schema=SPOT_SCHEMA)
