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

import re

from fastapi import APIRouter, HTTPException, Request

from sams.services.markdown import render_markdown
from sams.storage.repos.annotations import TARGET_TYPES
from sams.web.auth import require_user
from sams.web.display import moment_label

router = APIRouter()

#: 面板一屏 20 条：它是「看几条 + 写一条」的地方，不是归档柜；更多去 /annotations（计划 5）
PANEL_LIMIT = 20


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
    target_type: str,
    target_ref: str,
    error: str = "",
    draft: dict | None = None,
    status_code: int = 200,
):
    """面板的唯一渲染出口。`draft` 是被拒表单的原文回填——交回人刚敲的那份，不是折过的。"""
    web = request.app.state.web
    ctx = web.contexts.current()
    user = request.state.sams_user
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
    require_user(request)
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
    return _render_panel(request, target_type=target_type, target_ref=target_ref)
