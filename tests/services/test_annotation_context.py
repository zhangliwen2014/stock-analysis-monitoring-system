"""推送上下文的 provider：发送那一刻取「接收人可见」的批注（spec §7.1）。

隐私边界按接收人算：别人的私有批注一个字都不许进推送；团队共享关闭时，
既有的 team 批注对非作者隐藏（D127）——这些不是「尽量」，是这里逐条断言的东西。

还钉一件事：**坏掉的查询不许挡告警**（D126）。provider 吞掉一切异常返回 None，
让 router 按无上下文发送；「批注表被锁了」的代价是推送少了三行字，
而不是手机上收不到一条止损。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sams.services.annotation_context import AnnotationContextProvider
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.app_settings import TEAM_SHARING, AppSettingsRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

PASSWORD = "correct-horse-battery"
T0 = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)
CREATED = datetime(2026, 10, 22, 6, 3, tzinfo=UTC)  # 折到 Asia/Shanghai 是 14:03


class World:
    def __init__(self, db, settings) -> None:
        self.db = db
        self.users = UserRepo(db)
        self.me = self.users.create("viewer", PASSWORD)
        self.other = self.users.create("teammate", PASSWORD)
        self.annotations = AnnotationRepo(db)
        self.theses = ThesisRepo(db)
        self.settings_store = AppSettingsRepo(db)
        self.alerts = AlertRepo(db)
        self.settings = settings
        self.provider = AnnotationContextProvider(
            self.annotations, self.users, self.theses, self.settings_store, settings
        )

    def uid(self, name: str) -> str:
        return self.users.get_by_username(name).id

    def annotate(self, author: str, *, ref="A:601088", target="symbol", **over):
        created_at = over.pop("created_at", None)
        kw: dict = {
            "user_id": self.uid(author),
            "target_type": target,
            "target_ref": ref,
            "target_label": "中国神华",
            "body_md": "渠道库存见底，Q4 提价",
        }
        kw.update(over)
        made = self.annotations.create(**kw)
        if created_at is not None:
            # 仓库层的 create 按自己的钟写 created_at，排序用例要的是指定的那一刻
            self.db.execute(
                "UPDATE annotations SET created_at = ? WHERE id = ?",
                [created_at, made.id],
            )
        return made

    def alert(self, *, user: str = "viewer", **over):
        kw: dict = {
            "rule_id": "rule-1",
            "rule_name": "PR 低",
            "account_id": None,
            "account_name": "主账户",
            "market": "A",
            "code": "601088",
            "symbol_name": "中国神华",
            "severity": "mid",
            "stage": "入池",
            "value_text": "PR=0.31",
            "threshold_text": "PR≤0.4",
            "action_note": "",
            "triggered_at": T0,
        }
        kw.update(over)
        return self.alerts.create(self.uid(user), **kw)


@pytest.fixture
def world(db, settings) -> World:
    return World(db, settings)


def test_own_private_annotation_shows_up_with_me_as_author(world):
    world.annotate("viewer")
    made = world.provider(world.alert())
    assert made is not None
    assert [item.author_label for item in made.items] == ["我"]
    assert made.items[0].body_text == "渠道库存见底，Q4 提价"


def test_the_moment_label_is_folded_into_display_tz(world):
    world.annotate("viewer", created_at=CREATED)
    made = world.provider(world.alert())
    assert made.items[0].moment_label == "10-22 14:03"
    # 排序键也在场：UTC 那一刻原样带回来，跨源合并靠它，不是只留一个给屏幕看的串
    assert made.items[0].created_at.astimezone(UTC) == CREATED


def test_team_annotation_by_another_user_shows_with_their_name(world):
    world.annotate("teammate", visibility="team")
    made = world.provider(world.alert())
    assert [item.author_label for item in made.items] == ["teammate"]


def test_display_name_wins_over_username(world):
    world.users.set_display_name(world.uid("teammate"), "张三")
    world.annotate("teammate", visibility="team")
    made = world.provider(world.alert())
    assert made.items[0].author_label == "张三"


def test_another_users_private_annotation_never_shows_up(world):
    world.annotate("teammate")  # 默认 private
    made = world.provider(world.alert())
    assert made is not None and made.items == ()


def test_turning_off_team_sharing_hides_team_from_others(world):
    world.annotate("teammate", visibility="team")
    # `set` 废掉本进程缓存（world.settings_store 就是 fixture provider 持有的那个实例），
    # 不需要新实例
    world.settings_store.set(TEAM_SHARING, "off")
    made = world.provider(world.alert())
    assert made is not None and made.items == ()


def test_the_author_still_sees_their_own_team_annotation_when_sharing_is_off(world):
    world.annotate("viewer", visibility="team")
    # 同上：`set` 废掉本进程缓存，不需要新实例
    world.settings_store.set(TEAM_SHARING, "off")
    made = world.provider(world.alert())
    assert [item.author_label for item in made.items] == ["我"]


def test_a_system_alert_has_no_context_at_all(world):
    assert world.provider(world.alert(market="SYS", code="scheduler")) is None


def test_an_alert_without_a_code_has_no_context(world):
    assert world.provider(world.alert(code="")) is None


def test_thesis_annotations_join_when_the_alert_names_a_readable_thesis(world):
    thesis_id = world.theses.create(
        user_id=world.uid("viewer"), market="A", code="601088",
        style="PR", actor_id=world.uid("viewer"), now=T0,
    ).id
    world.annotate("viewer", target="thesis", ref=thesis_id, body_md="等半年线企稳")
    world.annotate("viewer")  # 该标的的 symbol 批注：两路候选要同时在场
    made = world.provider(world.alert(thesis_id=thesis_id))
    bodies = {item.body_text for item in made.items}
    assert "等半年线企稳" in bodies and "渠道库存见底，Q4 提价" in bodies


def test_a_foreign_or_vanished_thesis_falls_back_to_symbol_only(world):
    thesis_id = world.theses.create(
        user_id=world.uid("teammate"), market="A", code="601088",
        style="PR", actor_id=world.uid("teammate"), now=T0,
    ).id
    world.annotate("teammate", target="thesis", ref=thesis_id, body_md="别人的想法")
    world.annotate("viewer")
    made = world.provider(world.alert(thesis_id=thesis_id))
    assert [item.body_text for item in made.items] == ["渠道库存见底，Q4 提价"]


def test_markdown_gets_flattened_into_one_line(world):
    world.annotate("viewer", body_md="**渠道库存** 见底\n\n- Q4 提价\n- 已核实")
    made = world.provider(world.alert())
    assert made.items[0].body_text == "渠道库存 见底 Q4 提价 已核实"


def test_the_deep_link_lands_on_the_push_context(world, settings):
    over = replace(settings.server, public_base_url="http://lan:8765")
    with_url = replace(settings, server=over)
    fresh = AnnotationContextProvider(
        world.annotations, world.users, world.theses, world.settings_store, with_url
    )
    alert = world.alert()
    made = fresh(alert)
    assert made.link == f"http://lan:8765/alerts/{alert.id}"


def test_no_base_url_means_no_link(world):
    assert world.provider(world.alert()).link == ""


def test_a_broken_query_degrades_to_none_without_raising(world, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("库坏了")

    monkeypatch.setattr(world.annotations, "list_visible", boom)
    assert world.provider(world.alert()) is None


def test_pinned_annotation_is_first_among_candidates(world):
    older = world.annotate("viewer", body_md="后来的")
    world.annotations.set_pinned(older.id, user_id=world.uid("viewer"), pinned=True)
    made = world.provider(world.alert())
    assert made.items[0].pinned is True
