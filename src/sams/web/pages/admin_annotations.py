"""`/admin/annotations`（§6.1/D139）：元数据、策略、用量与 break-glass。

admin 的默认视野是「有哪些行」而不是「写了什么」：列表与详情头部只有元数据，
正文、revision 与附件要满足两者之一——team 且策略开，或对**这一条**有未过期的授权。
附件走本文件多出来那条 `/admin/.../attachments/...`：D139 说「附件与 revision 同权」，
用普通下载路由会先被「不是作者」拦死，授权就白批了。

遮蔽不止正文：不可读的行连 `target_label` 与标签文字都不上屏（spec:340/549）——
「贵州茅台」或一串标签本身就回答了「谁在关注什么」。symbol 只出代码，
非 symbol 只出字面量「私有 <type>」。判据收在 `_display_target` 一处，列表与详情共用。

三道闸的形状：
* `require_admin` 在每个端点体内第一行（403 落在「目标存不存在」之前，见
  `test_admin_web.py` 那条参数化的教训）；仓库调用一律走 `web.contexts.current()`（D113）。
* 可读性只有一处判据（`_readable`）：列表、详情、附件三条路由共用，分叉就等于漏一条。
* 无有效授权时附件直链按不存在返回 404（spec:343）：403 会说漏「这条私有批注真有附件」。
* 被拒的 break-glass **重渲染详情页并带真实状态码**（TooManyRequests→429、
  Conflict→409、其余→400）：人要看到「为什么不能读」的下一句是可用的表单，
  而吞掉状态码会让自动化读不出区别。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from sams.core.errors import Conflict, DatabaseLocked, SamsError, TooManyRequests
from sams.services.markdown import render_markdown
from sams.storage.repos.annotations import TARGET_TYPES
from sams.storage.repos.app_settings import TEAM_SHARING
from sams.web.auth import audit, csrf_protect, require_admin
from sams.web.display import moment_label
from sams.web.forms import form_data, one
from sams.web.pages.annotations import TARGET_TYPE_LABELS

router = APIRouter()

#: 一屏 50 行：这是「有哪些行」的总览，不是归档柜；更多走筛选条件（页面层先不分页）
_LIST_LIMIT = 50
#: 最近授权表：spec:340 钉的是 50 条——「刚才谁看过谁的什么」要够回看一小段班次
_RECENT_LOGS = 50
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


def _display_target(made, readable: bool) -> str:
    """目标上屏的唯一折法（spec:340/549）：列表与详情共用，分两处迟早漏一处。

    不可读的行不许带出 `target_label`——「贵州茅台」这种串本身就回答了
    「谁在关注什么」，遮蔽正文却泄 label 等于闸装反了。symbol 只留代码
    （admin 得知道挂的是谁），非 symbol 只留字面量「私有 <type>」。
    """
    if readable:
        return f"{made.target_type} · {made.target_label}"
    if made.target_type == "symbol":
        return made.target_ref.partition(":")[2] or made.target_ref
    return f"私有 {made.target_type}"


def _display_tags(made, readable: bool) -> str:
    """标签文字与 label 同罪同罚（spec:549）：可读才拼串，遮蔽一律「—」。"""
    if not readable:
        return "—"
    return "、".join(made.tags) or "—"


def _day_start(raw: str | None, tz: str, *, next_day: bool = False) -> datetime | None:
    """`YYYY-MM-DD` 折成 display_tz 的零点（`until` 要 +1d 才是开区间上界）。
    认不出来就 None——地址栏手滑不判 400，「等于没筛」与搜索页同一读法。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    midnight = parsed.replace(tzinfo=ZoneInfo(tz))
    return midnight + timedelta(days=1) if next_day else midnight


def _list_filters(request: Request, ctx, users) -> dict:
    """§6.5:340 的四项筛选：值全在白名单/可解析才进 WHERE，野值静默忽略。
    `user` 认用户名——地址栏里躺 uuid 既不可读也没人抄得动。"""
    query = request.query_params
    filters: dict = {}
    wanted_user = (query.get("user") or "").strip()
    if wanted_user:
        hit = next((u for u in users if u.username == wanted_user), None)
        if hit is not None:
            filters["user_id"] = hit.id
    wanted_type = (query.get("type") or "").strip()
    if wanted_type in TARGET_TYPES:
        filters["target_type"] = wanted_type
    wanted_visibility = (query.get("visibility") or "").strip()
    if wanted_visibility in ("private", "team"):
        filters["visibility"] = wanted_visibility
    tz = ctx.settings.server.display_tz
    since = _day_start(query.get("since"), tz)
    if since is not None:
        filters["since"] = since
    until = _day_start(query.get("until"), tz, next_day=True)
    if until is not None:
        filters["until"] = until
    return filters


