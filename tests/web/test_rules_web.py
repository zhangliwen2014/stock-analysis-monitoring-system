"""规则页。表单的形状来自 `core.rules.TEMPLATES`：加一种参数模板不用改页面。

参数怎么校验不在这一层测：`core/rules.py` 那套（默认值、上下界、野键）自有它的测试。
这里钉的是表单与仓库之间那几件只有页面会弄错的事——填了什么、回填了什么、
哪一种答案该用哪一个状态码。
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from sams.core.errors import DatabaseLocked
from sams.storage.repos.rules import RuleRepo


def _alice_id(logged) -> str:
    return logged.context.users.get_by_username("alice").id


def _open_account(logged, name="测试户") -> str:
    """持仓类规则必须挂账户（`_validate` 那道门），去重键里也有它。"""
    return logged.context.ledger.open_account("alice", name).id


def _desktop(logged, name="桌面"):
    """一条本机桌面通道：`desktop` 没有要配的字段，直接进仓库。"""
    return logged.context.channels_store.create(_alice_id(logged), "desktop", name, {})


def _stamped_on(logged, rule_id: str, on: date) -> None:
    """把「那几个数是哪天填的」挪到过去：今天的时钟再盖一次，屏幕上看得见。"""
    logged.context.db.execute("UPDATE rules SET params_on = ? WHERE id = ?", [on, rule_id])


def _checked_filters(page) -> dict[str, str]:
    """这一屏上勾着的开关，读成表单数据。

    测试自己手写一遍开关，验的只是「我写了什么」；照着渲染出来的东西交一次，
    页面没回填的那一格才会露出来。
    """
    return {name: "on" for name in re.findall(r'name="(filter_[a-z_]+)" checked', page.text)}


def _echoed(page, field: str) -> str:
    """这一屏某个输入框里的那个值（同上：读页面，不读测试自己写的常量）。"""
    found = re.search(rf'name="{field}"[^>]*value="([^"]*)"', page.text)
    assert found is not None, page.text
    return found.group(1)


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
    """只改名字：库里那份参数得原样留着，一个键都不被默认值顶掉（合并那一段写在端点里）。"""
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


def test_a_name_only_edit_leaves_the_param_date_alone(logged):
    """`params_on` 是「那几个数哪天填的」，除权换算按它决定止损线搬不搬。
    改个名字不该把它说成今天——那等于给这条线免掉一次该做的搬家。"""
    rule_id = _add(logged, params={"price": 1500.0})
    typed_on = date(2026, 3, 1)
    _stamped_on(logged, rule_id, typed_on)
    response = logged.client.post(
        f"/rules/{rule_id}/edit", data=_edit_rule_data(logged, name="换个名"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.params_on == typed_on
    # 没填的那一格也不是 0：留空是不改，不是把冷却抹掉
    assert shown.rule.cooldown_min == 30


def test_a_price_retyped_is_todays_number(logged):
    """反过来也得诚实：人把同一个数再敲一遍，说的就是「今天这里是这个数」（仓库那句注释）。
    判据是这一趟有没有把 `params` 交出去，不是合并后的那一份等不等。"""
    rule_id = _add(logged, params={"price": 1500.0})
    _stamped_on(logged, rule_id, date(2026, 3, 1))
    response = logged.client.post(
        f"/rules/{rule_id}/edit", data=_edit_rule_data(logged, param_price="1500"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.params["price"] == 1500.0
    assert shown.rule.params_on == logged.context.clock.now_utc().date()


def test_renaming_via_the_form_does_not_restamp_the_params(logged):
    """真正的用户路径：打开编辑页、改个名字、保存。浏览器会把每个参数框都提交一次——
    框里若预填着库里那个数，`touched` 就永远不为空、`params` 永远进 patch、`params_on`
    永远盖成今天（仓库的判据是「交没交」，`repos/rules.py`），止损线于是在除权日
    不再搬。所以参数框空着起步：留空 = 不动，与级别、冷却那两格同一份合同。"""
    rule_id = _add(logged, params={"price": 1500.0})
    typed_on = date(2026, 3, 1)
    _stamped_on(logged, rule_id, typed_on)
    page = logged.client.get(f"/rules/{rule_id}/edit")
    # 页面确实空着起步：预填哪天回来，这一条先红在这里
    assert _echoed(page, "param_price") == ""
    response = logged.client.post(
        f"/rules/{rule_id}/edit",
        data=_edit_rule_data(logged, name="只改个名", param_price=""),
        follow_redirects=False,
    )
    assert response.status_code == 303
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.params["price"] == 1500.0
    assert shown.rule.params_on == typed_on
    # 反向也要真：敲进去一个数（哪怕是同一个）才该盖成今天。名字这趟也得改一下：
    # 整份都没动时仓库的空改动闸先退回这一趟（`repos/rules.py`），那不是一次写
    response = logged.client.post(
        f"/rules/{rule_id}/edit",
        data=_edit_rule_data(logged, name="敲了个数", param_price="1500"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.params_on == logged.context.clock.now_utc().date()


def test_the_edit_page_pre_ticks_the_stored_channels(logged):
    """这一屏必须勾着库里那几条：不回填的框与压根没有那一格是两件事，
    而照着这一屏交一次的人不知道自己顺手清掉了什么。"""
    node = _desktop(logged)
    rule_id = _add(logged, channel_ids=[node.id])
    page = logged.client.get(f"/rules/{rule_id}/edit")
    assert page.status_code == 200
    assert f'name="channel_ids" value="{node.id}" checked' in page.text


def test_an_edit_that_never_saw_the_channel_boxes_keeps_them(logged):
    """这一屏压根没渲染那一格（表单里那个 `channels_offered` 不在），空名单就不是用户的答案：
    改个名字不该把一条已经指名发到某通道的规则挪回默认路由。"""
    node = _desktop(logged)
    rule_id = _add(logged, channel_ids=[node.id])
    response = logged.client.post(
        f"/rules/{rule_id}/edit", data=_edit_rule_data(logged, name="茅台止损改"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.channel_ids == [node.id]


def test_the_channel_boxes_survive_a_rejected_edit(logged):
    """被拒后重新渲染时勾着的还是用户刚勾的那几条：清掉它们等于替他把路由改了，
    而他这一趟压根只是想改个价。"""
    node = _desktop(logged)
    rule_id = _add(logged, channel_ids=[node.id])
    response = logged.client.post(
        f"/rules/{rule_id}/edit",
        data=_edit_rule_data(
            logged, channels_offered="1", channel_ids=node.id, param_price="一千四"
        ),
    )
    assert response.status_code == 400
    assert f'name="channel_ids" value="{node.id}" checked' in response.text


def test_unticking_every_channel_offered_is_a_real_answer(logged):
    """那一格渲染出来了而用户全不勾：那是「改回默认路由」，这一趟就得真的清掉。"""
    node = _desktop(logged)
    rule_id = _add(logged, channel_ids=[node.id])
    response = logged.client.post(
        f"/rules/{rule_id}/edit", data=_edit_rule_data(logged, channels_offered="1"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert logged.context.rule_service.show(_alice_id(logged), rule_id).rule.channel_ids == []


def test_a_disabled_channel_stays_visible_and_marked(logged):
    """已经存着的通道停用后仍要出现在这一屏（不显示就等于顺手把它清掉），
    但它勾不上——`_require_channels` 永远拒它，所以这一格得自己把话说明白。"""
    _desktop(logged, "兜底那条")
    muted = _desktop(logged, "静音那条")
    rule_id = _add(logged, channel_ids=[muted.id])
    logged.context.channels_store.set_enabled(muted.id, False)
    page = logged.client.get(f"/rules/{rule_id}/edit")
    assert page.status_code == 200
    assert "（已停用）" in page.text
    assert f'name="channel_ids" value="{muted.id}" checked' in page.text


def test_a_thesis_picked_from_the_dropdown_creates_the_rule(logged):
    """`target_type=thesis` 时下拉里那条就是挂载对象：要人另外把 uuid 敲进引用框
    （而填 `A/600519` 那种人话还会被形状闸拒）等于这个下拉只能产出一屏 400。"""
    thesis_id = logged.context.theses.add("alice", "A", "600519", note="高端酒").thesis.id
    response = logged.client.post(
        "/rules/new",
        data=_new_rule_data(
            logged, kind="thesis.drift", name="周复检掉档", target_type="thesis",
            target_ref="", thesis_id=thesis_id, param_price="",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    rule = logged.context.rule_service.list(_alice_id(logged))[0].rule
    assert rule.target_type == "thesis"
    assert rule.target_ref == thesis_id
    assert rule.thesis_id == thesis_id


def test_an_untouched_choice_leaves_the_value_to_the_template(logged):
    """下拉里没有一个「没填」的选项时，浏览器交的是 `choices[0]`：用户没碰过的那一项
    会被说成他显式选过。`basis` 的默认恰好也是 `choices[0]`，所以钉的是存进去的那一份。"""
    page = logged.client.get("/rules/new?kind=stop.ma")
    assert page.status_code == 200
    assert 'name="param_basis"' in page.text
    response = logged.client.post(
        "/rules/new",
        data=_new_rule_data(logged, kind="stop.ma", name="跌破周线", param_price="",
                            param_basis=""),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    rule = logged.context.rule_service.list(_alice_id(logged))[0].rule
    assert rule.params == {"basis": "weekly_ma20"}


def test_blank_means_unchanged_on_edit_and_default_on_create(logged):
    """同一句「留空」在两张屏上说的是两件事：新建时按配置的来，编辑时是不动这一项。
    措辞跟着 `rule_id` 分头走，否则编辑那一屏上这句话会把人骗去清空一个他不想动的数。"""
    rule_id = _add(logged)
    edit = logged.client.get(f"/rules/{rule_id}/edit")
    assert "留空是不改这一项" in edit.text
    assert "留空是按配置的默认值" not in edit.text
    fresh = logged.client.get("/rules/new?kind=stop.fixed")
    assert "留空是按配置的默认值" in fresh.text
    assert "模板默认" in fresh.text
    assert "不改这一项" not in fresh.text


def test_the_form_does_not_read_rosters_the_template_has_no_box_for(logged, monkeypatch):
    """名单按那一格在不在读：`stop.fixed` 这一屏既没有账户框也没有论的下拉，
    那两次读回来没人看（每开一次表单白问两趟库）。"""
    def refused(*args, **kwargs):
        raise AssertionError("这一屏没有那一格，不该读这份名单")

    monkeypatch.setattr(type(logged.context.sim_accounts), "list_for", refused)
    monkeypatch.setattr(type(logged.context.theses), "list", refused)
    assert logged.client.get("/rules/new?kind=stop.fixed").status_code == 200


def test_the_writes_are_behind_the_csrf_pair(logged):
    """三个写操作都还带着 `csrf_protect`：表单原文那份依赖与它是同一次 body 解析，
    谁也没把谁挤掉（挤掉了就是三条能被人拿页面替别人改规则的口子）。"""
    rule_id = _add(logged)
    assert logged.client.post("/rules/new", data={"kind": "stop.fixed"}).status_code == 403
    assert logged.client.post(f"/rules/{rule_id}/edit", data={"name": "x"}).status_code == 403
    assert logged.client.post(f"/rules/{rule_id}/toggle", data={"csrf": ""}).status_code == 403
    assert logged.context.rule_service.show(_alice_id(logged), rule_id).rule.name == "止损"


def test_the_edit_page_echoes_what_is_stored(logged):
    """编辑页上「回填」参数的方式是提示，不是预填：逗号串 `0.5,1.0` 出现在那一格旁边
    （`str([0.5, 1.0])` 带着空格和方括号，交回 `_as_tiers` 会被它自己拒），而输入框空着——
    预填等于替人把每个框都「填了一遍」，每一次保存都会被说成重填了那些数。
    过滤开关照旧勾着原样回填。"""
    account_id = _open_account(logged)
    rule_id = _add(
        logged, kind="tp.tiered", name="分批止盈", target_type="position", target_ref="*",
        account_id=account_id, params={"tiers": [0.5, 1.0]}, filters={"time_window": True},
    )
    page = logged.client.get(f"/rules/{rule_id}/edit")
    assert page.status_code == 200
    assert "0.5,1.0" in page.text
    assert "[0.5, 1.0]" not in page.text
    assert 'value="0.5,1.0"' not in page.text
    assert 'name="filter_time_window" checked' in page.text
    assert 'name="filter_data_freshness" checked' not in page.text
    # 照着这一屏交一次（只改名字，参数框照页面原样空着交）：开着的开关必须还开着，
    # 库里那份 tiers 也还得是那份。表单数据是从渲染里读的，不是测试另写一遍常量：
    # 页面回填错了（勾丢了、框变成预填）assertion 才会因「页面」而不是因「测试自己的写法」失败
    logged.client.post(
        f"/rules/{rule_id}/edit",
        data=_edit_rule_data(
            logged, **_checked_filters(page), param_tiers=_echoed(page, "param_tiers")
        ),
        follow_redirects=False,
    )
    shown = logged.context.rule_service.show(_alice_id(logged), rule_id)
    assert shown.rule.filters["time_window"] is True
    assert shown.rule.params["tiers"] == [0.5, 1.0]


def test_no_param_box_is_prefilled_on_the_edit_page(logged):
    """钉页面形状本身：输入框不带预填的 `value`，下拉不预选库里那个 choice——
    将来谁把预填加回来，这一条当场红，而不是等到除权那天止损线没搬才发现。
    库里那份以提示出现：看得见，不用重敲。"""
    rule_id = _add(logged, kind="stop.ma", name="跌破周线", params={"basis": "daily_ma60"})
    page = logged.client.get(f"/rules/{rule_id}/edit")
    assert page.status_code == 200
    # 存着的那一条不被预选：浏览器交出去的就不是「人没碰过的一格」
    assert 'value="daily_ma60" selected' not in page.text
    # 库里的选择以提示出现：看得见，不用重敲
    assert "当前：daily_ma60" in page.text


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
