"""通道仓库：写侧加密、读侧掩码，明文只有一个出口。

`credentials()` 之所以是这里唯一能返回明文的函数，不是因为别的函数「忘了」解密——
是因为把它们都写成解密的话，屏幕上迟早会出现一个 token。§4.2 那句话（admin 只见启用状态）
要能被机器复核，就得让「取明文」这件事在代码里只有一个形状。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import StorageError, ValidationError
from sams.storage.crypto import Cipher, mask_config
from sams.storage.db import Database
from sams.storage.ids import new_id

logger = logging.getLogger("sams.storage.channels")

#: 屏幕上那一列的宽度上限：`name` 没有长度上限会让 `channel list` 折行，
#: 折行的表格读不出「哪一行是默认」
NAME_MAX_CHARS = 40
_KIND_MAX_CHARS = 32
_ERROR_MAX_CHARS = 400

_COLUMNS = (
    "id, user_id, kind, name, enabled, is_default,"
    " last_ok_at, last_error, last_error_at, config_encrypted"
)


@dataclass(frozen=True)
class Channel:
    """一行通道。`config` 永远是**掩码视图**，明文在 `config_encrypted` 里、只有
    `ChannelRepo.credentials()` 能取。"""

    id: str
    user_id: str
    kind: str
    name: str
    enabled: bool
    is_default: bool
    last_ok_at: datetime | None
    last_error: str
    last_error_at: datetime | None
    config: dict[str, str]


def _require_name(name: str) -> str:
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValidationError(
            "通道必须有名字：`sams channel list` 里两行同 kind 的通道是不可区分的，"
            "而规则上存的正是 id"
        )
    if len(trimmed) > NAME_MAX_CHARS:
        raise ValidationError(f"通道名字请控制在 {NAME_MAX_CHARS} 字以内（收到 {len(trimmed)}）")
    return trimmed


def _require_kind(kind: str) -> str:
    """仓库层**不**认识通道的名单（那是 `monitor.notifiers` 的事，storage 不许 import monitor）。

    这里只挡空与超长；「不认识这个 kind」由 `sams channel add` 在工厂那一层报错，
    而健康页会数出库里那些工厂不认识的 kind（T-17 的 `unknown_kinds`）。
    """
    trimmed = (kind or "").strip().lower()
    if not trimmed:
        raise ValidationError(
            "通道类型不能为空：wecom / dingtalk / pushplus / email / desktop / voice"
        )
    if len(trimmed) > _KIND_MAX_CHARS:
        raise ValidationError(f"通道类型过长（{len(trimmed)} 字）")
    return trimmed


class ChannelRepo:
    def __init__(self, db: Database, *, cipher: Cipher, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.cipher = cipher
        #: 时间戳来自构造函数而不是每个方法的参数：漏传一次的那一处会写 `now()`，
        #: 而假时钟测不出「这个时间戳是谁打的」（与 `SessionRepo(db, clock)` 同一理由）
        self.clock = clock

    def create(
        self, user_id: str, kind: str, name: str, config: Mapping[str, object]
    ) -> Channel:
        if not user_id:
            raise ValidationError("通道必须属于一个用户：跨用户共用凭据没有审计口径")
        channel_kind = _require_kind(kind)
        label = _require_name(name)
        # 空配置在这里是合法的：`desktop`/`voice` 这两条本机通道**没有**要配的字段。
        # 「这条通道缺了 url 就发不出去」是 kind 的知识，它住在 monitor/notifiers，
        # 由 `NotifyService.add` 在落库之前问一次工厂——仓库层若自己判，就得认识名单
        if not isinstance(config, Mapping):
            raise ValidationError(
                f"通道 {label} 的配置要是一份 k=v（可空）：收到 {type(config).__name__}"
            )
        channel_id = new_id()
        self.db.execute(
            "INSERT INTO channels"
            " (id, user_id, kind, name, config_encrypted, enabled, is_default, created_at)"
            " VALUES (?, ?, ?, ?, ?, TRUE, ?, ?)",
            [
                channel_id,
                user_id,
                channel_kind,
                label,
                self.cipher.encrypt(config),
                # 这个用户的第一个通道自动成为默认：没有默认的那份库，「级别默认也没启用」
                # 这一条兜底路由永远走不到，而用户只会觉得「配了却不发」
                not self._any_for(user_id),
                self.clock.now_utc(),
            ],
        )
        created = self.show(channel_id)
        assert created is not None  # 同事务刚写完
        return created

    def show(self, channel_id: str, *, user_id: str | None = None) -> Channel | None:
        clauses, params = ["id = ?"], [channel_id]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM channels WHERE {' AND '.join(clauses)}", params
        )
        return self._to_channel(rows.to_dicts()[0]) if rows.height else None

    def list_for(self, user_id: str, *, kinds: Sequence[str] | None = None) -> list[Channel]:
        sql = f"SELECT {_COLUMNS} FROM channels WHERE user_id = ?"
        params: list[object] = [user_id]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' for _ in kinds)})"
            params.extend(list(kinds))
        # 默认的排前面：屏幕上第一眼要看的是「兜底会走哪条」
        sql += " ORDER BY is_default DESC, name"
        return [self._to_channel(row) for row in self.db.query(sql, params).to_dicts()]

    def by_kind(self, user_id: str, kind: str) -> list[Channel]:
        return [row for row in self.list_for(user_id, kinds=[_require_kind(kind)]) if row.enabled]

    def credentials(self, channel_id: str) -> dict[str, object]:
        """明文出口。只给 router 用：它要拿 url/token 去真的发一次。

        不带 `user_id` 参数不是漏洞：归属在路由那一步判（这条通道属不属于这次告警的
        接收人）。把它加在这里等于允许任何持有 repo 的人用「猜 id + 传别人的 user_id」
        去试探，而试探的结果与「不存在」在屏幕上是一样的。
        """
        token = self.db.scalar("SELECT config_encrypted FROM channels WHERE id = ?", [channel_id])
        if not token:
            raise StorageError(f"通道 {channel_id} 不存在（拿不到凭据也就不能发）")
        return self.cipher.decrypt(token)

    def set_enabled(self, channel_id: str, enabled: bool) -> None:
        node = self._require(channel_id)
        if not enabled and node.is_default:
            raise ValidationError(
                f"通道 {node.name} 是默认通道，不能停用：默认是「级别没配到通道」时的兜底，"
                "先把默认移给别的一条（`sams channel default`）再停用它"
            )
        # 重新启用即清掉上次失败的那句话：留着它，屏幕上会在一个已经恢复的通道旁边
        # 挂一条旧错误，而「这个通道现在能用吗」的答案就被那句噪音盖住。
        # 停用**不**清：`last_error` 是「为什么这条现在不发」的唯一答案。
        if enabled:
            self.db.execute(
                "UPDATE channels SET enabled = TRUE, last_error = NULL, last_error_at = NULL"
                " WHERE id = ?",
                [channel_id],
            )
        else:
            self.db.execute("UPDATE channels SET enabled = FALSE WHERE id = ?", [channel_id])

    def set_default(self, channel_id: str) -> None:
        node = self._require(channel_id)
        if not node.enabled:
            raise ValidationError(f"通道 {node.name} 处于停用状态：默认必须是一条能发出去的通道")
        with self.db.transaction():
            # 两条 UPDATE 必须在同一个事务里：中间断电的那一瞬库里没有默认通道，
            # 而「没有默认」会让兜底路由静默走到 no_channel
            self.db.execute(
                "UPDATE channels SET is_default = FALSE WHERE user_id = ? AND is_default",
                [node.user_id],
            )
            self.db.execute("UPDATE channels SET is_default = TRUE WHERE id = ?", [channel_id])

    def remove(self, channel_id: str) -> None:
        node = self._require(channel_id)
        with self.db.transaction():
            self.db.execute("DELETE FROM channels WHERE id = ?", [channel_id])
            if node.is_default:
                successor = self.db.scalar(
                    "SELECT id FROM channels WHERE user_id = ? AND enabled"
                    " ORDER BY name LIMIT 1",
                    [node.user_id],
                )
                if successor:
                    self.db.execute(
                        "UPDATE channels SET is_default = TRUE WHERE id = ?", [successor]
                    )
                    logger.info("默认通道 %s 已删除，移给 %s", node.name, successor)

    def mark_ok(self, channel_id: str) -> None:
        self._require(channel_id)
        self.db.execute(
            "UPDATE channels SET last_ok_at = ?, last_error = NULL, last_error_at = NULL"
            " WHERE id = ?",
            [self.clock.now_utc(), channel_id],
        )

    def mark_error(self, channel_id: str, *, error: str) -> None:
        self._require(channel_id)
        note = (error or "").strip()[:_ERROR_MAX_CHARS]
        self.db.execute(
            "UPDATE channels SET last_error = ?, last_error_at = ? WHERE id = ?",
            [note or "（对端没有给错误信息）", self.clock.now_utc(), channel_id],
        )

    def _any_for(self, user_id: str) -> bool:
        return bool(self.db.scalar("SELECT count(*) FROM channels WHERE user_id = ?", [user_id]))

    def _require(self, channel_id: str) -> Channel:
        node = self.show(channel_id)
        if node is None:
            raise StorageError(f"通道 {channel_id} 不存在")
        return node

    def _to_channel(self, row: Mapping[str, object]) -> Channel:
        token = row["config_encrypted"]
        try:
            masked = mask_config(self.cipher.decrypt(token) if token else {})
        except ValidationError as exc:
            # 库里这一行读不回来（多半是 secret_key 换过）。这里**不**把名字读成空：
            # 屏幕上要能列出这条通道并让用户删掉它，而不是让它凭空消失
            logger.warning("通道 %s 的凭据读不回来：%s", row["id"], exc)
            masked = {"（凭据读不回来）": str(exc)}
        return Channel(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            kind=str(row["kind"]),
            name=str(row["name"] or ""),
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            last_ok_at=row["last_ok_at"],
            last_error=str(row["last_error"] or ""),
            last_error_at=row["last_error_at"],
            config=masked,
        )
