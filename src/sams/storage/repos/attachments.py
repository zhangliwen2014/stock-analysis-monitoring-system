"""附件行：`annotation_attachments` 是文件索引的唯一出处（M4-B §5.2）。

`annotations.attachments_json` 是旧列，迁移只增不改，保持 `'[]'`；读它等于读一份
永远不会更新的副本。文件路径存**相对**的 `storage_path`：换机器只改根目录，
不动库里的每一行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

_COLUMNS = (
    "id, annotation_id, user_id, filename, media_type, size_bytes, sha256,"
    " storage_path, position, created_at"
)


@dataclass(frozen=True)
class Attachment:
    id: str
    annotation_id: str
    user_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    storage_path: str
    position: int
    created_at: datetime


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _require_text(value: str, what: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValidationError(f"附件的 {what} 不能为空")
    return text


class AttachmentRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def create(
        self,
        annotation_id: str,
        user_id: str,
        *,
        filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
        storage_path: str,
        position: int = 0,
    ) -> Attachment:
        annotation_id = _require_text(annotation_id, "annotation_id")
        user_id = _require_text(user_id, "user_id")
        filename = _require_text(filename, "文件名")
        media_type = _require_text(media_type, "媒体类型")
        storage_path = _require_text(storage_path, "存储路径")
        sha256 = _require_text(sha256, "sha256")
        if size_bytes <= 0:
            raise ValidationError("附件大小必须大于 0：空文件不是一张截图")
        if position < 0:
            raise ValidationError("附件位置不能为负")
        attachment_id = new_id()
        moment = self.clock.now_utc()
        self.db.execute(
            "INSERT INTO annotation_attachments"
            " (id, annotation_id, user_id, filename, media_type, size_bytes, sha256,"
            " storage_path, position, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                attachment_id,
                annotation_id,
                user_id,
                filename,
                media_type,
                int(size_bytes),
                sha256,
                storage_path,
                int(position),
                moment,
            ],
        )
        return Attachment(
            id=attachment_id,
            annotation_id=annotation_id,
            user_id=user_id,
            filename=filename,
            media_type=media_type,
            size_bytes=int(size_bytes),
            sha256=sha256,
            storage_path=storage_path,
            position=int(position),
            created_at=moment,
        )

    def get(self, attachment_id: str) -> Attachment | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM annotation_attachments WHERE id = ?", [attachment_id]
        )
        return None if rows.is_empty() else _to_attachment(rows.to_dicts()[0])

    def list_for(self, annotation_id: str) -> list[Attachment]:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM annotation_attachments WHERE annotation_id = ?"
            " ORDER BY position, created_at, id",
            [annotation_id],
        )
        return [_to_attachment(row) for row in rows.to_dicts()]

    def count_for(self, annotation_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT count(*) FROM annotation_attachments WHERE annotation_id = ?",
                [annotation_id],
            )
            or 0
        )

    def total_bytes(self, user_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT coalesce(sum(size_bytes), 0) FROM annotation_attachments"
                " WHERE user_id = ?",
                [user_id],
            )
            or 0
        )

    def usage_by_user(self) -> list[tuple[str, int, int]]:
        """每人 `(用户, 附件数, 字节)`：admin 用量表（D136 的「计数与占用」），按字节降序。

        聚合在 SQL 一次做完：全表扫一遍比每人一次 `total_bytes()` 少 N 条查询，
        而这一屏本来就要整张表。没附件的人不出现——用量表列的是占用了磁盘的人。
        """
        rows = self.db.query(
            "SELECT user_id, count(*) AS cnt, COALESCE(sum(size_bytes), 0) AS bytes"
            " FROM annotation_attachments GROUP BY user_id ORDER BY bytes DESC",
            [],
        ).to_dicts()
        return [(str(r["user_id"]), int(r["cnt"]), int(r["bytes"])) for r in rows]

    def delete(self, attachment_id: str) -> bool:
        deleted = self.db.scalar(
            "DELETE FROM annotation_attachments WHERE id = ? RETURNING id", [attachment_id]
        )
        return deleted is not None

    def delete_for(self, annotation_id: str) -> list[Attachment]:
        """单语句 `DELETE ... RETURNING`：拿到的就是被删掉的那些行。

        调用方要拿着这些路径去删文件，所以「读到行」和「行没了」必须发生在同一句里：
        先查再删的话，两条自动提交语句之间一旦失败，就留下查过但没删的孤儿行。
        RETURNING 的返回顺序不保证，所以排序在 Python 里做。
        """
        rows = self.db.query(
            f"DELETE FROM annotation_attachments WHERE annotation_id = ? RETURNING {_COLUMNS}",
            [annotation_id],
        )
        return sorted(
            (_to_attachment(row) for row in rows.to_dicts()),
            key=lambda row: (row.position, row.created_at, row.id),
        )

    def paths(self) -> set[str]:
        rows = self.db.query("SELECT storage_path FROM annotation_attachments")
        return {str(row["storage_path"]) for row in rows.to_dicts()}

    def iter_all(self) -> list[Attachment]:
        rows = self.db.query(f"SELECT {_COLUMNS} FROM annotation_attachments")
        return [_to_attachment(row) for row in rows.to_dicts()]


def _to_attachment(row: dict) -> Attachment:
    return Attachment(
        id=str(row["id"]),
        annotation_id=str(row["annotation_id"]),
        user_id=str(row["user_id"]),
        filename=str(row["filename"]),
        media_type=str(row["media_type"]),
        size_bytes=int(row["size_bytes"]),
        sha256=str(row["sha256"]),
        storage_path=str(row["storage_path"]),
        position=int(row["position"]),
        created_at=_utc(row["created_at"]),
    )
