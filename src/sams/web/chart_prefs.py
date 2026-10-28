"""图表偏好的词表与校验（D193）：周期条顺序、均线窗口。

档名与窗口的规矩只写这一次：设置页保存、个股图局部渲染、`bars` 接口的白名单都念
这一份，屏幕上不会出现「存进去的档名图上不认」。
"""

from __future__ import annotations

from sams.core.errors import ValidationError

#: 周期条：(参数值, 屏幕上那几个字)。分时那一格是视图切换，不在这张表里
CHART_PERIODS: tuple[tuple[str, str], ...] = (
    ("m1", "1分"),
    ("m5", "5分"),
    ("m15", "15分"),
    ("m30", "30分"),
    ("m60", "60分"),
    ("m120", "120分"),
    ("day", "日"),
    ("week", "周"),
    ("month", "月"),
    ("quarter", "季"),
    ("year", "年"),
    ("5d", "5日"),
    ("10d", "10日"),
)

#: 均线窗口最多六条：与 `MA_WINDOWS` 的默认条数同一个上限
MAX_MA_WINDOWS = 6
_MA_MESSAGE = "均线窗口要 1..500 之间的正整数，最多六条"


def parse_windows(raw: str) -> tuple[int, ...]:
    """`5,10,20,60` → `(5, 10, 20, 60)`；留空 = 用指标自己的那一组。

    句子里带「最多六条」与「1..500」是因为这三类填错（非数字、0 或负数、堆十几条）
    在同一个人手里会反复出现：`int()` 抛的 `ValueError` 不折成 `ValidationError`
    就越过全局处理器，换回一屏 500。
    """
    picked: list[int] = []
    for item in (raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError as exc:
            raise ValidationError(_MA_MESSAGE) from exc
        if not 1 <= value <= 500:
            raise ValidationError(_MA_MESSAGE)
        picked.append(value)
    deduped = tuple(dict.fromkeys(picked))
    if len(deduped) > MAX_MA_WINDOWS:
        raise ValidationError(_MA_MESSAGE)
    return deduped


def parse_periods(raw: str, known: tuple[str, ...]) -> tuple[str, ...]:
    """周期条顺序：逗号分隔的档名，必须把 `known` 每个**恰好一次**；留空 = 默认顺序。

    排序不是裁剪：少写一个档等于把它从屏幕上藏掉，而「这一档我这一市场用不到」
    该由置灰去说（D165），不该由偏好去藏。
    """
    text = (raw or "").strip()
    if not text:
        return ()
    picked = tuple(part.strip() for part in text.split(",") if part.strip())
    unknown = [name for name in picked if name not in known]
    if unknown:
        raise ValidationError(f"不认识的周期档 {'、'.join(unknown)}：可选的是 {', '.join(known)}")
    missing = [name for name in known if name not in picked]
    repeated = [name for name in known if picked.count(name) > 1]
    if missing or repeated:
        raise ValidationError(
            f"周期条要 {len(known)} 档、每档正好一次："
            f"缺 {'、'.join(missing) or '无'}，多 {'、'.join(repeated) or '无'}"
        )
    return picked


def ordered_periods(
    periods: tuple[tuple[str, str], ...], pref: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    """按偏好重排；空偏好=原顺序。

    库里存过的档可能在新版本里下了架：跳过不认识的，新加进词表的档接在末尾——
    偏好是排序，不是一份会过期的白名单。
    """
    if not pref:
        return periods
    labels = dict(periods)
    head = tuple((name, labels[name]) for name in pref if name in labels)
    tail = tuple(item for item in periods if item[0] not in pref)
    return head + tail


def windows_text(windows: tuple[int, ...]) -> str:
    """库里的窗口 → 输入框里那一串；空=默认，框里就空着。"""
    return ",".join(str(value) for value in windows)
