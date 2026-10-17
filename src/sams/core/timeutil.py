"""交易时段的时间合成与判定。

核心原则（spec §6）：调度一律以「市场本地时区 + 该市场当日时段」为准。
库里存的是本地 HH:MM 字符串 + tz；这里负责合成 tz-aware 的绝对时刻，
夏令时与节假日特殊时段因此无需特判——zoneinfo 已经把偏移算进去了。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sams.core.errors import ValidationError


def parse_hm(value: str) -> time:
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        raise ValidationError(f"时段必须是 HH:MM，收到 {value!r}") from None


@dataclass(frozen=True)
class SessionWindow:
    """市场本地时段，入库序列化为 sessions_json 的元素。"""

    name: str
    open: str
    close: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "open": self.open, "close": self.close}

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> SessionWindow:
        return cls(name=raw["name"], open=raw["open"], close=raw["close"])


@dataclass(frozen=True)
class CalendarDay:
    market: str
    day: date
    is_trading: bool
    is_half_day: bool
    tz: str
    windows: tuple[SessionWindow, ...] = ()


@dataclass(frozen=True)
class Session:
    """合成后的绝对时段（tz-aware）。"""

    name: str
    open: datetime
    close: datetime

    @property
    def duration_min(self) -> float:
        return (self.close - self.open).total_seconds() / 60


def _at(day: date, hm: str, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, parse_hm(hm), tzinfo=tz)


def day_sessions(
    day: CalendarDay,
    *,
    include_extended: bool = False,
    extended: tuple[tuple[str, str, str], ...] = (),
) -> list[Session]:
    """把 CalendarDay 合成绝对时段。

    默认只返回常规时段：盘前盘后由规则的 time_window 显式 opt-in，
    否则盘中轮询量会凭空放大（spec §6.1）。
    """
    if not day.is_trading:
        return []
    tz = ZoneInfo(day.tz)
    sessions = [
        Session(w.name, _at(day.day, w.open, tz), _at(day.day, w.close, tz))
        for w in day.windows
    ]
    sessions.sort(key=lambda s: s.open)

    if include_extended:
        first, last = sessions[0], sessions[-1]
        for name, raw_open, raw_close in extended:
            open_dt = _at(day.day, raw_open, tz)
            close_dt = _at(day.day, raw_close, tz)
            if name.startswith("pre"):
                close_dt = min(close_dt, first.open)
            elif name.startswith("post"):
                open_dt = max(open_dt, last.close)
            if open_dt < close_dt:
                sessions.append(Session(name, open_dt, close_dt))
        sessions.sort(key=lambda s: s.open)
    return sessions


def find_active_session(sessions: list[Session], when: datetime) -> Session | None:
    if when.tzinfo is None:
        raise ValidationError("when 必须是 tz-aware datetime")
    instant = when.astimezone(UTC)
    for session in sessions:
        if session.open.astimezone(UTC) <= instant < session.close.astimezone(UTC):
            return session
    return None


def is_market_open(
    day: CalendarDay,
    when: datetime,
    *,
    include_extended: bool = False,
    extended: tuple[tuple[str, str, str], ...] = (),
) -> bool:
    sessions = day_sessions(day, include_extended=include_extended, extended=extended)
    return find_active_session(sessions, when) is not None


def post_close_run_at(
    day: CalendarDay,
    offset_min: int,
    *,
    extended: tuple[tuple[str, str, str], ...] = (),
) -> datetime:
    """盘后任务触发点（UTC）= 当日最后一个常规时段收盘 + 该市场数据延迟偏移。

    偏移按市场配置（spec §6.4）：美股用较大偏移让任务落到下一个工作日上午，
    而不是北京时间凌晨。
    """
    sessions = day_sessions(day, include_extended=False, extended=extended)
    if not sessions:
        raise ValidationError(f"{day.market} {day.day} 非交易日，无盘后触发点")
    if offset_min < 0:
        raise ValidationError("post_close_min 不能为负")
    return sessions[-1].close.astimezone(UTC) + timedelta(minutes=offset_min)
