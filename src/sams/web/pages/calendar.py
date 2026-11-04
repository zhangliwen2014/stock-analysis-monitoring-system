"""事件与复检 /calendar（D232 P0-2）。

只回答「我的标的最近披露/公告了什么」：自选、持仓、推进中的论点各算一路，
按截至日回看 30 天。财报预约日与未来除权日没有可靠数据源入库，
这一屏不猜未来，把「为什么没有」直接写给用户看。

披露日缺失与「没有披露」是两件事：库里有财报但 `published_date` 为空（港股/美股常见）
的标的单独点出来，否则它会在「最近披露」里静默消失，被读成「没披露」（同 D61）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fastapi import APIRouter, Request

from sams.services.recheck import (
    DEFAULT_WINDOW_DAYS,
    recent_announcements,
    recent_disclosures,
)
from sams.storage.repos.theses import OPENED, TRACKING
from sams.web.auth import require_user

router = APIRouter()

_RECHECK_STATUSES = (OPENED, TRACKING)


@dataclass(frozen=True)
class Unpublished:
    market: str
    code: str
    name: str


@router.get("/calendar")
def calendar_page(request: Request):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    as_of = web.core.clock.today()
    symbols = _my_symbols(ctx, user)
    disclosures, announcements, unpublished = _gather(ctx, symbols, as_of)
    return web.render(
        request,
        "calendar.html",
        title="事件与复检",
        as_of=as_of,
        window_days=DEFAULT_WINDOW_DAYS,
        symbol_count=len(symbols),
        disclosures=recent_disclosures(disclosures, as_of=as_of),
        announcements=recent_announcements(announcements, as_of=as_of),
        unpublished=unpublished,
    )


def _my_symbols(ctx, user) -> list[tuple[str, str]]:
    found: dict[tuple[str, str], None] = {}
    for market, code in ctx.watchlists.codes(user.id):
        found[(market, code)] = None
    for account in ctx.sim_accounts.list_for(user.id):
        for position in ctx.sim_ledger.positions(account.id):
            if position.qty != 0:
                found[(position.market, position.code)] = None
    for thesis in ctx.theses.list(user.username):
        if thesis.status in _RECHECK_STATUSES:
            found[(thesis.market, thesis.code)] = None
    return list(found)


def _name(ctx, market: str, code: str) -> str:
    found = ctx.symbols.get(market, code) or {}
    return str(found.get("name") or code)


def _gather(ctx, symbols, as_of: date):
    disclosure_rows: list[dict] = []
    announcement_rows: list[dict] = []
    unpublished: list[Unpublished] = []
    for market, code in symbols:
        name = _name(ctx, market, code)
        latest = ctx.financials_store.history(market, code, as_of=as_of, limit=1)
        if latest.is_empty():
            raw = ctx.financials_store.recent(market, code, limit=1)
            if not raw.is_empty():
                unpublished.append(Unpublished(market=market, code=code, name=name))
        else:
            row = latest.row(0, named=True)
            disclosure_rows.append(
                {
                    "market": market,
                    "code": code,
                    "name": name,
                    "report_date": row["report_date"],
                    "published_date": row["published_date"],
                }
            )
        for item in ctx.announcements_store.latest(market, code, limit=10).to_dicts():
            item["name"] = name
            announcement_rows.append(item)
    return disclosure_rows, announcement_rows, unpublished
