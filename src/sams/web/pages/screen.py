"""选股页 /screen（D172/D180/D182/D184）。

只做三件事：把条件翻成引擎的一篮、把那一篮说清（范围 / 三个数 / 无数原因）、
把人的一句话（方案）存起来。取数一律读库：行上那些 PE/PB 是快照那一天的那一份
（D181）；「立即重采」只排队，不在这条线程上打外网（D176）。
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.core.errors import ValidationError
from sams.core.screener_fields import (
    FIELDS,
    OP_LABELS,
    OPS,
    Condition,
    describe_all,
    fields_for,
)
from sams.services.screener import preset_strategy_id
from sams.storage.repos.watchlists import MAX_ITEMS_PER_USER
from sams.web.auth import csrf_protect, require_user
from sams.web.display import moment_label
from sams.web.forms import form_data, many, one

router = APIRouter()

PAGE_SIZE = 50
MISSING_CODES = 20
STALE_DAYS = 3

_FORM_BODY = Depends(form_data)


@router.get("/screen")
def screen_page(request: Request):
    user = require_user(request)
    query = request.query_params
    market, preset, rows = _state(request, user.id, query)
    return _render(
        request,
        user,
        market=market,
        preset=preset,
        rows=rows,
        page=_page_number(query.get("page")),
        confirm_all=query.get("confirm") == "all",
        notice=_notice(query),
    )


def _state(request: Request, user_id: str, query) -> tuple[str, object | None, list[dict]]:
    ctx = request.app.state.web.contexts.current()
    markets = sorted(ctx.settings.markets)
    preset_id = (query.get("preset") or "").strip()
    if preset_id:
        preset = _preset(ctx, user_id, preset_id)
        return preset.market, preset, [_draft_of(item) for item in preset.conditions]
    market = (
        query.get("market") or ("A" if "A" in markets else (markets[0] if markets else ""))
    ).upper()
    if market not in markets:
        raise HTTPException(
            status_code=400, detail=f"市场 {market or '（空）'} 没配：只有 {', '.join(markets)}"
        )
    rows = _rows_from(query)
    return market, None, rows or [_draft()]


def _render(
    request: Request,
    user,
    *,
    market: str,
    preset,
    rows: list[dict],
    page: int = 1,
    confirm_all: bool = False,
    error: str = "",
    notice: str = "",
):
    web = request.app.state.web
    ctx = web.contexts.current()
    markets = sorted(ctx.settings.markets)
    specs = {item.name: item for item in FIELDS}
    snapshot = None
    stale = False

    conditions: tuple[Condition, ...] = ()
    result = None
    if not error:
        try:
            conditions = _conditions(rows)
            if conditions:
                result = ctx.screener.run(market, conditions)
        except ValidationError as exc:
            error = str(exc)
    if result is None:
        day = ctx.snapshot_store.latest_day(market)
        if day is not None:
            frame = ctx.snapshot_store.load_day(market, day)
            snapshot = {
                "day": day,
                "taken_label": moment_label(
                    frame["taken_at"].max() if not frame.is_empty() else None,
                    ctx.settings.server.display_tz,
                ),
                "rows": frame.height,
            }
            stale = (ctx.clock.today() - day).days > STALE_DAYS

    enabled = {item.name for item in fields_for(market)}
    field_options = [
        {
            "name": item.name,
            "label": item.label,
            "unit": item.unit,
            "enabled": item.name in enabled,
            "why": ""
            if item.name in enabled
            else f"{market} 没有这一列（{'/'.join(sorted(item.markets))} 才有）",
        }
        for item in FIELDS
    ]
    hits = result.hits if result is not None else ()
    total = len(hits)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), pages)
    shown = hits[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    held = {code for item_market, code in ctx.watchlists.codes(user.id) if item_market == market}
    plan = None
    if confirm_all and result is not None:
        already = sum(1 for hit in hits if hit.code in held)
        quota = max(0, MAX_ITEMS_PER_USER - ctx.watchlists.count(user.id))
        plan = {
            "total": len(hits),
            "already": already,
            "will_add": min(len(hits) - already, quota),
            "capped": max(0, len(hits) - already - quota),
            "max": MAX_ITEMS_PER_USER,
        }
    view = [_hit_row(hit, conditions, held) for hit in shown]
    counts_view = (
        [
            {
                "label": specs[item.field].label,
                "unit": specs[item.field].unit,
                "passed": item.passed,
                "failed": item.failed,
                "missing": item.missing,
            }
            for item in result.fields
        ]
        if result is not None
        else []
    )
    pager = {
        "prev": _url(market, rows, preset=preset, page=page - 1) if page > 1 else "",
        "next": _url(market, rows, preset=preset, page=page + 1) if page < pages else "",
    }
    presets_view = []
    for item in ctx.screener_presets.list(user.id):
        runs = ctx.scan_store.recent(
            market=item.market,
            strategy_id=preset_strategy_id(item.name, item.conditions),
            limit=1,
        )
        presets_view.append(
            {
                "id": item.id,
                "name": item.name,
                "market": item.market,
                "summary": describe_all(item.conditions),
                "updated": moment_label(item.updated_at, ctx.settings.server.display_tz),
                "last_run": moment_label(runs[0]["created_at"], ctx.settings.server.display_tz)
                if runs
                else "",
            }
        )
    add_url = _url(market, [*rows, _draft()])
    remove_urls = [_url(market, rows[:i] + rows[i + 1 :]) for i in range(len(rows))]
    if not rows:
        add_url = _url(market, [_draft()])
    return web.render(
        request,
        "screen.html",
        markets=markets,
        market=market,
        preset=preset,
        rows=rows,
        add_url=add_url,
        remove_urls=remove_urls,
        field_options=field_options,
        ops=[(op, OP_LABELS.get(op, op)) for op in OPS],
        result=result,
        labels=[specs[item.field] for item in conditions] if result is not None else [],
        counts_view=counts_view,
        view=view,
        total=total,
        pages=pages,
        pager=pager,
        missing=result.missing if result is not None else (),
        missing_codes=MISSING_CODES,
        snapshot=snapshot,
        stale=stale,
        stale_days=STALE_DAYS,
        error=error,
        notice=notice,
        page=page,
        plan=plan,
        confirm_all=confirm_all,
        presets=presets_view,
    )


def _page_number(text) -> int:
    try:
        return max(1, int(text or 1))
    except ValueError:
        return 1


def _rows_from(query) -> list[dict]:
    names = query.getlist("f")
    ops = query.getlist("o")
    values = query.getlist("v")
    return [
        _draft(name.strip(), op.strip(), value.strip())
        for name, op, value in zip(names, ops, values, strict=False)
    ]


def _posted_rows(data: dict[str, list[str]]) -> list[dict]:
    rows = [
        _draft(name.strip(), op.strip(), value.strip())
        for name, op, value in zip(
            data.get("f", []), data.get("o", []), data.get("v", []), strict=False
        )
    ]
    return rows or [_draft()]


def _draft(name: str = "", op: str = "<=", value="") -> dict[str, str]:
    return {"field": name, "op": op if op in OPS else "<=", "value": _text(value)}


def _draft_of(condition: Condition) -> dict[str, str]:
    return _draft(condition.field, condition.op, condition.value)


def _text(value) -> str:
    if isinstance(value, tuple):
        return f"{value[0]:g}..{value[1]:g}"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _conditions(rows: list[dict]) -> tuple[Condition, ...]:
    return tuple(_condition(row) for row in rows if row["field"])


def _condition(row: dict) -> Condition:
    name, op, text = row["field"], row["op"], row["value"]
    if op == "between":
        low, _, high = text.partition("..")
        return Condition(name, "between", (_number(low, name), _number(high, name)))
    return Condition(name, op, _number(text, name))


def _number(text: str, name: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValidationError(f"{name} 的值要一个数，收到 {text!r}") from None


def _url(
    market: str,
    rows: list[dict],
    *,
    preset=None,
    page: int | None = None,
    confirm: bool = False,
    notice: str = "",
    extra: dict | None = None,
) -> str:
    pairs: list[tuple[str, str]] = [("market", market)]
    if preset is not None:
        pairs.append(("preset", preset.id))
    else:
        for row in rows:
            pairs += [("f", row["field"]), ("o", row["op"]), ("v", row["value"])]
    if page is not None and page > 1:
        pairs.append(("page", str(page)))
    if confirm:
        pairs.append(("confirm", "all"))
    if notice:
        pairs.append(("notice", notice))
    for key, value in (extra or {}).items():
        pairs.append((key, str(value)))
    return f"/screen?{urlencode(pairs)}"


def _preset(ctx, user_id: str, preset_id: str):
    preset = ctx.screener_presets.get(user_id, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="没有这条筛选方案")
    return preset


def _notice(query) -> str:
    kind = query.get("notice") or ""
    if kind == "queued":
        return "已把全市场快照排进下一拍：跑完刷新这一页"
    if kind == "saved":
        return "方案已保存"
    if kind == "renamed":
        return "方案已改名"
    if kind == "updated":
        return "方案的条件已覆盖为当前这一组"
    if kind == "deleted":
        return "方案已删除（已加进自选的票不受影响）"
    if kind == "ran":
        return "方案已按今天的快照重跑"
    if kind == "added":
        added = _count(query.get("added"))
        already = _count(query.get("already"))
        capped = _count(query.get("capped"))
        parts = [f"加入自选 {added} 只"]
        if already:
            parts.append(f"{already} 只原本就在")
        if capped:
            parts.append(f"{capped} 只因自选上限 {MAX_ITEMS_PER_USER} 只没加")
        return "；".join(parts)
    return ""


@router.post("/screen/recollect")
def recollect(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    require_user(request)
    ctx = request.app.state.web.contexts.current()
    market = one(data, "market").strip().upper()
    rows = _posted_rows(data)
    try:
        ctx.scheduler.request_run(job="spot", market=market)
    except ValidationError as exc:
        return _render(
            request,
            request.state.sams_user,
            market=market,
            preset=None,
            rows=rows,
            error=str(exc),
        )
    return RedirectResponse(_url(market, rows, notice="queued"), status_code=303)


def _hit_row(hit, conditions: tuple[Condition, ...], held: set[str]) -> dict:
    return {
        "code": hit.code,
        "name": hit.name or "—",
        "cells": [
            "—" if hit.values.get(item.field) is None else f"{hit.values[item.field]:g}"
            for item in conditions
        ],
        "price": "—" if hit.values.get("price") is None else f"{hit.values['price']:g}",
        "held": hit.code in held,
    }


@router.post("/screen/watchlist")
def add_to_watchlist(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    """三个入口最后都落在 `watchlists.add`（D184）：单行、勾选、全部。

    「全部」越过分页：服务端按这组条件重跑，不把屏幕上那 50 行提交回来。
    先一次 dry-run 页（`confirm=all`），按下去才真写。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    market = one(data, "market").strip().upper()
    rows = _posted_rows(data)
    action = one(data, "action")
    if action == "all":
        if one(data, "confirm") != "yes":
            return RedirectResponse(_url(market, rows, confirm=True), status_code=303)
        try:
            result = ctx.screener.run(market, _conditions(rows))
        except ValidationError as exc:
            return _render(request, user, market=market, preset=None, rows=rows, error=str(exc))
        codes = [hit.code for hit in result.hits]
    elif action == "checked":
        codes = [code.strip() for code in many(data, "codes") if code.strip()]
    elif action.startswith("one:"):
        codes = [action.partition(":")[2].strip()]
    else:
        raise HTTPException(status_code=400, detail="不认识的加自选动作")
    added = already = capped = 0
    for code in dict.fromkeys(codes):
        if ctx.watchlists.contains(user.id, market, code):
            already += 1
            continue
        try:
            ctx.watchlists.add(user.id, market, code)
        except ValidationError:
            capped += 1
            continue
        ctx.symbol_interest.ensure(market, code, actor=user.username)
        added += 1
    return RedirectResponse(
        _url(
            market,
            rows,
            notice="added",
            extra={"added": added, "already": already, "capped": capped},
        ),
        status_code=303,
    )


