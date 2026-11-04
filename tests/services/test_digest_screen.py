"""摘要里的选股方案行（S4-B / D256）：只念新命中，按方案归并，静音的不列。

方案新命中已经落成告警，但摘要不把它按「一只票一行」摊开——同一晚一个方案命中
几十只时，那会把摘要变成第二份选股结果。摘要只答「哪个方案今晚放了什么进来」。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_digest_service import NOW, World

from sams.core.notify import MID


@pytest.fixture
def world(db, settings):
    return World(db, settings)


def _hit(world, code, *, preset="便宜", preset_id="p1", at=NOW, name="雷"):
    return world.alerts.create(
        world.user.id,
        rule_id=None,
        rule_name=f"方案《{preset}》",
        account_id=None,
        account_name="",
        market="A",
        code=code,
        symbol_name=f"名称{code}",
        severity=MID,
        stage="hit",
        value_text=f"{name}{code}",
        threshold_text="自动监控新命中",
        action_note="点开个股页核对；这不是买卖建议",
        triggered_at=at,
        facts={
            "kind": "screen",
            "preset_id": preset_id,
            "preset_name": preset,
            "first_seen": at.date().isoformat(),
            "conditions": "现值市赚率 PR ≤ 0.5",
        },
    )


def test_new_screen_hits_are_grouped_by_preset_instead_of_by_symbol(world):
    world.watch("601318")
    _hit(world, "600519")
    _hit(world, "000001")

    outcome = world.run()

    body = outcome.digest.body
    assert "【选股方案】" in body
    assert "方案《便宜》新命中 2 只（000001、600519）" in body
    assert outcome.digest.has_changes is True


def test_a_screen_muted_symbol_is_not_listed(world):
    world.watch("601318")
    world.mutes.add(world.user.id, "A", "600519", kind="screen")
    _hit(world, "600519")
    _hit(world, "000001")

    body = world.run().digest.body

    assert "600519" not in body
    assert "方案《便宜》新命中 1 只（000001）" in body


def test_a_fully_muted_preset_does_not_get_a_line(world):
    world.watch("601318")
    world.mutes.add(world.user.id, "A", "600519", kind="screen")
    _hit(world, "600519")

    outcome = world.run()

    assert "【选股方案】" not in outcome.digest.body
    assert outcome.digest.has_changes is False


def test_screen_hits_do_not_appear_twice_in_the_symbol_section(world):
    world.hold("600519")
    _hit(world, "600519")

    body = world.run().digest.body

    assert body.count("600519") == 1
    assert "自动监控新命中" not in body
    assert "方案《便宜》新命中 1 只（600519）" in body


def test_screen_only_day_breaks_the_post_heartbeat_silence(world):
    from datetime import date

    from test_digest_service import _seed_history

    world.watch("601318")
    _seed_history(world, 10, end=date(2026, 10, 16))
    _hit(world, "600519", at=datetime(2026, 10, 20, 7, 0, tzinfo=UTC))

    outcome = world.run(as_of=date(2026, 10, 20))

    assert outcome.digest.has_changes is True
    assert outcome.pushed is True
