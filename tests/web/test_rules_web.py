"""规则页。表单的形状来自 `core.rules.TEMPLATES`：加一种参数模板不用改页面。

参数怎么校验不在这一层测：`core/rules.py` 那套（默认值、上下界、野键）自有它的测试。
这里钉的是表单与仓库之间那几件只有页面会弄错的事——填了什么、回填了什么、
哪一种答案该用哪一个状态码。
"""

from __future__ import annotations

import pytest

from sams.core.errors import DatabaseLocked
from sams.storage.repos.rules import RuleRepo


def _alice_id(logged) -> str:
    return logged.context.users.get_by_username("alice").id


def _open_account(logged, name="测试户") -> str:
    """持仓类规则必须挂账户（`_validate` 那道门），去重键里也有它。"""
    return logged.context.ledger.open_account("alice", name).id


def _add(logged, **overrides) -> str:
    """从服务层直接建一条：绕开页面，好让页面的测试只测页面自己那一段。"""
    kwargs = dict(
        kind="stop.fixed", name="止损", target_type="symbol",
        target_ref="A/600519", params={"price": 1400.0},
    )
    kwargs.update(overrides)
    return logged.context.rule_service.add(_alice_id(logged), **kwargs).rule.id


def _new_rule_data(logged, **overrides) -> dict:
    data = {
        "csrf": logged.csrf, "kind": "stop.fixed", "name": "茅台止损",
        "target_type": "symbol", "target_ref": "A/600519",
        "severity": "strong", "cooldown_min": "30",
        "param_price": "1400",
        "filter_no_open_alert": "on",
    }
    data.update(overrides)
    return data


def _edit_rule_data(logged, **overrides) -> dict:
    data = {"csrf": logged.csrf, "name": "茅台止损", "severity": "strong", "cooldown_min": "30"}
    data.update(overrides)
    return data


def test_the_list_shows_how_many_targets_a_rule_reaches(logged):
    """列表走的是 `rule_service.list` 而不是仓库那份：覆盖几个对象与「为什么没动静」
    只有服务层展开过才知道。一条覆盖 0 个对象的规则是静默失败，屏幕上必须说得出原因。"""
    rule_id = _add(
        logged, kind="price.cross_below", name="整个清单",
        target_type="watchlist", target_ref="*", params={"price": 10.0},
    )
    page = logged.client.get("/rules")
    assert page.status_code == 200
    assert "整个清单" in page.text
    assert f"/rules/{rule_id}/edit" in page.text
    assert "自选股清单是空的" in page.text


def test_rules_new_renders_the_contract(logged):
    page = logged.client.get("/rules/new?kind=stop.fixed&target_type=symbol&target_ref=A/600519")
    assert page.status_code == 200
    assert "止损价" in page.text
    # URL 上带来的对象要落进输入框，否则用户得把刚点开的那只票再敲一遍
    assert 'value="A/600519"' in page.text


def test_a_different_kind_renders_its_own_contract(logged):
    """表单不是照着 `stop.fixed` 那一个例子写死的：换一个 kind 就换一套参数。"""
    page = logged.client.get("/rules/new?kind=tp.tiered")
    assert page.status_code == 200
    assert "分档" in page.text
    assert "止损价" not in page.text
    # tiers 那一shape的输入框拿默认值当写法示范：解析器认的就是这个逗号串
    assert 'placeholder="0.5,1.0"' in page.text


def test_the_target_picker_cannot_offer_an_unwritable_type(logged):
    """`portfolio` 在写入时必被拒（组合级这一层还没有输入，U28），下拉里给一个存不上的
    选项就是坏掉的控件。`price.cross_below` 的模板没收窄 targets，五种都在名单上，测它。"""
    page = logged.client.get("/rules/new?kind=price.cross_below")
    assert page.status_code == 200
    assert 'value="portfolio"' not in page.text
    assert 'value="watchlist"' in page.text


def test_create_bumps_the_rules_version_and_redirects(logged):
    before = logged.core.versions.current("rules")
    response = logged.client.post(
        "/rules/new", data=_new_rule_data(logged), follow_redirects=False
    )
    assert response.status_code == 303
    assert logged.core.versions.current("rules") == before + 1
    assert "茅台止损" in logged.client.get("/rules").text
    # 303 只说明「收了」：那些字段真的落进库里那一条，才是「建对了」
    views = logged.context.rule_service.list(_alice_id(logged))
    assert [view.rule.name for view in views] == ["茅台止损"]
    rule = views[0].rule
    assert rule.params["price"] == 1400.0
    assert rule.cooldown_min == 30
    assert rule.severity == "strong"
    # 没勾的三个开关也是答案的一部分：漏一个键仓库会按「未设置」补 False，
    # 所以这里断的是「勾了的那一个真的进去了」
    assert rule.filters["no_open_alert"] is True


