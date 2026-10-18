"""日线仓库：幂等 upsert 与读取。

幂等靠 (market, code, date, adjust) 主键 + INSERT OR REPLACE，因此采集重试安全（spec §5.3）。
读取一律带 `adjust = 'raw'`：库里目前只有一种口径，但把口径写进查询而不是靠约定，
是为了让 M2 引入后复权序列时不可能悄悄读到混着两种价的同一根 K 线。

仓库层挡掉的是「存进去就会被当成真数用」的那几类：口径混淆、NaN 覆盖上一根好 K 线、
收盘价为 0（区间跌幅立刻变 −100%）、非正的开高低与 high<low 的不自洽极值（置空，不陪葬整行）。
负成交量/负成交额原样存——那是源侧数据质量问题，属于采集与清洗的职责，
在这里丢弃会让「源给了脏数」和「这一天没数据」在库里长得一样；它由 `sams health` 如实报出来。

`market` 与 `code` 的形态在这里归一（D46 的下沉 + D50）：主键由这一层定义，键的形态就该由
这一层保证。只在服务边界归一是漏的——`WatchlistRepo.add` 过去只归一 code，`market` 原样收下：
写成 `a` 的那一行在清单里看得见，行情按 `A` 查，症状是「这只票没有行情」而库里明明有行。
键的形态不该取决于哪个调用方记得归一。
读侧与写侧同一条规则（`_form`/`_canonical_codes` 都调 `canonical_code`）：各仓库自己
`upper()` 看着一样，实际会让 K 线存 `700`、除权记录存 `00700`——同一只票两个键。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from uuid import uuid4

import polars as pl

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database

logger = logging.getLogger("sams.storage.bars")

REQUIRED = ("market", "code", "date", "close", "source")
#: 入库唯一口径：不复权。后复权序列在读取层由 corp_actions 现算（0003 迁移的注释里写明理由）。
ADJUST = "raw"
COLUMNS = (
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
)

#: 除 close 之外的数值列：出现 NaN/Inf 时置空而不是丢整行——缺成交量不该陪葬一根有效 K 线
_OPTIONAL_NUMERIC = ("open", "high", "low", "volume", "amount")


def _canonical_codes(markets: list[str], codes: list[str]) -> list[str]:
    """按市场归一代码形态：`canonical_code` 是唯一的规则（域规则在 `core/codes.py`）。

    各仓库自己 `upper()` 看着像同一条规则，其实是两条：`700` 归一成 `00700` 是域规则，
    而 `upper()` 留它作 `700`。K 线于是与 `corp_actions`/`watchlists` 用两个键描述同一只票，
    「这天的除权在 K 线上找不到对应记录」那条闸会为一个**存在**的键报警。
    """
    return [_form(m, c) for m, c in zip(markets, codes, strict=True)]


def _form(market: str, code: str) -> str:
    """读侧的键形态与 `_prepare` 写侧同一条规则（D50）：两条规则迟早会分叉。

    分叉的症状不是报错而是「读不到」：写侧存 `00700`、读侧拿 `700` 查，
    报告上就成了「这只票这段时间没行情」，而库里那排 K 线好好的。
    """
    return canonical_code(market, code)


def _prepare(df: pl.DataFrame) -> pl.DataFrame:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise StorageError(f"bars_daily 缺少必需列: {missing}")
    if "adjust" in df.columns:
        raise StorageError(
            "bars_daily 的口径由仓库层固定为 raw；自带 adjust 列的推导序列若在这里入库，"
            "会被静默改名成 raw 并覆盖真正的不复权 K 线（后复权请在读取层由 corp_actions 现算）"
        )
    if df.schema["date"] != pl.Date:
        # cast(pl.Date) 会把带时区的 datetime 一律取 UTC 日历日：A 股 00:30+08:00 存成前一天
        raise StorageError(
            f"bars_daily.date 必须是 pl.Date（交易日），收到 {df.schema['date']}；"
            "带时刻请先按该市场时区取 date"
        )
    out = df
    for col in COLUMNS:
        if col not in out.columns:
            dtype = pl.Date if col == "date" else pl.Float64
            out = out.with_columns(pl.lit(None, dtype=dtype).alias(col))
    out = out.select(list(COLUMNS)).with_columns(
        pl.col("market").cast(pl.Utf8).str.strip_chars().str.to_uppercase(),
        pl.col("code").cast(pl.Utf8).str.strip_chars().str.to_uppercase(),
        pl.col("source").cast(pl.Utf8),
        *[pl.col(c).cast(pl.Float64) for c in ("close", *_OPTIONAL_NUMERIC)],
    )
    before = out.height
    # is_not_null 挡不住 NaN：pandas 侧的「缺数据」正是 NaN，它会通过非空校验并覆盖上一根好 K 线。
    # close <= 0 同一条道：0 是「今天没成交」写进了收盘列，会让区间跌幅变成 −100%，
    # 对上层来说和真正的暴跌长得一模一样——止损规则会被一根脏 K 线打死。
    out = out.filter(
        pl.col("close").is_finite() & (pl.col("close") > 0) & pl.col("date").is_not_null()
    )
    dropped = before - out.height
    if dropped:
        # 缺收盘价（或收盘价为 0）的行不能存，但也不静默消失：记录后再丢弃
        logger.warning("bars_daily 丢弃 %d 行（收盘价缺失或非正）", dropped)
    # 空市场名是另一回事：那一行有正确的价格，却再也没有任何查询能读到它
    keyed = out.filter(pl.col("market").str.len_chars() > 0)
    if keyed.height < out.height:
        logger.warning(
            "bars_daily 丢弃 %d 行（市场名为空：那一行永远读不到）", out.height - keyed.height
        )
    out = keyed
    # 空代码与空市场同一回事：那一行有正确的价格，却再也没有任何查询能读到它
    coded = out.filter(pl.col("code").str.len_chars() > 0)
    if coded.height < out.height:
        logger.warning(
            "bars_daily 丢弃 %d 行（代码为空：那一行永远读不到）", out.height - coded.height
        )
    out = coded.with_columns(
        pl.Series("code", _canonical_codes(out["market"].to_list(), out["code"].to_list()))
    )
    bad = out.select(
        pl.sum_horizontal(
            *[~pl.col(c).is_finite() & pl.col(c).is_not_null() for c in _OPTIONAL_NUMERIC]
        ).sum()
    ).item()
    if bad:
        logger.warning("bars_daily 有 %d 个非有限数值（NaN/Inf）已置空", bad)
    out = out.with_columns(
        pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c)
        for c in _OPTIONAL_NUMERIC
    )
    # 非正的开高低不是价格而是「这个字段没数据」：置空，让 M2 的振幅/形态规则显式看到缺失
    out = out.with_columns(
        pl.when(pl.col(c) > 0).then(pl.col(c)).otherwise(None).alias(c)
        for c in ("open", "high", "low")
    )
    # high/low 必须把 close 包住：源写反了极值，这两个值就同时不可信，两头都置空
    inverted = (
        (pl.col("high") < pl.col("low"))
        | (pl.col("high") < pl.col("close"))
        | (pl.col("low") > pl.col("close"))
    )
    broken = out.filter(inverted).height
    if broken:
        logger.warning("bars_daily 有 %d 行 OHLC 不自洽（high/low 已置空）", broken)
    out = out.with_columns(
        pl.when(inverted).then(None).otherwise(pl.col(c)).alias(c) for c in ("high", "low")
    )
    return out.unique(subset=["market", "code", "date"], keep="last").sort(
        ["market", "code", "date"]
    )


class BarRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        """写入并返回受影响行数（不含被丢弃的脏行）。

        同键后写覆盖先写：数据源回填改数是常态，留两行等于让同一根 K 线有两个收盘价。
        """
        if df.is_empty():
            return 0
        try:
            prepared = _prepare(df)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 — 脏列型别让调用方去认 polars 异常
            raise StorageError(f"bars_daily 写入数据不合法: {exc}") from exc
        if prepared.is_empty():
            return 0
        # 名字唯一 + finally 注销：同名视图会被后一批覆盖，异常路径下残留会喂错下一批
        view = f"incoming_bars_{uuid4().hex[:8]}"
        self.db.register(view, prepared)
        try:
            self.db.execute(
                f"""
                INSERT OR REPLACE INTO bars_daily
                    (market, code, date, open, high, low, close, volume, amount,
                     source, updated_at)
                SELECT market, code, date, open, high, low, close, volume, amount,
                       source, now()
                FROM {view}
                """
            )
        finally:
            self.db.unregister(view)
        return prepared.height

    def latest_date(self, market: str, code: str) -> date | None:
        value = self.db.scalar(
            "SELECT max(date) FROM bars_daily WHERE adjust = ? AND market = ? AND code = ?",
            [ADJUST, canonical_market(market), _form(market, code)],
        )
        if value is None:
            return None
        return value.date() if isinstance(value, datetime) else value

    def load(
        self,
        market: str,
        codes: list[str],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        if not codes:
            schema = {c: pl.Float64 for c in COLUMNS if c != "date"} | {"date": pl.Date}
            return pl.DataFrame(schema=schema)
        sql = ["SELECT * FROM bars_daily WHERE adjust = ? AND market = ? AND code = ANY(?)"]
        wanted = _canonical_codes([market] * len(codes), codes)
        params: list = [ADJUST, canonical_market(market), wanted]
        if start is not None:
            sql.append("AND date >= ?")
            params.append(start)
        if end is not None:
            sql.append("AND date <= ?")
            params.append(end)
        return self.db.query(" ".join(sql) + " ORDER BY code, date", params)

    def count(self, market: str | None = None, code: str | None = None) -> int:
        # 与 load/latest_date 同一个口径过滤，否则行数会算进库里另一套复权序列
        sql = "SELECT count(*) FROM bars_daily WHERE adjust = ?"
        params: list = [ADJUST]
        if market:
            sql += " AND market = ?"
            params.append(canonical_market(market))
        if code:
            if not market:
                # 没有市场就不知道该按哪种形态归一代码（`700` 在 HK 是 00700，在 A 是原样），
                # 蒙一个形态去查就是「读不到且不报错」那条老路
                raise ValidationError("按代码统计行数必须给市场：代码的键形态按市场归一")
            sql += " AND code = ?"
            params.append(_form(market, code))
        return int(self.db.scalar(sql, params) or 0)
