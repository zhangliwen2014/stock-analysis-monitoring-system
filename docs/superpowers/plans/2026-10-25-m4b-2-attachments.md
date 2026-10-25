# M4-B 计划 2：附件与回收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让私有批注能带截图：上传时校验与重编码、按用户配额落盘、按可见性授权读取，并由 prune 硬清除过期批注与孤儿文件。

**Architecture:** 新增 `AttachmentRepo`（`annotation_attachments` 表是文件的唯一出处）与 `AttachmentService`（Pillow 重编码、EXIF 去除、长边缩放、配额、原子落盘、读取授权）。回收拆成 `AnnotationPruner`（软删满 30 天硬清除 + 孤儿文件扫描），由现有 `PruneService` 串起来并暴露 CLI 开关。`bootstrap` 把三者接进线程上下文。

**Tech Stack:** Python 3.13、DuckDB、Pillow、pytest、ruff（E,F,I,UP,B,SIM；line-length 100）。

---

## 约束（每个任务都适用）

- 只用 `uv run …`，禁止 pip/venv；禁止 `ruff format`。
- 测试命令一律带 `-m "not live"` 之外的默认筛（项目 pytest 配置已排除 live）。
- 只允许改任务列出的文件；提交用 Bash 里的 `git`（不要 `cd <repo> && …`）。
- 数据库连接每线程一条（D113）；新服务不自己开连接。
- 上限都是**拒绝线**：超了报错，不悄悄截断。

---

### Task 1: 附件仓库

**Files:**
- Create: `src/sams/storage/repos/attachments.py`
- Test: `tests/storage/test_attachments_repo.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/storage/test_attachments_repo.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_attachments_repo.py -q`
Expected: FAIL —— `ModuleNotFoundError: sams.storage.repos.attachments`。

- [ ] **Step 3: 写仓库**

创建 `src/sams/storage/repos/attachments.py`：

