"""同步管理：手动请求、持久执行台账与自动补数队列的只读视图和两个止损动作。"""

from __future__ import annotations

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.core.errors import SamsError
from sams.web.auth import audit, csrf_protect, require_admin
from sams.web.display import moment_label
from sams.web.forms import form_data, one
from sams.web.pages.admin import _error_text

router = APIRouter()
_FORM_BODY = Depends(form_data)

_PER_PAGE = 20
_TASK_STATUSES = {"running", "ok", "failed"}
_BACKFILL_STATUSES = {"pending", "deferred", "done"}
_TASK_STATUS_OPTIONS = (
    ("running", "执行中"),
    ("ok", "成功"),
    ("failed", "失败"),
)
_BACKFILL_STATUS_OPTIONS = (
    ("pending", "待执行"),
    ("deferred", "已暂缓"),
    ("done", "完成"),
)
_REQUEST_LABELS = {
    "pending": "排队中",
    "running": "执行中",
    "ok": "成功",
    "failed": "失败",
    "skipped": "跳过",
    "cancelled": "已取消",
}
_TASK_LABELS = {"running": "执行中", "ok": "成功", "failed": "失败"}
_BACKFILL_LABELS = {"pending": "待执行", "deferred": "已暂缓", "done": "完成"}


def _page_number(raw: str) -> int:
    try:
        page = int(raw or "1")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="页码必须是正整数") from exc
    if page < 1:
        raise HTTPException(status_code=400, detail="页码必须是正整数")
    return page


@router.get("/admin/sync")
def sync_page(request: Request):
    require_admin(request)
    ctx = request.app.state.web.contexts.current()
    tz = ctx.settings.server.display_tz
    market = (request.query_params.get("market") or "").strip().upper() or None
    status = (request.query_params.get("status") or "").strip() or None
    backfill_status = (request.query_params.get("backfill_status") or "").strip() or None
    if market is not None and market not in ctx.settings.markets:
        raise HTTPException(status_code=400, detail=f"市场 {market} 没有配置")
    if status is not None and status not in _TASK_STATUSES:
        raise HTTPException(status_code=400, detail="执行状态只能是 running/ok/failed")
    if backfill_status is not None and backfill_status not in _BACKFILL_STATUSES:
        raise HTTPException(
            status_code=400, detail="补数状态只能是 pending/deferred/done"
        )
    page = _page_number(request.query_params.get("page", ""))
    runs, run_total = ctx.task_runs.history(
        market=market, status=status, page=page, per_page=_PER_PAGE
    )
    backfills, backfill_total = ctx.backfills_store.list_filtered(
        status=backfill_status, market=market, page=page, per_page=_PER_PAGE
    )
    requests = ctx.scheduler.request_history()
    return request.app.state.web.render(
        request,
        "admin_sync.html",
        markets=sorted(ctx.settings.markets),
        market=market or "",
        status=status or "",
        backfill_status=backfill_status or "",
        task_status_options=_TASK_STATUS_OPTIONS,
        backfill_status_options=_BACKFILL_STATUS_OPTIONS,
        page=page,
        run_pages=max(1, ceil(run_total / _PER_PAGE)),
        backfill_pages=max(1, ceil(backfill_total / _PER_PAGE)),
        runs=[
            {
                "row": run,
                "status": _TASK_LABELS.get(run.status, run.status),
                "started": moment_label(run.started_at, tz, seconds=True),
                "finished": (
                    moment_label(run.finished_at, tz, seconds=True)
                    if run.finished_at
                    else "—"
                ),
                "error": _error_text(run.error or ""),
                "note": _error_text(run.note or ""),
            }
            for run in runs
        ],
        run_total=run_total,
        backfills=[
            {
                **row.__dict__,
                "label": _BACKFILL_LABELS.get(row.status, row.status),
                "error": _error_text(row.last_error or ""),
            }
            for row in backfills
        ],
        backfill_total=backfill_total,
        requests=[
            {
                "id": item.request.id,
                "job": item.request.job or "市场全部任务",
                "market": item.request.market or "—",
                "requested": moment_label(item.request.requested_at, tz, seconds=True),
                "status": item.status,
                "label": _REQUEST_LABELS.get(item.status, item.status),
                "notes": [_error_text(note) for note in item.notes],
            }
            for item in requests
        ],
        queued=request.query_params.get("queued", ""),
    )


@router.post("/admin/sync/cancel")
def cancel_sync(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    require_admin(request)
    ctx = request.app.state.web.contexts.current()
    request_id = one(data, "request_id").strip()
    if not request_id or not ctx.scheduler.cancel_request(request_id):
        raise HTTPException(status_code=400, detail="这条请求已经执行或被领取，不能再取消")
    audit(request, action="admin.sync.cancel", target=request_id, ok=True)
    return RedirectResponse("/admin/sync", status_code=303)


@router.post("/admin/sync/backfills/retry")
def retry_backfill(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    require_admin(request)
    ctx = request.app.state.web.contexts.current()
    row_id = one(data, "row_id").strip()
    try:
        retried = ctx.backfills_store.retry_deferred(row_id)
    except SamsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not retried:
        raise HTTPException(status_code=400, detail="只有已暂缓的补数记录可以重新排队")
    audit(request, action="admin.sync.retry_backfill", target=row_id, ok=True)
    return RedirectResponse("/admin/sync", status_code=303)
