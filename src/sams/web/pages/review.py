"""/review 复盘工作台（M4-B §6.4、D135）：首屏是清单，不是仪表盘。

窗口按 display_tz 切：`7d`/`30d` 是含今天往前 n 天的零点起，`week` 是本周一 00:00 起；
上界一律次日零点、开区间（与 `AlertRepo.list_for` 的 `until` 同规）。口径不在这一层：
行从 `AlertRepo` 的窗口查询来（原始 dict），分子分母在 `services/review_stats.py`，
这一层只做窗口算术、拼行与那句「—」——比率与时刻都不带给模板算（`moment_label` 在
Python 里折，同收件箱）。仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from sams.services.review_stats import RuleRatio, summarize, top_rule_ratios
from sams.storage.repos.alerts import HANDLED
from sams.web.auth import require_user
from sams.web.display import action_label, alert_status, dismiss_label, moment_label

router = APIRouter()

#: 下拉与那道检查读的同一份：三个键就是 URL 里能出现的三个形状
WINDOWS = ("7d", "30d", "week")
_WINDOW_LABELS = {"7d": "近 7 天", "30d": "近 30 天", "week": "本周"}

#: 清单的截断（§6.4：超 24h 前 20 条）；批注取 10 条——第 11 条开始读的是兴趣不是义务
OVERDUE_LIMIT = 20
NOTE_LIMIT = 10

#: §6.4 逐字给的空状态话术：窗口不同也不改它——「本周」在这里是「这一段时间」的口语，
#: 换措辞要过产品评审，不在页面里即兴
EMPTY_NOTE = "本周没有告警。可以去看看自选与持仓，或写一条复盘批注。"


def _bounds(now_local: datetime, window: str) -> tuple[datetime, datetime]:
    """窗口的头尾：`since` 含、`until` 排（次日零点）。

    「近 7 天」含今天：起点是今天减 6 天的零点。减 7 会让每天零点那一秒在相邻窗口里
    各出现一次——与 `list_for` 开区间同一个理由，反着成立。

    display_tz 按无 DST 的墙钟日理解（生产用 Asia/Shanghai）：若配成 DST 时区，
    `replace(hour=0)` 加整天会在春秋切换日漂一小时——这是已接受的限制，不改算法。
    """
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "week":
        since = midnight - timedelta(days=midnight.weekday())
    else:
        days = 7 if window == "7d" else 30
        since = midnight - timedelta(days=days - 1)
    return since, midnight + timedelta(days=1)


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _duration(hours: float | None) -> str:
    """中位关闭时长的口语：不到两天说小时，更久说天——`119.0 小时` 那种话没人读。"""
    if hours is None:
        return "—"
    return f"{hours:.1f} 小时" if hours < 48 else f"{hours / 24:.1f} 天"


def _ref_of(row: Mapping[str, Any]) -> tuple[str, str]:
    """处置那一行的 (说法, 链接)。`trade` 的引用是新建的成交 id，没有详情页——链到持仓；
    `watch` 链到自选。旧版行（没有 `action_kind`）两个都没有，那格由「旧版」占位。"""
    kind = str(row.get("action_kind") or "")
    ref = str(row.get("action_ref") or "")
    if kind == "thesis" and ref:
        return action_label(kind), f"/theses/{ref}"
    if kind == "rule" and ref:
        return action_label(kind), f"/rules/{ref}/edit"
    if kind == "trade":
        return action_label(kind), "/portfolio"
    if kind == "watch":
        return action_label(kind), "/watchlist"
    if kind:
        return action_label(kind), ""
    return "旧版", ""


def _rule_rows(items: list[RuleRatio]) -> list[dict]:
    return [
        {
            "id": item.rule_id,
            "name": item.rule_name or item.rule_id,
            "url": f"/rules/{item.rule_id}/edit",
            "dismissed": item.dismissed,
            "triggered": item.triggered,
            "ratio": _rate(item.ratio),
        }
        for item in items
    ]


@router.get("/review")
def review_page(request: Request, window: str = Query("7d")):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    key = window.strip()
    if key not in WINDOWS:
        raise HTTPException(
            status_code=400, detail=f"未知窗口 {key!r}：只有 {'、'.join(WINDOWS)}"
        )
    tz = ctx.settings.server.display_tz
    now_utc = ctx.clock.now_utc()
    since, until = _bounds(now_utc.astimezone(ZoneInfo(tz)), key)
    rows = ctx.alerts_store.list_window(user.id, since=since, until=until)
    stats = summarize(rows)
    ranked, thin = top_rule_ratios(rows)
    overdue_rows = [
        {
            "id": row["id"],
            "at": moment_label(row["triggered_at"], tz),
            "rule_name": str(row["rule_name"] or ""),
            "symbol": f"{row['market']}/{row['code']}",
            "status": alert_status(str(row["status"])),
            # 「未到点」的那一格决定徽标措辞：到点前它仍是「你让它稍后提醒」，不是新的意外
            "deferred": row["deferred_until"] is not None and row["deferred_until"] > now_utc,
        }
        for row in ctx.alerts_store.list_overdue(user.id, now=now_utc, limit=OVERDUE_LIMIT)
    ]
    handled_rows = []
    for row in rows:
        if row["status"] != HANDLED:
            continue
        label, url = _ref_of(row)
        handled_rows.append(
            {
                "id": row["id"],
                "at": moment_label(row["triggered_at"], tz),
                "rule_name": str(row["rule_name"] or ""),
                "label": label,
                "url": url,
            }
        )
    note_rows = [
        {
            "id": note.id,
            "at": moment_label(note.created_at, tz),
            "target": note.target_label or note.target_ref,
            "body": (note.body_md[:120] + "…") if len(note.body_md) > 120 else note.body_md,
            "tags": "、".join(note.tags),
        }
        for note in ctx.annotations.list_recent(
            user.id,
            since=since,
            team_sharing=ctx.app_settings.team_sharing(),
            limit=NOTE_LIMIT,
        )
    ]
    return web.render(
        request,
        "review.html",
        title="复盘",
        window=key,
        window_choices=[(value, _WINDOW_LABELS[value]) for value in WINDOWS],
        since_label=f"{since:%m-%d %H:%M} 至 {until:%m-%d %H:%M}（不含）",
        open_total=ctx.alerts_store.count_open(user.id),
        overdue_total=ctx.alerts_store.count_overdue(user.id, now=now_utc),
        window_total=stats.total,
        overdue=overdue_rows,
        handled=handled_rows,
        ranked=_rule_rows(ranked),
        thin=_rule_rows(thin),
        notes=note_rows,
        rate_rows=[
            ("闭环率", _rate(stats.close_rate), f"{stats.closed}/{stats.total}"),
            ("已读率", _rate(stats.ack_rate), f"{stats.acknowledged}/{stats.total}"),
            ("处置率", _rate(stats.act_rate), f"{stats.handled}/{stats.closed}"),
            ("忽略率", _rate(stats.dismiss_rate), f"{stats.dismissed}/{stats.closed}"),
            ("中位关闭时长", _duration(stats.median_close_hours), "仅终态；隔夜算进中位数"),
        ],
        action_rows=[
            (action_label(kind), count)
            for kind, count in sorted(stats.action_kinds.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        dismiss_rows=[
            (dismiss_label(code), count)
            for code, count in sorted(stats.dismiss_codes.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        legacy_handled=stats.legacy_handled,
        legacy_dismiss=stats.legacy_dismiss,
        empty=stats.total == 0 and not overdue_rows,
        empty_note=EMPTY_NOTE,
    )
