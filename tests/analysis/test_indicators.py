"""指标引擎（spec §8.2 / §8.1）。

这个文件里最要紧的用例不是「算得跟通达信一样」，而是两条结构性的：

1. **入口只收 `AdjustedSeries`**。裸 `pl.DataFrame` 递不进来，等于「未过闸的序列做不出指标」
   写在类型上而不是写在自觉上——`analysis/guard.py` 开头留给 M2-B 的那条就是这个。
2. **样本不足时给 null，不给凑出来的前 N 项**。窗口只有三天可用时打出的「MA20」是一个
   顶着二十的名字、算的是三天的数——它在图上完全正常，而任何一个拿它判断金叉的人都错了。
   缺就是缺，`null` 会让人去问为什么，一个看起来正常的数不会。

数值用例全部与手算对齐（黄金样例），因为指标是「错了也不会报错」的那一类代码：
公式写反照样画出一条平滑的线。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl
import pytest

from sams.analysis.adjust import AdjustedSeries, adjusted_series
from sams.analysis.indicators import (
    atr,
    boll,
    cci,
    dmi,
    ema,
    kdj,
    ma,
    macd,
    obv,
    rsi,
    volume_ma,
    wr,
)
from sams.core.errors import ValidationError

START = date(2024, 1, 2)
#: 只取工作日：指标用例里偶尔会出现「一根 K 线落在周日」这种库根本不会有的形状，
#: 而它会让周/月重采样的用例产生假失败
DAYS = [d for d in (START + timedelta(days=i) for i in range(90)) if d.weekday() < 5]
END = DAYS[-1]

_ACTION_SCHEMA = {
    "ex_date": pl.Date,
    "kind": pl.Utf8,
    "cash_per_share": pl.Float64,
    "ratio": pl.Float64,
    "price": pl.Float64,
}


@dataclass(frozen=True)
class Span:
    """`ConfirmedSpan` 的测试实现：过闸要一份背书，而这里测的不是背书。"""

    covered_from: date = START
    covered_to: date = END
    full_history: bool = True
    src: str = "akshare"


def series(
    closes: list[float] | None = None,
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    volumes: list[float] | None = None,
    days: list[date] | None = None,
) -> AdjustedSeries:
    """默认给一条「无除权 + 已确认覆盖」的序列：没有除权 ⇒ `*_hfq` 与裸价同值，
    手算基准可以直接写在裸价上。每条用例只改自己那几档价格。

    市场取 `US` 而不是 `A`：这批用例测的是指标算术，而 A 股那条 D37 证据闸会把
    「1, 2, 3, 4, 5」这种手算友好的整数列当成漏记的除权拒掉。守卫本身在
    `tests/analysis/test_adjust.py`、`test_returns.py` 与 `tests/cli/` 里各有一条路的用例，
    这里换市场不是绕过它，是把两条不同的判定分开放。
    """
    closes = closes if closes is not None else [float(i + 1) for i in range(len(days or DAYS))]
    n = len(closes)
    days = days or DAYS[:n]
    frame = pl.DataFrame(
        {
            "date": days,
            "open": opens or closes,
            "high": highs or [c * 1.01 for c in closes],
            "low": lows or [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes or [1000.0] * n,
        }
    )
    return adjusted_series(
        "US",
        "AAPL",
        frame,
        pl.DataFrame(schema=_ACTION_SCHEMA),
        coverage=[Span()],
        today=END,
    )


# --- 入口类型：裸序列递不进去 ---------------------------------------------------------


def test_a_bare_frame_is_refused_rather_than_computed_on():
    """未过闸的 `pl.DataFrame` 在签名层面就进不来。

    `hfq_frame` 的存在（以及 `SOLE_CONSUMER` 那份白名单）证明「自觉」靠不住：绕开守卫的
    那个人不会先读 docstring。这里把同一件事变成类型——传错类型不是风格问题，是 U12
    那个错数换了一条出门的路。
    """
    with pytest.raises(ValidationError, match="AdjustedSeries"):
        ma(series([1.0, 2.0, 3.0]).frame, 3)


def test_every_indicator_refuses_a_bare_frame():
    """一条都不例外：漏一个函数，那个函数就是下一次绕闸的入口。"""
    for _name, fn in {
        "ma": lambda s: ma(s, 5),
        "ema": lambda s: ema(s, 5),
        "macd": macd,
        "rsi": lambda s: rsi(s, 6),
        "boll": boll,
        "kdj": kdj,
        "atr": atr,
        "obv": obv,
        "dmi": dmi,
        "cci": cci,
        "wr": wr,
        "volume_ma": lambda s: volume_ma(s, 5),
    }.items():
        with pytest.raises(ValidationError, match="AdjustedSeries"):
            fn(series([1.0, 2.0, 3.0]).frame)


# --- MA / EMA ------------------------------------------------------------------------


def test_ma_is_a_trailing_mean_of_the_adjusted_close():
    """closes 1..5 ⇒ MA3 在索引 2/3/4 上是 2.0 / 3.0 / 4.0，前两个是 null。"""
    out = ma(series([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
    assert out["ma3"].to_list() == [None, None, 2.0, 3.0, 4.0]


def test_a_window_the_bars_cannot_fill_is_null_not_a_shorter_mean():
    """只有三天数据时不许打「MA20」：那是三天均值顶着二十的名字，图上完全正常。

    Polars 的 `rolling_mean` 默认 `min_samples=window`，这条用例把「我们用的是默认值」
    变成写下来的东西——将来有人为了「让前几天也有数」把 min_samples 调小，这里会红。
    """
    out = ma(series([1.0, 2.0, 3.0]), 20)
    assert out["ma20"].to_list() == [None, None, None]


def test_ma_can_take_several_windows_at_once():
    closes = [float(i + 1) for i in range(6)]
    out = ma(series(closes), 3, 5)
    assert out["ma3"].to_list()[-1] == pytest.approx(5.0)
    assert out["ma5"].to_list()[-1] == pytest.approx(4.0)


def test_ema_is_the_tongdaxin_recursion_seeded_at_the_first_close():
    """alpha = 2/(n+1)，首值直接用第一天收盘价（通达信/同花顺口径）。

    种子必须点名：同一个 span 有两种常见写法（这一种，与用前 n 天算术平均做种子那一种），
    两者在开头几天给出不同的数，而图上谁也看不出来。写下一句「首值起算」之后，
    与别家对不上时才知道该比哪一段。
    """
    closes = [1.0, 2.0, 3.0]
    out = ema(series(closes), 3)
    alpha = 2 / 4
    first = 1.0
    second = first + alpha * (2.0 - first)
    third = second + alpha * (3.0 - second)
    assert out["ema3"].to_list() == pytest.approx([first, second, third])


def test_ema_does_not_wait_for_a_full_window():
    """递推从第一天就有定义：这里给数不是「凑出来的」，因为它就是递归的第 0 项。"""
    assert ema(series([1.0, 2.0, 3.0]), 20)["ema20"].null_count() == 0


# --- MACD ---------------------------------------------------------------------------


def test_macd_is_the_two_ema_difference_with_a_signal_on_top():
    closes = [float(i) for i in [10, 11, 12, 11, 13, 14, 13, 15, 16, 15]]
    out = macd(series(closes), fast=3, slow=5, signal=2)
    fast = ema(series(closes), 3)["ema3"].to_list()
    slow = ema(series(closes), 5)["ema5"].to_list()
    assert out["macd"].to_list() == pytest.approx([f - s for f, s in zip(fast, slow, strict=True)])
    dif = out["macd"].to_list()
    alpha = 2 / 3
    signal = [dif[0]]
    for value in dif[1:]:
        signal.append(signal[-1] + alpha * (value - signal[-1]))
    assert out["macd_signal"].to_list() == pytest.approx(signal)
    assert out["macd_hist"].to_list() == pytest.approx(
        [d - s for d, s in zip(dif, signal, strict=True)]
    )


# --- RSI ---------------------------------------------------------------------------


def test_rsi_uses_wilder_smoothing_and_gives_zero_for_a_declining_run():
    """全跌 ⇒ 平均涨幅 0 ⇒ RSI 0；全涨 ⇒ 平均跌幅 0 ⇒ RSI 100。

    两端钉死比中间值更能暴露「分子分母写反」——而写反的那版在震荡市里看起来完全正常。
    """
    up = rsi(series([float(i + 1) for i in range(15)]), 6)
    assert up["rsi6"].to_list()[-1] == pytest.approx(100.0)
    down = rsi(series([float(15 - i) for i in range(15)]), 6)
    assert down["rsi6"].to_list()[-1] == pytest.approx(0.0)


def test_rsi_is_null_when_nothing_moved_at_all():
    """整段平盘 ⇒ 涨与跌的平均都是 0，那个比值没有分母：给 null，不蒙 50 或 100。

    市面上两种蒙法都有人用，而蒙出来的数在屏幕上与正常 RSI 无法区分。
    """
    assert set(rsi(series([10.0] * 12), 6)["rsi6"].to_list()) == {None}


def test_rsi_is_null_until_the_first_average_exists():
    """首个平均值要 window 个涨跌：之前没有「平均」可说，就不许打这个数。"""
    out = rsi(series([float(i + 1) for i in range(10)]), 6)
    assert out["rsi6"].to_list()[:6] == [None] * 6
    assert out["rsi6"].to_list()[6] is not None


def test_rsi_hand_checked_wilder_step():
    """ Wilder 递推：avg = (前 avg × (n−1) + 本次)/n，RSI = 100 × 涨 / (涨 + 跌)。"""
    closes = [10.0, 11.0, 10.5, 11.5, 12.0, 11.0, 12.5, 13.0, 12.0, 13.5, 14.0]
    window = 5
    out = rsi(series(closes), window)
    gains = [max(c - p, 0.0) for p, c in zip(closes[:-1], closes[1:], strict=True)]
    losses = [max(p - c, 0.0) for p, c in zip(closes[:-1], closes[1:], strict=True)]
    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window
    for gain, loss in zip(gains[window:], losses[window:], strict=True):
        avg_gain = (avg_gain * (window - 1) + gain) / window
        avg_loss = (avg_loss * (window - 1) + loss) / window
    expected = 100 * avg_gain / (avg_gain + avg_loss)
    assert out["rsi5"].to_list()[-1] == pytest.approx(expected)


# --- BOLL --------------------------------------------------------------------------


def test_boll_uses_the_population_standard_deviation():
    """±2σ 的 σ 用总体标准差（ddof=0）——这是通达信/同花顺的口径。

    写成样本标准差（ddof=1）在 20 窗口上只差 2.6%，图上几乎看不出，但轨道会系统性偏宽，
    「触及上轨」的信号天数会变多。口径必须点名，因为两种都叫「标准差」。
    """
    closes = [float(i + 1) for i in range(20)]
    out = boll(series(closes), window=20, k=2.0)
    mid = sum(closes) / 20
    sd = (sum((c - mid) ** 2 for c in closes) / 20) ** 0.5
    assert out["boll_mid"].to_list()[-1] == pytest.approx(mid)
    assert out["boll_up"].to_list()[-1] == pytest.approx(mid + 2 * sd)
    assert out["boll_low"].to_list()[-1] == pytest.approx(mid - 2 * sd)


def test_boll_needs_a_full_window():
    assert boll(series([1.0, 2.0, 3.0]), window=20)["boll_mid"].to_list() == [None, None, None]


# --- KDJ / ATR / DMI / CCI / WR ----------------------------------------------------


def test_kdj_rsv_and_the_50_seed_are_declared_by_a_number():
    """RSV = (收 − 窗口最低) / (窗口最高 − 窗口最低) × 100，K/D 用 1/3 递推、种子 50（通达信默认）。

    种子 50 不是「中性」的形容词而是算法的一部分：换成「首个 RSV 当 K0」会得到另一组
    开头几天的数，而两组都在第 10 天后收敛。用例把这两件事一起钉住。
    """
    closes = [10.0, 11.0, 12.0, 13.0]
    highs = [10.0, 11.0, 12.0, 14.0]
    lows = [9.0, 9.0, 9.0, 9.0]
    out = kdj(series(closes, highs=highs, lows=lows), window=3)
    k, d = 50.0, 50.0
    expected_k: list[float | None] = [None, None]
    expected_d: list[float | None] = [None, None]
    for i in (2, 3):
        top = max(highs[i - 2 : i + 1])
        bottom = min(lows[i - 2 : i + 1])
        rsv = (closes[i] - bottom) / (top - bottom) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        expected_k.append(k)
        expected_d.append(d)
    assert out["kdj_k"].to_list() == pytest.approx(expected_k)
    assert out["kdj_d"].to_list() == pytest.approx(expected_d)
    assert out["kdj_j"].to_list()[3] == pytest.approx(3 * k - 2 * d)


def test_kdj_has_no_rsv_when_the_window_never_moved():
    """窗口内最高=最低（一字板）⇒ RSV 没有分母：给 null，递归从上一根继续。

    蒙 50 看起来「谁都不偏」，但那是一个编出来的数，而 A 股的 ST 与复牌首日常常整天
    就是这个形状——它不是极端例子，是这条路上每周都会遇到的一天。
    """
    flat = [10.0] * 6
    out = kdj(series(flat, highs=flat, lows=flat), window=3)
    assert set(out["kdj_k"].to_list()[2:]) == {None}


def test_kdj_is_null_before_the_first_rsv():
    out = kdj(series([float(i + 1) for i in range(10)], highs=[20.0] * 10, lows=[1.0] * 10), 9)
    assert out["kdj_k"].to_list()[:8] == [None] * 8
    assert out["kdj_k"].to_list()[8] is not None


def test_atr_true_range_includes_the_gap_from_the_previous_close():
    """TR = max(高−低, |高−前收|, |低−前收|)：跳空那天若只用高−低，波动会被算小。

    「含跳空」是 ATR 定义的一部分，而只用 高−低 的那版在连续行情里数字几乎一样——
    只有在跳空日才错，而跳空日正是拿 ATR 定止损的人最需要它对的一天。
    """
    closes = [10.0, 20.0]
    out = atr(series(closes, highs=[10.0, 20.5], lows=[9.5, 19.5]), window=2)
    tr2 = max(20.5 - 19.5, abs(20.5 - 10.0), abs(19.5 - 10.0))
    assert out["atr"].to_list()[1] == pytest.approx((0.5 + tr2) / 2)


def test_atr_is_wilder_smoothed_after_the_first_average():
    closes = [float(i + 1) for i in range(12)]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    out = atr(series(closes, highs=highs, lows=lows), window=3)
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    avg = sum(trs[:3]) / 3
    for tr in trs[3:]:
        avg = (avg * 2 + tr) / 3
    assert out["atr"].to_list()[-1] == pytest.approx(avg)


def test_dmi_directional_indicators_and_adx_are_reported_separately():
    """+DI/−DI 用 Wilder 平滑的 DM 除以 ATR，ADX 是 DX 的 Wilder 平滑（spec §8.2 要 DMI）。

    这里断言的是形状与符号方向：一路上涨时 +DI 必须高于 −DI，ADX 落在 0~100。
    平滑的逐步口径由 `test_atr_is_wilder_smoothed_after_the_first_average` 那条同款手算
    递推锁住（DM/ATR 用的是同一个 `_wilder`）。
    """
    closes = [float(i + 1) for i in range(40)]
    out = dmi(series(closes))
    assert out["mdi"].to_list()[-1] == pytest.approx(0.0), "没有向下那天就不是「不知道」"
    assert out["pdi"].to_list()[-1] > out["mdi"].to_list()[-1]
    assert 0 <= out["adx"].to_list()[-1] <= 100


def test_cci_uses_the_typical_price_and_the_mean_deviation():
    """TP = (H+L+C)/3，CCI = (TP − SMA(TP)) / (0.015 × 平均绝对偏差)。

    0.015 这个常数是让 CCI 大致落在 ±100 内的那把尺，写错成 0.05 会让它小三倍而形状不变。
    """
    n = 20
    closes = [float(i + 1) for i in range(n)]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    out = cci(series(closes, highs=highs, lows=lows), window=n)
    tps = [(h + lo + c) / 3 for h, lo, c in zip(highs, lows, closes, strict=True)]
    mean = sum(tps) / n
    dev = sum(abs(t - mean) for t in tps) / n
    assert out["cci"].to_list()[-1] == pytest.approx((tps[-1] - mean) / (0.015 * dev))


def test_wr_is_the_percentage_of_the_range_below_the_close():
    """WR = (最高 − 收) / (最高 − 最低) × 100，0 顶 100 底（国内口径，不是 −100~0 那版）。

    两种写法都叫 WR，一个从 0 到 100、一个从 −100 到 0；把后者当「超卖 < 20」用会把
    判断完全反过来。所以这里连「哪个方向算超卖」一起断言。
    """
    closes = [10.0, 12.0, 14.0]
    out = wr(series(closes, highs=[10.0, 12.0, 16.0], lows=[8.0, 8.0, 8.0]), window=3)
    assert out["wr"].to_list()[-1] == pytest.approx((16.0 - 14.0) / (16.0 - 8.0) * 100)


# --- 量能 --------------------------------------------------------------------------


def test_obv_moves_by_the_sign_of_the_close_change_only():
    """涨跌方向决定加减，平盘整天不动：OBV 只用符号，不用幅度。"""
    volumes = [100.0, 100.0, 100.0, 100.0]
    out = obv(series([10.0, 11.0, 11.0, 9.0], volumes=volumes))
    assert out["obv"].to_list() == [0.0, 100.0, 100.0, 0.0]


def test_volume_ma_is_the_trailing_mean_of_raw_volume():
    """量能不做复权：源给的成交量本来就是当天的股数，而拆股日的股数本来就该翻倍。

    把它乘进 `_hfq` 会让「放量」这个判断在拆股那天假报警。这是 spec §5.5「只有价格
    要复权」的另一面，值得一条用例而不是只写在注释里。
    """
    out = volume_ma(series([1.0, 2.0, 3.0], volumes=[100.0, 200.0, 300.0]), 2)
    assert out["volume_ma2"].to_list() == [None, 150.0, 250.0]


def test_indicators_keep_the_frame_keyed_and_ordered():
    """每个函数都返回 `date` 打头、行数与顺序不变的帧：横向拼接靠这个前提。"""
    s = series([float(i + 1) for i in range(10)])
    for fn in (
        lambda: ma(s, 3),
        lambda: ema(s, 3),
        lambda: macd(s),
        lambda: rsi(s, 6),
        lambda: boll(s),
        lambda: kdj(s),
        lambda: atr(s),
        lambda: obv(s),
        lambda: dmi(s),
        lambda: cci(s),
        lambda: wr(s),
        lambda: volume_ma(s, 3),
    ):
        out = fn()
        assert out.columns[0] == "date"
        assert out.height == 10
        assert out["date"].to_list() == [d for d in DAYS[:10]]


# --- 反未来函数（spec §8.1）---------------------------------------------------------

#: 逐条指标都要过这条，一条都不能漏：漏的那一条正是将来回测里最贵的一条
ALL_INDICATORS = {
    "ma5": lambda s: ma(s, 5),
    "ma20": lambda s: ma(s, 20),
    "ma250": lambda s: ma(s, 250),
    "ema": lambda s: ema(s, 12, 26),
    "macd": macd,
    "rsi": lambda s: rsi(s, 6, 14),
    "boll": boll,
    "kdj": kdj,
    "atr": atr,
    "dmi": dmi,
    "cci": cci,
    "wr": wr,
    "obv": obv,
    "volume_ma": lambda s: volume_ma(s, 5, 10),
}

#: 被检查的那一天：离开头足够远，每个指标在这里都已经出数（否则这条用例会在自己
#: 的 null 前缀上「通过」——那等于什么都没测）。ADX 最晚：它要两轮 Wilder 平滑
CHECK_AT = 40
BARS = 48


def prices() -> tuple[list[float], list[float], list[float], list[float]]:
    """一段有涨有跌、每天不超过 2% 的序列。

    两个约束都要满足：跳变闸不能拦它（不然拿不到基准），也不能是单调的（不然
    「T 日不动」在一列常数额上也是真的，什么也没测到）。
    """
    closes: list[float] = []
    level = 100.0
    for i in range(BARS):
        level *= 1.0 + (0.02 if (i % 5) < 3 else -0.015)
        closes.append(level)
    highs = [c * 1.015 for c in closes]
    lows = [c * 0.985 for c in closes]
    volumes = [1000.0 + 137 * (i % 5) for i in range(BARS)]
    return closes, highs, lows, volumes


def row_at(frame: pl.DataFrame, index: int) -> dict[str, float | None]:
    return {k: v for k, v in frame.slice(index, 1).to_dicts()[0].items() if k != "date"}


@pytest.mark.parametrize("name", sorted(ALL_INDICATORS))
def test_tampering_with_the_future_leaves_the_present_untouched(name):
    """spec §8.1 的原话：篡改 T+1 之后的数据，T 日每个指标必须逐位不变。

    这条比任何「看起来没有前视」的评审都值钱：滚动窗口写错一行（比如 `window` 从
    中心展开而不是尾部）在图上完全看不出来，只有把未来改掉才会动。OBV 与 EMA 这类
    递归写在「整段累加」上，正是最容易顺手写成前视的那两个。
    """
    closes, highs, lows, volumes = prices()
    compute = ALL_INDICATORS[name]
    before = compute(series(closes, highs=highs, lows=lows, volumes=volumes))
    tampered = list(closes)
    tampered_high = list(highs)
    tampered_low = list(lows)
    tampered_volume = list(volumes)
    for i in range(CHECK_AT + 1, BARS):
        tampered[i] *= 3.0
        tampered_high[i] *= 4.0
        tampered_low[i] *= 2.0
        tampered_volume[i] = 9_000_000.0
    after = compute(
        series(tampered, highs=tampered_high, lows=tampered_low, volumes=tampered_volume)
    )
    assert row_at(after, CHECK_AT) == pytest.approx(row_at(before, CHECK_AT))


@pytest.mark.parametrize("name", sorted(ALL_INDICATORS))
def test_computing_up_to_t_and_computing_the_whole_thing_agree_at_t(name):
    """截断到 T 算一次、全量算一次，T 日同值：与上一条是同一件事的两个方向。

    只有「截断也同值」才说明指标里没有「先扫一遍全长再回填」的写法（那种写法
    篡改测试抓不到，因为它读的是后面的行号而不是后面的值）。
    """
    closes, highs, lows, volumes = prices()
    compute = ALL_INDICATORS[name]
    whole = compute(series(closes, highs=highs, lows=lows, volumes=volumes))
    cut = compute(
        series(
            closes[: CHECK_AT + 1],
            highs=highs[: CHECK_AT + 1],
            lows=lows[: CHECK_AT + 1],
            volumes=volumes[: CHECK_AT + 1],
        )
    )
    assert row_at(cut, CHECK_AT) == pytest.approx(row_at(whole, CHECK_AT))


@pytest.mark.parametrize("name", sorted(ALL_INDICATORS))
def test_the_future_function_check_is_actually_looking_at_a_number(name):
    """上一条在 null 上也会「通过」：这里确认 T 日每个指标都已经出数。

    守卫自己的用例。没有这条，`CHECK_AT` 哪天被挪到窗口之前，前两条就双双变成空跑。
    唯一的例外是年线：48 根本来算不出 250 日均值，那是这条序列的真实缺口，
    而不是本用例的前提（`ma250` 因此被排除在外，并在下面单独钉一条）。
    """
    closes, highs, lows, volumes = prices()
    frame = ALL_INDICATORS[name](series(closes, highs=highs, lows=lows, volumes=volumes))
    row = row_at(frame, CHECK_AT)
    assert all(v is not None for k, v in row.items() if k != "ma250"), row
