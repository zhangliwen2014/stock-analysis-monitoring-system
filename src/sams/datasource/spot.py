"""全市场快照的纯函数：字段位置、A 股 symbol 前缀、解析器、空帧与探针（D186/D192）。

东财三个 `*_spot_em` 在本机全不通（实测 RemoteDisconnected），腾讯的批量报价是唯一
还活着的通道：GBK 的 JS 赋值语句，一条请求实测最多 880 个代码（1000 个回 414），
所以 `SPOT_BATCH = 800`。

字段位置是按四只票（茅台、平安银行、腾讯、苹果）交叉核对的。港美股行的位置与 A 股
不同，所以三个市场各有一张表（D188/D226）：港股 57/58 才是 PE(TTM)/PB，美股 39 才是
PE(TTM) 且 PB 没有可核对的位置。拿不准的格子留 None 并拒绝注册那一列——比出一个假数便宜。

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
from sams.datasource.base import a_share_symbol
from sams.datasource.quote import echo_matches, field_float, parse_moment

#: 与 `tencent._TENCENT_LINE` 同一形状。那边的是私有名，跨模块引用私有名比多三行正则更糟
_LINE = re.compile(r'v_([^=]+)="([^"]*)"')

#: 一条请求最多带多少个代码（实测边界：880 可、1000 回 414）
SPOT_BATCH = 800

#: 三个市场的时间格式各不相同：A 股没有分隔符，港股是斜杠，美股是连字符
_TS_FORMATS = {
    "A": ("%Y%m%d%H%M%S",),
    "HK": ("%Y/%m/%d %H:%M:%S",),
    "US": ("%Y-%m-%d %H:%M:%S",),
}

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
#: 港股行的位置与 A 股同名不同位：第 39 格在 A 股是 PE(TTM)，在这里是 PE(动)；
#: 港股 57/58 才是 PE(TTM)/PB。44/45 同样是流通/总市值（00386 两者相差五倍，
#: 拿 44 当总市值会把中石化的市值写成 1127 亿）。换手、量比、PE(动) 在港股行里
#: 没有已核对的格子，留空而不是拿 49 格的 52 周最低价顶上。
SPOT_INDEX_HK = {
    "name": 1,
    "price": 3,
    "pct_chg": 32,
    "amount": 37,
    "amplitude": 43,
    "float_cap": 44,
    "market_cap": 45,
    "pe_ttm": 57,
    "pb": 58,
}
SPOT_INDEX_BY_MARKET = {"A": SPOT_INDEX, "HK": SPOT_INDEX_HK}
#: 美股行的位置（D226）：39 是 PE(TTM)，37 是成交额，44/45 是流通/总市值。
#: 与 AAPL/MSFT/TSLA/BABA 四只票对过新浪：振幅=(33−34)/4、PE=3÷47 的 EPS、
#: 44/45 单位同为亿。PB 在美股行里没有可交叉核对的位置，不注册——宁可少一格。
SPOT_INDEX_US = {
    "name": 1,
    "price": 3,
    "pct_chg": 32,
    "amount": 37,
    "amplitude": 43,
    "float_cap": 44,
    "market_cap": 45,
    "pe_ttm": 39,
}
SPOT_INDEX_BY_MARKET["US"] = SPOT_INDEX_US
#: 行至少要有这么多格：A 股最后一格是 pe_dyn(52)，港股是 pb(58)，美股是总市值(45)
SPOT_MIN_FIELDS = {"A": 53, "HK": 59, "US": 46}
#: A 股成交额藏在第 35 个复合字段（价/量/额）的第三段，单位元；港股是第 37 格单个数
_AMOUNT_SLOT = 35

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
    """本系统代码 → 腾讯快照 symbol（`sh600519` / `sz000001` / `bj920992` / `hk00700` / `usAAPL`）。

    A 股的前缀直接取 `a_share_symbol`：把 920xxx 发成 `sz` 会拿到空行，症状是
    「代码表里 353 只票整批消失」而不是一条报错（D186）。号段只写在公共件那一次，
    这里再写一遍就会跟日线那份漂开。
    美股只加 `us` 前缀、保留点号：`usBRK.B` 有行，`usBRK-B` 没有（D226）。
    """
    if market not in ("A", "HK", "US"):
        raise ValueError(f"腾讯快照只供 A/HK/US，收到 {market}")
    symbol = canonical_code(market, code)
    if market == "HK":
        return f"hk{symbol}"
    if market == "US":
        return f"us{symbol}"
    return a_share_symbol(symbol)


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
    index = SPOT_INDEX_BY_MARKET[market]
    minimum = SPOT_MIN_FIELDS[market]
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
        if len(row) < minimum:
            raise FieldDrift(
                "spot",
                f"{market} 快照行只有 {len(row)} 格（要 {minimum}）：字段位置变了，"
                f"{canonical} 这批不写库",
            )
        moment = parse_moment(row[30], _TS_FORMATS[market], zone)
        name = row[index["name"]].strip()
        amount = (
            field_float(row[_AMOUNT_SLOT].split("/"), 2)
            if market == "A"
            else field_float(row, index["amount"])
        )
        rows.append(
            {
                "market": market,
                "code": canonical,
                "name": name or None,
                "quoted_on": moment.astimezone(zone).date() if moment else None,
                "price": field_float(row, index["price"]),
                "pct_chg": field_float(row, index["pct_chg"]),
                "amount": amount,
                # 港股与美股行里这三格没有已核对的位置：None 是「不知道」，不是 0
                "turnover_rate": field_float(row, index["turnover_rate"])
                if "turnover_rate" in index
                else None,
                "volume_ratio": field_float(row, index["volume_ratio"])
                if "volume_ratio" in index
                else None,
                "amplitude": field_float(row, index["amplitude"]),
                "pe_ttm": field_float(row, index["pe_ttm"]),
                "pe_dyn": field_float(row, index["pe_dyn"]) if "pe_dyn" in index else None,
                "pb": field_float(row, index["pb"]) if "pb" in index else None,
                "market_cap": field_float(row, index["market_cap"]),
                "float_cap": field_float(row, index["float_cap"]),
                # A 股没有独立的 ST 标记列：名字里的 ST 就是交易所的口径；港股没有 ST
                "is_st": "ST" in name if market == "A" else False,
                "source": "tencent",
            }
        )
    if not rows:
        raise EmptyResponse("spot", f"{market} 快照里一行都没对上请求的代码")
    return pl.DataFrame(rows, schema=SPOT_SCHEMA)
