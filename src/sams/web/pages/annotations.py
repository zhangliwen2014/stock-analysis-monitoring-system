"""批注面板与批注写入（M4-B §6.1/§6.2）。

面板是一枚 htmx fragment：页面只嵌一行容器，读与写都回到 `_render_panel` 这一个出口——
GET 与被拒后的重渲染共用一套行结构，分两份就会分叉（措辞、可见性、时区折法各漂一处）。

闸不在这一层：可见性在 `AnnotationRepo.list_visible`（SQL 里挡），归属在
`AnnotationService`，正文安全在 `render_markdown`（D122 的配置：禁 HTML/图片、链接白名单）。
模板里那个 `| safe` 因此是安全的——那一串 HTML 只可能是 render_markdown 自己产的。

作者标签与推送上下文同一口径：本人「我」，他人 display_name 回退用户名（§7.1）。
时刻全部在 Python 里折到 display_tz 才上屏（`moment_label`），模板不碰 datetime。

仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

import io
import re
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile

from sams.core.errors import Conflict, DatabaseLocked, NotFound, SamsError
from sams.services.attachments import MB
from sams.services.markdown import render_markdown
from sams.storage.repos.annotations import TARGET_TYPES
from sams.web.auth import audit, csrf_protect, require_user
from sams.web.display import moment_label

router = APIRouter()

#: 面板一屏 20 条：它是「看几条 + 写一条」的地方，不是归档柜；更多去 /annotations（计划 5）
PANEL_LIMIT = 20

#: 附件分片的依赖提到模块级一份（B008 不许在参数默认值里调 `File`，
#: 与 `pages/settings.py` 的 `_form_body` 同一处置）
_files_field = File(default=[])


def _panel_id(target_type: str, target_ref: str) -> str:
    """把目标键折成一个 DOM id：`A:600519` 里的冒号进不了 `#选择器`，htmx 的 swap 会抓空。"""
    return re.sub(r"[^0-9A-Za-z]+", "-", f"annotations-{target_type}-{target_ref}").strip("-")


def _author_label(ctx, made, viewer_id: str) -> str:
    if made.user_id == viewer_id:
        return "我"
    author = ctx.users.get(made.user_id)
    if author is None:
        return "未知作者"
    return author.display_name or author.username


def _render_panel(
    request: Request,
    *,
    user,
    target_type: str,
    target_ref: str,
    error: str = "",
    draft: dict | None = None,
    status_code: int = 200,
):
    """面板的唯一渲染出口。`draft` 是被拒表单的原文回填——交回人刚敲的那份，不是折过的。

    `user` 由端点用 `require_user` 认好后显式传进来：这一层不再回头去摸
    `request.state.sams_user`。登录的前置条件收在端点那一处，Task 2 复用同一个
    出口时不必再猜它从哪来。
    """
    web = request.app.state.web
    ctx = web.contexts.current()
    sharing = ctx.app_settings.team_sharing()
    tz = ctx.settings.server.display_tz
    items = ctx.annotations.list_target(
        user.id, target_type, target_ref, team_sharing=sharing, limit=PANEL_LIMIT
    )
    rows = []
    for made in items:
        rows.append(
            {
                "id": made.id,
                "author": _author_label(ctx, made, user.id),
                "at": moment_label(made.created_at, tz),
                "pinned": made.pinned,
                "visibility": "团队" if made.visibility == "team" else "私有",
                "tags": made.tags,
                "body_html": render_markdown(made.body_md),
                "body_md": made.body_md,
                "own": made.user_id == user.id,
                "updated_at": made.updated_at.isoformat(),
            }
        )
    return web.render(
        request,
        "partials/annotations.html",
        status_code=status_code,
        target_type=target_type,
        target_ref=target_ref,
        panel_id=_panel_id(target_type, target_ref),
        rows=rows,
        error=error,
        draft=draft or {},
        empty_note="还没有批注。写下这只票的逻辑、风险或今天的异动，告警推送会自动带上最近 3 条。",
    )


@router.get("/api/partials/annotations")
def annotations_panel(request: Request):
    user = require_user(request)
    query = request.query_params
    target_type = (query.get("target_type") or "").strip()
    target_ref = (query.get("target_ref") or "").strip()
    if target_type not in TARGET_TYPES or not target_ref:
        # 缺目标的面板没有意义：回 400 而不是回一枚空白 fragment，
        # 「加载失败」与「还没有批注」在屏幕上是两句不同的话
        raise HTTPException(
            status_code=400,
            detail="批注面板要知道挂在哪个目标：target_type 与 target_ref 都必填",
        )
    return _render_panel(request, user=user, target_type=target_type, target_ref=target_ref)


# --- 写路（§6.2/§10）------------------------------------------------------------------


def _clean_files(files: list[UploadFile | str]) -> list[UploadFile]:
    """丢掉不是「一张真文件」的分片。浏览器「没选文件」直接提交发的是 `filename=""`
    的分片，而 httpx 干脆省掉 filename 属性——那种 part 解析出来是一枚 str。
    留着任何一种，「0 张附件」就被数成「1 张」。

    认「不是 str」而不是 `isinstance(f, UploadFile)`：`UploadFile | str` 的联合注解
    会让 FastAPI 跳过它自己的升格，真文件到这里是 **starlette** 的那个 UploadFile。
    """
    return [f for f in files if not isinstance(f, str) and (f.filename or "").strip()]


def _part_size(upload: UploadFile) -> int:
    """seek/tell 量分片大小，不整读进内存：5 MB 的闸门若先读再判，闸门本身
    就是那条被绕过的路（一次 multipart 能塞几十张 5 MB）。"""
    handle = upload.file
    handle.seek(0, io.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    return size


#: 护两张卡的读—判—写：端点跑在线程池上，`LoginLimiter` 自己不带锁（它设计时
#: 只有一个调用方），`RateCard` docstring 里那条「读—判—写必须同锁」在这里由这把锁补
_limit_lock = threading.Lock()


def _limit_hit(web, user_id: str, *, uploads: int) -> int:
    """§10 的「每分钟 30 条」是窗口计数：先问两张卡要不要等，谁要等就交谁的秒数；
    都放行才各记一次尝试。`record_failure` 的名字是登录留下的，这里记的是每一次尝试——
    窗口要数的是「发过多少次」，不是「失败过多少次」，否则连点永远顶不到上限。"""
    with _limit_lock:
        write_key = ("annotation-write", user_id)
        wait = web.annotation_writes.blocked_for(write_key)
        if wait:
            return wait
        upload_key = ("attachment-upload", user_id)
        if uploads:
            wait = web.attachment_uploads.blocked_for(upload_key)
            if wait:
                return wait
        web.annotation_writes.record_failure(write_key)
        if uploads:
            web.attachment_uploads.record_failure(upload_key)
        return 0


@router.post("/annotations")
def annotation_create(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    target_type: str = Form(""),
    target_ref: str = Form(""),
    body_md: str = Form(""),
    tags: str = Form(""),
    # `str` 那一支接的就是 httpx/浏览器不发真文件时落进来的普通字段分片
    files: list[UploadFile | str] = _files_field,
):
    web = request.app.state.web
    user = require_user(request)
    ctx = web.contexts.current()
    target_type = target_type.strip()
    target_ref = target_ref.strip()
    if target_type not in TARGET_TYPES or not target_ref:
        raise HTTPException(
            status_code=400,
            detail="批注要挂在明确的目标上：target_type 与 target_ref 都必填",
        )
    draft = {"body_md": body_md, "tags": tags}

    def fail(error: str, status_code: int):
        # 被拒也要回面板本体：htmx swap 的容器是面板，回 error.html 会把整页换掉
        return _render_panel(
            request,
            user=user,
            target_type=target_type,
            target_ref=target_ref,
            error=error,
            draft=draft,
            status_code=status_code,
        )

    clean = _clean_files(files)
    wait = _limit_hit(web, user.id, uploads=len(clean))
    if wait:
        return fail(
            f"写得太快：请 {wait} 秒后再来（每分钟 30 条批注、10 次附件上传）", 429
        )
    limits = ctx.settings.annotations
    if len(clean) > limits.max_attachments:
        return fail(f"一条批注最多 {limits.max_attachments} 张附件", 400)
    # 尺寸闸在 create **之前**（§10）：人都传完了才说「批注存了、图没了」，
    # 等于让草稿断在一半。被拒的这次一条都不写，原文回填。
    oversize = [
        upload.filename
        for upload in clean
        if _part_size(upload) > limits.max_attachment_mb * MB
    ]
    if oversize:
        return fail(
            f"单张附件最多 {limits.max_attachment_mb} MB，"
            f"{'、'.join(oversize)} 超了：这条批注一个字都没写，去一半再传",
            400,
        )
    try:
        made = ctx.annotations.create(
            user.id,
            target_type=target_type,
            target_ref=target_ref,
            body_md=body_md,
            tags=tags,
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return fail(str(exc), 400)
    rejected: list[str] = []
    for upload in clean:
        try:
            ctx.attachments.add(
                user.id, made.id, filename=upload.filename, data=upload.file.read()
            )
        except DatabaseLocked:
            raise
        except SamsError as exc:
            # 批注成立之后才拦下的附件不回滚批注：草稿是人的，图只是图——
            # 点名「哪张、为什么」，其余照常上屏
            rejected.append(f"{upload.filename}：{exc}")
    audit(
        request,
        action="annotation.create",
        target=made.id,
        ok=not rejected,
        detail=f"rejected={len(rejected)}" if rejected else "",
    )
    error = f"批注已保存，但这些附件被拒：{'；'.join(rejected)}" if rejected else ""
    # 用 `made` 的归一键重渲染：服务把 `A:600519` 折成 canonical 之后，
    # 面板 id 与行都要跟着那个键，否则下一次 GET 拼出的容器对不上号
    return _render_panel(
        request,
        user=user,
        target_type=made.target_type,
        target_ref=made.target_ref,
        error=error,
    )


@router.post("/annotations/preview")
def annotation_preview(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    body_md: str = Form(""),
):
    require_user(request)
    # 预览与上屏同一条渲染管线（`render_markdown`）：两套渲染器迟早分叉，
    # 分叉的代价是「预览里好看、发出去是另一个样子」
    return Response(render_markdown(body_md), media_type="text/html; charset=utf-8")


def _load_for_write(request: Request, annotation_id: str):
    """写路自己的归属闸：先认人、再拿批注，NotFound 与「不是你的」都折成 404。

    服务层 `get(team_sharing=True)` 会放出他人的团队批注——读得到不等于写得动，
    所以这里再过一道 `user_id` 归属。不存在与不可见是同一句话：不暴露存在性。
    """
    web = request.app.state.web
    user = require_user(request)
    ctx = web.contexts.current()
    try:
        made = ctx.annotations.get(user.id, annotation_id, team_sharing=True)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="批注不存在或不可见") from exc
    if made.user_id != user.id:
        raise HTTPException(status_code=404, detail="批注不存在或不可见")
    return web, ctx, user, made


@router.post("/annotations/{annotation_id}/edit")
def annotation_edit(
    request: Request,
    annotation_id: str,
    _csrf: None = Depends(csrf_protect),
    body_md: str = Form(""),
    tags: str = Form(""),
    updated_at: str = Form(""),
):
    web, ctx, user, made = _load_for_write(request, annotation_id)

    def fail(error: str, status_code: int):
        return _render_panel(
            request,
            user=user,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=error,
            status_code=status_code,
        )

    wait = _limit_hit(web, user.id, uploads=0)
    if wait:
        return fail(f"写批注太频繁了：{wait} 秒后再试（每分钟 30 条）", 429)
    # 空串=人不带版本号来改：服务层跳过并发检查（面板上的编辑永远带着 hidden 的
    # updated_at，这条支路接的是脚本与旧表单）；坏了的戳才拦
    stamp: datetime | None = None
    if updated_at:
        try:
            stamp = datetime.fromisoformat(updated_at)
        except ValueError:
            return fail("编辑基线的时间戳读不出来：关闭编辑器、重新打开再改", 400)
    try:
        ctx.annotations.edit(
            user.id,
            made.id,
            body_md=body_md,
            tags=tags,
            expected_updated_at=stamp,
        )
    except DatabaseLocked:
        raise
    except Conflict as exc:
        # 409 也重渲染面板：人要知道的不是「失败了」，而是「现在上面是什么」
        return fail(str(exc), 409)
    except SamsError as exc:
        return fail(str(exc), 400)
    audit(request, action="annotation.edit", target=made.id, ok=True)
    return _render_panel(
        request, user=user, target_type=made.target_type, target_ref=made.target_ref
    )


@router.post("/annotations/{annotation_id}/pin")
def annotation_pin(
    request: Request,
    annotation_id: str,
    _csrf: None = Depends(csrf_protect),
    pinned: str = Form("1"),
):
    web, ctx, user, made = _load_for_write(request, annotation_id)

    def fail(error: str, status_code: int):
        return _render_panel(
            request,
            user=user,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=error,
            status_code=status_code,
        )

    wait = _limit_hit(web, user.id, uploads=0)
    if wait:
        return fail(f"写批注太频繁了：{wait} 秒后再试（每分钟 30 条）", 429)
    try:
        # 服务层是显式赋值不是 toggle：模板已按当前状态给出反方向的 "1"/"0"
        ctx.annotations.pin(user.id, made.id, pinned=pinned.strip() == "1")
    except DatabaseLocked:
        raise
    except SamsError as exc:
        # MAX_PINNED 的拒绝（`AnnotationRepo.set_pinned`）走的正是这一支
        return fail(str(exc), 400)
    audit(request, action="annotation.pin", target=made.id, ok=True)
    return _render_panel(
        request, user=user, target_type=made.target_type, target_ref=made.target_ref
    )


@router.post("/annotations/{annotation_id}/delete")
def annotation_delete(
    request: Request, annotation_id: str, _csrf: None = Depends(csrf_protect)
):
    web, ctx, user, made = _load_for_write(request, annotation_id)

    def fail(error: str, status_code: int):
        return _render_panel(
            request,
            user=user,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=error,
            status_code=status_code,
        )

    wait = _limit_hit(web, user.id, uploads=0)
    if wait:
        return fail(f"写批注太频繁了：{wait} 秒后再试（每分钟 30 条）", 429)
    try:
        ctx.annotations.delete(user.id, made.id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return fail(str(exc), 400)
    audit(request, action="annotation.delete", target=made.id, ok=True)
    return _render_panel(
        request, user=user, target_type=made.target_type, target_ref=made.target_ref
    )
