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
    except SyntaxError as exc:
        # Pillow 的 verify() 对坏字节（如 IDAT CRC 不符）用的是**内建 SyntaxError**：
        # 它不是 OSError 也不是图片族的异常，单列一支折成 ValidationError，
        # 否则这条会漏过 web 写路只兜 SamsError 的分支，代价是一屏 500
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

    def remove(self, viewer_id: str, annotation_id: str, attachment_id: str) -> Attachment:
        """作者删附件：先过批注归属（与写正文同一条闸），再删行、再删文件。

        顺序与新增相反——行没了文件才动：中间崩了最坏留一个孤儿文件给 prune，
        反过来就是「库里指着一张不存在的图」。
        """
        made = self.annotations.get(viewer_id, annotation_id)
        if made.user_id != viewer_id:
            raise NotFound(f"批注 {annotation_id} 不是你的：删不了别人批注上的附件")
        row = self.attachments.get(attachment_id)
        if row is None or row.annotation_id != annotation_id:
            raise NotFound(f"附件 {attachment_id} 不存在或不属于这条批注")
        self.attachments.delete(attachment_id)
        try:
            self.path_of(row).unlink(missing_ok=True)
        except OSError as exc:
            # 行已经没了，配额也随之回落：文件删不掉只是占盘，交给 prune 收尾
            logger.warning("附件文件删除失败（行已删，交给 prune）：%s", exc)
        return row

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
