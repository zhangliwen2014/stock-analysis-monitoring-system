"""`alerts`（发生了什么）与 `notify_log`（为了把它送出去试了几次）。

分两张表不是洁癖：一条告警发三个通道是 1 + 3 行，而不是一告警一投递一列。
合成一张表的后果是三条通道的告警在 `sams alerts` 里长成三条一样的事。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import StorageError, ValidationError
from sams.core.notify import FAILED, NO_CHANNEL, OUTCOMES, SENT, SEVERITIES
from sams.storage.db import Database
from sams.storage.ids import new_id

logger = logging.getLogger("sams.storage.alerts")

#: 与 0001 的 `alerts.status` 一致：人对这条告警做了什么，不是发没发出去
STATUS = ("new", "handled", "dismissed")

_ERROR_MAX_CHARS = 400

_ALERT_COLUMNS = (
    "id, user_id, rule_id, account_id, market, code, stage, triggered_at,"
    " status, payload_json, severity, handled_at, handle_note, dismiss_reason"
)


def _moment(value: datetime) -> datetime:
    """带时区才收（与 `repos/ledger.py` 同一条规则）：TIMESTAMPTZ 会按会话时区解释裸值，
    那等于把北京 22:00 的一条止损记成 UTC 22:00。"""
    if value.tzinfo is None:
        raise ValidationError("告警时刻必须带时区（例如 2026-10-20T22:00+08:00）")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Delivery:
    id: str
    channel_id: str | None
    outcome: str
    error: str
    attempts: int
    ts: datetime


@dataclass(frozen=True)
class Alert:
    id: str
    user_id: str
    rule_id: str | None
    account_id: str | None
    market: str
    code: str
    stage: str
    triggered_at: datetime
    status: str
    payload: dict
    #: 下面几个来自 `payload_json`：它们是要显示的文字，不是可查询的事实，
    #: 所以不占列（占列的话，措辞改版就要动表）
    rule_name: str = ""
    account_name: str = ""
    symbol_name: str = ""
    severity: str = "mid"
    channel_ids: list[str] = field(default_factory=list)


class AlertRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def create(
        self,
        user_id: str,
        *,
        rule_id: str | None,
        rule_name: str,
        account_id: str | None,
        account_name: str,
        market: str,
        code: str,
        symbol_name: str,
        severity: str,
        stage: str,
        value_text: str,
        threshold_text: str,
        action_note: str,
        triggered_at: datetime,
        channel_ids: Sequence[str] = (),
        extra: Mapping[str, object] | None = None,
    ) -> Alert:
        if severity not in SEVERITIES:
            raise ValidationError(
                f"未知严重级别 severity={severity!r}：路由按 {list(SEVERITIES)} 三个值分支"
            )
        if not user_id:
            raise ValidationError("告警必须属于一个用户：没有接收人就没有路由")
        moment = _moment(triggered_at)
        payload = {
            "rule_name": rule_name,
            "account_name": account_name,
            "symbol_name": symbol_name,
            "severity": severity,
            "value_text": value_text,
            "threshold_text": threshold_text,
            "action_note": action_note,
            "channel_ids": list(channel_ids),
            "extra": dict(extra or {}),
        }
        alert_id = new_id()
        self.db.execute(
            "INSERT INTO alerts"
            " (id, user_id, rule_id, account_id, market, code, stage, triggered_at,"
            " status, payload_json, severity)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
            [
                alert_id,
                user_id,
                rule_id,
                account_id,
                market,
                code,
                stage,
                moment,
                json.dumps(payload, ensure_ascii=False),
                severity,
            ],
        )
        created = self.get(alert_id)
        assert created is not None  # 刚写完，同一条连接里一定读得回来
        return created

    def get(self, alert_id: str) -> Alert | None:
        rows = self.db.query(f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE id = ?", [alert_id])
        return self._to_alert(rows.to_dicts()[0]) if rows.height else None

    def list_for(self, user_id: str, *, status: str | None = None, limit: int = 50) -> list[Alert]:
        if status is not None and status not in STATUS:
            raise ValidationError(f"未知状态 {status!r}：只有 {list(STATUS)}")
        sql = f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE user_id = ?"
        params: list[object] = [user_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY triggered_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [self._to_alert(row) for row in self.db.query(sql, params).to_dicts()]

    def latest_for(
        self,
        rule_id: str,
        market: str,
        code: str,
        *,
        account_id: str | None = None,
    ) -> Alert | None:
        """这条去重键上最近的一次命中（§10.1 的键 + D94 的判据都从它取）。

        问「最近一条」而不是「冷却窗口内有没有」：引擎要知道的是**上一档是什么**，
        而 `EXISTS` 只回答有没有——「有没有」决定不了要不要穿过冷却。一次问清还省掉
        第二次查询；这一层是单进程同步一趟，没有并发插入要担心。

        `account_id` 参与匹配，且**不是**「给了才过滤」：`IS NOT DISTINCT FROM` 让
        NULL 与 NULL 算同一件事。写成 `= ?` 的话，所有非持仓类规则（`account_id` 为
        NULL）永远查不到自己的上一次命中，于是每一趟都放行。
        走的正是 `idx_alerts_throttle (rule_id, market, code, triggered_at)`。
        """
        rows = self.db.query(
            f"SELECT {_ALERT_COLUMNS} FROM alerts"
            " WHERE rule_id = ? AND market = ? AND code = ?"
            " AND account_id IS NOT DISTINCT FROM ?"
            " ORDER BY triggered_at DESC, id DESC LIMIT 1",
            [rule_id, market, code, account_id],
        )
        return self._to_alert(rows.to_dicts()[0]) if rows.height else None

    def record(
        self,
        alert_id: str,
        channel_id: str | None,
        outcome: str,
        *,
        error: str = "",
        attempts: int = 1,
    ) -> None:
        """一次投递尝试的结果。

        `channel_id=None` 只允许配 `suppressed`/`no_channel`。这条约束写在 Python 而不是
        DDL：DuckDB 的 CHECK 报错到屏幕上只剩一个约束名，而这一句要能说清
        「为什么压制没有通道」——压制的意思是「还没有选到通道就停了」。
        """
        if outcome not in OUTCOMES:
            raise ValidationError(f"未知投递结果 outcome={outcome!r}：只有 {list(OUTCOMES)}")
        if self.get(alert_id) is None:
            raise StorageError(f"告警 {alert_id} 不存在（投递记录必须挂在一条告警上）")
        if outcome == NO_CHANNEL and channel_id is not None:
            raise ValidationError(
                "no_channel 不能带 channel_id：那一句话的意思是「一个通道都没选到」，"
                "配一个 id 就自相矛盾了"
            )
        if outcome in (SENT, FAILED) and not channel_id:
            raise ValidationError(f"{outcome} 必须带 channel_id：一次真发过的事件总得发到哪")
        # `suppressed` 两种形状都合法：免打扰压制发生在选路之前（channel_id 为空），
        # 而「这台机器没有桌面」是关于某一条具体通道的事实（id 要留着才认得出是哪条）
        if attempts < 0:
            raise ValidationError(f"attempts 不能为负（收到 {attempts}）")
        self.db.execute(
            "INSERT INTO notify_log (id, alert_id, channel_id, ts, attempt, outcome, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                new_id(),
                alert_id,
                channel_id,
                self.clock.now_utc(),
                int(attempts),
                outcome,
                (error or "").strip()[:_ERROR_MAX_CHARS],
            ],
        )

    def deliveries(self, alert_id: str) -> list[Delivery]:
        rows = self.db.query(
            "SELECT id, channel_id, coalesce(outcome, 'failed') AS outcome,"
            " coalesce(error, '') AS error, attempt, ts FROM notify_log"
            " WHERE alert_id = ? ORDER BY ts, id",
            [alert_id],
        )
        return [
            Delivery(
                id=str(row["id"]),
                channel_id=row["channel_id"],
                outcome=str(row["outcome"]),
                error=str(row["error"] or ""),
                attempts=int(row["attempt"] or 1),
                ts=row["ts"],
            )
            for row in rows.to_dicts()
        ]

    def set_status(self, alert_id: str, status: str, *, note: str = "") -> None:
        """人对这条告警的处理。router 永远不调它（§11 的那条分离）。"""
        if status not in STATUS:
            raise ValidationError(f"未知状态 {status!r}：只有 {list(STATUS)}")
        if self.get(alert_id) is None:
            raise StorageError(f"告警 {alert_id} 不存在")
        moment = self.clock.now_utc()
        self.db.execute(
            "UPDATE alerts SET status = ?, handled_at = ?, handle_note = ?, dismiss_reason = ?"
            " WHERE id = ?",
            [
                status,
                moment if status != "new" else None,
                note or None,
                note if status == "dismissed" else None,
                alert_id,
            ],
        )

    def _to_alert(self, row: Mapping[str, object]) -> Alert:
        raw = row["payload_json"]
        try:
            payload = json.loads(raw) if isinstance(raw, str) and raw else dict(raw or {})
        except json.JSONDecodeError as exc:
            # 一行的 JSON 坏了不能让整张列表读不出来：这一条报出去，其余照常
            logger.warning("告警 %s 的 payload_json 解不开：%s", row["id"], exc)
            payload = {}
        return Alert(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            rule_id=row["rule_id"],
            account_id=row["account_id"],
            market=str(row["market"] or ""),
            code=str(row["code"] or ""),
            stage=str(row["stage"] or ""),
            triggered_at=row["triggered_at"],
            status=str(row["status"] or "new"),
            payload=payload,
            rule_name=str(payload.get("rule_name") or ""),
            account_name=str(payload.get("account_name") or ""),
            symbol_name=str(payload.get("symbol_name") or ""),
            #: `alerts.severity` 那一列是权威，payload 里的那份只是冗余副本：
            #: 两者不一致时以列为准，因为列是 0015 之后新写的、payload 是老数据里搬来的
            severity=str(row["severity"] or payload.get("severity") or "mid"),
            channel_ids=list(payload.get("channel_ids") or []),
        )
