"""一条去重键上的当前状态：跃迁去重的唯一持久事实。

状态只回答「上一次判到哪一档」，不回答「哪一天」：`bar_ts` 是证据，不是键。
把它放进键里，每天一根新 bar 就会重发一次——那正是这一层要修的病。

键里的 `account_id` 落库成 `account_key`（NULL → 空串）：DuckDB 的主键不收 NULL，
而自选/价格类规则不挂账户是常态。仓库对外仍用 `account_id: str | None`，
空串只在表里出现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.core.rules import STAGES
from sams.storage.db import Database

#: `clear` 是「上一次判过、没到」；其余是命中档位
CLEAR = "clear"
STATES = (CLEAR, *STAGES)


def _key(account_id: str | None) -> str:
    return account_id or ""


@dataclass(frozen=True)
class AlertState:
    user_id: str
    rule_id: str
    market: str
    code: str
    account_id: str | None
    state: str
    bar_ts: date | None
    value_text: str | None
    updated_at: datetime


class AlertStateRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def get(
        self,
        user_id: str,
        rule_id: str,
        market: str,
        code: str,
        account_id: str | None = None,
    ) -> AlertState | None:
        rows = self.db.query(
            "SELECT user_id, rule_id, market, code, account_key, state, bar_ts,"
            " value_text, updated_at FROM alert_states"
            " WHERE user_id = ? AND rule_id = ? AND market = ? AND code = ?"
            " AND account_key = ?",
            [user_id, rule_id, market, code, _key(account_id)],
        )
        return self._to_state(rows.to_dicts()[0]) if rows.height else None

    def set(
        self,
        user_id: str,
        rule_id: str,
        market: str,
        code: str,
        account_id: str | None = None,
        *,
        state: str,
        bar_ts: date | None = None,
        value_text: str | None = None,
    ) -> AlertState:
        if state not in STATES:
            raise ValidationError(
                f"未知告警状态 state={state!r}：只有 {list(STATES)}"
            )
        moment = self.clock.now_utc()
        self.db.execute(
            "INSERT INTO alert_states"
            " (user_id, rule_id, market, code, account_key, state, bar_ts,"
            " value_text, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (user_id, rule_id, market, code, account_key) DO UPDATE SET"
            " state = excluded.state, bar_ts = excluded.bar_ts,"
            " value_text = excluded.value_text, updated_at = excluded.updated_at",
            [
                user_id,
                rule_id,
                market,
                code,
                _key(account_id),
                state,
                bar_ts,
                value_text,
                moment,
            ],
        )
        got = self.get(user_id, rule_id, market, code, account_id)
        assert got is not None  # 刚写完，同一条连接里一定读得回来
        return got

    def clear(
        self,
        user_id: str,
        rule_id: str,
        market: str,
        code: str,
        account_id: str | None = None,
    ) -> bool:
        """判过、没到：只把非 clear 的那一行翻成 clear，`bar_ts` 留着当证据。

        返回是否真改了一行：绝大多数趟会命中 0 行，这个返回值让调用方不必再读一次。
        """
        changed = self.db.scalar(
            "UPDATE alert_states SET state = ?, updated_at = ?"
            " WHERE user_id = ? AND rule_id = ? AND market = ? AND code = ?"
            " AND account_key = ? AND state <> ? RETURNING user_id",
            [
                CLEAR,
                self.clock.now_utc(),
                user_id,
                rule_id,
                market,
                code,
                _key(account_id),
                CLEAR,
            ],
        )
        return changed is not None

    def _to_state(self, row) -> AlertState:
        key = str(row["account_key"] or "")
        return AlertState(
            user_id=str(row["user_id"]),
            rule_id=str(row["rule_id"]),
            market=str(row["market"]),
            code=str(row["code"]),
            account_id=key or None,
            state=str(row["state"]),
            bar_ts=row["bar_ts"],
            value_text=row["value_text"],
            updated_at=row["updated_at"],
        )
