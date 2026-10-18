"""指标（spec §8.2）。全部纯函数：输入一条已过闸的复权序列，输出带新列的帧，不碰库、不联网。

三条写在这里就定死的口径：

1. **入口只收 `AdjustedSeries`，不收 `pl.DataFrame`**。裸序列递不进来，「不做指标不过闸」
   这件事就不依赖任何人的自觉（`analysis/guard.py` 开头留给这一层的正是这条）。传错类型
   不是风格问题：U12 那个错数每次换个出口出门，都是因为「手上那条序列是怎么来的」在函数
   签名里看不出来。
2. **样本不足 ⇒ `null`，不是用较短的窗口凑一个数**。`MA20` 在只有三天数据时打出来的值是
   三天均值顶着二十的名字，在图上完全正常，而拿它判断金叉的人已经错了。`null` 会让人去问
   为什么，一个看起来正常的数不会。（同一条道理见 D36：0 行不等于没分红。）
3. **只有价格复权，量不复权**。源给的成交量本来就是当天的股数，拆股那天它本来就该翻倍；
   乘进 `_hfq` 会让「放量」在拆股日假报警。

递推类指标（EMA/KDJ/RSI/ATR/DMI）的**种子必须点名**：同一个 span/窗口在市面上至少有两种
常见写法（首值起算 vs 用前 n 项均值做种子），开头几天给出不同的数，之后收敛——图上谁也
看不出来，只有对账时才发现两家从来不比前几天。这里统一用通达信口径（首值起算、KDJ 种子 50），
并在每条用例里把这个数算出来。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import polars as pl

from sams.analysis.adjust import AdjustedSeries
from sams.core.errors import ValidationError

#: spec §8.2 要的均线组。250 在库里常常凑不满——那是 `null`，不是「这几年 average」
MA_WINDOWS: tuple[int, ...] = (5, 10, 20, 60, 120, 250)


def _prices(series: object, *columns: str) -> tuple[pl.DataFrame, list[list[float | None]]]:
    """入口守卫 + 取列。任何指标函数的第一步都是它，`null` 之外的形状都由它保证。"""
    if not isinstance(series, AdjustedSeries):
        raise ValidationError(
            "指标只接受过闸的 `AdjustedSeries`（现在是 "
            f"{type(series).__name__}）：裸价格序列或自己乘过因子的帧都不算数——"
            "复权序列的唯一出口是 `analysis.adjust.adjusted_series()`，"
            "它前面的覆盖确认与跳变证据是这些数字有资格被打印的前提（D36/D37/D42）"
        )
    frame = series.frame
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValidationError(f"过闸序列缺少列 {missing}：这条序列不是按指标需要的档位算的")
    values = [frame[c].to_list() for c in columns]
    days = frame["date"].to_list()
    # 空值要在这里就说出口，不能留到 `max()` 上崩栈，也不能「跳过那根继续」：
    # 递推类指标（KDJ/ATR/DMI/EMA）跳一根之后，整条曲线都与正确的不同，而图上看不出来
    for column, loaded in zip(columns, values, strict=True):
        holes = [i for i, v in enumerate(loaded) if v is None]
        if holes:
            raise ValidationError(
                f"过闸序列的列 {column} 有 {len(holes)} 个空值（最早 {days[holes[0]]}）："
                "缺数那天算不出指标，而「跳过它继续递推」会让之后的整条曲线都与正解不同"
            )
    return frame, values


def _out(frame: pl.DataFrame, columns: dict[str, Sequence[float | None]]) -> pl.DataFrame:
    """保持 `date` 打头、行数与顺序不变：横向拼接（多个指标并排看）靠这个前提。"""
    out = frame.select("date")
    for name, values in columns.items():
        out = out.with_columns(pl.Series(name, list(values), dtype=pl.Float64))
    return out


def _mean_window(values: list[float], window: int) -> list[float | None]:
    return [None if i < window - 1 else sum(values[i - window + 1 : i + 1]) / window
            for i in range(len(values))]


def _wilder(values: list[float | None], window: int) -> list[float | None]:
    """Wilder 平滑：首个平均值取前 `window` 项的算术平均，其后 avg = (avg×(n−1) + x)/n。

    起算位置往后找第一个「前 `window` 项全可用」的位置：涨跌序列的第一根没有前收、
    DM 的第一根没有前一日高低价，那些位置本来就是 `null`，把它们算进平均等于把
    「没有」当成 0（会让第一天的 RSI 偏低、ATR 偏小）。
    """
    out: list[float | None] = [None] * len(values)
    start = window - 1
    while start < len(values) and any(v is None for v in values[start - window + 1 : start + 1]):
        start += 1
    if start >= len(values):
        return out
    avg = sum(values[start - window + 1 : start + 1]) / window
    out[start] = avg
    for i in range(start + 1, len(values)):
        if values[i] is None:
            # 中段 null 不折成 0.0：DMI 在 s_tr==0（一字板/零波幅窗）那一步会把 dx 打成
            # None，`or 0.0` 会在那一刻之后把整条 ADX 钉死在错误值上。
            # 正确行为：这一根输出 None，递推状态冻结，下一根从上一个有效 avg 继续。
            out[i] = None
        else:
            avg = (avg * (window - 1) + values[i]) / window
            out[i] = avg
    return out


def _ema(values: list[float], span: int) -> list[float]:
    """alpha = 2/(span+1)，首值 = 第一天本身（通达信口径，见模块文档第 3 条）。"""
    alpha = 2 / (span + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(out[-1] + alpha * (value - out[-1]))
    return out


def ma(series: object, *windows: int) -> pl.DataFrame:
    """收盘价的后复权算术均线。`windows` 不给就用 spec §8.2 那一组。"""
    frame, (close,) = _prices(series, "close_hfq")
    wins = windows or MA_WINDOWS
    return _out(frame, {f"ma{w}": _mean_window(close, w) for w in wins})


def ema(series: object, *spans: int) -> pl.DataFrame:
    frame, (close,) = _prices(series, "close_hfq")
    return _out(frame, {f"ema{s}": _ema(close, s) for s in (spans or (12, 26))})


def macd(series: object, fast: int = 12, slow: int = 26, signal: int = 9) -> pl.DataFrame:
    """DIF = EMA(fast) − EMA(slow)，DEA = EMA(DIF, signal)，柱 = DIF − DEA（×2 是行情软件的
    画法而不是定义，这里给未乘 2 的那一个：乘 2 只改刻度，而改过的数会被当成 MACD 对账）。
    """
    frame, (close,) = _prices(series, "close_hfq")
    dif = [f - s for f, s in zip(_ema(close, fast), _ema(close, slow), strict=True)]
    dea = _ema(dif, signal)
    hist = [d - s for d, s in zip(dif, dea, strict=True)]
    return _out(frame, {"macd": dif, "macd_signal": dea, "macd_hist": hist})


def rsi(series: object, *windows: int) -> pl.DataFrame:
    """Wilder RSI：涨幅与跌幅各做 Wilder 平滑，`100 × 涨 / (涨 + 跌)`。"""
    frame, (close,) = _prices(series, "close_hfq")
    deltas = [c - p for p, c in zip(close[:-1], close[1:], strict=True)]
    gains: list[float | None] = [None, *(max(d, 0.0) for d in deltas)]
    losses: list[float | None] = [None, *(max(-d, 0.0) for d in deltas)]
    out: dict[str, Sequence[float | None]] = {}
    for window in windows or (6, 14):
        avg_gain = _wilder(gains, window)
        avg_loss = _wilder(losses, window)
        values: list[float | None] = []
        for gain, loss in zip(avg_gain, avg_loss, strict=True):
            if gain is None or loss is None:
                values.append(None)
            elif gain + loss == 0:
                # 整段平盘：这个比值没有分母。给 null，不蒙一个 50 或 100（模块文档第 2 条）
                values.append(None)
            else:
                values.append(100 * gain / (gain + loss))
        out[f"rsi{window}"] = values
    return _out(frame, out)


def boll(series: object, window: int = 20, k: float = 2.0) -> pl.DataFrame:
    """中轨 = MA(window)，上下轨 = 中轨 ± k×σ，σ 用**总体**标准差（ddof=0，通达信口径）。

    写成样本标准差（ddof=1）在 20 窗口上只差 2.6%，图上看不出，但轨道系统性偏宽、
    「触上轨」的信号天数变多。两种都叫标准差，所以这里点名是哪一个。
    """
    frame, (close,) = _prices(series, "close_hfq")
    out: dict[str, Sequence[float | None]] = {}
    mid = _mean_window(close, window)
    up: list[float | None] = []
    low: list[float | None] = []
    for i, m in enumerate(mid):
        if m is None:
            up.append(None)
            low.append(None)
            continue
        piece = close[i - window + 1 : i + 1]
        sd = (sum((c - m) ** 2 for c in piece) / window) ** 0.5
        up.append(m + k * sd)
        low.append(m - k * sd)
    out["boll_mid"] = mid
    out["boll_up"] = up
    out["boll_low"] = low
    return _out(frame, out)


def kdj(series: object, window: int = 9) -> pl.DataFrame:
    """RSV 在 `window` 根高低价里取，K/D 用 1/3 递推、**种子 50**（通达信默认），J = 3K − 2D。

    种子不是「中性」的形容词而是算法的一部分：换成「首个 RSV 当 K0」会得到另一组开头
    几天的数，两组都在十几天后收敛，只有从第一天对账才看得出来。

    窗口内最高=最低（一字板，ST 与停牌复牌首日常见）时 RSV 没有分母：这一天的 K/D/J 给
    `null`，递归从上一根继续——蒙 50 是「谁都不偏」，但那是编一个数，而 null 会让人去问
    为什么（模块文档第 2 条）。
    """
    frame, (high, low, close) = _prices(series, "high_hfq", "low_hfq", "close_hfq")
    k_list: list[float | None] = []
    d_list: list[float | None] = []
    j_list: list[float | None] = []
    prev_k, prev_d = 50.0, 50.0
    for i in range(len(close)):
        if i < window - 1:
            k_list.append(None)
            d_list.append(None)
            j_list.append(None)
            continue
        piece_high = max(high[i - window + 1 : i + 1])
        piece_low = min(low[i - window + 1 : i + 1])
        span = piece_high - piece_low
        if span == 0:
            k_list.append(None)
            d_list.append(None)
            j_list.append(None)
            continue
        rsv = (close[i] - piece_low) / span * 100
        prev_k = 2 / 3 * prev_k + 1 / 3 * rsv
        prev_d = 2 / 3 * prev_d + 1 / 3 * prev_k
        k_list.append(prev_k)
        d_list.append(prev_d)
        j_list.append(3 * prev_k - 2 * prev_d)
    return _out(frame, {"kdj_k": k_list, "kdj_d": d_list, "kdj_j": j_list})


def _true_range(high: list[float], low: list[float], close: list[float]) -> list[float]:
    """TR = max(高−低, |高−前收|, |低−前收|)，首根没有前收 ⇒ 只用 高−低。

    「含跳空」是 ATR 定义的一部分：只用 高−低 的那版在连续行情里数字几乎一样，
    只有在跳空日才错——而跳空日正是拿 ATR 定止损的人最需要它对的那一天。
    """
    out = [high[0] - low[0]]
    for i in range(1, len(close)):
        prev = close[i - 1]
        out.append(max(high[i] - low[i], abs(high[i] - prev), abs(low[i] - prev)))
    return out


def atr(series: object, window: int = 14) -> pl.DataFrame:
    frame, (high, low, close) = _prices(series, "high_hfq", "low_hfq", "close_hfq")
    return _out(frame, {"atr": _wilder(_true_range(high, low, close), window)})


def dmi(series: object, window: int = 14) -> pl.DataFrame:
    """+DI / −DI = 100 × Wilder(±DM) / Wilder(TR)，ADX = Wilder(|+DI − −DI| / (+DI + −DI))。"""
    frame, (high, low, close) = _prices(series, "high_hfq", "low_hfq", "close_hfq")
    up_move = [None] + [high[i] - high[i - 1] for i in range(1, len(high))]
    down_move = [None] + [low[i - 1] - low[i] for i in range(1, len(low))]
    # 方向不是这一边时是 **0**，不是「不知道」：一路上涨的序列 −DM 恒为 0，写成 None 会让
    # 整条 −DI 变成 null，于是「+DI > −DI」这个最该有的判断反而算不出来
    plus = [None if u is None else (u if u > 0 and u > (d or 0.0) else 0.0)
            for u, d in zip(up_move, down_move, strict=True)]
    minus = [None if d is None else (d if d > 0 and d > (u or 0.0) else 0.0)
             for u, d in zip(up_move, down_move, strict=True)]
    tr = _true_range(high, low, close)
    s_tr = _wilder(tr, window)
    pdi = [
        None if not t or p is None else 100 * p / t
        for p, t in zip(_wilder(plus, window), s_tr, strict=True)
    ]
    mdi = [
        None if not t or m is None else 100 * m / t
        for m, t in zip(_wilder(minus, window), s_tr, strict=True)
    ]
    dx = [
        None
        if p is None or m is None or (p + m) == 0
        else 100 * abs(p - m) / (p + m)
        for p, m in zip(pdi, mdi, strict=True)
    ]
    return _out(frame, {"pdi": pdi, "mdi": mdi, "adx": _wilder(dx, window)})


def cci(series: object, window: int = 20) -> pl.DataFrame:
    """TP = (H+L+C)/3；CCI = (TP − SMA(TP)) / (0.015 × 平均绝对偏差)。

    0.015 那把尺让 CCI 大致落在 ±100 内。写错成 0.05 只是整体缩小三倍，形状一模一样，
    而「突破 +100」这条线恰好是按这个尺度画的。
    """
    frame, (high, low, close) = _prices(series, "high_hfq", "low_hfq", "close_hfq")
    tp = [(h + lo + c) / 3 for h, lo, c in zip(high, low, close, strict=True)]
    mean = _mean_window(tp, window)
    out: list[float | None] = []
    for i, m in enumerate(mean):
        if m is None:
            out.append(None)
            continue
        piece = tp[i - window + 1 : i + 1]
        dev = sum(abs(t - m) for t in piece) / window
        out.append(None if dev == 0 else (tp[i] - m) / (0.015 * dev))
    return _out(frame, {"cci": out})


def wr(series: object, window: int = 14) -> pl.DataFrame:
    """WR = (窗口最高 − 收) / (窗口最高 − 窗口最低) × 100，**0 顶 100 底**（国内口径）。

    另一种写法是 −100~0，两者都叫 WR。把后者当「< 20 算超卖」用会判断完全反过来，
    所以这里连方向一起断言，不只断言算式。
    """
    frame, (high, low, close) = _prices(series, "high_hfq", "low_hfq", "close_hfq")
    out: list[float | None] = []
    for i in range(len(close)):
        if i < window - 1:
            out.append(None)
            continue
        piece_high = max(high[i - window + 1 : i + 1])
        piece_low = min(low[i - window + 1 : i + 1])
        span = piece_high - piece_low
        out.append(None if span == 0 else (piece_high - close[i]) / span * 100)
    return _out(frame, {"wr": out})


def obv(series: object) -> pl.DataFrame:
    """累计能量潮：涨 +量、跌 −量、平盘不动。只用符号，不用幅度。"""
    frame, (close, volume) = _prices(series, "close_hfq", "volume")
    out = [0.0]
    for i in range(1, len(close)):
        if close[i] == close[i - 1]:
            delta = 0.0
        else:
            delta = volume[i] if close[i] > close[i - 1] else -volume[i]
        out.append(out[-1] + delta)
    return _out(frame, {"obv": out})


def volume_ma(series: object, *windows: int) -> pl.DataFrame:
    """成交量均线，吃**裸量**（模块文档第 3 条：量不复权）。"""
    frame, (volume,) = _prices(series, "volume")
    return _out(frame, {f"volume_ma{w}": _mean_window(volume, w) for w in (windows or (5, 10))})


#: 名字 → 用默认参数算这一个指标。`sams analysis series --indicators` 吃这套名字，
#: M2-D 的估值合成与 M4 的告警规则将来也吃同一套。
#: 「同一份实现」（spec §8.1）不只是盘中与盘后：**命令行上看到的 MA20 与规则里判断的
#: MA20 必须是同一个函数吐的**，否则一次对账就能把整套判断的信用问掉。
#: 参数写死在这里是有意的：可调的参数属于 `--window` 那种显式入口，不属于一张表的默认值。
INDICATORS: dict[str, Callable[[AdjustedSeries], pl.DataFrame]] = {
    "ma": lambda s: ma(s),
    "ema": lambda s: ema(s),
    "macd": macd,
    "rsi": rsi,
    "boll": boll,
    "kdj": kdj,
    "atr": atr,
    "dmi": dmi,
    "cci": cci,
    "wr": wr,
    "obv": obv,
    "volume_ma": lambda s: volume_ma(s),
}

#: 屏幕上默认并排给的那几组。其余的（atr/dmi/cci/wr/obv/ema/volume_ma）要 `--indicators`
#: 点名：一屏二十列没人读，而「默认给哪些」是排版问题，不是口径问题——点名的那些算法一样。
DEFAULT_INDICATORS: tuple[str, ...] = ("ma", "macd", "rsi", "boll", "kdj")


def metrics(series: object, names: Sequence[str] = DEFAULT_INDICATORS) -> pl.DataFrame:
    """一次算好几组指标，横向并成一张表：`date` 打头，行序与输入序列一致。

    每个函数都从同一条序列取同一列、都经过 `_out`，所以行数与日期天然对齐；
    对齐这件事不需要断言，`_out` 的「保持行数与顺序不变」就是它的保证。
    """
    unknown = [name for name in names if name not in INDICATORS]
    if unknown:
        raise ValidationError(
            f"不认识的指标名 {unknown}：可选的是 {', '.join(sorted(INDICATORS))}"
        )
    frame, _ = _prices(series, "close_hfq")
    columns: dict[str, list[float | None]] = {}
    for name in names:
        built = INDICATORS[name](series)
        for column in built.columns:
            if column == "date":
                continue
            # 两个指标产出同名的列 ⇒ 后一个静默盖掉前一个，屏幕上少一栏而数字都还在
            if column in columns:
                raise ValidationError(
                    f"指标 {name} 与前面的指标都产出列 {column}：并表时只能有一个留下，"
                    "请给其中一个换名字"
                )
            columns[column] = built[column].to_list()
    return pl.DataFrame({"date": frame["date"].to_list(), **columns})