```python
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

    def delete(self, attachment_id: str) -> bool:
        before = self.get(attachment_id)
        self.db.execute("DELETE FROM annotation_attachments WHERE id = ?", [attachment_id])
        return before is not None

    def delete_for(self, annotation_id: str) -> list[Attachment]:
        """先取行再删：调用方要拿着这些路径去删文件，删完就再也查不到它们了。"""
        rows = self.list_for(annotation_id)
        self.db.execute(
            "DELETE FROM annotation_attachments WHERE annotation_id = ?", [annotation_id]
        )
        return rows

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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_attachments_repo.py -q`
Expected: 全绿。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache src/sams/storage/repos/attachments.py tests/storage/test_attachments_repo.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/repos/attachments.py tests/storage/test_attachments_repo.py
git commit -m "feat(storage): 附件行仓库，路径只存相对值（M4-B §5.2）"
```

---

### Task 2: 附件服务：重编码、配额与读取

**Files:**
- Modify: `pyproject.toml`、`uv.lock`（`uv add pillow`）
- Create: `src/sams/services/attachments.py`
- Test: `tests/services/test_attachments_service.py`

- [ ] **Step 1: 加依赖**

Run: `uv add pillow`
Expected: `pyproject.toml` 的 dependencies 出现 `pillow`，`uv.lock` 更新。

Run: `uv run python -c "from PIL import Image; print(Image.__version__)"`
Expected: 打印一个版本号（不限定具体值）。

- [ ] **Step 2: 写失败测试**

创建 `tests/services/test_attachments_service.py`：

```python
"""附件用例：先看字节再解码，重编码之后才落盘（M4-B §5.2）。

这里的每一张图都是用例现造的：不读仓库里的样张，也不依赖 Pillow 的默认保存参数。
「重编码」用三条可观察的事实钉住——输出文件的文件头、EXIF 不再出现、长边不超过 2560。
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from support import FrozenClock

import sams.services.attachments as attachments_module
from sams.core.errors import NotFound, ValidationError
from sams.services.annotations import AnnotationService
from sams.services.attachments import AttachmentService, attachments_root
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.attachments import AttachmentRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
USER, OTHER = "trader", "keeper"
PWD = "correct-horse-battery"


def image_bytes(fmt="PNG", size=(64, 48), color=(10, 20, 30, 255), exif=None):
    image = Image.new("RGBA" if len(color) == 4 else "RGB", size, color)
    buffer = BytesIO()
    kw = {"format": fmt}
    if exif is not None:
        kw["exif"] = exif
    image.save(buffer, **kw)
    return buffer.getvalue()


@pytest.fixture
def world(db, settings, tmp_path):
    clock = FrozenClock(NOW)
    users = UserRepo(db)
    user = users.create(USER, PWD)
    other = users.create(OTHER, PWD)
    symbols = SymbolRepo(db)
    theses = ThesisRepo(db)
    rules = RuleRepo(db, clock=clock)
    alerts = AlertRepo(db, clock=clock)
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=symbols)
    annotations = AnnotationService(
        AnnotationRepo(db, clock=clock),
        users,
        symbols,
        theses,
        rules,
        alerts,
        accounts,
        orders,
        settings,
    )
    note = annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md="先看图"
    )
    other_note = annotations.create(
        other.id, target_type="symbol", target_ref="A:600519", body_md="别人的"
    )
    store = AttachmentRepo(db, clock=clock)
    service = AttachmentService(store, annotations, settings, root=tmp_path / "attachments")
    return SimpleNamespace(
        clock=clock,
        users=users,
        user=user,
        other=other,
        annotations=annotations,
        note=note,
        other_note=other_note,
        store=store,
        service=service,
        root=tmp_path / "attachments",
    )


def add(world, data=None, *, note=None, user=None, filename="截图.png"):
    return world.service.add(
        (user or world.user).id,
        (note or world.note).id,
        filename=filename,
        data=image_bytes() if data is None else data,
    )


def stored(world, row):
    return (world.root / row.storage_path).read_bytes()


def test_a_png_is_reencoded_and_stored_under_the_user_folder(world):
    row = add(world)
    assert row.media_type == "image/png"
    assert row.storage_path.startswith(f"{world.user.id}/")
    assert stored(world, row).startswith(b"\x89PNG\r\n\x1a\n")
    assert world.service.resolve(world.user.id, world.note.id, row.id).id == row.id


def test_a_jpeg_is_reencoded_to_jpeg_and_loses_exif(world):
    exif = Image.Exif()
    exif[0x010F] = "secret-camera"
    row = add(world, image_bytes("JPEG", color=(200, 10, 10), exif=exif))
    assert row.media_type == "image/jpeg"
    raw = stored(world, row)
    assert raw.startswith(b"\xff\xd8\xff")
    assert b"secret-camera" not in raw
    with Image.open(BytesIO(raw)) as image:
        assert not dict(image.getexif())


def test_a_transparent_image_stays_png(world):
    row = add(world, image_bytes("PNG", size=(8, 8), color=(0, 0, 0, 0)))
    assert row.media_type == "image/png"


def test_the_long_edge_is_capped(world):
    row = add(world, image_bytes("PNG", size=(3000, 100), color=(1, 2, 3, 255)))
    with Image.open(BytesIO(stored(world, row))) as image:
        assert max(image.size) == 2560


def test_a_pixel_bomb_is_refused(world, monkeypatch):
    monkeypatch.setattr(attachments_module, "MAX_PIXELS", 100)
    with pytest.raises(ValidationError, match="像素"):
        add(world, image_bytes("PNG", size=(64, 48)))


@pytest.mark.parametrize(
    ("payload", "word"),
    (
        (b"GIF89a" + b"\x00" * 16, "GIF"),
        (b"%PDF-1.4\n%\xe2\xe3", "PDF"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "SVG"),
        (b"not an image at all", "PNG"),
    ),
)
def test_only_png_jpeg_webp_are_accepted(world, payload, word):
    with pytest.raises(ValidationError, match=word):
        add(world, payload)


def test_an_oversized_upload_is_refused_before_decoding(world, settings):
    world.service.settings = replace(
        settings, annotations=replace(settings.annotations, max_attachment_mb=1)
    )
    with pytest.raises(ValidationError, match="单张"):
        add(world, b"x" * (attachments_module.MB + 1))


def test_the_attachment_cap_is_per_annotation(world, settings):
    world.service.settings = replace(
        settings, annotations=replace(settings.annotations, max_attachments=1)
    )
    add(world)
    with pytest.raises(ValidationError, match="最多"):
        add(world, image_bytes("PNG", size=(4, 4)))


def test_the_user_quota_counts_previous_uploads(world, settings, monkeypatch):
    world.service.settings = replace(
        settings, annotations=replace(settings.annotations, max_user_total_mb=1)
    )
    monkeypatch.setattr(world.store, "total_bytes", lambda user_id: attachments_module.MB)
    with pytest.raises(ValidationError, match="配额"):
        add(world)


def test_another_users_annotation_is_not_found(world):
    made = world.annotations.create(
        world.other.id, target_type="symbol", target_ref="A:600519", body_md="别人的"
    )
    with pytest.raises(NotFound):
        world.service.add(
            world.user.id, made.id, filename="x.png", data=image_bytes()
        )


def test_reading_and_listing_hide_another_users_attachment(world):
    row = add(world)
    with pytest.raises(NotFound):
        world.service.resolve(world.other.id, world.note.id, row.id)
    with pytest.raises(NotFound):
        world.service.list_for(world.other.id, world.note.id)


def test_a_deleted_annotation_hides_its_attachments(world):
    row = add(world)
    world.annotations.delete(world.user.id, world.note.id)
    with pytest.raises(NotFound):
        world.service.resolve(world.user.id, world.note.id, row.id)


def test_a_missing_file_is_reported_not_raised(world):
    row = add(world)
    (world.root / row.storage_path).unlink()
    assert world.service.load(row) is None
    assert world.service.missing_count() == 1


def test_a_storage_path_cannot_escape_the_root(world):
    from sams.core.errors import StorageError
    from sams.storage.repos.attachments import Attachment

    row = Attachment(
        id="x",
        annotation_id=world.note.id,
        user_id=world.user.id,
        filename="x.png",
        media_type="image/png",
        size_bytes=1,
        sha256="a" * 64,
        storage_path="../../etc/passwd",
        position=0,
        created_at=NOW,
    )
    with pytest.raises(StorageError, match="根目录"):
        world.service.path_of(row)


def test_the_default_root_sits_next_to_the_database(tmp_path, settings):
    bare = replace(settings, annotations=replace(settings.annotations, attachments_dir=""))
    assert attachments_root(bare) == Path(settings.db_path).parent / "attachments"
    configured = replace(
        settings, annotations=replace(settings.annotations, attachments_dir=str(tmp_path))
    )
    assert attachments_root(configured) == tmp_path
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/services/test_attachments_service.py -q`
Expected: FAIL —— `ModuleNotFoundError: sams.services.attachments`。

- [ ] **Step 4: 写服务**

创建 `src/sams/services/attachments.py`：

```python
"""附件用例（M4-B §5.2）：校验、重编码、落盘与下载授权。

