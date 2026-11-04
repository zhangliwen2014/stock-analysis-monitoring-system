"""摘要里的资讯行（S3-C，D245/D246）：只读命中，最多两条/标的，静音不列。

命中记录本身就带原文链接与关键词，所以摘要不必回头再读 `news_items`：
`channel_ids` 为空的关键词「只进摘要不推送」，它的落地就在这里。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from test_digest_service import NOW, World, _seed_history


@pytest.fixture
def world(db, settings):
    return World(db, settings)


def test_a_keyword_hit_lands_in_the_symbol_section_with_its_link(world):
    world.hold("600519")
    world.hit("600519", title="贵州茅台：关于回购股份的公告")

    outcome = world.run()

    body = outcome.digest.body
    assert "【持仓】" in body
    assert "资讯「贵州茅台：关于回购股份的公告」" in body
    assert "关键词：回购" in body
    assert "https://example.test/N1" in body
    assert outcome.digest.has_changes is True
    assert outcome.pushed is True


def test_at_most_two_news_items_per_symbol_newest_first(world):
    world.hold("600519")
    for index, minute in enumerate((1, 2, 3)):
        world.hit(
            "600519",
            f"N{index}",
            title=f"旧闻 {index}",
            keyword=f"词{index}",
            at=NOW.replace(minute=minute),
        )

    body = world.run().digest.body

    assert "旧闻 2" in body and "旧闻 1" in body
    assert "旧闻 0" not in body
    assert body.count("资讯「") == 2


def test_one_item_hit_by_two_keywords_renders_once_with_both_words(world):
    world.hold("600519")
    world.hit("600519", title="公司披露回购与减持")
    world.hit("600519", keyword="减持", title="公司披露回购与减持")

    body = world.run().digest.body

    lines = [line for line in body.splitlines() if "资讯「" in line]
    assert len(lines) == 1
    # 两个词都印在同一行；词序按词面排，不靠主键（uuid 顺序每跑一次都不一样）
    assert "关键词：" in lines[0] and "回购" in lines[0] and "减持" in lines[0]


def test_a_muted_symbols_news_is_neither_listed_nor_a_change(world):
    world.watch("601318")
    world.mutes.add(world.user.id, "A", "601318", note="等财报")
    world.hit("601318")

    outcome = world.run()

    assert "601318" not in outcome.digest.body
    assert outcome.digest.has_changes is False


def test_news_about_a_symbol_outside_the_sections_goes_to_other(world):
    world.watch("601318")
    world.hit("000001", title="平安银行：关于回购股份")

    body = world.run().digest.body

    assert "【其他】" in body
    assert "000001" in body and "平安银行：关于回购股份" in body


def test_another_users_hit_never_enters_this_digest(world):
    world.watch("601318")
    world.hit("601318", user_id=world.other_user.id, title="别人的资讯")

    outcome = world.run()

    assert "别人的资讯" not in outcome.digest.body
    assert outcome.digest.has_changes is False


def test_news_only_day_breaks_the_post_heartbeat_silence(world):
    world.watch("601318")
    _seed_history(world, 10, end=date(2026, 10, 16))
    world.hit(
        "601318",
        at=datetime(2026, 10, 20, 7, 0, tzinfo=UTC),
        title="今天才采到的旧闻",
    )

    outcome = world.run(as_of=date(2026, 10, 20))

    assert outcome.digest.has_changes is True
    assert outcome.pushed is True


def test_news_lines_follow_the_symbols_alert_lines(world):
    world.hold("600519")
    alert = world.alert("600519")
    world.hit("600519", title="回购公告")

    body = world.run().digest.body

    assert body.index(alert.id) < body.index("资讯「回购公告」")


def test_a_news_mute_hides_the_news_lines_but_keeps_the_alert_lines(world):
    world.hold("600519")
    world.alert("600519", rule="止损")
    world.hit("600519", title="贵州茅台：关于回购股份的公告")
    world.mutes.add(world.user.id, "A", "600519", kind="news")

    outcome = world.run()

    assert "资讯「" not in outcome.digest.body
    assert "「止损」" in outcome.digest.body
    assert outcome.digest.has_changes is True


def test_a_rule_mute_hides_the_alert_lines_but_keeps_the_news_lines(world):
    world.hold("600519")
    world.alert("600519", rule="止损")
    world.hit("600519", title="贵州茅台：关于回购股份的公告")
    world.mutes.add(world.user.id, "A", "600519", kind="rule")

    outcome = world.run()

    assert "「止损」" not in outcome.digest.body
    assert "资讯「贵州茅台：关于回购股份的公告」" in outcome.digest.body
    assert outcome.digest.has_changes is True


def test_a_digest_mute_hides_every_line_and_is_not_a_change(world):
    world.hold("600519")
    world.alert("600519", rule="止损")
    world.hit("600519", title="贵州茅台：关于回购股份的公告")
    world.mutes.add(world.user.id, "A", "600519", kind="digest")

    outcome = world.run()

    assert "「止损」" not in outcome.digest.body
    assert "资讯「" not in outcome.digest.body
    assert outcome.digest.has_changes is False