def _count(text) -> int:
    try:
        return max(0, int(text or 0))
    except ValueError:
        return 0


@router.post("/screen/presets")
def save_preset(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    market = one(data, "market").strip().upper()
    rows = _posted_rows(data)
    try:
        ctx.screener_presets.create(user.id, one(data, "name"), market, list(_conditions(rows)))
    except ValidationError as exc:
        return _render(request, user, market=market, preset=None, rows=rows, error=str(exc))
    return RedirectResponse(_url(market, rows, notice="saved"), status_code=303)


@router.post("/screen/presets/{preset_id}/run")
def run_preset(request: Request, preset_id: str, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    preset = _preset(ctx, user.id, preset_id)
    try:
        result = ctx.screener.run(preset.market, list(preset.conditions))
    except ValidationError as exc:
        rows = [_draft_of(item) for item in preset.conditions]
        return _render(
            request, user, market=preset.market, preset=preset, rows=rows, error=str(exc)
        )
    if result.day is not None:
        ctx.scan_store.record(
            on=result.day,
            market=preset.market,
            strategy_id=preset_strategy_id(preset.name, preset.conditions),
            universe_size=result.universe_size,
            hits=[{"code": hit.code, "name": hit.name, **hit.values} for hit in result.hits],
        )
    return RedirectResponse(_url(preset.market, [], preset=preset, notice="ran"), status_code=303)


@router.post("/screen/presets/{preset_id}/rename")
def rename_preset(
    request: Request,
    preset_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    preset = _preset(ctx, user.id, preset_id)
    rows = [_draft_of(item) for item in preset.conditions]
    try:
        ctx.screener_presets.rename(user.id, preset.id, one(data, "name"))
    except ValidationError as exc:
        return _render(
            request, user, market=preset.market, preset=preset, rows=rows, error=str(exc)
        )
    return RedirectResponse(
        _url(preset.market, [], preset=preset, notice="renamed"), status_code=303
    )


@router.post("/screen/presets/{preset_id}")
def overwrite_preset(
    request: Request,
    preset_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    preset = _preset(ctx, user.id, preset_id)
    market = one(data, "market").strip().upper()
    rows = _posted_rows(data)
    if market != preset.market:
        return _render(
            request,
            user,
            market=market,
            preset=preset,
            rows=rows,
            error=f"方案的市场不能改（现在是 {preset.market}）：要换市场请另存一条",
        )
    try:
        ctx.screener_presets.update_conditions(user.id, preset.id, list(_conditions(rows)))
    except ValidationError as exc:
        return _render(request, user, market=market, preset=preset, rows=rows, error=str(exc))
    return RedirectResponse(_url(market, rows, preset=preset, notice="updated"), status_code=303)


@router.post("/screen/presets/{preset_id}/delete")
def delete_preset(request: Request, preset_id: str, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    preset = _preset(ctx, user.id, preset_id)
    ctx.screener_presets.delete(user.id, preset.id)
    return RedirectResponse(_url(preset.market, [], notice="deleted"), status_code=303)