上传的文件只经过一次「先看字节、再解码、再重编码」：扩展名与客户端给的
Content-Type 都不作数。重编码同时完成去 EXIF、缩长边与统一格式，所以磁盘上的
每个文件都是我们自己写出来的，而不是用户交上来的那一个。

写入顺序固定为「暂存 → 校验/重编码 → 移入最终路径 → 写库行」。反过来的话，
进程在两步之间崩溃会留下「库里有行、磁盘没文件」——那是一条永远显示「附件缺失」
的记录；现在的顺序最坏只留下一个孤儿文件，由 prune 回收。
"""

from __future__ import annotations

import hashlib
import logging
import os
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import NotFound, StorageError, ValidationError
from sams.services.annotations import AnnotationService
from sams.storage.ids import new_id
from sams.storage.repos.attachments import Attachment, AttachmentRepo

logger = logging.getLogger("sams.services.attachments")

MB = 1024 * 1024
#: 长边上限：手机截图的 3x 分辨率通常在这个数以内，再大只是占盘
MAX_EDGE = 2560
#: 解压后像素数上限（图片炸弹）。40MP 足够放下任何一张研究截图
MAX_PIXELS = 40_000_000
JPEG_QUALITY = 85
TMP_DIR = ".tmp"


def attachments_root(settings: Settings) -> Path:
    """附件根目录：配置为空时落在库文件旁边，备份时与库一起复制。"""
    configured = settings.annotations.attachments_dir.strip()
    if configured:
        return Path(configured)
    return Path(settings.db_path).parent / "attachments"


def safe_path(root: Path, relative: str) -> Path:
    """把库里那个相对路径拼回绝对路径，并拒绝 `..` 逃逸。

    两处（下载与 prune）必须用同一份检查：各写一遍的话，先改的那一处会把另一处
    留在「只差一个 `..` 就能读 /etc」的状态里。
    """
    base = root.resolve()
    path = (base / relative).resolve()
    if path != base and base not in path.parents:
        raise StorageError(
            f"附件路径 {relative!r} 逃出了根目录：库里的相对路径只能是上传时写进去的那一个"
        )
    return path


def _sniff(data: bytes) -> tuple[str, str]:
    head = data[:512]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        raise ValidationError("不收 GIF：动图会绕过「一张图」的限制，也只会在详情里失控")
    if head.startswith(b"%PDF-"):
        raise ValidationError("不收 PDF：它不是截图，内嵌脚本也不能靠图片通道防")
    if b"<svg" in head.lower():
        raise ValidationError("不收 SVG：它可以内嵌脚本，重编码也去不干净")
    raise ValidationError("只收 PNG/JPEG/WebP：文件头不是这三种之一")


def _reencode(data: bytes) -> tuple[str, str, bytes]:
    """校验并重编码，返回 `(media_type, ext, payload)`。"""
    _sniff(data)
    try:
        with Image.open(BytesIO(data)) as probe:
            probe.verify()
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise ValidationError(
                    f"图片解压后有 {width * height} 像素，超过上限 {MAX_PIXELS}："
                    "这不是一张截图"
                )
            image = ImageOps.exif_transpose(image)
            if max(image.size) > MAX_EDGE:
                image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            if has_alpha:
                image.convert("RGBA").save(buffer, format="PNG", optimize=True)
                return "image/png", "png", buffer.getvalue()
            image.convert("RGB").save(
                buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True
            )
            return "image/jpeg", "jpg", buffer.getvalue()
    except ValidationError:
        raise
    except UnidentifiedImageError as exc:
        raise ValidationError("读不出这张图：文件头像是图片，内容却不是") from exc
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"图片解码失败：{exc}") from exc


class AttachmentService:
    def __init__(
        self,
        attachments: AttachmentRepo,
        annotations: AnnotationService,
        settings: Settings,
        *,
        root: Path,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.attachments = attachments
        self.annotations = annotations
        self.settings = settings
        #: 根目录由组装根决定：容器里的服务不该自己读环境变量
        self.root = Path(root)
        self.clock = clock

    # --- 写 -----------------------------------------------------------------------------

    def add(
        self, user_id: str, annotation_id: str, *, filename: str, data: bytes
    ) -> Attachment:
        # 归属与「软删后不可写」都由批注服务回答：别人的批注统一 NotFound
        self.annotations.get(user_id, annotation_id)
        limits = self.settings.annotations
        if len(data) > limits.max_attachment_mb * MB:
            raise ValidationError(
                f"单张附件最多 {limits.max_attachment_mb} MB（收到 {len(data) / MB:.1f} MB）"
            )
        if self.attachments.count_for(annotation_id) >= limits.max_attachments:
            raise ValidationError(
                f"一条批注最多 {limits.max_attachments} 张附件："
                "更多截图请写成第二条批注"
            )
        if self.attachments.total_bytes(user_id) + len(data) > limits.max_user_total_mb * MB:
            raise ValidationError(
                f"附件总配额 {limits.max_user_total_mb} MB 已用满："
                "先删掉旧批注，等 prune 回收后再传"
            )
        media_type, ext, payload = _reencode(data)
        display = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        display = display[:120] or f"image.{ext}"
        attachment_id = new_id()
        moment = self.clock.now_utc()
        relative = f"{user_id}/{moment:%Y}/{moment:%m}/{attachment_id}.{ext}"
        final = self.root / relative
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = self.root / TMP_DIR
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"{attachment_id}.part"
        tmp.write_bytes(payload)
        os.replace(tmp, final)
        try:
            return self.attachments.create(
                annotation_id,
                user_id,
                filename=display,
                media_type=media_type,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                storage_path=relative,
                position=self.attachments.count_for(annotation_id),
            )
        except Exception:
            # 库行没写成，磁盘上就不该留这一张：否则它永远是一条无人引用的孤儿
            final.unlink(missing_ok=True)
            raise

    # --- 读 -----------------------------------------------------------------------------

    def list_for(
        self, viewer_id: str, annotation_id: str, *, team_sharing: bool = False
    ) -> list[Attachment]:
        self.annotations.get(viewer_id, annotation_id, team_sharing=team_sharing)
        return self.attachments.list_for(annotation_id)

    def resolve(
        self,
        viewer_id: str,
        annotation_id: str,
        attachment_id: str,
        *,
        team_sharing: bool = False,
    ) -> Attachment:
        """下载前的授权：先证明批注可见，再证明这一行属于那条批注。"""
        self.annotations.get(viewer_id, annotation_id, team_sharing=team_sharing)
        row = self.attachments.get(attachment_id)
        if row is None or row.annotation_id != annotation_id:
            raise NotFound(f"附件 {attachment_id} 不存在或不属于这条批注")
        return row

    def path_of(self, attachment: Attachment) -> Path:
        return safe_path(self.root, attachment.storage_path)

    def load(self, attachment: Attachment) -> bytes | None:
        """文件缺失返回 `None`：那是「附件缺失」的一行，不是一个 500。"""
        try:
            return self.path_of(attachment).read_bytes()
        except FileNotFoundError:
            logger.warning("附件文件缺失：%s", attachment.storage_path)
            return None

    def missing_count(self) -> int:
        return sum(1 for row in self.attachments.iter_all() if not self.path_of(row).is_file())
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/services/test_attachments_service.py tests/storage/test_attachments_repo.py -q`
Expected: 全绿。若 `test_a_missing_file_is_reported_not_raised` 失败，先检查 `missing_count` 是否真的遍历了全部行。

- [ ] **Step 6: ruff**

Run: `uv run ruff check --no-cache src/sams/services/attachments.py src/sams/storage/repos/attachments.py tests/services/test_attachments_service.py tests/storage/test_attachments_repo.py`
Expected: All checks passed。

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml uv.lock src/sams/services/attachments.py src/sams/storage/repos/attachments.py tests/services/test_attachments_service.py tests/storage/test_attachments_repo.py
git commit -m "feat(annotations): 附件重编码、配额与授权读取（M4-B §5.2）"
```

---

### Task 3: 硬清除与孤儿回收

**Files:**
- Create: `src/sams/services/annotation_prune.py`
- Test: `tests/services/test_annotation_prune.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/services/test_annotation_prune.py`：

```python
"""软删满 30 天的硬清除与孤儿文件（M4-B §5.4）。

