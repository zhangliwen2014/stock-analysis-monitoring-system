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
from sams.core.notify import FAILED, NO_CHANNEL, OUTCOMES, SENT, SEVERITIES, SYSTEM_MARKET
from sams.storage.db import Database
from sams.storage.ids import new_id

logger = logging.getLogger("sams.storage.alerts")

#: 与 0001 的 `alerts.status` 一致：人对这条告警做了什么，不是发没发出去
STATUS = ("new", "handled", "dismissed")
NEW, HANDLED, DISMISSED = STATUS

_ERROR_MAX_CHARS = 400

_ALERT_COLUMNS = (
    "id, user_id, rule_id, account_id, market, code, stage, triggered_at,"
    " status, payload_json, severity, handled_at, handle_note, dismiss_reason, is_test"
)


def _moment(value: datetime) -> datetime:
    """带时区才收（与 `repos/ledger.py` 同一条规则）：TIMESTAMPTZ 会按会话时区解释裸值，
    那等于把北京 22:00 的一条止损记成 UTC 22:00。"""
    if value.tzinfo is None:
        raise ValidationError("告警时刻必须带时区（例如 2026-10-20T22:00+08:00）")
    return value.astimezone(UTC)


def _parse_ts(raw: object) -> datetime | None:
    """payload 里存的 ISO 时刻。存坏了的那一行不能让整个收件箱读不出来。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("payload 里的 quote_ts 读不懂：%r", raw)
        return None


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
    #: 「人做了什么」那三列（0001 就有，此前没有读它们的人）。
    #: 库里没有「谁按的」那一列——`user_id` 是收件人，不是操作者；这三列答的是
    #: 按成了什么、什么时候按的、留了哪一句
    handled_at: datetime | None = None
    handle_note: str | None = None
    dismiss_reason: str | None = None
    #: 下面几个来自 `payload_json`：它们是要显示的文字，不是可查询的事实，
    #: 所以不占列（占列的话，措辞改版就要动表）
    rule_name: str = ""
    account_name: str = ""
    symbol_name: str = ""
    severity: str = "mid"
    channel_ids: list[str] = field(default_factory=list)
    #: `alerts.is_test` 那一列（0019）：测试告警不进 no_open_alert 与健康积压（D131）
    is_test: bool = False
    #: 这三个来自 payload（D120）：要显示与回放的事实，不占列——占列的话措辞改版又要动表
    intraday: bool = False
    quote_ts: datetime | None = None
    quote_source: str | None = None


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
        is_test: bool = False,
        intraday: bool = False,
        quote_ts: datetime | None = None,
        quote_source: str | None = None,
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
            "is_test": bool(is_test),
            "intraday": bool(intraday),
            "quote_ts": None if quote_ts is None else _moment(quote_ts).isoformat(),
            "quote_source": quote_source,
        }
        alert_id = new_id()
        self.db.execute(
            "INSERT INTO alerts"
            " (id, user_id, rule_id, account_id, market, code, stage, triggered_at,"
            " status, payload_json, severity, is_test)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)",
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
                bool(is_test),
            ],
        )
        created = self.get(alert_id)
        assert created is not None  # 刚写完，同一条连接里一定读得回来
        return created

    def get(self, alert_id: str, *, user_id: str | None = None) -> Alert | None:
        """按 id 读那一条；给了 `user_id` 就只读那一个用户名下的。

        默认不挡，因为 `record`/`set_status` 内部那次读要的就是「先把行读出来，归属另外判」：
        那两处各自有一条更准的拒绝话。挡在仓库层而不是命令层（D81）：M3-D 的调度器与 M4 的
        后台都不经过命令层，而挡在命令层的闸对它们是空的。
        """
        clauses, params = ["id = ?"], [alert_id]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        rows = self.db.query(
            f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE {' AND '.join(clauses)}", params
        )
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

    def latest_open(self, user_id: str, market: str, code: str) -> Alert | None:
        """该 (用户, 市场, 代码) 上最新一条**未处理**告警——`no_open_alert` 的输入（§10.1）。

        刻意不带 `account_id`：这一条过滤器的语义是「这个用户在这只票上已经有一条没点掉的
        提醒」，把账户算进去的话，A 账户里那条未处理的止损会挡不住 B 账户的第二条推送，
        用户收到两条内容一样、只有账户名不同的告警。
        与 `latest_for` 的分工也在这里：那一头管冷却（同键同账户），这一头管刷屏（同票同人）。
        测试告警（D131）不进来：/settings/test-alert 发的那条不该把真告警挡一整天。
        """
        rows = self.db.query(
            f"SELECT {_ALERT_COLUMNS} FROM alerts"
            " WHERE user_id = ? AND market = ? AND code = ? AND status = 'new'"
            " AND coalesce(is_test, FALSE) = FALSE"
            " ORDER BY triggered_at DESC, id DESC LIMIT 1",
            [user_id, market, code],
        )
        return self._to_alert(rows.to_dicts()[0]) if rows.height else None

    def has_system_alert(self, user_id: str, component: str, *, since: datetime) -> bool:
        """这个人名下从 `since` 起有没有过关于这个组件的系统告警（D102 的去重键）。

        问的是**库里有没有那一行**，不是那个人有没有真的收到一条通知：
        压制与 `no_channel` 照样留下这一行，而这一行正是压住后续告警的那个东西。
        `code` 与股票代码共用一列，靠 `market = 'SYS'` 与 `rule_id IS NULL` 分开：
        一条恰好把 `code` 写成组件名的规则告警不能被当成冷却的起点。
        `triggered_at` 与 `since` 必须在同一条时间轴上：调用方传「现在 - 冷却」，
        这里不读 `clock`，否则窗口的宽度会变成两个时钟之差。
        答案是一个 `bool` 而不是那一行：调用方拿到行也只为问同一个问题，
        为它解出整份 `payload_json` 是那一次白花的解码。
        """
        return bool(
            self.db.scalar(
                "SELECT EXISTS ("
                " SELECT 1 FROM alerts"
                " WHERE user_id = ? AND market = ? AND code = ? AND rule_id IS NULL"
                " AND triggered_at >= ?)",
                [user_id, SYSTEM_MARKET, component, _moment(since)],
            )
        )

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

    def set_status(
        self, alert_id: str, status: str, *, user_id: str, note: str = ""
    ) -> Alert:
        """人对这条告警做了什么。router 永远不调它（§11 的那条分离），所以没有内部调用方。

        `user_id` 必填：归属闸只有恒传才成立。M3-D 的调度器与 M4 的后台都不经过命令层（D81），
        而一个可选的归属参数会被下一个调用方忘传——忘传的那一刻屏幕上看不出来。
        `handled` 与 `dismissed` 各写自己那一列：两列问的是两件事（「打算怎么处理」与
        「为什么不处理」），同一句话写两遍，忽略率就会把按错的也算进去。
        """
        if status not in STATUS:
            raise ValidationError(f"未知状态 {status!r}：只有 {list(STATUS)}")
        row = self.get(alert_id)
        if row is None:
            raise StorageError(f"告警 {alert_id} 不存在（处理动作必须落在一条真实告警上）")
        if row.user_id != user_id:
            raise StorageError(
                f"告警 {alert_id} 不属于 {user_id}：那条是发给另一个人的，"
                "你把它标成已处理，那个人就再也看不到它了"
            )
        text = (note or "").strip()
        if status == DISMISSED and not text:
            raise ValidationError(
                "dismissed 必须带原因：那一列是「忽略率」唯一的输入，"
                "空着的那一条统计不出来，也回答不了「这条规则是不是在瞎报」"
            )
        moment = self.clock.now_utc()
        self.db.execute(
            "UPDATE alerts SET status = ?, handled_at = ?, handle_note = ?, dismiss_reason = ?"
            " WHERE id = ?",
            [
                status,
                moment if status != NEW else None,
                text if status == HANDLED else None,
                text if status == DISMISSED else None,
                alert_id,
            ],
        )
        moved = self.get(alert_id)
        assert moved is not None  # 刚写完，同一条连接里一定读得回来
        return moved

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
            handled_at=row["handled_at"],
            handle_note=row["handle_note"],
            dismiss_reason=row["dismiss_reason"],
            rule_name=str(payload.get("rule_name") or ""),
            account_name=str(payload.get("account_name") or ""),
            symbol_name=str(payload.get("symbol_name") or ""),
            #: `alerts.severity` 那一列是权威，payload 里的那份只是冗余副本：
            #: 两者不一致时以列为准，因为列是 0015 之后新写的、payload 是老数据里搬来的
            severity=str(row["severity"] or payload.get("severity") or "mid"),
            channel_ids=list(payload.get("channel_ids") or []),
            is_test=bool(row["is_test"]),
            intraday=bool(payload.get("intraday") or False),
            quote_ts=_parse_ts(payload.get("quote_ts")),
            quote_source=(
                str(payload["quote_source"]) if payload.get("quote_source") else None
            ),
        )
