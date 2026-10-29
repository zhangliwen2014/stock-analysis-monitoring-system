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
from sams.web.display import moment_label

router = APIRouter()

#: 记账成功后回到这一页的那句话：303 只说明「收了」，屏幕上要有人说「记下了」
_NOTICES = {
    "account": "账户已开好。",
    "renamed": "账户名已修改。",
    "undone": "成交已撤销，按正确的重记一笔。",
    "fx": "换汇已记账。",
    "cash": "现金已调整。",
    "fill": "成交已记一笔。",
}


#: 市场在录入表单里的叫法：港股通不是市场，是 A 股账户买 HK 的那条通道（D208）
_MARKET_LABELS = {"A": "A 股", "HK": "港股通（HK）", "US": "美股（US）"}
_ACCOUNT_LABELS = {"A": "A 股账户", "HK": "港股账户", "US": "美股账户"}


def _number(raw: str, label: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValidationError(f"{label} 要是数字，收到 {raw!r}") from exc


def _qty(value: float) -> str:
    """股数带千分位：`{1_000_000:g}` 会印成 `1e+06`，与「数量（股）」这一列对不上。"""
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _fill_defaults(ctx: ServiceContext, markets: list[str], accounts: list[dict]) -> dict[str, str]:
    """录入成交的默认账户与市场：第一个账户、它的主场（D208）。

    字母序第一个市场恒是 A，与「这个人平时买什么」无关；港股账户每次手改下拉，
    就是在为一处没有信息的默认值付手续费。
    """
    account = accounts[0] if accounts else None
    home = account["home_market"] if account else ""
    return {
        "account_id": account["id"] if account else "",
        "side": "buy",
        "market": home if home in markets else (markets[0] if markets else ""),
        "code": "",
        "qty": "",
        "price": "",
        "fee": "0",
        "on": str(ctx.clock.today()),
        "thesis_id": "",
    }


def _fill_rates(ctx: ServiceContext, markets: list[str]) -> dict[str, dict[str, str]]:
    """每个市场那一格汇率的预填值与两句提示（D209）。

    唯一的规则是「有标的币种现金就花它，没有才按汇率折报告币种」：预填一个汇率会把
    D207「换好了 HKD 再买」悄悄改成 CNY 结算，HKD 现金从此趴在账上不动。
    """
    report = ctx.settings.report_currency
    out: dict[str, dict[str, str]] = {}
    for market in markets:
        currency = ctx.settings.markets[market].currency
        if currency == report:
            same = f"以 {currency} 计价，与报告币种相同：不需要汇率"
            out[market] = {
                "currency": currency, "report": report, "rate": "",
                "with_rate": same, "with_cash": same,
            }
            continue
        rate, caption = _rate_hint(ctx, currency, report)
        out[market] = {
            "currency": currency,
            "report": report,
            "rate": rate,
            "with_rate": f"{caption}；填了就从 {report} 结算，留空则从 {currency} 现金扣",
            "with_cash": f"{caption}；这笔从 {currency} 现金扣，要以 {report} 结算就填汇率",
        }
    return out


def _fill_rate_default(
    rates: dict[str, dict[str, str]], account: dict | None, market: str
) -> str:
    info = rates.get(market)
    if info is None or not info["rate"]:
        return ""
    held = account["cash_currencies"] if account else []
    return "" if info["currency"] in held else info["rate"]


def _fill_rate_hint(
    rates: dict[str, dict[str, str]], account: dict | None, market: str
) -> str:
    info = rates.get(market)
    if info is None:
        return ""
    held = account["cash_currencies"] if account else []
    if info["currency"] == info["report"]:
        return info["with_rate"]
    return info["with_cash"] if info["currency"] in held else info["with_rate"]


def _cash_defaults(ctx: ServiceContext) -> dict[str, str]:
    return {
        "account_id": "",
        "side": "deposit",
        "currency": ctx.settings.report_currency,
        "amount": "",
        "reason": "",
    }


def _rate_hint(ctx: ServiceContext, base: str, quote: str) -> tuple[str, str]:
    """哪一个是可预填的汇率、这一句说它从哪来（D207）。

    库里只存一个方向；反方向取倒数并**说出来**，不假装它是库里那条。
    两边都没有就空着：给一个猜的数比让用户填券商成交价更糟。
    """
    today = ctx.clock.today()
    found = ctx.fx_store.latest(today, base, quote)
    if found is not None:
        source = found.source or "—"
        return (
            f"{found.rate:g}",
            f"预填：{base}→{quote} {found.rate:.4f}（{found.day}，{source}）",
        )
    reverse = ctx.fx_store.latest(today, quote, base)
    if reverse is not None:
        rate = 1 / reverse.rate
        source = reverse.source or "—"
        return (
            f"{rate:.6g}",
            f"预填：由 {quote}→{base} {reverse.rate:.4f} 取倒数（{reverse.day}，{source}）",
        )
    return "", f"库里没有 {base}→{quote} 的汇率：填券商给你的成交汇率，或先 `sams sync fx`"


def _fx_defaults(ctx: ServiceContext, currencies: list[str]) -> dict[str, str]:
    base = ctx.settings.report_currency
    # 港股通是这一栏存在的原因：有 HKD 就默认 CNY→HKD，没有就换到另一种币
    quote = "HKD" if base != "HKD" and "HKD" in currencies else next(
        (item for item in currencies if item != base), base
    )
    rate, _ = _rate_hint(ctx, base, quote)
    return {
        "account_id": "",
        "from_ccy": base,
        "to_ccy": quote,
        "amount": "",
        "rate": rate,
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


def _fills(ctx: ServiceContext, account, tz: str) -> list[dict]:
    """最近 50 笔成交（新的在上）：撤销要能看见「撤的是哪一笔」（D205）。"""
    names: dict[tuple[str, str], str] = {}
    rows = []
    for order in list(reversed(ctx.sim_ledger.orders(account.id)))[:50]:
        key = (order.market, order.code)
        if key not in names:
            # 先读全主档再退回 `symbols`：成交记录里的港股通常没有那行 `symbols`
            # （补录、主档同步前记的），只有主档一直有名称（D210）
            found = ctx.symbol_list_store.get(order.market, order.code) or ctx.symbols.get(
                order.market, order.code
            )
            names[key] = str((found or {}).get("name") or "")
        rows.append(
            {
                "id": order.id,
                "at": moment_label(order.ts, tz, seconds=True),
                "market": order.market,
                "code": order.code,
                "name": names[key],
                "side": "买入" if order.side == "buy" else "卖出",
                "qty": _qty(order.qty),
                "price": order.price,
                "fee": order.fee,
                "currency": order.currency,
                # CNY 少了一笔、订单上写 HKD：不把结算说出来，读账的人找不到两条流水的关系
                "settled": (
                    f"按 {order.fx_rate_applied:g} 折 {account.report_currency}"
                    if order.fx_rate_applied is not None
                    else ""
                ),
            }
        )
    return rows


def _account_view(
    ctx: ServiceContext,
    username: str,
    account,
    *,
    rename_name: str | None = None,
    tz: str,
) -> dict:
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
        # 开户时选的「平时买什么」：录入成交的市场默认取它（D208）
        "home_market": account.home_market,
        # 改名那一格平时就是现在的名字；被拒的那次回填的是刚交上来的那个（D204）
        "rename_name": account.name if rename_name is None else rename_name,
        "currency": summary.report_currency,
        "cash": [
            {"currency": name, "amount": amount}
            for name, amount in sorted(summary.cash_by_currency.items())
        ],
        # 汇率格的默认值看它：账户里已经有这种币的现金，就不预填汇率（D209）
        "cash_currencies": [
            name for name, amount in sorted(summary.cash_by_currency.items()) if amount > 0
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
        "fills": _fills(ctx, account, tz),
        "fill_total": ctx.sim_ledger.order_rows(account.id),
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
    rename_form: dict[str, str] | None = None,
    fx_form: dict[str, str] | None = None,
):
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    markets = sorted(ctx.settings.markets)
    tz = ctx.settings.server.display_tz
    accounts = [
        _account_view(
            ctx,
            user.username,
            account,
            rename_name=(rename_form or {}).get(account.id),
            tz=tz,
        )
        for account in ctx.sim_accounts.list_for(user.id)
    ]
    theses = ctx.theses.list(user.username)
    currencies = sorted(
        {ctx.settings.report_currency, *(cfg.currency for cfg in ctx.settings.markets.values())}
    )
    fx = fx_form or _fx_defaults(ctx, currencies)
    fill_rates = _fill_rates(ctx, markets)
    if fill_form is None:
        fill = _fill_defaults(ctx, markets, accounts)
        fill_account = accounts[0] if accounts else None
        fill["fx_rate"] = _fill_rate_default(fill_rates, fill_account, fill["market"])
        # 初始渲染时这一格的值就是自动值：换市场时可以被覆盖
        fill_auto = fill["fx_rate"]
    else:
        fill = fill_form
        fill_account = next(
            (row for row in accounts if row["id"] == fill["account_id"]), None
        ) or (accounts[0] if accounts else None)
        # 被拒回填的值是用户打的：换市场也不要覆盖它
        fill_auto = ""
    fill_hint = _fill_rate_hint(fill_rates, fill_account, fill["market"])
    return web.render(
        request,
        "portfolio.html",
        status_code=status,
        title="持仓",
        # 市场只能是配置里那几个（同自选页那个下拉）：写一个没配的市场，推不出标的币种，
        # 拒回来的那句话讲的是币种而不是「你市场填错了」
        markets=markets,
        currencies=currencies,
        market_labels=_MARKET_LABELS,
        account_labels=_ACCOUNT_LABELS,
        accounts=accounts,
        theses=theses,
        error=error,
        notice=_NOTICES.get(notice, ""),
        # 三张表单各自回填：一张被拒不该改写另一张的当前值（D197）
        fill=fill,
        fill_rates=fill_rates,
        fill_hint=fill_hint,
        fill_auto=fill_auto,
        cash=cash_form or _cash_defaults(ctx),
        fx=fx,
        fx_hint=_rate_hint(ctx, fx["from_ccy"], fx["to_ccy"])[1],
        account_form=account_form or {"name": "", "cash": "", "home_market": "A"},
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
    home_market: str = Form("A"),
):
    """网页开户：空态那句「去跑 CLI」在 `sams serve` 独占库时是一条死路（D194）。"""
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    form = {"name": name, "cash": cash, "home_market": home_market}
    try:
        amount = _number(cash, "起始资金") if cash.strip() else 0.0
        if amount < 0:
            raise ValidationError("起始资金不能是负数")
        account = ctx.ledger.open_account(
            user.username, name.strip(), home_market=home_market
        )
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


@router.post("/portfolio/accounts/{account_id}/rename")
def rename_account(
    request: Request,
    account_id: str,
    _csrf: None = Depends(csrf_protect),
    name: str = Form(""),
):
    """开户时名字打错（「中国银行」/「中国银河」）就在那张卡上改，不必重开一个户（D204）。"""
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    try:
        ctx.ledger.rename_account(user.username, account_id, name)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _portfolio_page(
            request, status=400, error=str(exc), rename_form={account_id: name}
        )
    return RedirectResponse("/portfolio?notice=renamed", status_code=303)


@router.post("/portfolio/accounts/{account_id}/fills/{order_id}/undo")
def undo_fill(
    request: Request,
    account_id: str,
    order_id: str,
    _csrf: None = Depends(csrf_protect),
):
    """录错市场/代码的出口：撤销这笔成交，再按正确的重记一笔（D205）。"""
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    try:
        ctx.ledger.undo_fill(user.username, account_id, order_id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _portfolio_page(request, status=400, error=str(exc))
    return RedirectResponse("/portfolio?notice=undone", status_code=303)


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


@router.post("/ledger/fx")
def ledger_fx(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    account_id: str = Form(""),
    from_ccy: str = Form("CNY"),
    to_ccy: str = Form("HKD"),
    amount: str = Form(""),
    rate: str = Form(""),
):
    """显式换汇：港股通先换 HKD 再买（D207）。汇率按用户填的实际成交价。"""
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    form = {
        "account_id": account_id,
        "from_ccy": from_ccy.strip().upper(),
        "to_ccy": to_ccy.strip().upper(),
        "amount": amount,
        "rate": rate,
    }
    try:
        ctx.ledger.convert(
            user.username,
            account_id,
            form["from_ccy"],
            form["to_ccy"],
            _number(amount, "金额"),
            _number(rate, "汇率"),
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _portfolio_page(request, status=400, error=str(exc), fx_form=form)
    return RedirectResponse("/portfolio?notice=fx", status_code=303)


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
    fx_rate: str = Form(""),
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
        "fx_rate": fx_rate,
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
        rate = _number(fx_rate, "汇率") if fx_rate.strip() else None
        moment = _trade_moment(ctx, on, market)
    except ValidationError as exc:
        return _portfolio_page(request, status=400, error=str(exc), fill_form=form)
    try:
        kwargs = dict(
            qty=quantity,
            price=trade_price,
            fee=trade_fee,
            fx_rate=rate,
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
