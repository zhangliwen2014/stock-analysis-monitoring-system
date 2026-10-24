"""论点列表、详情与推进。推进的合法档位来自 next_steps，页面不自己发明。"""

from __future__ import annotations

from sams.core.errors import DatabaseLocked
from sams.storage.repos.theses import ThesisRepo


def _alice_id(logged) -> str:
    return logged.context.users.get_by_username("alice").id


def _thesis_id(logged, code: str = "600519") -> str:
    """直接从服务立一条论：绕开页面，好让页面的测试只测页面自己那一段。"""
    report = logged.context.theses.add("alice", "A", code, note="高端酒")
    return report.thesis.id


def _create(logged, **fields):
    data = {"csrf": logged.csrf, "market": "A", "code": "600036", "style": "value", "note": ""}
    data.update(fields)
    return logged.client.post("/theses/create", data=data, follow_redirects=False)


def test_theses_list_and_create(logged):
    ctx = logged.context
    before = {thesis.id for thesis in ctx.theses.list("alice")}
    assert logged.client.get("/theses").status_code == 200

    response = _create(logged, note="低估值银行")
    assert response.status_code == 303
    # 303 只说明「收了」：库里真多出一条、且 redirect 指的就是那一条，才是「立对了」
    created = [thesis for thesis in ctx.theses.list("alice") if thesis.id not in before]
    assert [thesis.code for thesis in created] == ["600036"]
    assert response.headers["location"] == f"/theses/{created[0].id}"

    detail = logged.client.get(response.headers["location"])
    assert detail.status_code == 200
    # 立论那句话落在开局那条事件的 note 上（`Thesis` 这条 dataclass 没有 note 字段）
    assert "低估值银行" in detail.text
    # 列表那一行指的是刚建的这一条：链接与标的写法都在同一张 <a> 上
    assert f'/theses/{created[0].id}">A/600036' in logged.client.get("/theses").text


def test_thesis_detail_links_the_symbol_and_shows_the_opening_note(logged):
    thesis_id = _thesis_id(logged)
    detail = logged.client.get(f"/theses/{thesis_id}")
    assert detail.status_code == 200
    assert "高端酒" in detail.text
    # 详情要能走到那只票的现场：这一串只在链接真渲染出来时才出现在页面上
    assert "/symbol/A/600519" in detail.text


def test_detail_shows_the_stored_levels_and_says_which_day_they_are(logged):
    """库里那几列价位要摆出来，并说清它们属于哪一天（措辞抄服务，不另造第三句）。"""
    thesis_id = _thesis_id(logged)
    logged.context.theses.revise(
        "alice", thesis_id, stop_price=1200.0, target_price=3000.0, reward_risk=2.5
    )
    page = logged.client.get(f"/theses/{thesis_id}")
    assert page.status_code == 200
    assert "1,200.00" in page.text
    assert "3,000.00" in page.text
    # 手填的数没有读数日：把它说成「截至 某一天」等于屏幕替一个没有式子的数编一个出处
    assert "没有测量日（手填）" in page.text