硬清除保留墓碑：`id/user_id/target_type/target_ref/target_label/created_at/deleted_at/
purged_at` 还在，计数与审计仍成立；正文、标签、revision 与附件消失。
孤儿文件按修改时间判断，不按目录名：换机器复制过来时 mtime 可能被保留，
而那正是「它不是今天刚丢下的」的证据。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from io import BytesIO
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
    rows = [service.add(user.id, note.id, filename=f"{i}.png", data=image_bytes()) for i in range(2)]
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_annotation_prune.py -q`
Expected: FAIL —— `ModuleNotFoundError: sams.services.annotation_prune`。

- [ ] **Step 3: 写回收服务**

创建 `src/sams/services/annotation_prune.py`：

```python
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
from sams.services.attachments import TMP_DIR, safe_path
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
        self, *, purged_days: int = PURGED_DAYS, orphan_days: int = ORPHAN_DAYS, dry_run: bool = False
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
                        " purged_at = ?, updated_at = ? WHERE id = ?",
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
            if TMP_DIR in path.relative_to(self.root).parts:
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
```

> 注：`safe_path` 越界时抛 `StorageError`，那个异常会终止整次 prune；这比静默跳过安全——
> 库里的路径如果已经逃出根目录，说明有人改过库，继续删别的文件只会掩盖它。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/services/test_annotation_prune.py -q`
Expected: 全绿。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache src/sams/services/annotation_prune.py tests/services/test_annotation_prune.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/services/annotation_prune.py tests/services/test_annotation_prune.py
git commit -m "feat(annotations): 软删满期硬清除与孤儿文件回收（M4-B §5.4）"
```

---

### Task 4: prune 服务与 CLI 开关

**Files:**
- Modify: `src/sams/services/prune.py`
- Modify: `src/sams/cli/db.py`
- Test: `tests/services/test_prune.py`、`tests/cli/test_prune_cli.py`

- [ ] **Step 1: 写失败测试**

在 `tests/services/test_prune.py` 末尾追加：

```python
def test_annotation_steps_appear_only_when_they_have_something_to_do(tmp_path, db):
    """零行不占一步：旧库没有批注时，`sams db prune` 的输出与今天一模一样。"""
    from sams.services.annotation_prune import AnnotationPruner
    from sams.storage.repos.annotations import AnnotationRepo
    from sams.storage.repos.attachments import AttachmentRepo

    clock = FrozenClock(NOW)
    pruner = AnnotationPruner(
        db,
        AnnotationRepo(db, clock=clock),
        AttachmentRepo(db, clock=clock),
        root=tmp_path / "attachments",
        clock=clock,
    )
    service = PruneService(
        db,
        quotes=QuoteRepo(db),
        task_runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        annotations=pruner,
        clock=clock,
    )
    seed_quotes(db)
    report = service.run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    assert "annotations_purged" not in [step.name for step in report.steps]
    assert report.files == 0


