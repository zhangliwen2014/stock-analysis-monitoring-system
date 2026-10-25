"""附件用例：先看字节再解码，重编码之后才落盘（M4-B §5.2）。

这里的每一张图都是用例现造的：不读仓库里的样张，也不依赖 Pillow 的默认保存参数。
「重编码」用三条可观察的事实钉住——输出文件的文件头、EXIF 不再出现、长边不超过 2560。
"""

from __future__ import annotations

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
from sams.storage.repos.app_settings import AppSettingsRepo
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
        # 这些用例不测策略闸：给一个默认开着的仓库把参数占上
        AppSettingsRepo(db, clock=clock),
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
