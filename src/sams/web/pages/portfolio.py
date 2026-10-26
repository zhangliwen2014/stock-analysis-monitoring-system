"""持仓与成交录入。录入走的是 `LedgerService` 的同一扇门，CLI 与页面没有第二套账。

页面上不自己算钱：`avg_cost`、现价、报告币种那三段全从 `PortfolioService` 读——那里
锚汇率、推币种、拆价格段与换汇段的规则是一份，CLI `sams ledger` 与页面必须读到同一份。
录入也不在这里校验现金与归属，`record_fill` 与 `_require_account` 才是那道门。

一行持仓可能「给不出数」而不是「数为 0」（缺现价、缺汇率、拆不出基准汇率）：那一句
`unavailable_note` 必须跟着这一行走到屏幕上，否则一笔换不出的持仓会显示成 0.00，
还站在一个把它排除在外的合计旁边。模板里那一列「不计入」与「备注」就是为这件事存在的。

仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from sams.bootstrap import ServiceContext
from sams.core.errors import DatabaseLocked, SamsError, ValidationError
from sams.web.auth import csrf_protect, require_user

router = APIRouter()


def _number(raw: str, label: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValidationError(f"{label} 要是数字，收到 {raw!r}") from exc


def _account_view(ctx: ServiceContext, username: str, account) -> dict:
    """一个账户的一屏：汇总 + 逐行持仓。键只留模板要读的那些。

    两个「合计」不在一个空间：上面那个 `total` 是**现金 + 持仓市值**（账户合计），
    每一行持仓带的 `pnl_report` 是**市值 − 成本**（这一行的盈亏）。列名写清「盈亏小计」，
    否则读的人会以为那一列相加就是顶上那个数。
    """
    summary = ctx.portfolio.summary(username, account.id)
    positions = [
        {
            "market": row.market,
            "code": row.code,
            # 成本、现价、价格盈亏这三列在标的币种里；后面那一列盈亏小计在报告币种里，
            # 中间隔着一个换汇段，两列永远不能相加出上面那个合计
            "currency": row.currency,
            # 股数是「多少股」不是「多少钱」：100.0 排在带币种的列里会被当成一个金额
            "qty": f"{row.qty:g}",
            "avg_cost": row.avg_cost,
            "last_price": row.last_price,
            "price_pnl": row.price_pnl,
            "pnl_report": row.total_report,
            "convertible": row.convertible,
            "note": row.unavailable_note,
        }
        for row in ctx.portfolio.positions(username, account.id)
    ]
    return {
        "id": account.id,
        "name": summary.name,
        "currency": summary.report_currency,
        "total": summary.total_report,
        "price_pnl": summary.price_pnl_report,
        "fx_pnl": summary.fx_pnl,
        "realized": summary.realized_report,
        "as_of": summary.as_of,
        "notes": summary.notes,
        "positions": positions,
    }


def _portfolio_page(request: Request, *, error: str | None = None, status: int = 200):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    accounts = [
        _account_view(ctx, user.username, account)
        for account in ctx.sim_accounts.list_for(user.id)
    ]
    theses = ctx.theses.list(user.username)
    return web.render(
        request,
        "portfolio.html",
        status_code=status,
        title="持仓",
        # 市场只能是配置里那几个（同自选页那个下拉）：写一个没配的市场，推不出标的币种，
        # 拒回来的那句话讲的是币种而不是「你市场填错了」
        markets=sorted(ctx.settings.markets),
        accounts=accounts,
        theses=theses,
        error=error,
    )


@router.get("/portfolio")
def portfolio_page(request: Request):
    require_user(request)
    return _portfolio_page(request)


@router.post("/ledger/fill")
def ledger_fill(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    account_id: str = Form(""),
    market: str = Form(""),
    code: str = Form(""),
    side: str = Form("buy"),
    qty: str = Form(""),
    price: str = Form(""),
    fee: str = Form("0"),
    thesis_id: str = Form(""),
):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    try:
        if not account_id.strip():
            # 名下没有账户时那个 <select> 只能交上来一个空 id：交给 `_require_account`
            # 会得到「账户  不存在或不属于用户 …」——空 id 混进一句讲别的账户的话。
            # 这一步真正要做的是开户与注资，两句都是可执行的命令（§5.4 规则 4）
            raise ValidationError(
                f"还没有账户可记账：先 `sams ledger account add --user {user.username} "
                f"--name 主账户` 开一个，再按你打算买入的币种注资 "
                f"`sams ledger cash --user {user.username} --account 开户给出的那个 id "
                f"--currency CNY --amount 100000 --reason 起始资金`"
                f"（账户里没有那种币的现金就买不了，§5.4 规则 4）"
            )
        quantity = _number(qty, "数量")
        trade_price = _number(price, "价格")
        trade_fee = _number(fee, "费用") if fee.strip() else 0.0
    except ValidationError as exc:
        return _portfolio_page(request, status=400, error=str(exc))
    try:
        kwargs = dict(
            qty=quantity,
            price=trade_price,
            fee=trade_fee,
            thesis_id=thesis_id.strip() or None,
        )
        # 市场与代码不在这里归一：`_fill` 自己归一并自己推币种，页面重复一遍
        # 只会得到两套规则
        if side == "buy":
            ctx.ledger.buy(account_id, user.username, market, code, **kwargs)
        elif side == "sell":
            ctx.ledger.sell(account_id, user.username, market, code, **kwargs)
        else:
            raise ValidationError("方向只能是 buy 或 sell")
    except DatabaseLocked:
        # 库被调度线程握着：那是「过一会儿再记」，不是「你填错了」，交给全局处理器答 503
        raise
    except SamsError as exc:
        # 业务拒绝（没有那种币的现金、超卖、账户不是你的）留在这里说：那几句是可执行的，
        # 而 error.html 给的是一屏没有表单的话
        return _portfolio_page(request, status=400, error=str(exc))
    # 成交即登记兴趣：主档建行 + 排补数拍（D144）；表单原样交出去，归一在登记处
    ctx.symbol_interest.ensure(market, code, actor=user.username)
    return RedirectResponse("/portfolio", status_code=303)
