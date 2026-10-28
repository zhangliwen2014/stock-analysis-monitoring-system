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

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from sams.bootstrap import ServiceContext
from sams.core.codes import canonical_market
from sams.core.errors import DatabaseLocked, SamsError, ValidationError
from sams.web.auth import csrf_protect, require_user

router = APIRouter()

#: 记账成功后回到这一页的那句话：303 只说明「收了」，屏幕上要有人说「记下了」
_NOTICES = {
    "account": "账户已开好。",
    "cash": "现金已调整。",
    "fill": "成交已记一笔。",
}


def _number(raw: str, label: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValidationError(f"{label} 要是数字，收到 {raw!r}") from exc


def _qty(value: float) -> str:
    """股数带千分位：`{1_000_000:g}` 会印成 `1e+06`，与「数量（股）」这一列对不上。"""
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _fill_defaults(ctx: ServiceContext, markets: list[str]) -> dict[str, str]:
    return {
        "account_id": "",
        "side": "buy",
        "market": markets[0] if markets else "",
        "code": "",
        "qty": "",
        "price": "",
        "fee": "0",
        "on": str(ctx.clock.today()),
        "thesis_id": "",
    }


def _cash_defaults(ctx: ServiceContext) -> dict[str, str]:
    return {
        "account_id": "",
        "side": "deposit",
        "currency": ctx.settings.report_currency,
        "amount": "",
        "reason": "",
    }


def _trade_moment(ctx: ServiceContext, raw: str, market: str) -> datetime | None:
    """成交日按市场本地 15:00 记账（与 CLI 的 `--on` 同一条口径，D196）。

    空字符串就是「按现在」；写了就必须是 YYYY-MM-DD——解析失败时静默改成今天，
    补录的一笔就悄悄落在了错的一天。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"成交日要写成 YYYY-MM-DD，收到 {text!r}") from None
    zone = ctx.settings.market_cfg(canonical_market(market)).zone()
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=zone)


def _account_view(ctx: ServiceContext, username: str, account) -> dict:
    """一个账户的一屏：汇总 + 逐行持仓。键只留模板要读的那些。

    现金（可用）与持仓市值分开给：「合计」是一个**总资产**，只把它端出来，
    读的人无法拆回还能买多少、手里那些票值多少（C1 验收 P1-3，D195）。
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
            "qty": _qty(row.qty),
            "avg_cost": row.avg_cost,
            "last_price": row.last_price,
            # 每一行自己的行情日：停牌那一行不能借账户的「截至」显得和最新那天一样新
            "price_day": str(row.last_price_date) if row.last_price_date else "",
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
        "cash": [
            {"currency": name, "amount": amount}
            for name, amount in sorted(summary.cash_by_currency.items())
        ],
        "value": [
            {"currency": name, "amount": amount}
            for name, amount in sorted(summary.position_value_by_currency.items())
        ],
        "total": summary.total_report,
        "price_pnl": summary.price_pnl_report,
        "fx_pnl": summary.fx_pnl,
        "realized": summary.realized_report,
        "as_of": summary.as_of,
        "notes": summary.notes,
        "duplicates": ctx.portfolio.duplicate_holdings(username, account.id),
        "positions": positions,
    }


def _portfolio_page(
    request: Request,
    *,
    error: str | None = None,
    status: int = 200,
    notice: str = "",
    fill_form: dict[str, str] | None = None,
    cash_form: dict[str, str] | None = None,
    account_form: dict[str, str] | None = None,
):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    markets = sorted(ctx.settings.markets)
    accounts = [
        _account_view(ctx, user.username, account)
        for account in ctx.sim_accounts.list_for(user.id)
    ]
    theses = ctx.theses.list(user.username)
    currencies = sorted(
        {ctx.settings.report_currency, *(cfg.currency for cfg in ctx.settings.markets.values())}
    )
    return web.render(
        request,
        "portfolio.html",
        status_code=status,
        title="持仓",
        # 市场只能是配置里那几个（同自选页那个下拉）：写一个没配的市场，推不出标的币种，
        # 拒回来的那句话讲的是币种而不是「你市场填错了」
        markets=markets,
        currencies=currencies,
        accounts=accounts,
        theses=theses,
        error=error,
        notice=_NOTICES.get(notice, ""),
        # 三张表单各自回填：一张被拒不该改写另一张的当前值（D197）
        fill=fill_form or _fill_defaults(ctx, markets),
        cash=cash_form or _cash_defaults(ctx),
        account_form=account_form or {"name": "", "cash": ""},
    )


