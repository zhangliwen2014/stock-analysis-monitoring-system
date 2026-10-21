"""除权换算：把人写下来的价搬进今天的价空间（spec §10.2 的强制验收）。

全部手搓：`Snapshot` 那五个新字段（`factors`/`bars_from`/`stop_on`/`target_on`/`moved`）
就是这一枚的输入契约，所以这一整个文件不碰库，也不碰引擎。

期望值是量出来的，不是编的：前收 10.00、10 派 1.0 ⇒ 除权参考价 9.00、当日因子 1.111111，
写在它前面的「10 元」应该站在 9.00。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.analysis.adjust import DayFactor
from sams.core.rules import template_for
from sams.services.rules.anchor import move_prices
from sams.services.rules.snapshot import Snapshot
from sams.storage.repos.rules import Rule

AS_OF = date(2024, 10, 26)
FIRST_BAR = date(2024, 1, 1)
FACTOR = 10.0 / 9.0
DIVIDEND = DayFactor(ex_date=AS_OF, prev_close=10.0, ex_ref=9.0, factor=FACTOR, cash=1.0)
#: 库里那段唯一的一条除权（`adjusted_series` 现算出来的那一批，取数处负责填）
STEPS: tuple[DayFactor, ...] = (DIVIDEND,)


def snap(**over) -> Snapshot:
    base = {
        "market": "A",
        "code": "601088",
        "currency": "CNY",
        "price": 9.3,
        "price_on": AS_OF,
        "factors": STEPS,
        "bars_from": FIRST_BAR,
    }
    base.update(over)
    return Snapshot(**base)


def make_rule(
    params: dict, *, kind: str = "stop.fixed", params_on: date | None = AS_OF
) -> Rule:
    return Rule(
        id="r1",
        user_id="u1",
        name="跌破 10 元",
        kind=kind,
        enabled=True,
        target_type="symbol",
        target_ref="A/601088",
        params=params,
        filters={},
        cooldown_min=30,
        severity="strong",
        channel_ids=[],
        params_on=params_on,
    )


def moved_columns(got) -> tuple[str, ...]:
    return tuple(move.column for move in got.moves)


def test_no_dividend_in_the_window_moves_nothing() -> None:
    """那两个价空间是同一个 ⇒ 一个数都不许动，也不许因为「没有日期戳」而拒绝判断。

    这一枚是快路径的守卫：库里绝大多数标的这一段时间里没有除权，任何按锚缺失做的
    拒绝在没有除权时也生效的话，屏幕上就是一片没有结论的规则。
    """
    rule = make_rule({"price": 10.0}, params_on=None)
    got = move_prices(rule, template_for("stop.fixed"), snap(factors=()))
    assert got.params == {"price": 10.0}
    assert got.blocked == ()
    assert got.moves == ()


def test_a_money_param_moves_by_every_factor_after_its_day() -> None:
    """止损价 10 元是 1 月里填的，10 月派了 1 元 ⇒ 今天它站在 9.00。"""
    rule = make_rule({"price": 10.0}, params_on=date(2024, 3, 1))
    got = move_prices(rule, template_for("stop.fixed"), snap())
    assert got.params["price"] == pytest.approx(9.0)
    assert moved_columns(got) == ("params.price",)
    assert got.moves[0].ex_dates == (AS_OF,)
    assert got.snapshot.price == 9.3  # 现价本身当然不动：它已经是今天的裸价


def test_a_ratio_param_is_left_alone() -> None:
    """`pct` 是比例，不是价：除权不改变「成本下浮 8%」这句话的意思。"""
    rule = make_rule({"pct": 0.08}, kind="stop.pct", params_on=date(2024, 3, 1))
    got = move_prices(rule, template_for("stop.pct"), snap(avg_cost=12.0, opened_on=AS_OF))
    assert got.params == {"pct": 0.08}
    assert "params.pct" not in moved_columns(got)


def test_the_day_it_was_filled_is_measured_at_that_close() -> None:
    """除权当天填的价说的是除权之后那根 K 线 ⇒ 不搬。"""
    rule = make_rule({"price": 10.0}, params_on=AS_OF)
    got = move_prices(rule, template_for("stop.fixed"), snap())
    assert got.params == {"price": 10.0}
    assert got.moves == ()


def test_a_row_without_that_key_is_left_for_the_next_net() -> None:
    """那一行压根没有这个键 ⇒ 这里一个数都不搬、也一句都不报。

    「缺参数」不是搬运这一段的结论：求值那一头的 `KeyError` 网早就有一句写着去哪个命令补，
    这里再报一句是同一件事的两件外套——而两处「搬不动」里只有一处带着正确的下一步。
    """
    rule = make_rule({}, params_on=date(2024, 3, 1))
    got = move_prices(rule, template_for("stop.fixed"), snap())
    assert got.params == {}
    assert got.blocked == () and got.moves == ()


def test_an_unknown_fill_day_refuses_to_judge_a_price_rule() -> None:
    """0017 之前建的那一行没有锚：不搬、也不判，并且说出该跑哪条命令。

    拿原值去比今天的价是误报；搬了又怕搬少了，因为不知道要跨几档。这一条走的是
    「宁可这一条不响」（口径 1），而屏幕上那句 skipped 必须指得回 `sams rule edit`。
    """
    rule = make_rule({"price": 10.0}, params_on=None)
    got = move_prices(rule, template_for("stop.fixed"), snap())
    assert got.params == {"price": 10.0}
    assert got.moves == ()
    assert len(got.blocked) == 1
    assert "rule edit" in got.blocked[0]


def test_a_number_older_than_the_local_history_refuses_to_judge() -> None:
    """锚比第一根 K 线还早 ⇒ 中间那段的除权根本没记进来，搬它是少搬。

    这不是抬杠：装机半年后建的老仓，建仓日就在本地 K 线之前。修法是补行情，不是放行。
    """
    rule = make_rule({"price": 10.0}, params_on=date(2023, 5, 6))
    got = move_prices(rule, template_for("stop.fixed"), snap())
    assert got.params == {"price": 10.0}
    assert got.blocked and "sync daily" in got.blocked[0]


def test_the_cost_moves_when_the_position_precedes_the_dividend() -> None:
    rule = make_rule({"pct": 0.08}, kind="stop.pct")
    got = move_prices(
        rule, template_for("stop.pct"), snap(avg_cost=12.0, opened_on=date(2024, 3, 1))
    )
    assert got.snapshot.avg_cost == pytest.approx(10.8)
    assert moved_columns(got) == ("avg_cost",)


def test_a_cost_that_came_after_the_dividend_is_not_touched() -> None:
    """除权之后建的仓，成本本来就在今天的坐标系里。"""
    rule = make_rule({"pct": 0.08}, kind="stop.pct")
    got = move_prices(rule, template_for("stop.pct"), snap(avg_cost=12.0, opened_on=AS_OF))
    assert got.snapshot.avg_cost == 12.0
    assert got.moves == () and got.blocked == ()


def test_a_cost_without_a_fill_day_blocks_only_rules_that_judge_it() -> None:
    """同一个搬不动的成本，对要拿它做判断的规则是「不判」，对别的只是「屏幕上注明」。

    `stop.thesis_drift` 看的是打分，与成本无关（§10.2）：把它一起变成 skipped，等于用
    一个不相关的缺口关掉一条还能响的规则。
    """
    stuck = snap(avg_cost=12.0, opened_on=None)
    judging = move_prices(
        make_rule({"pct": 0.08}, kind="stop.pct"), template_for("stop.pct"), stuck
    )
    assert len(judging.blocked) == 1 and judging.snapshot.avg_cost == 12.0
    # 缺的是建仓日，补 K 线修不好它：那句话指错命令，人就白跑一趟
    assert "ledger buy" in judging.blocked[0] and "sync daily" not in judging.blocked[0]
    display = move_prices(
        make_rule({}, kind="stop.thesis_drift"), template_for("stop.thesis_drift"), stuck
    )
    assert display.blocked == ()
    assert display.snapshot.avg_cost == 12.0
    assert display.snapshot.missing and "成本" in display.snapshot.missing[0]


def test_a_cost_older_than_the_local_history_points_at_the_bars() -> None:
    """成本有建仓日，只是那个日子在本地 K 线之前 ⇒ 这一趟的下一步是补行情。

    与上一枚成对：同一个「搬不动的成本」，两种缺口，两句不同的话。写成同一句的话，
    一半的人会去补 K 线，另一半会去重记那一笔，而两者都只修好其中一种。
    """
    got = move_prices(
        make_rule({"pct": 0.08}, kind="stop.pct"),
        template_for("stop.pct"),
        snap(avg_cost=12.0, opened_on=date(2023, 5, 6)),
    )
    assert got.snapshot.avg_cost == 12.0
    assert len(got.blocked) == 1
    assert "sync daily" in got.blocked[0] and "ledger buy" not in got.blocked[0]


def test_the_thesis_line_moves_when_it_has_a_reading_day() -> None:
    got = move_prices(
        make_rule({"basis": "weekly_ma20"}, kind="stop.ma"),
        template_for("stop.ma"),
        snap(stop_line=8.556, stop_on=date(2024, 9, 2)),
    )
    assert got.snapshot.stop_line == pytest.approx(8.556 / FACTOR)
    assert moved_columns(got) == ("stop_line",)


def test_a_hand_typed_thesis_line_is_annotated_not_blocked() -> None:
    """手填的价位没有测量日（0013）：注明，不关掉规则。

    没有任何模板拿 `stop_line` 做判断（要盯那条线的人建的是 `stop.fixed`，它的锚是
    `params_on`）。为一条不参与判断的列拒绝出数，报出来的「不判」是假的。
    """
    got = move_prices(
        make_rule({"basis": "weekly_ma20"}, kind="stop.ma"),
        template_for("stop.ma"),
        snap(stop_line=8.556, stop_on=None),
    )
    assert got.blocked == ()
    assert got.snapshot.stop_line == 8.556
    assert got.snapshot.missing and "止损" in got.snapshot.missing[0]
    assert "thesis revise" in got.snapshot.missing[0]


def test_the_target_price_copied_from_the_thesis_moves_too() -> None:
    """`tp.tiered` 量的是「成本 → 目标价」那一段，两个端点得站在同一个价空间里。

    这一列有两个来源（`snapshot._target`：论上那一列优先，没有才是本次合成的），
    抄来的那一个和成本一样是某天写下的裸价：10 派 1 之后，当年写的 20.00 说的是今天的 18.00。
    """
    got = move_prices(
        make_rule({"tiers": ["0.5", "1.0"]}, kind="tp.tiered"),
        template_for("tp.tiered"),
        snap(target_price=20.0, target_on=date(2024, 3, 1)),
    )
    assert got.snapshot.target_price == pytest.approx(20.0 / FACTOR)
    assert moved_columns(got) == ("target_price",)


def test_a_target_price_synthesized_today_is_not_touched() -> None:
    """口径 3 的正面：出自本趟的那个数，锚就是本趟那一天，跨不过任何除权日。

    「是价就搬」对抄来的数是对的、对合成出来的是错的（外套穿两件），而区分它们的不是
    一份例外名单，只有那个锚。
    """
    got = move_prices(
        make_rule({"tiers": ["0.5", "1.0"]}, kind="tp.tiered"),
        template_for("tp.tiered"),
        snap(target_price=20.0, target_on=AS_OF),
    )
    assert got.snapshot.target_price == 20.0
    assert got.moves == () and got.blocked == ()


def test_a_hand_typed_target_blocks_the_rule_that_judges_it() -> None:
    """手填的目标价没有测量日，而 `tp.tiered` 拿它当刻度终点 ⇒ 整条不判（口径 1）。

    这里错的方向不是「慢一步」而是「永远偏少」：终点停在除权前，进度算出来永远偏低，
    屏幕上每一趟都在报「还剩三档」。
    """
    got = move_prices(
        make_rule({"tiers": ["0.5", "1.0"]}, kind="tp.tiered"),
        template_for("tp.tiered"),
        snap(target_price=20.0, target_on=None),
    )
    assert got.snapshot.target_price == 20.0
    assert len(got.blocked) == 1
    assert "--target" in got.blocked[0] and "不判" in got.blocked[0]


def test_a_hand_typed_target_is_only_annotated_for_a_rule_that_does_not_judge_it() -> None:
    """同一个搬不动的目标价，对 `tp.tiered` 是「不判」，对 `stop.fixed` 只是「屏幕上注明」。

    后者的判断只用它自己那条线（`params.price`）与现价，那一列填的是哪天的价与它无关。
    """
    got = move_prices(
        make_rule({"price": 10.0}, kind="stop.fixed"),
        template_for("stop.fixed"),
        snap(target_price=20.0, target_on=None),
    )
    assert got.blocked == ()
    assert got.snapshot.target_price == 20.0
    assert got.snapshot.missing and "目标价" in got.snapshot.missing[0]


def test_two_dividends_stack() -> None:
    """跨两次派息 ⇒ 除两次。累乘的那段在 `hfq_frame` 里，这里必须在同一侧。"""
    second = DayFactor(
        ex_date=date(2024, 6, 1), prev_close=11.0, ex_ref=10.0, factor=11.0 / 10.0
    )
    rule = make_rule({"price": 10.0}, params_on=date(2024, 1, 2))
    got = move_prices(rule, template_for("stop.fixed"), snap(factors=(second, DIVIDEND)))
    assert got.params["price"] == pytest.approx(10.0 / (11.0 / 10.0) / FACTOR)
    assert got.moves[0].ex_dates == (date(2024, 6, 1), AS_OF)  # 顺序跟着因子表走


def test_a_snapshot_without_a_window_start_moves_nothing() -> None:
    """读不到那段 K 线从哪天开始 ⇒ 无法证明因子表是完整的，一个数都不搬。

    这一列归取数处填；走到 None 说明有人手搓了半份快照。不判比搬错好。
    """
    rule = make_rule({"price": 10.0}, params_on=date(2024, 3, 1))
    got = move_prices(rule, template_for("stop.fixed"), snap(bars_from=None))
    assert got.params == {"price": 10.0}
    assert got.moves == () and len(got.blocked) == 1


def test_the_move_line_names_the_days_it_crossed() -> None:
    """正文里那行「换算」必须说清凭什么动了这个数（D54 的并列给数要给到原因）。"""
    rule = make_rule({"price": 10.0}, params_on=date(2024, 3, 1))
    got = move_prices(rule, template_for("stop.fixed"), snap())
    line = got.moves[0].line
    assert "止损价" in line and "10.00 CNY" in line and "9.00 CNY" in line
    assert str(date(2024, 3, 1)) in line and str(AS_OF) in line


def test_a_refusal_sentence_names_its_own_fix() -> None:
    """两句「搬不动」各指各的命令：一个补参数、一个补行情，写反了人就白跑一趟（D55）。"""
    no_anchor = move_prices(
        make_rule({"price": 10.0}, params_on=None), template_for("stop.fixed"), snap()
    )
    too_old = move_prices(
        make_rule({"price": 10.0}, params_on=date(2023, 5, 6)),
        template_for("stop.fixed"),
        snap(),
    )
    assert "rule edit" in no_anchor.blocked[0] and "sync daily" not in no_anchor.blocked[0]
    assert "sync daily" in too_old.blocked[0] and "rule edit" not in too_old.blocked[0]
