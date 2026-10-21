"""维护面：四样仓库层看不见的东西，加一次跨表的原子改写。

仓库全用真的。`rules.update` 与 `theses.set_levels` 之间的回滚是这一枚文件的验收线，
把它 stub 掉就只剩下「两个函数各测了一次」。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import polars as pl
import pytest
import support_valuation

from sams.core.errors import StorageError, ValidationError
from sams.services.rules.derive import RuleDeriver
from sams.services.rules.maintenance import RuleService

CODE = support_valuation.CODE
NAME = "u-maintain"
PWD = "long-enough-to-hash"
T0 = datetime(2024, 10, 26, 15, 0, tzinfo=UTC)
LEVELS = {
    "entry_low": 30.0,
    "entry_high": 36.0,
    "stop_price": 28.0,
    "target_price": 52.0,
    "reward_risk": 2.0,
}


@pytest.fixture
def container(db, settings):
    got = support_valuation.make_container(db, settings)
    # 主档那一行是这份夹具补的，不是 `support_valuation.seed` 漏的：那边只播 K 线、覆盖、
    # 基本面与逐日估值（它服务的是读数与命令行那两条链，用不到 symbols）。而 `_notes` 那条
    # 「库里没有」读的就是它——不补，一条完全正常的规则也会带上一句 note，
    # 而下面那些用例断言的正是「正常 ⇒ 无话可说」。
    got.symbols.upsert(
        pl.DataFrame([{"market": "A", "code": CODE, "name": "中国神华", "currency": "CNY"}])
    )
    return got


@pytest.fixture
def uid(container) -> str:
    return container.users.create(NAME, PWD).id


def make_service(container, settings=None) -> RuleService:
    """照 `bootstrap` 里那一套装配：取数那一坨直接用 `container.rule_data`，
    不在这里手抄第二遍装配（同一个理由见 `test_rules_recheck.py` 的 `build`）。
    """
    return RuleService(
        rules=container.rules_store,
        theses=container.theses_store,
        accounts=container.sim_accounts,
        channels=container.channels_store,
        users=container.users,
        data=container.rule_data,
        db=container.db,
        settings=container.settings if settings is None else settings,
        clock=container.clock,
    )


def add(container, uid, **over):
    args = {
        "kind": "stop.fixed",
        "name": "跌破 10 元",
        "target_type": "symbol",
        "target_ref": f"A/{CODE}",
        "params": {"price": 10.0},
    }
    args.update(over)
    return make_service(container).add(uid, **args)


def derived_stop(container, uid):
    """一条论 → 派生规则 → 把那条 `stop.fixed` 拿出来。"""
    thesis = container.theses_store.create(
        user_id=uid,
        market="A",
        code=CODE,
        style="value",
        actor_id=uid,
        now=T0,
        status="proposed",
        **LEVELS,
    )
    made = RuleDeriver(container.rules_store, container.theses_store).derive(
        uid, thesis.id
    )
    return thesis, next(row for row in made.created if row.kind == "stop.fixed")


# --- 四样补上的检查 -----------------------------------------------------------------------


def test_a_plain_rule_expands_to_one_object_and_says_nothing(container, uid) -> None:
    view = add(container, uid)
    assert view.targets == 1
    assert view.notes == ()
    assert view.rule.enabled is True


def test_an_unknown_user_is_refused_before_the_row_is_written(container) -> None:
    with pytest.raises(ValidationError, match="没有用户"):
        add(container, "no-such-user")
    assert container.rules_store.list_for("no-such-user") == []


def test_a_channel_of_someone_else_is_named_not_dropped(container, uid) -> None:
    """静默丢掉一个写错的通道 id = 用户以为发到 A 群，其实一条没发。"""
    other = container.users.create("someone-else", PWD)
    theirs = container.channels_store.create(other.id, "desktop", "别人的桌面", {})

    with pytest.raises(ValidationError, match="不属于这个用户"):
        add(container, uid, channel_ids=[theirs.id])
    assert container.rules_store.list_for(uid) == []


def test_a_disabled_channel_is_refused_at_write_time(container, uid) -> None:
    # 用户名下的**第一个**通道自动成为默认（`ChannelRepo.create` 里那句 `not _any_for`），
    # 而默认通道不能停用（`set_enabled` 会拒）。所以先建一条占住默认，再建要停用的那一条。
    container.channels_store.create(uid, "pushplus", "兜底的那条", {})
    node = container.channels_store.create(uid, "desktop", "桌面", {})
    container.channels_store.set_enabled(node.id, False)

    with pytest.raises(ValidationError, match="已停用"):
        add(container, uid, channel_ids=[node.id])


def test_a_foreign_account_is_refused(container, uid) -> None:
    other = container.users.create("third-party", PWD)
    theirs = container.sim_accounts.open(other.id, "别人的户")

    with pytest.raises(ValidationError, match="名下没有账户"):
        add(
            container,
            uid,
            kind="stop.pct",
            params={"pct": 0.08},
            target_type="position",
            target_ref="*",
            account_id=theirs.id,
        )


def test_the_default_cooldown_comes_from_settings(container, uid) -> None:
    """两个数一样时这一句断不出「读的是配置」：`RuleRepo.create` 的默认写死 30，
    夹具那份 `settings.cooldown_min` 也是 30。所以这里换成 7——不换成 7，
    把 `add()` 里那次查找删掉、纯靠仓库默认，这个用例照样绿。
    """
    lean = replace(container.settings, cooldown_min=7)
    assert make_service(container, settings=lean).add(
        uid,
        kind="stop.fixed",
        name="跌破 10 元",
        target_type="symbol",
        target_ref=f"A/{CODE}",
        params={"price": 10.0},
    ).rule.cooldown_min == 7
    assert add(container, uid, name="另一条", cooldown_min=5).rule.cooldown_min == 5


# --- notes：那一行为什么没动静 -------------------------------------------------------------


def test_an_empty_watchlist_shows_up_as_a_rule_that_cannot_fire(container, uid) -> None:
    view = add(
        container,
        uid,
        name="清单上每条的止损",
        target_type="watchlist",
        target_ref="*",
    )
    assert view.targets == 0
    assert "自选股清单是空的" in view.notes[0]


def test_a_position_rule_on_a_flat_account_says_so(container, uid) -> None:
    account = container.sim_accounts.open(uid, "我的户")
    view = add(
        container,
        uid,
        kind="stop.pct",
        params={"pct": 0.08},
        target_type="position",
        target_ref="*",
        account_id=account.id,
    )
    assert view.targets == 0
    assert "没有持仓" in view.notes[0]


def test_a_symbol_that_is_not_in_the_library_is_flagged(container, uid) -> None:
    """`symbol` 永远展开成一个对象（形状决定的），所以「库里没这条标的」要单独说。"""
    view = add(container, uid, target_ref="A/600000", name="没同步的那只")
    assert view.targets == 1
    assert any("库里没有" in note for note in view.notes)


def test_a_disabled_rule_says_it_is_not_being_evaluated(container, uid) -> None:
    made = add(container, uid)
    make_service(container).set_enabled(uid, made.rule.id, False)

    shown = make_service(container).show(uid, made.rule.id)
    assert shown.rule.enabled is False
    assert "已停用" in shown.notes[0]
    assert shown.targets == 1


def test_list_wraps_every_rule_with_its_own_explanation(container, uid) -> None:
    add(container, uid)
    add(
        container,
        uid,
        name="清单上每条的止损",
        target_type="watchlist",
        target_ref="*",
    )

    # 不写成列表断言：`list_for` 按 `created_at, id` 排，而这个世界的时钟是冻住的，
    # 同一秒建的两条先后取决于随机 id——屏幕上那行的顺序由 CLI 排，不由这里赌。
    by_name = {row.rule.name: row.targets for row in make_service(container).list(uid)}
    assert by_name == {"跌破 10 元": 1, "清单上每条的止损": 0}


def test_show_names_a_rule_that_is_not_there(container, uid) -> None:
    with pytest.raises(StorageError, match="不存在"):
        make_service(container).show(uid, "no-such-rule")


# --- 编辑：一次改两张表 --------------------------------------------------------------------


def test_editing_a_derived_stop_moves_the_thesis_column(container, uid) -> None:
    thesis, rule = derived_stop(container, uid)
    report = make_service(container).edit(uid, rule.id, params={"price": 30.0})

    assert report.written_back == {"stop_price": 30.0}
    assert report.rule.params["price"] == 30.0
    assert container.theses_store.get(uid, thesis.id).stop_price == 30.0
    # 一次改动一条修订史（§4.2）：回看「这条线是什么时候挪的」要有那一行
    assert len(container.rules_store.revisions(rule.id)) == 1


def test_a_thesis_side_refusal_rolls_the_rule_back(container, uid) -> None:
    """止损改到 60 > 目标 52：两条相反的了结路不能同时成立（D81），而规则也不能留下那一改。"""
    thesis, rule = derived_stop(container, uid)

    with pytest.raises(ValidationError, match="不在目标"):
        make_service(container).edit(uid, rule.id, params={"price": 60.0})

    assert container.rules_store.get(rule.id, user_id=uid).params["price"] == 28.0
    assert container.theses_store.get(uid, thesis.id).stop_price == 28.0
    assert container.rules_store.revisions(rule.id) == []


def test_editing_a_plain_rule_writes_back_nothing(container, uid) -> None:
    """挂在自己心里的价上：改了它不该动任何论点（T-15 那条判据在维护面的另一头）。"""
    made = add(container, uid)
    report = make_service(container).edit(uid, made.rule.id, params={"price": 12.0})

    assert report.written_back == {}
    assert report.thesis_id is None
    assert report.rule.params["price"] == 12.0


def test_an_unusable_channel_is_refused_on_edit_too(container, uid) -> None:
    made = add(container, uid)
    with pytest.raises(ValidationError, match="通道"):
        make_service(container).edit(uid, made.rule.id, channel_ids=["ghost"])


def test_a_bad_severity_is_refused_and_moves_no_column(container, uid) -> None:
    """反方向的半件事：规则那头的校验没过时，论上那一列也不该已经动了。"""
    thesis, rule = derived_stop(container, uid)

    with pytest.raises(ValidationError, match="severity"):
        make_service(container).edit(
            uid, rule.id, params={"price": 30.0}, severity="scream"
        )

    assert container.theses_store.get(uid, thesis.id).stop_price == 28.0


def test_a_non_numeric_price_is_refused_by_the_repo(container, uid) -> None:
    """`float("八")` 必须在仓库层就被 `_as_number` 折成 `ValidationError`：穿过校验的话，
    下一跳是 `write_back` 里的 `float(value)`，那里抛的是 `ValueError`，而事务块只认前者。
    """
    thesis, rule = derived_stop(container, uid)

    with pytest.raises(ValidationError, match="要的是数字"):
        make_service(container).edit(uid, rule.id, params={"price": "八"})

    assert container.theses_store.get(uid, thesis.id).stop_price == 28.0


def test_a_derived_rule_cannot_be_removed_and_says_why(container, uid) -> None:
    _, rule = derived_stop(container, uid)
    with pytest.raises(ValidationError, match="不能删除"):
        make_service(container).remove(uid, rule.id)


def test_removing_a_plain_rule_leaves_it_gone(container, uid) -> None:
    made = add(container, uid)
    assert make_service(container).remove(uid, made.rule.id) is True
    assert container.rules_store.list_for(uid) == []