def _meta_rows(ctx, admin, made_by: dict[str, str], sharing: bool, filters: dict) -> list[dict]:
    tz = ctx.settings.server.display_tz
    rows = []
    for made in ctx.annotations_store.list_metadata(limit=_LIST_LIMIT, **filters):
        # 逐行问可读性：N+1 与面板同一取舍——一屏 50 行、本地 DuckDB，换不来分叉的代价
        readable = _readable(ctx, admin, made, sharing=sharing)
        rows.append(
            {
                "id": made.id,
                "owner": made_by.get(made.user_id, made.user_id[:8]),
                "at": moment_label(made.created_at, tz),
                "updated": moment_label(made.updated_at, tz),
                "target": _display_target(made, readable),
                "tags": _display_tags(made, readable),
                "revisions": ctx.annotations_store.count_revisions(made.id),
                "visibility": "团队" if made.visibility == "team" else "私有",
                "attachments": ctx.attachments_store.count_for(made.id),
                "deleted": made.deleted_at is not None,
                "purged": made.purged_at is not None,
            }
        )
    return rows


@router.get("/admin/annotations")
def admin_annotations_page(request: Request):
    # admin 这次要接返回值：列表逐行问可读性（遮蔽 label/标签与遮正文是同一道判据）
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    users = ctx.users.list_users()
    names = {user.id: (user.display_name or user.username) for user in users}
    sharing = ctx.app_settings.team_sharing()
    filters = _list_filters(request, ctx, users)
    usage = [
        {
            "user": names.get(user_id, user_id[:8]),
            # 批注数与「用量表只有一个出处」同规则：问 `count_all`，不在页面里折 SQL
            "annotations": ctx.annotations_store.count_all(user_id),
            "count": count,
            "mb": f"{size_bytes / (1024 * 1024):.1f}",
        }
        for user_id, count, size_bytes in ctx.attachments_store.usage_by_user()
    ]
    logs = [
        {
            "admin": names.get(row.admin_user_id, row.admin_user_id[:8]),
            # 所有者与管理员并排：「谁看过谁的什么」才是这一表要答的完整问题（spec:340）
            "owner": names.get(row.owner_user_id, row.owner_user_id[:8]),
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
        sharing=sharing,
        total=ctx.annotations_store.count_metadata(**filters),
        missing=ctx.attachments.missing_count(),
        rows=_meta_rows(ctx, admin, names, sharing, filters),
        usage=usage,
        logs=logs,
        minutes=ctx.settings.annotations.break_glass_minutes,
        notice=_notice(request, with_minutes=False),
        # shown 回显「有效的那一份」：野值不进 WHERE 也不回填表单，表单看起来就是没筛
        shown={
            "user": (request.query_params.get("user") or "").strip()
            if "user_id" in filters
            else "",
            "type": filters.get("target_type", ""),
            "visibility": filters.get("visibility", ""),
            "since": (request.query_params.get("since") or "").strip()
            if "since" in filters
            else "",
            "until": (request.query_params.get("until") or "").strip()
            if "until" in filters
            else "",
        },
        user_choices=[(user.username, names.get(user.id, user.username)) for user in users],
        type_choices=[("", "全部目标"), *[(t, TARGET_TYPE_LABELS[t]) for t in TARGET_TYPES]],
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
            "target": _display_target(made, readable),
            "tags": _display_tags(made, readable),
            "visibility": "团队" if made.visibility == "team" else "私有",
            "deleted": made.deleted_at is not None,
            "purged": made.purged_at is not None,
        },
        readable=readable,
        body=body,
        revisions=revisions,
        images=images,
        granted_until=moment_label(grants.granted_until, tz) if grants else "",
        # 常驻横幅（spec:341/549）：有效期内每一页都要答「为什么现在读得到」——
        # 到点和理由缺一不可，过期后 grants 为空，横幅随正文一起收回
        grant_banner=(
            f"临时授权至 {moment_label(grants.granted_until, tz)}，理由：{grants.reason}"
            if grants
            else ""
        ),
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
        # 429 是「过一会儿再来」（服务那句话带等待秒数），409 是「这条永远批不动了」
        #（墓碑），400 是「表单敲错了」：三种处置要在状态码上先分得开
        if isinstance(exc, TooManyRequests):
            status = 429
        elif isinstance(exc, Conflict):
            status = 409
        else:
            status = 400
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
    # 不存在、只剩墓碑、没有效授权——三者同一个 404 同一句文案（spec:343/530）：
    # 任何一点区别（哪怕只是「附件不存在」vs「批注不存在」）都在告诉探测者
    # 「这条私有批注真有附件」。普通用户那条下载路由不归这里管。
    if made is None or made.purged_at is not None:
        raise HTTPException(status_code=404, detail="附件不存在")
    sharing = ctx.app_settings.team_sharing()
    if not _readable(ctx, admin, made, sharing=sharing):
        raise HTTPException(status_code=404, detail="附件不存在")
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
