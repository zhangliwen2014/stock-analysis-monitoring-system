"""建规则那一屏：看得见现价，保存前能试算（R-22 剩余）。

现价读的是 `web/quotes.py` 那一份统一模型，本文件不重测它怎么挑价、怎么标 as-of
（`tests/web/test_quote_view.py` 在管）。这里钉的是表单这一屏独有的三件事：
什么时候有现价那句话、绝对价那一格的相对位置说对了没有、试算那颗按钮真的什么都不写。
"""

from __future__ import annotations

from datetime import date

import polars as pl


def _alice_id(logged) -> str:
    return logged.context.users.get_by_username("alice").id


def _seed_close(logged, *, code: str = "600519", close: float = 2.2, day=date(2026, 10, 30)):
    """库里有一根日线：休市那一屏的现价就是它（没有行情板可读，见 conftest 的 FakeRuntime）。

    OHLC 按收盘价摆开：`bars.upsert` 会对不自洽的行把 high/low 置空并告警，
    那种行读出来的收盘还是同一个数，但屏幕上多出一句与本意无关的警告。

    顺带记一笔公司行为覆盖：试算走的是引擎，引擎判价要过复权闸（D49），没确认过的票
    交的是「判不了」而不是一个数——那一摞会把这一屏真正要验的东西换成一句采集提示。
    """
    logged.context.corp_actions_coverage.record(
        "A", code, covered_from=date(2000, 1, 1), covered_to=day,
        full_history=True, src="akshare",
    )
    logged.context.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"], "code": [code], "date": [day],
                "open": [close * 0.99], "high": [close * 1.01], "low": [close * 0.98],
                "close": [close], "volume": [100.0], "amount": [close * 100],
                "source": ["akshare"],
            }
        )
    )


def _new(
    logged,
    *,
    kind: str = "stop.fixed",
    target_type: str = "symbol",
    target_ref: str = "A/600519",
):
    return logged.client.get(
        f"/rules/new?kind={kind}&target_type={target_type}&target_ref={target_ref}"
    )


# --- 现价那一格 ------------------------------------------------------------------------


def test_the_new_rule_form_shows_the_price_of_the_symbol_it_is_mounted_on(logged):
    """个股页那个「新建止损」链接带着标的进来，现价就该在这一屏上，不必先保存。"""
    _seed_close(logged, close=1392.05)
    page = _new(logged)
    assert page.status_code == 200
    assert "1,392.05" in page.text
    assert "元" in page.text
    assert "10-30 收盘" in page.text
    assert "akshare" in page.text


def test_the_form_without_daily_bars_says_what_to_run(logged):
    """没有日线时说的是下一步命令，而不是「现价 —」：后者读起来像页面坏了。"""
    page = _new(logged, target_ref="A/000001")
    assert page.status_code == 200
    assert "sams sync daily --market A --code 000001" in page.text
    assert "现价 —" not in page.text


def test_the_form_says_a_reference_without_market_is_not_a_symbol(logged):
    """只敲六位数字时不猜是哪只票：那一格说自己读不出引用，整屏照常渲染。"""
    _seed_close(logged, close=1392.05)
    page = _new(logged, target_ref="600519")
    assert page.status_code == 200
    assert "读不出一只票" in page.text
    assert "1,392.05" not in page.text


def test_a_rule_mounted_on_a_collection_says_there_is_no_single_price(logged):
    """挂在全部自选上没有「这一只的现价」：留空白会让人以为这一屏漏了一格。"""
    _seed_close(logged)
    page = _new(logged, target_type="watchlist", target_ref="*")
    assert page.status_code == 200
    assert "没有单一现价" in page.text


# --- 绝对价那一格：线离现价多远 ----------------------------------------------------------


def _reject_a_post(logged, **overrides) -> object:
    """交一张缺名字的表：创建被拒，同一屏带着刚填的原文重渲染（`param_*` 因此有了值）。"""
    data = {
        "csrf": logged.csrf, "kind": "stop.fixed", "name": "",
        "target_type": "symbol", "target_ref": "A/600519", "param_price": "1400",
    }
    data.update(overrides)
    return logged.client.post("/rules/new", data=data), data


def test_a_stop_line_above_the_price_says_the_price_is_already_below_it(logged):
    """填的线在现价上方：那一格要说「现价已经在这条线下面」，不是让人自己心算。"""
    _seed_close(logged, close=1392.05)
    page, _ = _reject_a_post(logged, param_price="1400")
    assert page.status_code == 400
    assert "这条线在现价上方 0.57%" in page.text


def test_a_stop_line_below_the_price_says_how_much_more_it_has_to_fall(logged):
    """填的线在现价下方：说的是还差多少，方向反了整句话就变成「已经破了」。"""
    _seed_close(logged, close=1392.05)
    page, _ = _reject_a_post(logged, param_price="1200")
    assert page.status_code == 400
    assert "这条线在现价下方 13.80%" in page.text



