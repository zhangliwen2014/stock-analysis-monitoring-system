"""论点：列表、新建、详情与推进。合法的下一步只有 `next_steps` 一处知道。

页面不写第二份状态名单：`ThesisService.next_steps` 就是仓库那张 `TRANSITIONS`
（`storage/repos/theses.py`），下拉里给的与被拒时念的都是它。「哪一步现在走得了」由仓库
判（`advance` 那道闸还管着建仓要价位齐备），页面只在它之前把合法档位说回去。

新建也不在这里归一市场与代码（与持仓页录成交同一个理由，见 `portfolio.py`）：
`ThesisService.add` 走的三条路各自都归一——`_known_market` 先 `canonical_market` 再比配置、
`ValuationService.read` 归一后才取数、`ThesisRepo.create` 落库前过 `_norm`。表单上人能敲出
`a` 或 `sh600519`，那些形态服务层都认得；页面再排一遍只会得到两套规则。

时刻不在模板里 `strftime`：`TIMESTAMPTZ` 的回读在 UTC 与会话本地（Asia/Shanghai）之间
不稳定，而这条时间线一拉就是几个月。事件行在 Python 里拼好，moment 走
`format_utc_moment`（写法点名时区，CLI 整条时间线用的也是它）；列表那一列只要月日时分，
走 `display.moment_label`。库里那几列价位属于哪一天也一样——措辞抄服务，不另造第三句。

仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.core.errors import DatabaseLocked, SamsError, StorageError, ValidationError
from sams.core.timeutil import format_utc_moment
from sams.services.valuation import STYLE_NAMES
from sams.storage.repos.theses import Thesis
from sams.web.auth import csrf_protect, require_user
from sams.web.display import moment_label

router = APIRouter()

#: 详情那一屏摆的几列价位。标签与服务那张对照表同一批词（`services/theses.py` 的 `_ROWS`：
#: 买入区/止损/目标/赔率），判「库里到底有没有价位」也算这几列——不含 `win_rate`：
#: 那一列是 M5 回填的，有它而价位全空时被算成「价位填过了」就把一条空计划说成有计划
_LEVEL_ROWS = (
    ("买入区", ("entry_low", "entry_high")),
    ("止损", ("stop_price",)),
    ("目标", ("target_price",)),
    ("赔率", ("reward_risk",)),
)
_LEVEL_COLUMNS = tuple(dict.fromkeys(col for _, cols in _LEVEL_ROWS for col in cols))


def _require_own(ctx, user, thesis_id: str) -> Thesis:
    """不存在与不是你的都 404：`require` 那句 `StorageError` 不该往上冒给浏览器。

    但 `DatabaseLocked` 也是 `StorageError` 的子类（`core/errors.py`）：库被调度线程握着
    的时候答「不存在或属于别人」是一句自信的错误话——它会让人以为这条论没了，
    而该做的是过一会儿再来（同一形状见 `portfolio.py` 的录入）。
    """
    try:
        return ctx.theses_store.require(user.id, thesis_id)
    except DatabaseLocked:
        raise
    except StorageError:
        raise HTTPException(status_code=404) from None


def _list_page(request: Request, *, error: str | None = None, status: int = 200):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    tz = ctx.settings.server.display_tz
    rows = [
        {
            "id": thesis.id,
            "market": thesis.market,
            "code": thesis.code,
            "status": thesis.status,
            "style": thesis.style,
            # 不把 datetime 直接交给模板：那等于让 Jinja 去猜这一列属于哪个时区
            "updated_at": moment_label(thesis.updated_at, tz),
        }
        for thesis in ctx.theses.list(user.username)
    ]
    return web.render(
        request,
        "theses.html",
        status_code=status,
        title="论点",
        rows=rows,
        # 下拉里的值就是 `ThesisService.add` 收得下的那批模板名。模板的中文名（价值型/趋势型）
        # 住在 analysis 层，web 不许 import 它（`tests/test_layering.py`），所以这里给的是名字本身
        styles=STYLE_NAMES,
        error=error,
    )


def _levels(thesis: Thesis) -> list[dict]:
    return [
        {
            "label": label,
            "low": getattr(thesis, columns[0]),
            "high": getattr(thesis, columns[1]) if len(columns) > 1 else None,
            "kind": "range" if len(columns) > 1 else "one",
        }
        for label, columns in _LEVEL_ROWS
    ]


def _levels_note(thesis: Thesis) -> str:
    """那几列价位属于哪一天。

    两句话逐字抄 `ThesisReport.stored_on`（`services/theses.py`）：同一个事实在两处各造
    一句，改一处的措辞另一处就永远对不上。价位一列都没有时不能说「手填」——那是给用户
    安一个他没做过的动作，所以那种情况说的是「库里还没有价位」（`stored_axis` 的同一句话）。
    """
    if not any(getattr(thesis, column) is not None for column in _LEVEL_COLUMNS):
        return "库里还没有价位"
    return f"截至 {thesis.levels_on}" if thesis.levels_on is not None else "没有测量日（手填）"


def _detail_page(
    request: Request, thesis: Thesis, *, error: str | None = None, status: int = 200
):
    """详情这一屏只读两条：`require` 与 `events`。

    不走 `ThesisService.show`：它会把估值整趟重算一遍，而这一页要回答的是「库里存了什么、
    走过哪几步」，那些数今天重算出来也不属于这一条论点（漂移的判定要拿建仓时那份快照，D76）。
    """
    web = request.app.state.web
    ctx = web.contexts.current()
    events = [
        {
            # 措辞与 CLI 那条时间线同源：同一个瞬间在页面上与在 `sams thesis show` 里
            # 必须读成一个样子，而这条链跨几个月时差八小时是看不出来的
            "at": "–" if event.ts is None else format_utc_moment(event.ts),
            "from": event.from_status or "–",
            "to": event.to_status,
            "note": event.note or "",
        }
        for event in ctx.theses_store.events(thesis.user_id, thesis.id)
    ]
    return web.render(
        request,
        "thesis.html",
        status_code=status,
        title=f"论点 {thesis.market}/{thesis.code}",
        thesis=thesis,
        events=events,
        # 合法档位只有一个出处：这张表变了，下拉与被拒时那句念的都跟着变
        steps=ctx.theses.next_steps(thesis.status),
        levels=_levels(thesis),
        levels_note=_levels_note(thesis),
        # 库里存的是小数口径（0.55 = 55%，D58）。不套 `pct`：那个全局带正负号，是给涨跌用的，
        # 「胜率 +55.00%」会把一个概率读成一次涨幅
        win_rate=None if thesis.win_rate is None else f"{thesis.win_rate:.1%}",
        win_rate_samples=thesis.win_rate_samples,
        error=error,
    )


@router.get("/theses")
def theses_page(request: Request):
    require_user(request)
    return _list_page(request)


@router.post("/theses/create")
def theses_create(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    market: str = Form(""),
    code: str = Form(""),
    style: str = Form("value"),
    note: str = Form(""),
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    try:
        # 表单值原样交出去（只 strip）：`add` 那三条路自己会归一，见模块开头
        report = ctx.theses.add(
            user.username,
            market.strip(),
            code.strip(),
            style=style.strip(),
            note=note.strip() or None,
        )
    except DatabaseLocked:
        # 库被调度线程握着：那是「过一会儿再立」，不是「你填错了」，交给全局处理器答 503
        raise
    except SamsError as exc:
        # 业务拒绝（没配的市场、没这个模板、同一只票已有活的论）留在这里说：
        # 那几句点名了能填什么，而 error.html 是一屏没有表单的话
        return _list_page(request, status=400, error=str(exc))
    return RedirectResponse(f"/theses/{report.thesis.id}", status_code=303)


@router.get("/theses/{thesis_id}")
def thesis_page(request: Request, thesis_id: str):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    thesis = _require_own(ctx, user, thesis_id)
    return _detail_page(request, thesis)


@router.post("/theses/{thesis_id}/advance")
def thesis_advance(
    request: Request,
    thesis_id: str,
    _csrf: None = Depends(csrf_protect),
    to: str = Form(""),
    note: str = Form(""),
):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    thesis = _require_own(ctx, user, thesis_id)
    steps = ctx.theses.next_steps(thesis.status)
    try:
        if to not in steps:
            # 这一步在页面上拦，是为了把合法的档位一起说回去（下拉里本来就只有这几个）；
            # 真正的闸在仓库，`advance` 走到那儿还会再拒一次
            raise ValidationError(
                f"不能从 {thesis.status} 推进到 {to or '（空）'}："
                f"合法的下一步是 {'/'.join(steps) or '（没有，这是终点）'}"
            )
        ctx.theses.advance(user.username, thesis_id, to, note=note.strip() or None)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _detail_page(request, thesis, status=400, error=str(exc))
    return RedirectResponse(f"/theses/{thesis_id}", status_code=303)
