"""附件行：排序、配额求和与按批注批量删除（M4-B §5.2）。

文件本身不在这里：仓库只管「哪一行指向哪个相对路径」。把文件读写放进仓库，
测试就得为每个用例准备一个临时目录，而这里要钉的只是行。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.storage.repos.attachments import AttachmentRepo

NOW = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)


@pytest.fixture
def repo(db):
    return AttachmentRepo(db, clock=FrozenClock(NOW))


def add(repo, annotation_id="note-1", user_id="trader", **over):
    kw = {
        "filename": "截图.png",
        "media_type": "image/png",
        "size_bytes": 1024,
        "sha256": "a" * 64,
        "storage_path": f"{user_id}/2026/10/att-1.png",
        "position": 0,
    }
    kw.update(over)
    return repo.create(annotation_id, user_id, **kw)


def test_round_trip_and_clock(repo):
    made = add(repo)
    got = repo.get(made.id)
    assert got == made
    assert got.created_at == NOW
    assert got.filename == "截图.png"


def test_rows_come_back_in_position_order(repo):
    second = add(repo, storage_path="trader/2026/10/b.png", position=1)
    first = add(repo, storage_path="trader/2026/10/a.png", position=0)
    assert [row.id for row in repo.list_for("note-1")] == [first.id, second.id]


def test_count_and_total_bytes_are_per_annotation_and_user(repo):
    add(repo, annotation_id="note-1", size_bytes=100)
    add(repo, annotation_id="note-1", size_bytes=200, storage_path="trader/2026/10/b.png")
    add(repo, annotation_id="note-2", user_id="keeper", size_bytes=400,
        storage_path="keeper/2026/10/c.png")
    assert repo.count_for("note-1") == 2
    assert repo.total_bytes("trader") == 300
    assert repo.total_bytes("keeper") == 400
    assert repo.total_bytes("ghost") == 0


def test_delete_for_returns_the_rows_it_removed(repo):
    first = add(repo)
    second = add(repo, storage_path="trader/2026/10/b.png")
    removed = repo.delete_for("note-1")
    assert [row.id for row in removed] == [first.id, second.id]
    assert repo.list_for("note-1") == []
    assert repo.delete_for("note-1") == []


def test_paths_are_the_set_the_pruner_needs(repo):
    add(repo)
    add(repo, user_id="keeper", storage_path="keeper/2026/10/c.png")
    assert repo.paths() == {"trader/2026/10/att-1.png", "keeper/2026/10/c.png"}


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("filename", ""),
        ("media_type", ""),
        ("storage_path", ""),
        ("size_bytes", 0),
        ("position", -1),
    ),
)
def test_bad_rows_are_refused(repo, field, value):
    with pytest.raises(ValidationError):
        add(repo, **{field: value})
