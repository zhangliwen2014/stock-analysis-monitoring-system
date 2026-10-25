"""软删满 30 天的硬清除与孤儿文件（M4-B §5.4）。

硬清除保留墓碑：`id/user_id/target_type/target_ref/target_label/created_at/deleted_at/
purged_at` 还在，计数与审计仍成立；正文、标签、revision 与附件消失。
孤儿文件按修改时间判断，不按目录名：换机器复制过来时 mtime 可能被保留，
而那正是「它不是今天刚丢下的」的证据。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from support import FrozenClock

from sams.core.errors import NotFound, ValidationError
from sams.services.annotation_prune import AnnotationPruner
from sams.services.annotations import AnnotationService
from sams.services.attachments import AttachmentService
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.attachments import AttachmentRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

PWD = "correct-horse-battery"


def image_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def world(db, settings, tmp_path):
    clock = FrozenClock(datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    users = UserRepo(db)
    user = users.create("trader", PWD)
    symbols = SymbolRepo(db)
    accounts = SimAccountRepo(db)
    annotations = AnnotationService(
        AnnotationRepo(db, clock=clock),
        users,
        symbols,
        ThesisRepo(db),
        RuleRepo(db, clock=clock),
        AlertRepo(db, clock=clock),
        accounts,
        SimOrderRepo(db, accounts, markets=settings.markets, symbols=symbols),
        settings,
    )
    store = AttachmentRepo(db, clock=clock)
    service = AttachmentService(store, annotations, settings, root=tmp_path / "attachments")
    note = annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md="要删"
    )
    annotations.edit(user.id, note.id, body_md="改过一版")  # 硬清除要连 revision 一起删
    kept = annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md="留着"
    )
    rows = [
        service.add(user.id, note.id, filename=f"{i}.png", data=image_bytes()) for i in range(2)
    ]
    return SimpleNamespace(
        db=db,
        clock=clock,
        users=users,
        user=user,
        annotations=annotations,
        store=store,
        service=service,
        note=note,
        kept=kept,
        rows=rows,
        root=tmp_path / "attachments",
        pruner=AnnotationPruner(
            db, AnnotationRepo(db, clock=clock), store, root=tmp_path / "attachments", clock=clock
        ),
    )


def age(world, days):
    world.clock.advance(days=days)


def test_a_recently_deleted_note_is_left_alone(world):
    world.annotations.delete(world.user.id, world.note.id)
    age(world, 29)
    report = world.pruner.run(purged_days=30, orphan_days=30)
    assert report.annotations == 0
    with pytest.raises(NotFound):
        world.annotations.get(world.user.id, world.note.id)
    assert world.store.list_for(world.note.id)


def test_purging_clears_content_but_keeps_the_tombstone(world):
    world.annotations.delete(world.user.id, world.note.id)
    assert world.db.scalar(
        "SELECT count(*) FROM annotation_revisions WHERE annotation_id = ?", [world.note.id]
    ) == 1
    age(world, 31)
    report = world.pruner.run(purged_days=30, orphan_days=30)
    assert report.annotations == 1
    assert report.attachments == 2
    row = world.db.query(
        "SELECT body_md, tags_json, pinned, deleted_at, purged_at, target_label"
        " FROM annotations WHERE id = ?",
        [world.note.id],
    ).to_dicts()[0]
    assert row["body_md"] == ""
    assert row["tags_json"] == "[]"
    assert row["pinned"] is False
    assert row["deleted_at"] is not None and row["purged_at"] is not None
    assert row["target_label"] == "600519"
    assert world.db.scalar(
        "SELECT count(*) FROM annotation_revisions WHERE annotation_id = ?", [world.note.id]
    ) == 0
    assert world.store.list_for(world.note.id) == []
    assert all(not (world.root / r.storage_path).exists() for r in world.rows)


def test_the_live_note_keeps_its_files(world):
    world.annotations.delete(world.user.id, world.note.id)
    age(world, 31)
    world.pruner.run(purged_days=30, orphan_days=30)
    assert world.db.scalar(
        "SELECT count(*) FROM annotations WHERE id = ? AND purged_at IS NULL",
        [world.kept.id],
    ) == 1


def test_an_old_unreferenced_file_is_removed(world):
    orphan = world.root / world.user.id / "2026" / "01" / "orphan.png"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    old = world.clock.now_utc().timestamp() - 31 * 86400
    os.utime(orphan, (old, old))
    report = world.pruner.run(purged_days=30, orphan_days=30)
    assert report.orphan_files == 1
    assert report.orphan_bytes == 6
    assert not orphan.exists()


def test_a_referenced_file_is_never_treated_as_an_orphan(world):
    old = world.clock.now_utc().timestamp() - 31 * 86400
    for row in world.rows:
        os.utime(world.root / row.storage_path, (old, old))
    report = world.pruner.run(purged_days=30, orphan_days=30)
    assert report.orphan_files == 0
    assert all((world.root / row.storage_path).exists() for row in world.rows)


def test_a_dry_run_changes_nothing(world):
    world.annotations.delete(world.user.id, world.note.id)
    age(world, 31)
    report = world.pruner.run(purged_days=30, orphan_days=30, dry_run=True)
    assert report.dry_run is True
    assert report.annotations == 1 and report.attachments == 2
    assert world.store.list_for(world.note.id)
    assert all((world.root / row.storage_path).exists() for row in world.rows)
    assert world.db.scalar(
        "SELECT body_md FROM annotations WHERE id = ?", [world.note.id]
    ) != ""


@pytest.mark.parametrize("field", ("purged_days", "orphan_days"))
def test_retention_must_be_positive(world, field):
    with pytest.raises(ValidationError):
        world.pruner.run(**{field: 0})


def test_an_old_tmp_part_file_is_reclaimed(world):
    stale = world.root / ".tmp" / "deadbeef.part"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"partial")
    old = world.clock.now_utc().timestamp() - 31 * 86400
    os.utime(stale, (old, old))
    report = world.pruner.run(purged_days=30, orphan_days=30)
    assert report.orphan_files == 1
    assert not stale.exists()


def test_a_fresh_tmp_part_file_is_left_alone(world):
    live = world.root / ".tmp" / "writing.part"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"partial")
    report = world.pruner.run(purged_days=30, orphan_days=30)
    assert report.orphan_files == 0
    assert live.exists()


def test_an_unreadable_orphan_entry_is_skipped_not_fatal(world, monkeypatch):
    candidate = world.root / "u1" / "2026" / "01"
    candidate.mkdir(parents=True)
    path = candidate / "orphan.png"
    path.write_bytes(b"x")
    old = world.clock.now_utc().timestamp() - 31 * 86400
    os.utime(path, (old, old))
    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        # rglob 给出的是新的 Path 实例，只能 patch 类；带参数的调用是 is_file/exists 发的
        if self == path and not kwargs:
            raise PermissionError("挂载点抽风")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    report = world.pruner.run(purged_days=30, orphan_days=30)
    assert report.orphan_files == 0
    assert path.exists()
    monkeypatch.setattr(Path, "stat", real_stat)
    report = world.pruner.run(purged_days=30, orphan_days=30)
    assert report.orphan_files == 1
    assert not path.exists()
