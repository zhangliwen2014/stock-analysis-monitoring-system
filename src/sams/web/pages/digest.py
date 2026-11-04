"""每日摘要 /digest（S1-C）。

一天一条、只说变化。列表按交易日倒序，停更的日子也在：没有变化的那一天只落行
不推送，但网页上必须看得到，否则「每天产出摘要」就只剩推送那一半。详情页直接印
库里那一行，正文不重算——摘要是快照，不是视图。

投递结局与错误同住一行（`delivery_outcome`/`delivery_error`）：摘要不是告警，
没有 `notify_log` 可挂。「未推送」与「发送失败」是两句不同的话，不合并成空白。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from sams.storage.repos.digests import Digest
from sams.web.auth import require_user
from sams.web.back import local_back
from sams.web.display import delivery_label, moment_label

router = APIRouter()

#: 一屏三十天：约六周，够看心跳期有没有断，也不把页面拖成一张年表
LIST_LIMIT = 30

NOT_PUSHED = "未推送（无变化）"
NOT_PUSHED_CHANGED = "未推送"


def _delivery(digest: Digest) -> str:
    if digest.delivery_outcome:
        return delivery_label(digest.delivery_outcome)
    return NOT_PUSHED_CHANGED if digest.has_changes else NOT_PUSHED


def _row(digest: Digest) -> dict:
    return {
        "id": digest.id,
        "market": digest.market,
        "as_of": digest.as_of,
        "title": digest.title,
        "has_changes": digest.has_changes,
        "delivery": _delivery(digest),
        "error": digest.delivery_error,
    }


@router.get("/digest")
def digest_list(request: Request):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    return web.render(
        request,
        "digest.html",
        title="每日摘要",
        digests=[_row(d) for d in ctx.digests_store.list_for(user.id, limit=LIST_LIMIT)],
    )


@router.get("/digest/{digest_id}")
def digest_detail(request: Request):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    digest = ctx.digests_store.get_by_id(request.path_params["digest_id"])
    # 「不存在」与「不是你的」答同一句：归属检查在读正文之前
    if digest is None or digest.user_id != user.id:
        raise HTTPException(status_code=404, detail="没有这条摘要")
    tz = ctx.settings.server.display_tz
    back = local_back(request.query_params.get("back"))
    return web.render(
        request,
        "digest_detail.html",
        title=digest.title,
        digest=digest,
        back_url=back or "/digest",
        back_label="← 返回" if back else "每日摘要",
        delivery=_delivery(digest),
        delivered_at=(
            moment_label(digest.delivered_at, tz, seconds=True)
            if digest.delivered_at
            else ""
        ),
    )