def test_dry_run_reports_files_without_deleting_them(tmp_path, db):
    from sams.services.annotation_prune import AnnotationPruner
    from sams.storage.repos.annotations import AnnotationRepo
    from sams.storage.repos.attachments import AttachmentRepo

    clock = FrozenClock(NOW)
    old = tmp_path / "attachments" / "u1" / "2026" / "01" / "orphan.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"12345")
    stamp = (NOW - timedelta(days=31)).timestamp()
    os.utime(old, (stamp, stamp))
    pruner = AnnotationPruner(
        db,
        AnnotationRepo(db, clock=clock),
        AttachmentRepo(db, clock=clock),
        root=tmp_path / "attachments",
        clock=clock,
    )
    service = PruneService(
        db,
        quotes=QuoteRepo(db),
        task_runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        annotations=pruner,
        clock=clock,
    )
    report = service.run(
        quotes_sessions=5, task_runs_days=90, heartbeats_keep=2, dry_run=True
    )
    assert report.files == 1
    assert report.file_bytes == 5
    assert old.exists()
```

> 这两个用例需要 `import os`、`from datetime import timedelta`（文件顶部已有 `timedelta`）。

在 `tests/cli/test_prune_cli.py` 末尾追加：

```python
def add_orphan_file(config_path):
    root = config_path.parent / "attachments" / "u1" / "2026" / "01"
    root.mkdir(parents=True)
    path = root / "orphan.png"
    path.write_bytes(b"1234567")
    old = (datetime.now(UTC) - timedelta(days=31)).timestamp()
    os.utime(path, (old, old))
    return path