@router.get("/portfolio")
def portfolio_page(request: Request, notice: str = ""):
    require_user(request)
    return _portfolio_page(request, notice=notice)


@router.post("/portfolio/accounts")
def open_account(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    name: str = Form(""),
    cash: str = Form(""),
):
    """网页开户：空态那句「去跑 CLI」在 `sams serve` 独占库时是一条死路（D194）。"""
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    form = {"name": name, "cash": cash}
    try:
        amount = _number(cash, "起始资金") if cash.strip() else 0.0
        if amount < 0:
            raise ValidationError("起始资金不能是负数")
        account = ctx.ledger.open_account(user.username, name.strip())
        if amount:
            ctx.ledger.deposit(
                account.id,
                ctx.settings.report_currency,
                amount,
                username=user.username,
                reason="起始资金",
                actor_id=user.id,
            )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _portfolio_page(request, status=400, error=str(exc), account_form=form)
    return RedirectResponse("/portfolio?notice=account", status_code=303)


@router.post("/ledger/cash")
def ledger_cash(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    account_id: str = Form(""),
    side: str = Form("deposit"),
    currency: str = Form("CNY"),
    amount: str = Form(""),
    reason: str = Form(""),
):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    form = {
        "account_id": account_id,
        "side": side,
        "currency": currency,
        "amount": amount,
        "reason": reason,
    }
    try:
        if side not in ("deposit", "withdraw"):
            raise ValidationError("现金动作只能是 deposit 或 withdraw")
        value = _number(amount, "金额")
        if value <= 0:
            raise ValidationError("金额要大于 0")
        label = "入金" if side == "deposit" else "取出"
        move = ctx.ledger.deposit if side == "deposit" else ctx.ledger.withdraw
        move(
            account_id,
            currency.strip().upper(),
            value,
            username=user.username,
            reason=reason.strip() or label,
            actor_id=user.id,
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _portfolio_page(request, status=400, error=str(exc), cash_form=form)
    return RedirectResponse("/portfolio?notice=cash", status_code=303)


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
    on: str = Form(""),
):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    form = {
        "account_id": account_id,
        "side": side,
        "market": market,
        "code": code,
        "qty": qty,
        "price": price,
        "fee": fee,
        "on": on,
        "thesis_id": thesis_id,
    }
    try:
        if not account_id.strip():
            # 名下没有账户时那个 <select> 只能交上来一个空 id：交给 `_require_account`
            # 会得到「账户  不存在或不属于 …」——空 id 混进一句讲别的账户的话。
            # 开户与注资现在都在这一页上，所以下一步指回表单本身
            raise ValidationError(
                "还没有账户可记账：先开一个账户（页面下方），注资后就能记成交"
            )
        quantity = _number(qty, "数量")
        trade_price = _number(price, "价格")
        trade_fee = _number(fee, "费用") if fee.strip() else 0.0
        moment = _trade_moment(ctx, on, market)
    except ValidationError as exc:
        return _portfolio_page(request, status=400, error=str(exc), fill_form=form)
    try:
        kwargs = dict(
            qty=quantity,
            price=trade_price,
            fee=trade_fee,
            thesis_id=thesis_id.strip() or None,
            ts=moment,
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
        return _portfolio_page(request, status=400, error=str(exc), fill_form=form)
    # 成交即登记兴趣：主档建行 + 排补数拍（D144）；表单原样交出去，归一在登记处
    ctx.symbol_interest.ensure(market, code, actor=user.username)
    return RedirectResponse("/portfolio?notice=fill", status_code=303)