def test_a_percent_parameter_never_gets_a_line_computed_from_the_price(logged):
    """8% 说的是**成本**下浮 8%，不是现价下浮 8%：那一格旁边不许出现按现价算的那条线。

    这一条看着像「什么都没测」，但它拦的是最顺手的一种错：拿现价乘一下印在旁边，
    屏幕上说的是「你的线在这儿」，引擎算的是另一个数——而这两个数只有除权日才会不一致。
    """
    logged.context.ledger.open_account("alice", "测试户")
    _seed_close(logged, close=1392.05)
    page = logged.client.post(
        "/rules/new",
        data={
            "csrf": logged.csrf, "kind": "stop.pct", "name": "",
            "target_type": "position", "target_ref": "*",
            "account_id": logged.context.sim_accounts.list_for(_alice_id(logged))[0].id,
            "param_pct": "8",
        },
    )
    assert page.status_code == 400
    assert 'name="param_pct"' in page.text
    assert "这条线在现价" not in page.text


# --- 保存前试算 --------------------------------------------------------------------------


def _draft_post(logged, **overrides) -> dict:
    data = {
        "csrf": logged.csrf, "kind": "stop.fixed", "name": "草稿",
        "target_type": "symbol", "target_ref": "A/600519", "param_price": "1400",
    }
    data.update(overrides)
    return data


def test_a_preview_judges_the_draft_and_leaves_the_library_untouched(logged):
    """「试算（不保存）」判的就是刚填的那几个数，而库里一行都不许多。

    这一枚是整个按钮的验收线：屏幕说「会命中」而 `rules`/`alerts`/`alert_states` 都没动，
    才叫试算。断言按读库来，不按「页面没说已保存」——那一句话是渲染出来的，写没写它都有。
    """
    _seed_close(logged, close=1100.0)
    uid = _alice_id(logged)
    before = logged.context.rule_service.list(uid)
    open_before = logged.context.alerts_store.count_open(uid)
    page = logged.client.post("/rules/preview", data=_draft_post(logged, param_price="1250"))
    assert page.status_code == 200
    assert 'name="param_price"' in page.text, "表单要在同一屏，改一个数再试一次"
    assert "这次试算" in page.text
    assert "试算：真跑会写一条告警" in page.text, "命中那一行要说的是真跑会怎样"
    assert "1100.00 CNY / 1250.00 CNY" in page.text
    assert "数据 2026-10-30" in page.text, "现值属于哪天，试算这一屏也说得出"
    assert 'href="/alerts/' not in page.text
    assert logged.context.rule_service.list(uid) == before
    assert logged.context.alerts_store.count_open(uid) == open_before
    # 草稿的 id 是 `DRAFT_ID` 那个哨兵：状态表里没有这一行 = 这一趟没写过任何东西
    assert logged.context.alert_states.get(uid, "draft", "A", "600519", None) is None


def test_the_preview_cannot_be_told_to_write(logged, monkeypatch):
    """只读写在引擎里，不是一个能被网页忘传的开关：网页交不出 `dry_run`，也交不出 `is_test`。

    演练告警（`is_test`）会真落库，它绝不能是「试算」的副作用；把这两样从参数表上拿掉，
    网页这一路就没有「顺手传了个 True」的可能。

    桩打在类上：`contexts.current()` 每次可能给出重建过的那一份，打在实例上等于没打。
    """
    from sams.services.rules.engine import RuleEngine

    real = RuleEngine.preview
    seen: dict = {}

    def spy(self, user_id, rule, **kwargs):
        seen["user_id"] = user_id
        seen["rule"] = rule
        seen["kwargs"] = kwargs
        return real(self, user_id, rule, **kwargs)

    monkeypatch.setattr(RuleEngine, "preview", spy)
    _seed_close(logged, close=1100.0)
    page = logged.client.post("/rules/preview", data=_draft_post(logged, param_price="1250"))
    assert page.status_code == 200
    assert seen["kwargs"] == {}, "只读不是参数：那一开关不该由调用方负责"
    assert seen["rule"].id == "draft", "判的是草稿，不是某条已经落库的规则"
    assert seen["rule"].params == {"price": 1250.0}, "判的是刚敲的那一个数"


def test_a_preview_without_the_required_parameter_leaves_the_form_with_the_answer(
    logged, monkeypatch
):
    """少填必填参数：400、那句话在屏上、原文回填，而引擎一次都没被调用。

    试算不是保存路径的旁路：判据与保存同一套（`RuleRepo.build` 走的就是 `create` 那份
    `_validate`），所以填错了要在这里就说，而不是让人按下保存才看见。
    """
    from sams.services.rules.engine import RuleEngine

    calls: list = []
    monkeypatch.setattr(RuleEngine, "preview", lambda self, *a, **k: calls.append((a, k)))
    _seed_close(logged, close=1100.0)
    page = logged.client.post(
        "/rules/preview", data=_draft_post(logged, param_price="", name="草稿·少填")
    )
    assert page.status_code == 400
    assert "少了必填参数 price" in page.text
    assert 'value="草稿·少填"' in page.text, "名字那一格的原文得回到框里"
    assert calls == []


