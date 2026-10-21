"""§9.2：一条论 → 六条规则。派生要能重复跑，回写要能指名道姓。

仓库层全用真的：`by_thesis` 与 `remove` 那两道闸就是这一枚文件的验收线，
把它们 stub 掉等于把「不可删除，只能停用」那句话删掉。
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, date, datetime

import pytest

from sams.core.errors import ValidationError
from sams.core.rules import template_for
from sams.services.rules.derive import (
    TIERS,
    WRITE_BACK,
    NotDerived,
    RuleDeriver,
    blueprints_for,
    write_back,
)
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.theses import Thesis, ThesisRepo

USER = "u-derive"
T0 = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
LEVELS = {
    "entry_low": 32.0,
    "entry_high": 36.0,
    "stop_price": 28.0,
    "target_price": 52.0,
    "reward_risk": 2.0,
}
SIX = {
    "price.cross_below",
    "stop.fixed",
    "tp.tiered",
    "thesis.odds_below",
    "scheduled.reevaluate",
    "thesis.drift",
}
#: 与账户无关的那五条：论的事实，换个账户还是同一件事
FIVE_NOT_PER_ACCOUNT = SIX - {"tp.tiered"}


@pytest.fixture
def theses(db) -> ThesisRepo:
    return ThesisRepo(db)


@pytest.fixture
def rules(db) -> RuleRepo:
    return RuleRepo(db)


def make_thesis(theses, **over):
    levels = dict(LEVELS)
    levels.update(over)
    return theses.create(
        user_id=USER,
        market="A",
        code="601088",
        style="value",
        actor_id=USER,
        now=T0,
        status="proposed",
        **{k: v for k, v in levels.items() if v is not None},
    )


def skip_reasons(got):
    """这次没建的那些，连它们各自的理由。"""
    return {item.kind: item.reason for item in got if isinstance(item, NotDerived)}


def one(rows, kind):
    """按 kind 取那一条：不许靠 `created[0]`——建表顺序变了不该让用例变色。"""
    return next(row for row in rows if row.kind == kind)


def test_six_blueprints_when_the_thesis_is_complete(theses) -> None:
    thesis = make_thesis(theses)
    got = blueprints_for(thesis, account_id="acc-1")
    assert len(got) == 6
    assert {item.kind for item in got} == SIX
    assert skip_reasons(got) == {}


def test_every_blueprint_names_which_column_it_came_from(theses) -> None:
    """`reason` 里那句「凭什么建这条」上的数字不许是常量的回声。

    1.5 是 §9.2 那一行写死的，5 是 `core/rules.py` 里 `within_days` 的默认：断字面值，
    改常量必须让用例变红——那两个数是给用户看的承诺，不是可以自由漂移的实现细节。
    """
    thesis = make_thesis(theses)
    by_kind = {item.kind: item for item in blueprints_for(thesis, account_id="acc-1")}
    assert "36" in by_kind["price.cross_below"].reason
    assert "28" in by_kind["stop.fixed"].reason
    assert "52" in by_kind["tp.tiered"].reason
    assert by_kind["tp.tiered"].params["tiers"] == TIERS
    assert by_kind["thesis.odds_below"].params["threshold"] == 1.5
    assert "跌破 1.5" in by_kind["thesis.odds_below"].reason
    assert by_kind["scheduled.reevaluate"].params["within_days"] == 5


def test_the_two_copied_prices_are_the_thesis_upper_edge_and_the_stop(theses) -> None:
    """蓝图里那两个数是**从哪一列抄的**：抄错列，屏幕上那句话还说得漂漂亮亮，
    而回写会把目标价写进论的 `stop_price`（`write_back` 按 kind 取列，不看 `reason`）。
    """
    thesis = make_thesis(theses)
    by_kind = {item.kind: item for item in blueprints_for(thesis, account_id="acc-1")}
    assert by_kind["price.cross_below"].params["price"] == 36.0  # 区间上沿
    assert by_kind["stop.fixed"].params["price"] == 28.0  # 止损价


def test_a_missing_target_skips_the_tier_only(theses) -> None:
    """缺哪一列就少哪一条，并且说得出为什么——不是六条悄悄变成五条。"""
    thesis = make_thesis(theses, target_price=None)
    got = blueprints_for(thesis, account_id="acc-1")
    missing = skip_reasons(got)
    assert set(missing) == {"tp.tiered"}
    assert "目标价" in missing["tp.tiered"]
    assert len(got) == 6


def test_a_tier_without_an_account_is_skipped_with_a_named_reason(theses) -> None:
    """`tp.tiered` 读的是这笔的成本：没有账户就没有成本，硬建出来是一条永远不响的规则。"""
    thesis = make_thesis(theses)
    missing = skip_reasons(blueprints_for(thesis))
    assert "账户" in missing["tp.tiered"]


def test_the_stored_rules_carry_the_thesis_numbers(theses, rules) -> None:
    """入库那一份也得是那两个数：蓝图对、`create` 走形（丢键、串化）一样是错的规则。"""
    thesis = make_thesis(theses)
    RuleDeriver(rules, theses).derive(USER, thesis.id, account_id="acc-1")
    stored = rules.by_thesis(thesis.id)
    assert one(stored, "price.cross_below").params["price"] == 36.0
    assert one(stored, "stop.fixed").params["price"] == 28.0


def test_the_reevaluate_rule_is_derived_with_the_open_alert_filter(
    theses, rules
) -> None:
    """`templates.scheduled_reevaluate` 明写「T-15 派生这一条时默认带 `no_open_alert`」：
    披露日窗口内每个交易日都会各判中一次，不带这个开关就是那段窗口天天响（§10.1 降噪）。
    """
    thesis = make_thesis(theses)
    reeval = one(blueprints_for(thesis, account_id="acc-1"), "scheduled.reevaluate")
    assert reeval.filters == {"no_open_alert": True}
    RuleDeriver(rules, theses).derive(USER, thesis.id, account_id="acc-1")
    stored = rules.by_thesis(thesis.id)
    assert one(stored, "scheduled.reevaluate").filters["no_open_alert"] is True
    # 另外五条没带这个开关：§10.1 的取向是「默认不降噪，静默的规则比吵的规则难查」
    assert {
        row.filters["no_open_alert"]
        for row in stored
        if row.kind != "scheduled.reevaluate"
    } == {False}


def test_derive_creates_six_and_attaches_them_to_the_thesis(theses, rules) -> None:
    thesis = make_thesis(theses)
    report = RuleDeriver(rules, theses).derive(USER, thesis.id, account_id="acc-1")
    assert len(report.created) == 6 and report.skipped == ()
    stored = rules.by_thesis(thesis.id)
    assert len(stored) == 6
    assert {row.target_type for row in stored} == {"thesis"}
    # 只有 `tp.tiered` 真的与账户有关：另外五条是论的事实，挂上账户就等于让引擎的去重键
    # （§10.1 含 account_id）带一个与这条规则无关的账户
    assert one(stored, "tp.tiered").account_id == "acc-1"
    assert {
        row.account_id for row in stored if row.kind in FIVE_NOT_PER_ACCOUNT
    } == {None}
    # 写进去的是比例串，库里读出来的是校验过的列表（`tp.tiered` 求值时按这个形状取）
    assert one(stored, "tp.tiered").params["tiers"] == [0.5, 1.0]


def test_deriving_twice_creates_nothing(rules, theses) -> None:
    thesis = make_thesis(theses)
    deriver = RuleDeriver(rules, theses)
    deriver.derive(USER, thesis.id, account_id="acc-1")
    again = deriver.derive(USER, thesis.id, account_id="acc-1")
    assert again.created == ()
    assert set(again.already) == SIX
    assert len(again.already) == 6
    stored = rules.by_thesis(thesis.id)
    assert len(stored) == 6
    assert sum(1 for row in stored if row.kind == "tp.tiered") == 1


def test_a_second_account_adds_only_its_own_tier(rules, theses) -> None:
    """同一论在第二个账户上建仓：只有分批止盈要再建一条（那一档读的是这一笔的成本），
    另外五条不许按账户复制——复制就是同一件事发 N 条重复告警。
    """
    thesis = make_thesis(theses)
    deriver = RuleDeriver(rules, theses)
    deriver.derive(USER, thesis.id, account_id="acc-1")
    again = deriver.derive(USER, thesis.id, account_id="acc-2")
    assert [row.kind for row in again.created] == ["tp.tiered"]
    assert [row.account_id for row in again.created] == ["acc-2"]
    assert set(again.already) == FIVE_NOT_PER_ACCOUNT
    stored = rules.by_thesis(thesis.id)
    assert len(stored) == 7
    assert {
        row.account_id for row in stored if row.kind == "tp.tiered"
    } == {"acc-1", "acc-2"}
    assert {
        row.account_id for row in stored if row.kind in FIVE_NOT_PER_ACCOUNT
    } == {None}


def test_a_second_derive_does_not_reset_edited_params(rules, theses) -> None:
    """幂等是「跳过」不是「覆盖」：用户把止损线改成 30 后再派生，库里得留着他那个 30。"""
    thesis = make_thesis(theses)
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    stop = one(made, "stop.fixed")
    rules.update(stop.id, changed_by=USER, params={"price": 30.0})
    again = RuleDeriver(rules, theses).derive(USER, thesis.id)
    assert again.created == ()
    kept = one(rules.by_thesis(thesis.id), "stop.fixed")
    assert kept.params["price"] == 30.0


def test_a_tier_list_survives_a_second_validation(rules, theses) -> None:
    """`tiers` 的两种形状都要在这一层走得通：库里读回的是列表，而 `update` 会把这一份
    再校验一遍——只测串那一支的话，「列表进得去」这件事其实没人保证过。
    """
    thesis = make_thesis(theses)
    tier = one(
        RuleDeriver(rules, theses).derive(USER, thesis.id, account_id="acc-1").created,
        "tp.tiered",
    )
    assert tier.params["tiers"] == [0.5, 1.0]
    updated = rules.update(tier.id, changed_by=USER, params={"tiers": [0.3, 0.9]})
    assert updated.params["tiers"] == [0.3, 0.9]
    assert one(rules.by_thesis(thesis.id), "tp.tiered").params["tiers"] == [0.3, 0.9]


def test_deriving_an_empty_thesis_creates_only_the_two_that_need_no_level(
    theses, rules
) -> None:
    """一条只有方向和风格的论：价位类全跳过，日历与漂移那两条照样建。"""
    thesis = make_thesis(
        theses,
        entry_low=None,
        entry_high=None,
        stop_price=None,
        target_price=None,
        reward_risk=None,
    )
    report = RuleDeriver(rules, theses).derive(USER, thesis.id)
    assert {row.kind for row in report.created} == {
        "scheduled.reevaluate",
        "thesis.drift",
    }
    assert len(report.skipped) == 4


def test_a_derived_rule_cannot_be_removed_but_can_be_disabled(theses, rules) -> None:
    """§9.2 那句「不可删除，只能停用」：派生出来的止损提醒不能被一次 `rule remove` 悄悄关掉。"""
    thesis = make_thesis(theses)
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    stop = one(made, "stop.fixed")
    with pytest.raises(ValidationError, match="不能删除"):
        rules.remove(stop.id, user_id=USER)
    assert rules.set_enabled(stop.id, False, user_id=USER).enabled is False


# --- 改参数 → 回写论上那一列 -------------------------------------------------------------


def test_editing_a_derived_stop_moves_the_thesis_stop(theses, rules) -> None:
    thesis = make_thesis(theses)
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    stop = one(made, "stop.fixed")
    assert write_back(stop, {"price": 28.0}, {"price": 30.0}) == {"stop_price": 30.0}


def test_editing_a_derived_entry_band_moves_the_upper_edge(theses, rules) -> None:
    thesis = make_thesis(theses)
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    cross = one(made, "price.cross_below")
    assert write_back(cross, {"price": 36.0}, {"price": 34.0}) == {"entry_high": 34.0}


def test_an_unchanged_number_writes_nothing(theses, rules) -> None:
    thesis = make_thesis(theses)
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    stop = one(made, "stop.fixed")
    assert write_back(stop, {"price": 28.0}, {"price": 28.0}) == {}


def test_a_change_without_a_price_writes_back_nothing(theses, rules) -> None:
    """改的是别的键（冷却）而表里那个键没动：回写按 `(列, 参数名)` 逐项取数，
    取不到新值就不动论点。
    """
    thesis = make_thesis(theses)
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    stop = one(made, "stop.fixed")
    before = {"price": 28.0, "cooldown_min": 30}
    after = {"price": 28.0, "cooldown_min": 60}
    assert write_back(stop, before, after) == {}


def test_the_write_back_table_names_a_real_param_and_a_real_column() -> None:
    """回写表的两项都必须对得上别处：参数名得真是那个 kind 的模板参数，列名得真是论
    上的那一列。写错任何一项都不报错——`write_back` 取不到值，于是**静默不回写**，
    用户在屏幕上把止损线改到 30，论上还写着 28。这一枚把「表与模板同步」钉住。
    """
    thesis_fields = {f.name for f in fields(Thesis)}
    for kind, (column, param_key) in WRITE_BACK.items():
        names = {p.name for p in template_for(kind).params}
        assert param_key in names, f"{kind} 的参数里没有 {param_key}：{sorted(names)}"
        assert column in thesis_fields, f"论上没有 {column} 这一列"


def test_a_rule_that_is_not_derived_writes_back_nothing(theses, rules) -> None:
    """挂在自己心里的价上的一条规则，改了它不该动任何论点。"""
    mine = rules.create(
        USER,
        kind="stop.fixed",
        name="我自己的止损价",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 20.0},
    )
    assert mine.thesis_id is None
    assert write_back(mine, {"price": 20.0}, {"price": 18.0}) == {}


def test_editing_the_tiers_writes_back_no_target(theses, rules) -> None:
    """分档是比例不是价位：把 `0.5,1.0` 改成 `0.3,0.8` 不该动论上的 `target_price`。"""
    thesis = make_thesis(theses)
    made = RuleDeriver(rules, theses).derive(USER, thesis.id, account_id="acc-1").created
    tier = one(made, "tp.tiered")
    assert write_back(tier, {"tiers": [0.5, 1.0]}, {"tiers": [0.3, 0.8]}) == {}


def test_a_derived_rule_carries_the_thesis_reading_day(theses, rules) -> None:
    """派生出来的那条线属于论上那次读数，不属于派生它的那一天。

    盖上今天等于宣布它不用搬（§10.2 的换算），而它其实是 `levels_on` 那天的行情算的。
    """
    thesis = theses.create(
        user_id=USER,
        market="A",
        code="601088",
        style="value",
        actor_id=USER,
        now=T0,
        status="proposed",
        levels_on=date(2026, 10, 19),
        **LEVELS,
    )
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    stop = next(row for row in made if row.kind == "stop.fixed")
    assert stop.params_on == date(2026, 10, 19)


def test_a_derived_rule_with_no_price_gets_no_anchor(theses, rules) -> None:
    """`scheduled.reevaluate` 的那几个天数是这一份文件给的常量，不属于论上那次读数。

    给它盖一个日期戳就是在给一个不指任何价位的锚：除权换算要搬的是「元」那一列，
    而这一条压根没有价可搬，戳躺在那里等着被误当成依据。
    """
    thesis = make_thesis(theses, levels_on=date(2026, 10, 19))
    assert thesis.levels_on == date(2026, 10, 19)
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    again = next(row for row in made if row.kind == "scheduled.reevaluate")
    assert again.params_on is None


def test_a_hand_typed_thesis_leaves_the_derived_rule_without_an_anchor(theses, rules) -> None:
    """手填的价位没有测量日（0013），派生出来的规则因此也没有：「不知道」不许被写成「今天」。"""
    thesis = make_thesis(theses)
    assert thesis.levels_on is None
    made = RuleDeriver(rules, theses).derive(USER, thesis.id).created
    stop = next(row for row in made if row.kind == "stop.fixed")
    assert stop.params_on is None
