"""批注的保留策略（M4-B §5.4）：软删满期硬清除，顺手扫掉孤儿文件。

硬清除是**墓碑**而整行删除：`id/user_id/target_type/target_ref/target_label/
created_at/deleted_at/purged_at` 留下，复盘计数与审计仍成立。正文、标签、revision
与附件行/文件消失——「30 天后就再也读不到」这句话必须是真的。

文件删除在库事务提交之后：反过来的话，事务回滚会留下「库里有行、磁盘没文件」。
中途崩溃只会留下孤儿文件，下一次扫描按 mtime 收走它们。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.services.attachments import safe_path
from sams.storage.db import Database
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.attachments import AttachmentRepo

logger = logging.getLogger("sams.services.annotation_prune")

PURGED_DAYS = 30
ORPHAN_DAYS = 30


@dataclass(frozen=True)
class AnnotationPruneReport:
    annotations: int = 0
    attachments: int = 0
    orphan_files: int = 0
    orphan_bytes: int = 0
    bytes: int = 0
    dry_run: bool = False


class AnnotationPruner:
    def __init__(
        self,
        db: Database,
        annotations: AnnotationRepo,
        attachments: AttachmentRepo,
        *,
        root: Path,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.db = db
        self.annotations = annotations
        self.attachments = attachments
        self.root = Path(root)
        self.clock = clock

    def run(
        self,
        *,
        purged_days: int = PURGED_DAYS,
        orphan_days: int = ORPHAN_DAYS,
        dry_run: bool = False,
    ) -> AnnotationPruneReport:
        for name, value in (("purged_days", purged_days), ("orphan_days", orphan_days)):
            if value < 1:
                raise ValidationError(f"{name}={value}：至少要 1；0 会把今天还在用的批注也删掉")
        now = self.clock.now_utc()
        purge_cutoff = now - timedelta(days=purged_days)
        orphan_cutoff = now - timedelta(days=orphan_days)
        targets = self.db.query(
            "SELECT id FROM annotations"
            " WHERE deleted_at IS NOT NULL AND purged_at IS NULL AND deleted_at <= ?",
            [purge_cutoff],
        ).to_dicts()
        purge_ids = [str(row["id"]) for row in targets]
        attachments = [row for note_id in purge_ids for row in self.attachments.list_for(note_id)]
        referenced = self.attachments.paths()
        orphans = self._orphans(referenced, orphan_cutoff)
        if not dry_run and purge_ids:
            with self.db.transaction():
                for note_id in purge_ids:
                    self.db.execute(
                        "DELETE FROM annotation_revisions WHERE annotation_id = ?", [note_id]
                    )
                    self.attachments.delete_for(note_id)
                    self.db.execute(
                        "UPDATE annotations SET body_md = '', tags_json = '[]', pinned = FALSE,"
                        " purged_at = ?, updated_at = ?"
                        " WHERE id = ? AND deleted_at IS NOT NULL AND purged_at IS NULL",
                        [now, now, note_id],
                    )
            for row in attachments:
                self._unlink(row.storage_path)
        if not dry_run:
            for path, _ in orphans:
                path.unlink(missing_ok=True)
        attachment_bytes = sum(row.size_bytes for row in attachments)
        return AnnotationPruneReport(
            annotations=len(purge_ids),
            attachments=len(attachments),
            orphan_files=len(orphans),
            orphan_bytes=sum(size for _, size in orphans),
            bytes=attachment_bytes + sum(size for _, size in orphans),
            dry_run=dry_run,
        )

    def _orphans(self, referenced: set[str], cutoff: datetime) -> list[tuple[Path, int]]:
        if not self.root.exists():
            return []
        found: list[tuple[Path, int]] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root).as_posix()
            if relative in referenced:
                continue
            stat = path.stat()
            moment = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            if moment > cutoff:
                continue
            found.append((path, int(stat.st_size)))
        return found

    def _unlink(self, relative: str) -> None:
        try:
            safe_path(self.root, relative).unlink(missing_ok=True)
        except OSError as exc:  # 权限/挂载点：留给孤儿扫描，不让一次 prune 全挂
            logger.warning("附件删除失败 %s: %s", relative, exc)