def test_a_preview_on_someone_elses_account_is_refused_like_a_save(logged, monkeypatch):
    """别人的账户：试算拒得和保存一样（`draft()` 走的是 `add` 那四道归属检查），一条规则都不留。

    少了这一道，屏幕上会出现「这条规则会响」而它根本存不下去——那种试算比没有试算更糟。
    """
    from sams.services.rules.engine import RuleEngine

    calls: list = []
    monkeypatch.setattr(RuleEngine, "preview", lambda self, *a, **k: calls.append((a, k)))
    _seed_close(logged, close=1100.0)
    uid = _alice_id(logged)
    before = logged.context.rule_service.list(uid)
    page = logged.client.post(
        "/rules/preview",
        data={
            "csrf": logged.csrf, "kind": "stop.pct", "name": "草稿·别人的户",
            "target_type": "position", "target_ref": "*", "account_id": "acct-not-mine",
            "param_pct": "8",
        },
    )
    assert page.status_code == 400
    assert "acct-not-mine" in page.text
    assert logged.context.rule_service.list(uid) == before
    assert calls == []


def test_the_edit_preview_judges_the_line_just_typed_not_the_one_stored(logged):
    """编辑页那颗试算判的是**刚改的**那条线；留空时回到库里那一份。

    库里的线不动（不落库），所以「留空」在试算这里必须说「按库里那份判」——它与保存那一路
    的「留空 = 不改这一项」是同一句话（`rule_edit_page` 那段注释）。
    """
    _seed_close(logged, close=1100.0)
    view = logged.context.rule_service.add(
        _alice_id(logged), kind="stop.fixed", name="已有那条", target_type="symbol",
        target_ref="A/600519", params={"price": 1250.0},
    )
    rule_id = view.rule.id
    page = logged.client.post(
        f"/rules/{rule_id}/edit/preview",
        data={"csrf": logged.csrf, "name": "已有那条", "param_price": "1400"},
    )
    assert page.status_code == 200
    assert "1100.00 CNY / 1400.00 CNY" in page.text
    assert "1250.00 CNY" not in page.text, "判的不能是库里那一份旧线"
    blank = logged.client.post(
        f"/rules/{rule_id}/edit/preview",
        data={"csrf": logged.csrf, "name": "已有那条", "param_price": ""},
    )
    assert blank.status_code == 200
    assert "1100.00 CNY / 1250.00 CNY" in blank.text
    assert logged.context.rule_service.show(_alice_id(logged), rule_id).rule.params == {
        "price": 1250.0
    }, "试算不该把刚敲的那个数写回库里"


def test_both_preview_endpoints_need_a_csrf_token(logged):
    """与 check / remove / demo 同一批：无 token 一律 403，不落一条规则也不判一趟。"""
    _seed_close(logged, close=1100.0)
    uid = _alice_id(logged)
    view = logged.context.rule_service.add(
        uid, kind="stop.fixed", name="已有那条", target_type="symbol",
        target_ref="A/600519", params={"price": 1250.0},
    )
    before = logged.context.rule_service.list(uid)
    data = _draft_post(logged, param_price="1250")
    data.pop("csrf")
    assert logged.client.post("/rules/preview", data=data).status_code == 403
    assert logged.client.post(
        f"/rules/{view.rule.id}/edit/preview", data={"name": "已有那条"}
    ).status_code == 403
    assert logged.context.rule_service.list(uid) == before


def test_a_preview_with_a_non_number_cooldown_stays_on_the_form(logged):
    """冷却那一格敲了汉字：400 回到表单、原文还在，不是一屏 500。

    这一条测的是兜底的位置：`_cooldown` 折的是 `ValidationError`，落在 try 外面就越过
    `except SamsError`，而保存那一路（`rule_create` / `rule_edit`）都在里面。
    """
    _seed_close(logged, close=1100.0)
    page = logged.client.post(
        "/rules/preview", data=_draft_post(logged, param_price="1250", cooldown_min="一千")
    )
    assert page.status_code == 400
    assert "冷却（分钟）" in page.text
    assert 'name="param_price"' in page.text


def test_the_form_branch_offers_a_way_back_to_the_list(logged):
    """填到一半想走得有个出口：这一屏从个股页进来时，浏览器后退会退回选股结果。"""
    _seed_close(logged, close=1100.0)
    page = _new(logged)
    assert page.status_code == 200
    assert 'href="/rules"' in page.text
    assert "返回规则列表" in page.text
    assert "试算（不保存）" in page.text