def test_prune_reports_and_collects_orphan_attachments(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    capsys.readouterr()
    path = add_orphan_file(config_path)

    assert cli.run(["db", "prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "attachments_orphan: 将删除 1 行" in out
    assert "文件: 将删除 1 个（7 字节）" in out
    assert path.exists()

    assert cli.run(["db", "prune"]) == 0
    out = capsys.readouterr().out
    assert "attachments_orphan: 删除 1 行" in out
    assert not path.exists()


@pytest.mark.parametrize(
    "flag", ["--annotations-purged-days", "--attachments-orphan-days"]
)
def test_annotation_retention_is_a_positive_int(config_path, flag):
    with pytest.raises(SystemExit) as exc:
        cli.run(["db", "prune", flag, "0"])
    assert exc.value.code == 2
```

> CLI 测试文件顶部需要 `import os`。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_prune.py tests/cli/test_prune_cli.py -q`
Expected: FAIL —— `TypeError: PruneService.__init__() got an unexpected keyword argument 'annotations'`，以及 CLI 没有新开关（退出码 2）。

- [ ] **Step 3: 接进 PruneService**

修改 `src/sams/services/prune.py`：

imports 追加：

```python
from sams.services.annotation_prune import (
    ORPHAN_DAYS,
    PURGED_DAYS,
    AnnotationPruner,
)
```

`PruneReport` 加两个带默认值的字段：

```python
    #: 附件文件数/字节数：与 `rows` 分开报——「删了 3 行」与「删了 3 个文件名下的 5 个文件」
    #: 是两件事，混成一个数会让 dry-run 的预估看起来像在数别的东西
    files: int = 0
    file_bytes: int = 0
```

`PruneService.__init__` 加参数（放在 `heartbeats` 之后、`clock` 之前）：

```python
        annotations: AnnotationPruner | None = None,
```

并保存 `self.annotations = annotations`。

`run` 签名加：

```python
        annotations_purged_days: int = PURGED_DAYS,
        attachments_orphan_days: int = ORPHAN_DAYS,
```

校验循环加这两项（值必须是正数，理由同其它三项）。**校验全部通过之后**再跑批注那一侧（顺序不能反：一个 `--task-runs-days 0` 应该在动任何文件之前就被拒）：

```python
        annotation_trim = (
            self.annotations.run(
                purged_days=annotations_purged_days,
                orphan_days=attachments_orphan_days,
                dry_run=dry_run,
            )
            if self.annotations is not None
            else AnnotationPruneReport()
        )
```

dry-run 分支返回：

```python
            return PruneReport(
                dry_run=True,
                steps=steps + _annotation_steps(annotation_trim),
                files=annotation_trim.attachments + annotation_trim.orphan_files,
                file_bytes=annotation_trim.bytes,
            )
```

实删分支在原有事务结束后，为有内容的批注步骤补 `task_runs` 行：

```python
            if self.annotations is not None:
                for step in _annotation_steps(annotation_trim):
                    run_id = self.task_runs.start(JOB_NAME)
                    self.task_runs.finish(
                        run_id, status=OK, rows_affected=step.rows, note=step.name
                    )
```

返回：

```python
        return PruneReport(
            dry_run=False,
            steps=steps + _annotation_steps(annotation_trim),
            files=annotation_trim.attachments + annotation_trim.orphan_files,
            file_bytes=annotation_trim.bytes,
        )
```

文件末尾加：

```python
def _annotation_steps(report: AnnotationPruneReport) -> tuple[PruneStep, ...]:
    """零行不占一步：旧库的输出与今天一模一样，新增的三步只在这台机器真的用过批注时出现。"""
    steps = (
        PruneStep("annotations_purged", report.annotations),
        PruneStep("attachments_purged", report.attachments),
        PruneStep("attachments_orphan", report.orphan_files),
    )
    return tuple(step for step in steps if step.rows)


```

并把 `AnnotationPruneReport` 加进 import。

- [ ] **Step 4: 接进 CLI**

修改 `src/sams/cli/db.py`：

imports 追加：

```python
from sams.services.annotation_prune import ORPHAN_DAYS, PURGED_DAYS
```

`prune` 子命令加两个参数（放在 `--heartbeats-keep` 之后）：

```python
    parser.add_argument(
        "--annotations-purged-days",
        type=_positive,
        default=PURGED_DAYS,
        metavar="N",
        help=f"软删满 N 天的批注硬清除（默认 {PURGED_DAYS}）",
    )
    parser.add_argument(
        "--attachments-orphan-days",
        type=_positive,
        default=ORPHAN_DAYS,
        metavar="N",
        help=f"没有对应批注行的附件文件，超过 N 天未变动的回收（默认 {ORPHAN_DAYS}）",
    )
```

`prune` 处理器把两个值传下去，并在步数之后打印文件数：

```python
    report = container.prune.run(
        quotes_sessions=args.quotes_sessions,
        task_runs_days=args.task_runs_days,
        heartbeats_keep=args.heartbeats_keep,
        annotations_purged_days=args.annotations_purged_days,
        attachments_orphan_days=args.attachments_orphan_days,
        dry_run=args.dry_run,
    )
    verb = "将删除" if report.dry_run else "删除"
    for step in report.steps:
        print(f"{step.name}: {verb} {step.rows} 行")
    if report.files or report.file_bytes:
        print(f"文件: {verb} {report.files} 个（{report.file_bytes} 字节）")
    print(f"{'预计' if report.dry_run else '合计'}: {report.total} 行")
    return 0
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/services/test_prune.py tests/cli/test_prune_cli.py -q`
Expected: 全绿。特别注意既有那条「每一步留一行 task_runs」的断言仍然只看到三步——空库不该多出三行零。

- [ ] **Step 6: ruff**

Run: `uv run ruff check --no-cache src/sams/services/prune.py src/sams/cli/db.py tests/services/test_prune.py tests/cli/test_prune_cli.py`
Expected: All checks passed。

- [ ] **Step 7: 提交**

```bash
git add src/sams/services/prune.py src/sams/cli/db.py tests/services/test_prune.py tests/cli/test_prune_cli.py
git commit -m "feat(prune): 批注硬清除、孤儿附件回收与两个保留开关（M4-B §5.4）"
```

---

### Task 5: 组装接线与全量验证

**Files:**
- Modify: `src/sams/bootstrap.py`
- Test: `tests/test_bootstrap.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_bootstrap.py` 末尾追加：

```python
def test_container_wires_attachments_and_the_pruner(container):
    """附件根与库同目录：备份时复制两样东西就够，不需要再记第二个位置。"""
    from io import BytesIO
    from pathlib import Path

    from PIL import Image

    built, _ = container
    user = built.users.create("writer", "correct-horse-battery")
    note = built.annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md="先看图"
    )
    buffer = BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 4)).save(buffer, format="PNG")
    row = built.attachments.add(user.id, note.id, filename="x.png", data=buffer.getvalue())
    root = Path(built.settings.db_path).parent / "attachments"
    assert (root / row.storage_path).is_file()
    assert built.attachments.resolve(user.id, note.id, row.id).id == row.id
    assert built.attachments_store.get(row.id) is not None
    assert built.prune.annotations is built.annotation_pruner
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bootstrap.py::test_container_wires_attachments_and_the_pruner -q`
Expected: FAIL —— `AttributeError: 'Container' object has no attribute 'attachments'`。

- [ ] **Step 3: 接线**

修改 `src/sams/bootstrap.py`：

imports 追加：

```python
from sams.services.annotation_prune import AnnotationPruner
from sams.services.attachments import AttachmentService, attachments_root
from sams.storage.repos.attachments import AttachmentRepo
```

`ServiceContext` 在 `annotations` 之后加：

```python
    #: 附件仓库：`attachments` 那个名字留给服务
    attachments_store: AttachmentRepo
    #: 附件用例：重编码与配额在这里，仓库只管行
    attachments: AttachmentService
    #: 批注回收：`sams serve` 的每日 prune 与 CLI 走同一个实例
    annotation_pruner: AnnotationPruner
```

`_context_on` 在 `annotations = AnnotationService(...)` 之后加：

```python
    attachments_store = AttachmentRepo(database, clock=clock)
    attachments = AttachmentService(
        attachments_store,
        annotations,
        cfg,
        root=attachments_root(cfg),
        clock=clock,
    )
    annotation_pruner = AnnotationPruner(
        database,
        annotations_store,
        attachments_store,
        root=attachments_root(cfg),
        clock=clock,
    )
```

`PruneService(...)` 构造（`_context_on` 里那一处）加：

```python
        annotations=annotation_pruner,
```

`build_serve` 里第二个 `PruneService(...)`（每日维护那一处）同样加：

```python
            annotations=maintenance_ctx.annotation_pruner,
```

`return ServiceContext(...)` 加：

```python
        attachments_store=attachments_store,
        attachments=attachments,
        annotation_pruner=annotation_pruner,
```

- [ ] **Step 4: 跑接线测试与分层守卫**

Run: `uv run pytest tests/test_bootstrap.py tests/test_layering.py -q`
Expected: 全绿。若分层守卫报 `services/attachments.py` 越界，检查它只 import `core`、`storage` 与 `services`——**不许** import `web`/`monitor`。

- [ ] **Step 5: 全量离线闸**

Run（后台跑，超时 600000 ms）：`uv run pytest -q`
Expected: 全部通过。重点看 `tests/services/test_prune.py` 与 `tests/cli/test_prune_cli.py`：`PruneReport` 的新字段不能改变旧输出。

Run: `uv run ruff check --no-cache src tests`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/bootstrap.py tests/test_bootstrap.py
git commit -m "feat(bootstrap): 附件仓库、附件服务与批注回收进线程上下文（M4-B §3）"
```

---

## 本计划完成时的状态

- 批注可带 PNG/JPEG/WebP 截图：魔数校验、Pillow `verify()`、40MP 上限、去 EXIF、长边 ≤ 2560、统一重编码。
- 每张 ≤ 5 MB、每条 ≤ 5 张、每用户 ≤ 512 MB，超限报错；文件落在 `<root>/<user>/<yyyy>/<mm>/<id>.<ext>`，库里只存相对路径。
- 下载先按批注可见性鉴权；文件缺失返回「缺失」而不是 500；`missing_count()` 供后续健康页使用。
- 软删满 30 天的批注硬清除正文/标签/revision/附件，墓碑保留；30 天前的孤儿文件被回收。
- `sams db prune --annotations-purged-days/--attachments-orphan-days` 与 `--dry-run` 报告批注数、文件数与字节数；零行不占一步。
- 容器里 `attachments_store`、`attachments` 可用，`prune.annotations` 已接。

**下一份计划（计划 3）从这里接：** 推送上下文（`AnnotationContext`、`AlertContextProvider`、`notify_log.context_json`、通道追加与预算）→ 闭环动作服务（`act_with_fill` 同事务成交、thesis/rule/watch 校验、同步写批注）。
