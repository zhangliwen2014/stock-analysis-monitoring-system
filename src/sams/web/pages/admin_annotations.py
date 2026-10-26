"""`/admin/annotations`（§6.1/D139）：元数据、策略、用量与 break-glass。

admin 的默认视野是「有哪些行」而不是「写了什么」：列表与详情头部只有元数据，
正文、revision 与附件要满足两者之一——team 且策略开，或对**这一条**有未过期的授权。
附件走本文件多出来那条 `/admin/.../attachments/...`：D139 说「附件与 revision 同权」，
用普通下载路由会先被「不是作者」拦死，授权就白批了。

三道闸的形状：
* `require_admin` 在每个端点体内第一行（403 落在「目标存不存在」之前，见
  `test_admin_web.py` 那条参数化的教训）；仓库调用一律走 `web.contexts.current()`（D113）。
* 可读性只有一处判据（`_readable`）：列表、详情、附件三条路由共用，分叉就等于漏一条。
* 被拒的 break-glass **重渲染详情页并带真实状态码**（Conflict→409、其余→400）：
  人要看到「为什么不能读」的下一句是可用的表单，而吞掉状态码会让自动化读不出区别。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from sams.core.errors import Conflict, DatabaseLocked, SamsError
from sams.services.markdown import render_markdown
from sams.storage.repos.app_settings import TEAM_SHARING
from sams.web.auth import audit, csrf_protect, require_admin
from sams.web.display import moment_label
from sams.web.forms import form_data, one

router = APIRouter()

#: 一屏 50 行：这是「有哪些行」的总览，不是归档柜；更多走分页参数（页面层先不铺开）
_LIST_LIMIT = 50
#: 最近授权表：break-glass 是低频动作，10 行足够回答「刚才谁看过什么」
_RECENT_LOGS = 10
_form_body = Depends(form_data)

#: 跳回列表/详情时屏幕上肯出现的那几句话（与 `pages/admin.py` 的 `_FEEDBACK` 同一原则：
#: query 里的原文不白名单就不上屏，否则等于往 admin 页塞一个自由文本框）
_NOTICES = frozenset(
    {
        "策略已切到 开",
        "策略已切到 关",
    }
)


def _notice(request: Request, *, with_minutes: bool) -> str:
    """只认自己发出去过的那几句提醒；`with_minutes` 放行「已授权 N 分钟」的模板。"""
    raw = request.query_params.get("notice") or ""
    if raw in _NOTICES:
        return raw
    if with_minutes and raw.startswith("已授权 ") and raw.endswith(" 分钟"):
        return raw
    return ""


def _readable(ctx, admin, made, *, sharing: bool) -> bool:
    """两者其一：team（且策略开、且是 symbol——写侧闸保证的形状，读侧再认一遍不亏），
    或此刻仍有效的那一条授权。"""
    if made.visibility == "team" and sharing and made.target_type == "symbol":
        return True
    return ctx.break_glass.can_read(admin.id, made.id)


def _meta_rows(ctx, made_by: dict[str, str]) -> list[dict]:
    tz = ctx.settings.server.display_tz
    rows = []
    for made in ctx.annotations_store.list_metadata(limit=_LIST_LIMIT):
        rows.append(
            {
                "id": made.id,
                "owner": made_by.get(made.user_id, made.user_id[:8]),
                "at": moment_label(made.created_at, tz),
                "updated": moment_label(made.updated_at, tz),
                "target": f"{made.target_type} · {made.target_label}",
                "visibility": "团队" if made.visibility == "team" else "私有",
                "attachments": ctx.attachments_store.count_for(made.id),
                "deleted": made.deleted_at is not None,
                "purged": made.purged_at is not None,
            }
        )
    return rows


@router.get("/admin/annotations")
def admin_annotations_page(request: Request):
    # 返回值不接：这一屏的名单与用量都按 user_id 折好，不需要「是谁在看」
    require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    names = {user.id: (user.display_name or user.username) for user in ctx.users.list_users()}
    usage = [
        {
            "user": names.get(user_id, user_id[:8]),
            "count": count,
            "mb": f"{size_bytes / (1024 * 1024):.1f}",
        }
        for user_id, count, size_bytes in ctx.attachments_store.usage_by_user()
    ]
    logs = [
        {
            "admin": names.get(row.admin_user_id, row.admin_user_id[:8]),
            "annotation": row.annotation_id,
            # 理由原文上屏是给 admin 页看的（它本来就写在审计里）；id 做成链接，
            # 「为什么看这一条」要能一路点到那一行的墓碑/详情
            "annotation_at": f"/admin/annotations/{row.annotation_id}",
            "reason": row.reason,
            "until": moment_label(row.granted_until, ctx.settings.server.display_tz),
        }
        for row in ctx.admin_access_store.list_recent(limit=_RECENT_LOGS)
    ]
    return web.render(
        request,
        "admin_annotations.html",
        title="批注与附件",
        sharing=ctx.app_settings.team_sharing(),
        total=ctx.annotations_store.count_metadata(),
        missing=ctx.attachments.missing_count(),
        rows=_meta_rows(ctx, names),
        usage=usage,
        logs=logs,
        minutes=ctx.settings.annotations.break_glass_minutes,
        notice=_notice(request, with_minutes=False),
    )


@router.post("/admin/annotations/policy")
def policy_set(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    value: str = Form(""),
):
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    wanted = value.strip()
    if wanted not in ("on", "off"):
        raise HTTPException(status_code=400, detail=f"策略只有 on/off（收到 {value!r}）")
    old = ctx.app_settings.get(TEAM_SHARING, default="on")
    try:
        ctx.app_settings.set(TEAM_SHARING, wanted, updated_by=admin.id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(
        request,
        action="annotation.policy",
        target=TEAM_SHARING,
        ok=True,
        detail=f"{old}->{wanted}",
    )
    label = "开" if wanted == "on" else "关"
    return RedirectResponse(f"/admin/annotations?notice=策略已切到 {label}", status_code=303)


def _detail(
    request: Request, admin, ctx, annotation_id: str, *, error: str = "", status_code: int = 200
):
    """详情渲染的唯一出口（成功与被拒的重渲染共用一份行结构，见 break_glass 的 except）。"""
    web = request.app.state.web
    made = ctx.annotations_store.get(annotation_id, include_deleted=True)
    if made is None:
        raise HTTPException(status_code=404, detail="批注不存在")
    sharing = ctx.app_settings.team_sharing()
    readable = _readable(ctx, admin, made, sharing=sharing)
    tz = ctx.settings.server.display_tz
    grants = ctx.admin_access_store.active(admin.id, made.id, now=ctx.clock.now_utc())
    body = None
    revisions = []
    images = []
    if readable and made.purged_at is None:
        # 墓碑没什么可渲染：readable 只说明「批过」，正文列已经被清成空串
        body = render_markdown(made.body_md)
        revisions = [
            {
                "at": moment_label(rev.edited_at, tz),
                "before": len(rev.before_md),
                "after": len(rev.after_md),
                "has_attachments": bool(rev.meta.get("attachments")),
            }
            for rev in ctx.annotations_store.revisions(made.id)
        ]
        for att in ctx.attachments_store.list_for(made.id):
            if ctx.attachments.path_of(att).is_file():
                images.append(
                    {
                        "url": f"/admin/annotations/{made.id}/attachments/{att.id}",
                        "filename": att.filename,
                    }
                )
    return web.render(
        request,
        "admin_annotation_detail.html",
        status_code=status_code,
        meta={
            "id": made.id,
            "owner": made.user_id,
            "at": moment_label(made.created_at, tz),
            "target": f"{made.target_type} · {made.target_label}",
            "visibility": "团队" if made.visibility == "team" else "私有",
            "deleted": made.deleted_at is not None,
            "purged": made.purged_at is not None,
        },
        readable=readable,
        body=body,
        revisions=revisions,
        images=images,
        granted_until=moment_label(grants.granted_until, tz) if grants else "",
        minutes=ctx.settings.annotations.break_glass_minutes,
        error=error,
        notice=_notice(request, with_minutes=True),
    )


@router.get("/admin/annotations/{annotation_id}")
def annotation_detail(request: Request, annotation_id: str):
    admin = require_admin(request)
    ctx = request.app.state.web.contexts.current()
    return _detail(request, admin, ctx, annotation_id)


@router.post("/admin/annotations/{annotation_id}/break-glass")
def break_glass(
    request: Request,
    annotation_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    reason = one(data, "reason")
    # 时长是治理参数（settings.annotations.break_glass_minutes）：Task 2 修复轮的单一来源口径
    minutes = ctx.settings.annotations.break_glass_minutes
    try:
        ctx.break_glass.request_access(admin.id, annotation_id, reason)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        status = 409 if isinstance(exc, Conflict) else 400
        audit(
            request,
            action="admin.break_glass",
            target=annotation_id,
            ok=False,
            detail=str(exc)[:80],
        )
        # 被拒也回详情页：人要看清「为什么不能读」的下一句是可用的表单，不是错误页一座。
        # 状态码不吞——「你写错了」与「本小时批满了」在自动化眼里必须是两回事。
        return _detail(request, admin, ctx, annotation_id, error=str(exc), status_code=status)
    audit(
        request,
        action="admin.break_glass",
        target=annotation_id,
        ok=True,
        detail=f"+{minutes}min",
    )
    return RedirectResponse(
        f"/admin/annotations/{annotation_id}?notice=已授权 {minutes} 分钟",
        status_code=303,
    )


@router.get("/admin/annotations/{annotation_id}/attachments/{attachment_id}")
def admin_attachment(request: Request, annotation_id: str, attachment_id: str):
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    made = ctx.annotations_store.get(annotation_id, include_deleted=True)
    # 墓碑没什么可下载：与「不存在」同一个回答，403 只留给「存在但没授权」
    if made is None or made.purged_at is not None:
        raise HTTPException(status_code=404, detail="批注不存在或只剩墓碑")
    sharing = ctx.app_settings.team_sharing()
    if not _readable(ctx, admin, made, sharing=sharing):
        raise HTTPException(
            status_code=403, detail="正文不可读：先在详情页为这条批注申请 break-glass"
        )
    row = ctx.attachments_store.get(attachment_id)
    if row is None or row.annotation_id != annotation_id:
        raise HTTPException(status_code=404, detail="附件不存在")
    data = ctx.attachments.load(row)
    if data is None:
        raise HTTPException(status_code=404, detail="附件文件缺失：库里还有这一行")
    return Response(
        content=data,
        media_type=row.media_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(row.filename, safe='')}",
            # 有意的：私人正文不许进中间缓存（普通下载路由是 private, max-age=300，
            # 这一条读的是刚批出来的授权，缓存一分钟都可能被下一个人捡到）
            "Cache-Control": "no-store",
        },
    )