def test_missing_required_param_shows_the_error_and_does_not_bump(logged):
    before = logged.core.versions.current("rules")
    response = logged.client.post("/rules/new", data=_new_rule_data(logged, param_price=""))
    assert response.status_code == 400
    assert "止损价" in response.text
    assert logged.core.versions.current("rules") == before
    assert logged.context.rule_service.list(_alice_id(logged)) == ()


def test_a_bad_number_shows_the_error(logged):
    response = logged.client.post("/rules/new", data=_new_rule_data(logged, param_price="一千四"))
    assert response.status_code == 400
    # 那句拒绝出自 `validate_params`：页面不另写一套措辞，CLI 与页面念的才是同一句
    assert "数字" in response.text
    assert "一千四" in response.text


def test_a_blank_cooldown_and_severity_fall_back_to_the_configured_ones(logged):
    """空冷却不是 0：0 是「没有冷却」，那条规则命中一次就响一次。
    空级别也不是空串：那是「用模板自己那一档」（`stop.fixed` 的 strong 才走得到语音）。"""
    response = logged.client.post(
        "/rules/new",
        data=_new_rule_data(logged, cooldown_min="", severity=""),
        follow_redirects=False,
    )
    assert response.status_code == 303
    rule = logged.context.rule_service.list(_alice_id(logged))[0].rule
    assert rule.cooldown_min == logged.context.settings.cooldown_min
    assert rule.cooldown_min != 0
    assert rule.severity == "strong"


def test_a_non_numeric_cooldown_is_refused_by_name(logged):
    """`int("一千")` 抛的是 `ValueError`，不是 `SamsError`：不自己折一下就是一屏 500。"""
    response = logged.client.post(
        "/rules/new", data=_new_rule_data(logged, cooldown_min="一千")
    )
    assert response.status_code == 400
    assert "冷却" in response.text
    assert logged.context.rule_service.list(_alice_id(logged)) == ()