def test_advance_moves_the_status(logged):
    thesis_id = _thesis_id(logged)
    ctx = logged.context
    alice_id = _alice_id(logged)
    thesis = ctx.theses_store.require(alice_id, thesis_id)
    steps = ctx.theses.next_steps(thesis.status)
    # 推进之前详情页的下拉里还没有 `opened`：留给下面做对照
    before = logged.client.get(f"/theses/{thesis_id}").text
    response = logged.client.post(
        f"/theses/{thesis_id}/advance",
        data={"csrf": logged.csrf, "to": steps[0], "note": "进入下一步"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    moved = ctx.theses_store.require(alice_id, thesis_id)
    assert moved.status == steps[0]
    # 时间线上留的是那句 note，不是一条空记录
    notes = [event.note for event in ctx.theses_store.events(alice_id, thesis_id)]
    assert "进入下一步" in notes
    # 列表那一行跟着状态走：那一格除了这一条论没有别处会写出这个词
    assert f">{steps[0]}" in logged.client.get("/theses").text
    # 推进之后那个下拉换了一张表：`opened` 只接在 `proposed` 之后，是推进后才出现的那一步
    after = logged.client.get(f"/theses/{thesis_id}").text
    assert '<option value="opened"' in after
    assert '<option value="opened"' not in before


def test_advance_to_an_illegal_status_re_renders(logged):
    """新论停在 observing，合法下一步只有 proposed 与 invalidated：归档那一步走不了。"""
    thesis_id = _thesis_id(logged)
    ctx = logged.context
    response = logged.client.post(
        f"/theses/{thesis_id}/advance",
        data={"csrf": logged.csrf, "to": "archived", "note": ""},
    )
    assert response.status_code == 400
    # 那句拒绝点名合法的下一步，名单出自 next_steps（分隔符与服务层同一用「、」）
    assert "合法的下一步是 proposed、invalidated" in response.text
    # 拒绝不能留下半个变更
    assert ctx.theses_store.require(_alice_id(logged), thesis_id).status == "observing"


def test_create_with_an_unknown_style_lists_the_real_names(logged):
    """下拉里的值必须是服务收得下的名字：`growth` 是拍出来的，收下它就是一句修不好的拒绝。"""
    response = _create(logged, style="growth")
    assert response.status_code == 400
    # 拒绝里点名了能用的模板名：只断这些名字本身，服务层改措辞不该弄坏 web 测试
    assert "value" in response.text
    assert "trend" in response.text
    # 被拒的名字本身要回显出来：这句只可能来自那句拒绝（表单里没有别处会写出 growth）
    assert "growth" in response.text
    # 被拒的这次什么都没写进库
    assert [t.code for t in logged.context.theses.list("alice")] == []


def test_create_with_an_unconfigured_market_says_which_ones_exist(logged):
    response = _create(logged, market="JP", code="7203")
    assert response.status_code == 400
    # `market_cfg` 的拒绝会点名已配置的市场：断市场代码本身，不断那句话的措辞
    assert "未知市场" in response.text
    assert "HK" in response.text
    assert "US" in response.text
    # 被拒的市场要回显：JP 不在下拉里，页面上它只可能来自那句拒绝
    assert "JP" in response.text
    assert [t.code for t in logged.context.theses.list("alice")] == []


def test_detail_says_the_plan_is_still_empty_rather_than_hand_filled(logged):
    """这一套替身库里当场那次估值给不出价位：那一屏只能说「库里还没有价位」。

    说成「没有测量日（手填）」是给用户安一个他没做过的动作（`stored_axis` 的那句话）。
    """
    thesis_id = _thesis_id(logged)
    page = logged.client.get(f"/theses/{thesis_id}")
    assert "库里还没有价位" in page.text
    assert "手填" not in page.text


def test_create_leaves_the_normalisation_to_the_service(logged):
    """页面只 strip、不归一：`a` 与 `sh600036` 这些形态服务层自己会折成库里那个唯一键。

    这条断言钉的是「别在页面再排一遍」——排两遍就会有两套规则，而模板里那个市场输入框
    交得出任何大小写与前后缀。
    """
    response = _create(logged, market="a", code="sh600036")
    assert response.status_code == 303
    assert [(t.market, t.code) for t in logged.context.theses.list("alice")] == [("A", "600036")]


def test_a_finished_thesis_offers_no_step(logged):
    """走完的论（archived）没有下一步：不该给一个空 <select> 加一个必然被拒的提交按钮。"""
    thesis_id = _thesis_id(logged)
    ctx = logged.context
    ctx.theses.revise(
        "alice", thesis_id, stop_price=1200.0, target_price=3000.0, reward_risk=2.5
    )
    for to in ("proposed", "opened", "tracking", "hit_target", "archived"):
        ctx.theses.advance("alice", thesis_id, to)
    page = logged.client.get(f"/theses/{thesis_id}")
    assert page.status_code == 200
    assert "没有下一步" in page.text
    assert '<select name="to">' not in page.text


def test_database_locked_is_503_not_404(logged, monkeypatch):
    """库被调度线程握着时答 404 是一句自信的错误话：它会让人以为这条论没了。

    真相是「过一会儿再来」，所以 `DatabaseLocked` 必须冒到全局处理器换成 503，
    而不是被 `_require_own` 兜 `StorageError` 的那句吞成「不存在或属于别人」。
    """

    def locked(self, user_id, thesis_id):
        raise DatabaseLocked("sams.duckdb", detail="Conflicting lock (PID 42)")

    monkeypatch.setattr(ThesisRepo, "require", locked)
    assert logged.client.get("/theses/any-id").status_code == 503


def test_another_users_thesis_is_404(logged):
    logged.context.users.create("bob", "another-correct-horse")
    report = logged.context.theses.add("bob", "A", "600036", note="bobs")
    response = logged.client.get(f"/theses/{report.thesis.id}")
    assert response.status_code == 404
    # 列表页也不能露出别人的 id：露了 id 就等于给了取内容的钥匙
    assert report.thesis.id not in logged.client.get("/theses").text
