"""单票静音按类别分组：键是（用户, 市场, 代码, 类别），不带规则。

静音讲的仍是「这只票别再烦我」，只是不再一按全静（D249）：
`all` 覆盖价格规则、资讯与摘要；`rule` / `news` / `digest` 各管一路。
旧表 `alert_mutes` 的行按 `all` 读，新写入只落 `alert_mute_scopes`。

重复静音是幂等的：保留第一次的时刻与备注；已经有 `all` 时再按某一类也不新起一行。
解除后不补发静音期间的历史——状态表在静音期间照常记录跃迁（见 `RuleEngine`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database

#: 一个类别一路读者：价格规则、资讯告警、选股新命中、每日摘要；`all` 全管
MUTE_KINDS = ("all", "rule", "news", "screen", "digest")
MUTE_KIND_LABELS = {
    "all": "全部",
    "rule": "价格规则",
    "news": "资讯",
    "screen": "选股",
    "digest": "摘要",
}

_SCOPE_COLUMNS = "user_id, market, code, kind, note, created_at"
_LEGACY_COLUMNS = "user_id, market, code, note, created_at"


def _kind(value: str) -> str:
    if value not in MUTE_KINDS:
        raise ValidationError(
            f"未知静音类别 {value!r}：可选的是 {'、'.join(MUTE_KINDS)}"
        )
    return value


@dataclass(frozen=True)
class Mute:
    user_id: str
    market: str
    code: str
    note: str
    created_at: datetime
    kind: str = "all"


class MuteRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def get(self, user_id: str, market: str, code: str, *, kind: str = "all") -> Mute | None:
        """这一类是否静着；`all` 与旧表都算数，所以返回的 `kind` 可能是 `all`。"""
        kind = _kind(kind)
        found = self._row(user_id, market, code, kind)
        if found is not None:
            return found
        if kind != "all":
            found = self._row(user_id, market, code, "all")
            if found is not None:
                return found
        return self._legacy(user_id, market, code)

    def add(
        self,
        user_id: str,
        market: str,
        code: str,
        *,
        note: str = "",
        kind: str = "all",
    ) -> Mute:
        kind = _kind(kind)
        existing = self.get(user_id, market, code, kind=kind)
        if existing is not None:
            return existing
        self.db.execute(
            "INSERT INTO alert_mute_scopes (user_id, market, code, kind, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [user_id, market, code, kind, (note or "").strip(), self.clock.now_utc()],
        )
        got = self._row(user_id, market, code, kind)
        assert got is not None  # 刚写完，同一条连接里一定读得回来
        return got

    def remove(self, user_id: str, market: str, code: str, *, kind: str = "all") -> bool:
        kind = _kind(kind)
        changed = self.db.scalar(
            "DELETE FROM alert_mute_scopes"
            " WHERE user_id = ? AND market = ? AND code = ? AND kind = ?"
            " RETURNING user_id",
            [user_id, market, code, kind],
        )
        if kind != "all":
            return changed is not None
        # 旧表只读，但「解除全部」得把旧行也删掉，否则迁移前静掉的票永远解不开
        legacy = self.db.scalar(
            "DELETE FROM alert_mutes WHERE user_id = ? AND market = ? AND code = ?"
            " RETURNING user_id",
            [user_id, market, code],
        )
        return changed is not None or legacy is not None

    def list_for(self, user_id: str) -> list[Mute]:
        rows = self.db.query(
            f"SELECT {_SCOPE_COLUMNS} FROM alert_mute_scopes WHERE user_id = ?",
            [user_id],
        )
        found = [self._to_mute(row) for row in rows.to_dicts()]
        seen = {(mute.market, mute.code, mute.kind) for mute in found}
        legacy = self.db.query(
            f"SELECT {_LEGACY_COLUMNS} FROM alert_mutes WHERE user_id = ?",
            [user_id],
        )
        found.extend(
            self._to_mute(row, kind="all")
            for row in legacy.to_dicts()
            if (str(row["market"]), str(row["code"]), "all") not in seen
        )
        found.sort(key=lambda mute: (mute.created_at, mute.market, mute.code, mute.kind))
        return found

    def scopes(self, user_id: str, market: str, code: str) -> list[Mute]:
        """这一只票上实际写着哪几类（不含 `all` 的兜底），详情页按它逐类解。"""
        return [
            mute
            for mute in self.list_for(user_id)
            if (mute.market, mute.code) == (market, code)
        ]

    def _row(self, user_id: str, market: str, code: str, kind: str) -> Mute | None:
        rows = self.db.query(
            f"SELECT {_SCOPE_COLUMNS} FROM alert_mute_scopes"
            " WHERE user_id = ? AND market = ? AND code = ? AND kind = ?",
            [user_id, market, code, kind],
        )
        return self._to_mute(rows.to_dicts()[0]) if rows.height else None

    def _legacy(self, user_id: str, market: str, code: str) -> Mute | None:
        rows = self.db.query(
            f"SELECT {_LEGACY_COLUMNS} FROM alert_mutes"
            " WHERE user_id = ? AND market = ? AND code = ?",
            [user_id, market, code],
        )
        return self._to_mute(rows.to_dicts()[0], kind="all") if rows.height else None

    def _to_mute(self, row, *, kind: str | None = None) -> Mute:
        return Mute(
            user_id=str(row["user_id"]),
            market=str(row["market"]),
            code=str(row["code"]),
            note=str(row["note"] or ""),
            created_at=row["created_at"],
            kind=str(row["kind"] if kind is None else kind),
        )