def test_edit_keeps_the_params_the_form_did_not_touch(logged):
    """service 的 `update` 是整份替换（`patch.get("params", before.params)`），缺的键由
    模板**默认值**补上而不是旧值：编辑必须自己从库里那一份起头盖。"""
    account_id = _open_account(logged)
    rule_id = _add(
        logged, kind="stop.time", name="拿住太久", target_type="position", target_ref="*",
        account_id=account_id, params={"days": 30.0, "max_gain_pct": 0.15},
    )
    response = logged.client.post(
        f"/rules/{rule_id}/edit", data=_edit_rule_data(logged, name="拿住太久改"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    # max_gain_pct 的模板默认是 0.0：留 0.15 才证明这一趟盖的是旧值而不是默认值
    assert shown.rule.params == {"days": 30.0, "max_gain_pct": 0.15}
    assert shown.rule.name == "拿住太久改"


def test_edit_moves_the_line_and_bumps(logged):
    rule_id = _add(logged, name="旧止损", params={"price": 1500.0})
    before = logged.core.versions.current("rules")
    response = logged.client.post(
        f"/rules/{rule_id}/edit",
        data=_edit_rule_data(logged, name="新止损", param_price="1380"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert logged.core.versions.current("rules") == before + 1
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.params["price"] == 1380.0
    assert shown.rule.name == "新止损"


def test_the_writes_are_behind_the_csrf_pair(logged):
    """三个写操作都还带着 `csrf_protect`：表单原文那份依赖与它是同一次 body 解析，
    谁也没把谁挤掉（挤掉了就是三条能被人拿页面替别人改规则的口子）。"""
    rule_id = _add(logged)
    assert logged.client.post("/rules/new", data={"kind": "stop.fixed"}).status_code == 403
    assert logged.client.post(f"/rules/{rule_id}/edit", data={"name": "x"}).status_code == 403
    assert logged.client.post(f"/rules/{rule_id}/toggle", data={"csrf": ""}).status_code == 403
    assert logged.context.rule_service.show(_alice_id(logged), rule_id).rule.name == "止损"


def test_the_edit_page_echoes_what_is_stored(logged):
    """回填的是库里那一份：过滤开关的勾与 tiers 那个逗号串。
    `str([0.5, 1.0])` 是 `[0.5, 1.0]`，交回 `_as_tiers` 会被拒——表单不能把自己写的东西读不懂。"""
    account_id = _open_account(logged)
    rule_id = _add(
        logged, kind="tp.tiered", name="分批止盈", target_type="position", target_ref="*",
        account_id=account_id, params={"tiers": [0.5, 1.0]}, filters={"time_window": True},
    )
    page = logged.client.get(f"/rules/{rule_id}/edit")
    assert page.status_code == 200
    assert 'value="0.5,1.0"' in page.text
    assert 'name="filter_time_window" checked' in page.text
    assert 'name="filter_data_freshness" checked' not in page.text
    # 照着这一屏交一次（只改名字）：那个开着的开关必须还开着——回填没勾，保存就把它关了
    logged.client.post(
        f"/rules/{rule_id}/edit",
        data=_edit_rule_data(logged, filter_time_window="on", param_tiers="0.5,1.0"),
        follow_redirects=False,
    )
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.filters["time_window"] is True
    assert shown.rule.params["tiers"] == [0.5, 1.0]


def test_edit_with_a_bad_number_rerenders_without_writing(logged):
    rule_id = _add(logged, params={"price": 1500.0})
    before = logged.core.versions.current("rules")
    response = logged.client.post(
        f"/rules/{rule_id}/edit", data=_edit_rule_data(logged, param_price="一千四")
    )
    assert response.status_code == 400
    assert "数字" in response.text
    assert logged.core.versions.current("rules") == before
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.params["price"] == 1500.0


def test_toggle_flips_enabled_both_ways(logged):
    rule_id = _add(logged)
    response = logged.client.post(
        f"/rules/{rule_id}/toggle", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert logged.context.rule_service.show(_alice_id(logged), rule_id).rule.enabled is False
    # 再点一次装回去：第二次真的生效才证明这一条走的是「翻转」而不是「一律停用」
    logged.client.post(f"/rules/{rule_id}/toggle", data={"csrf": logged.csrf})
    assert logged.context.rule_service.show(_alice_id(logged), rule_id).rule.enabled is True


def test_another_users_rule_is_404(logged):
    """服务层那句 `_require` 不区分「没有」与「不是你的」，页面也不许替它区分。"""
    bob = logged.context.users.create("bob", "another-correct-horse")
    rule_id = logged.context.rule_service.add(
        bob.id, kind="stop.fixed", name="bobs", target_type="symbol",
        target_ref="A/600036", params={"price": 30.0},
    ).rule.id
    assert logged.client.get(f"/rules/{rule_id}/edit").status_code == 404
    assert logged.client.post(
        f"/rules/{rule_id}/edit", data=_edit_rule_data(logged)
    ).status_code == 404
    assert logged.client.post(
        f"/rules/{rule_id}/toggle", data={"csrf": logged.csrf}
    ).status_code == 404
    # 列表也不能露出别人的 id 与名字
    page = logged.client.get("/rules")
    assert rule_id not in page.text
    assert "bobs" not in page.text


def test_an_unknown_kind_puts_the_list_back_on_the_page(logged):
    """拿着表单的人该看到的是有哪些 kind，而不是一屏拒绝：名单就是 `template_for` 那句话里的。"""
    page = logged.client.get("/rules/new?kind=stop.nope")
    assert page.status_code == 400
    assert "未知规则类型" in page.text
    # 那一串链接只在选 kind 那一屏渲染出来：错误页上不会有
    assert 'href="/rules/new?kind=' in page.text


@pytest.mark.parametrize(
    "call",
    [
        lambda client, csrf: client.get("/rules/whatever/edit"),
        lambda client, csrf: client.post("/rules/whatever/edit", data={"csrf": csrf, "name": "x"}),
        lambda client, csrf: client.post("/rules/whatever/toggle", data={"csrf": csrf}),
    ],
    ids=["edit-get", "edit-post", "toggle"],
)
def test_database_locked_is_503_on_the_read_of_a_rule(logged, monkeypatch, call):
    """`DatabaseLocked ⊂ StorageError ⊂ SamsError`：兜底把它答成 404「规则不存在」或
    400「你填错了」都是一句自信的错误话——真相是「过一会儿再来」（全局 handler 的 503）。"""

    def locked(self, *args, **kwargs):
        raise DatabaseLocked("sams.duckdb", detail="Conflicting lock (PID 42)")

    monkeypatch.setattr(RuleRepo, "get", locked)
    assert call(logged.client, logged.csrf).status_code == 503


def test_database_locked_is_503_on_create(logged, monkeypatch):
    """新建那一路的兜底是 `except SamsError` → 400「你填错了」：锁库时被拒的那次
    什么都没写，而页面会指着表单说是用户填的。"""

    def locked(self, *args, **kwargs):
        raise DatabaseLocked("sams.duckdb", detail="Conflicting lock (PID 42)")

    monkeypatch.setattr(RuleRepo, "create", locked)
    assert logged.client.post("/rules/new", data=_new_rule_data(logged)).status_code == 503
