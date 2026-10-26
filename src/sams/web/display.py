"""给模板用的显示函数：数字怎么排、状态叫什么，只在这里定一次。

`pct_chg` 本来就是百分数（0.65 = 0.65%），这里不要再乘 100。
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from sams.core.quotes import CLOSED, DELAYED, LIVE, NO_DATA, STALE
from sams.storage.repos.alerts import ACKNOWLEDGED, DISMISSED, HANDLED, NEW

_STATE_LABEL = {
    LIVE: "实时",
    DELAYED: "延迟",
    CLOSED: "休市",
    STALE: "停更",
    NO_DATA: "无行情",
}


def state_label(state: str) -> str:
    return _STATE_LABEL.get(state, state)


def index_close_label(day: date) -> str:
    """大盘条落回库里最后一行时那一格：`10-21 收盘`。

    「收盘」这个字眼和上面那几个状态词一样归这里定：看板两屏（自选行与大盘条）说的是
    同一套话，改名时不会一半屏幕跟着、一半留在路由里。
    """
    return f"{day:%m-%d} 收盘"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def pct_class(value: float | None) -> str:
    if value is None:
        return ""
    return "up" if value > 0 else ("down" if value < 0 else "")


def money(value: float | None, currency: str = "") -> str:
    if value is None:
        return "—"
    tail = f" {currency}" if currency else ""
    return f"{value:,.2f}{tail}"


def hms(moment: datetime | None, tz: str | None = None) -> str:
    """时刻显示到秒；给了 `tz` 就折算到那个时区再取时分秒。

    板上的时刻一律 UTC（`Quote.ts` 与 `MarketBoard.last_poll_at` 都是）：不折算就
    会把 15:00 的收盘写成 07:00，看上去像行情停更了八个小时。库里读出来的 naive
    值不猜它属于哪个时区，按原样显示——猜错比不猜更难查。
    """
    if moment is None:
        return "—"
    if tz and moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo(tz))
    return moment.strftime("%H:%M:%S")


def moment_label(value: datetime | None, tz: str | None = None, *, seconds: bool = False) -> str:
    """告警那一列的时刻：`%m-%d %H:%M`，折算口径与 `hms` 一致。

    `TIMESTAMPTZ` 回读在本库不稳定（`format_utc_moment` 的文档记的就是这件事）：同
    一屏上行情那行已经折到 `display_tz`，告警若在模板里直接 `strftime`，两个时刻属于
    两个时区而屏幕上看不出来——交易员会按错的那一个去回放。naive 值不猜它的时区。

    `seconds` 是给投递记录那张表的：同一条告警的三次尝试只差几秒，只到分钟会把它们
    印成一模一样的一列，看不出哪一次失败。
    """
    if value is None:
        return "—"
    if tz and value.tzinfo is not None:
        value = value.astimezone(ZoneInfo(tz))
    return value.strftime("%m-%d %H:%M:%S" if seconds else "%m-%d %H:%M")


def hhmm(value: dtime | None) -> str:
    """墙钟时刻 → `<input type=time>` 收得下的写法（到分）。

    `datetime.time` 没有时区，所以这里不涉及任何折算：它只管「屏幕上那一格该印什么」。
    带秒的那一份控件认不出来，浏览器会当成没填——于是「打开看一眼」就成了一次清除
    窗口的提交。`None` 交回空串而不是上面那几个 helper 用的 `—`：这一格是要回填进
    表单的，`—` 会被浏览器拒收，而空串正是「此刻没有窗口」。
    """
    return "" if value is None else value.strftime("%H:%M")


#: 告警状态的人话名字。键用 `repos/alerts.py` 那几个常量而不是字面量：状态改名时这里
#: 跟着编译不过或留下一个没人再写得出的旧标签，而字面量只会安静地一直走 `get` 的兜底分支。
ALERT_STATUS_LABELS = {
    NEW: "未处理",
    ACKNOWLEDGED: "已读",
    HANDLED: "已处理",
    DISMISSED: "已驳回",
}


def alert_status(value: str) -> str:
    return ALERT_STATUS_LABELS.get(value, value)


#: 处置动作与忽略原因的中文名：与 `repos/alerts.py` 的 `ACTION_KINDS`/`DISMISS_CODES`
#: 一一对应。新增一档忘配表时，屏幕上出现的是原键名而不是空白——漏配看得见
ACTION_LABELS = {
    "trade": "成交",
    "thesis": "关联观点",
    "rule": "调整规则",
    "watch": "加入自选",
    "other": "其他处置",
}
DISMISS_LABELS = {
    "misreport": "误报",
    "expected": "符合预期",
    "data_issue": "数据问题",
    "not_applicable": "不适用",
    "duplicate": "重复提醒",
    "other": "其他",
}
#: `alert_events.action` 那一列的读法（D138：五个动作各写一行）
EVENT_ACTION_LABELS = {
    "ack": "已读",
    "act": "处置",
    "dismiss": "忽略",
    "defer": "稍后",
    "undo": "撤销",
}


def action_label(value: str | None) -> str:
    if not value:
        return "—"
    return ACTION_LABELS.get(value, value)


def dismiss_label(value: str | None) -> str:
    if not value:
        return "—"
    return DISMISS_LABELS.get(value, value)
