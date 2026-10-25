"""alerts 的写入与 notify_log 的每一行。

两件事分得很开：`alerts` 是「发生了什么」的一条历史，`notify_log` 是「为了把它送出去
试了几次、每次什么结果」。合在一张表里的后果是一次告警三个通道变成三行 alert，
而 `sams alerts` 屏幕上会出现三条一模一样的事。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import Conflict, StorageError, ValidationError
from sams.core.notify import FAILED, NO_CHANNEL, SENT, SUPPRESSED
from sams.storage.repos.alerts import (
    ACKNOWLEDGED,
    DISMISSED,
    HANDLED,
    NEW,
    STATUS,
    AlertRepo,
)
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)

CREATE_KW = {
    "rule_id": "r1",
    "rule_name": "PR 低于 0.4",
    "account_id": "acc1",
    "account_name": "主账户",
    "market": "A",
    "code": "601088",
    "symbol_name": "中国神华",
    "severity": "mid",
    "stage": "入池",
    "value_text": "PR=0.31",
    "threshold_text": "PR≤0.4",
    "action_note": "等明日开盘复核",
    # 时刻是**入参**而不是仓库自己 `now()`：那会让「这条告警属于哪个交易日」在测试里不可控
    "triggered_at": datetime(2026, 10, 20, 14, 30, tzinfo=UTC),
    "channel_ids": ["c1", "c2"],
    "extra": {"pr": 0.31},
}


@pytest.fixture
def clock():
    return FrozenClock(NOW)


@pytest.fixture
def repo(db, clock):
    return AlertRepo(db, clock=clock)


@pytest.fixture
def seeded_user(db):
    return UserRepo(db).create("trader", "correct-horse-battery").id


@pytest.fixture
def other_user(db):
    return UserRepo(db).create("keeper", "correct-horse-battery").id


def test_create_returns_the_row_with_a_generated_id(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    assert alert.id
    assert alert.severity == "mid"
    assert alert.payload["extra"]["pr"] == 0.31


def test_severity_is_validated(repo, seeded_user):
    with pytest.raises(ValidationError, match="severity"):
        repo.create(seeded_user, **{**CREATE_KW, "severity": "urgent"})


def test_a_naive_moment_is_refused(repo, seeded_user):
    """TIMESTAMPTZ 会按会话时区解释裸值：北京 22:00 的一条止损会被记成 UTC 22:00。"""
    with pytest.raises(ValidationError, match="时区"):
        repo.create(
            seeded_user, **{**CREATE_KW, "triggered_at": datetime(2026, 10, 20, 14, 30)}
        )


def test_record_writes_one_row_per_attempt(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.record(alert.id, "c1", SENT, error="", attempts=2)
    repo.record(alert.id, "c2", FAILED, error="企微 errcode=93000", attempts=3)
    rows = repo.deliveries(alert.id)
    # 按通道比，不按顺序比：两条 `record` 落在同一个冻结时刻里，而 `(ts, id)` 里的 id 是
    # 随机串——断言顺序等于断言一个我们不打算提供的承诺（投递次序由 `notify_log` 的行序决定，
    # 那是写入侧的事，不是读侧的排序保证）
    assert {(r.channel_id, r.outcome, r.attempts) for r in rows} == {
        ("c1", SENT, 2),
        ("c2", FAILED, 3),
    }


def test_no_channel_and_dnd_rows_have_no_channel(repo, seeded_user):
    """选路之前就被挡下的那两种结果：channel_id 留 NULL，outcome 说清为什么。

    填一个假 id 会让健康页把压制算进「某个通道的失败率」。反过来，**选路之后**的压制
    （「这台机器发不出去」，附录 H 的 `unavailable` 那两支）带着 `channel_id`：那是关于
    那条通道的事实。`record` 对两种形状都不拦，拦与不拦由调用方决定，仓库不认识「机器」。
    """
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.record(alert.id, None, NO_CHANNEL, error="该用户没有启用任何通道", attempts=0)
    repo.record(alert.id, None, SUPPRESSED, error="23:00-07:00 免打扰（mid）", attempts=0)
    rows = repo.deliveries(alert.id)
    assert all(r.channel_id is None for r in rows)
    # 两条落在同一个冻结时刻里，`(ts, id)` 里的 id 是随机串：断言顺序等于断言一个
    # 不打算提供的承诺（同 `test_record_writes_one_row_per_attempt`）
    assert sorted(r.outcome for r in rows) == [NO_CHANNEL, SUPPRESSED]


def test_a_sent_row_must_name_a_channel(repo, seeded_user):
    """`sent` 却不写发到哪，健康页就数不出「哪个通道在坏」——那一行也没有别的用处。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="channel_id"):
        repo.record(alert.id, None, SENT, error="", attempts=1)


def test_unknown_outcome_is_refused(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="outcome"):
        repo.record(alert.id, "c1", "maybe", error="", attempts=1)


def test_record_of_an_unknown_alert_is_a_storage_error(repo):
    with pytest.raises(StorageError, match="不存在"):
        repo.record("nope", "c1", SENT, error="", attempts=1)


def test_reads_fold_the_null_outcome_to_failed(db, repo, seeded_user):
    """手工插的一行没有 outcome，也不能被读成「没结果」：宁缺勿滥的方向是不冒充成功。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    db.execute(
        "INSERT INTO notify_log (id, alert_id, channel_id, ts, attempt)"
        " VALUES (?, ?, ?, now(), 1)",
        ["raw1", alert.id, "cX"],
    )
    row = next(r for r in repo.deliveries(alert.id) if r.channel_id == "cX")
    assert row.outcome == FAILED


def test_get_carries_the_display_fields(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    got = repo.get(alert.id)
    assert got.rule_name == "PR 低于 0.4"
    assert got.symbol_name == "中国神华"
    assert got.channel_ids == ["c1", "c2"]
    assert got.status == NEW


def test_get_of_an_unknown_alert_is_none(repo):
    assert repo.get("nope") is None


def test_list_for_user_is_newest_first(repo, seeded_user, other_user):
    """两条各给一个**不同**的时刻：同刻的两条谁先谁后由随机 id 决定，
    断言那个顺序等于断言一个不打算提供的承诺。"""
    first = repo.create(seeded_user, **CREATE_KW)
    second = repo.create(
        seeded_user,
        **{
            **CREATE_KW,
            "value_text": "PR=0.29",
            "triggered_at": datetime(2026, 10, 20, 15, 30, tzinfo=UTC),
        },
    )
    repo.create(other_user, **CREATE_KW)
    rows = repo.list_for(seeded_user)
    assert [r.id for r in rows] == [second.id, first.id]


def test_list_for_filters_by_status(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="已手动复核")
    assert repo.list_for(seeded_user, status=NEW) == []
    handled = repo.list_for(seeded_user, status=HANDLED)[0]
    assert handled.status == HANDLED


def test_list_for_filters_by_severity_market_code_and_window(db):
    """收件箱的筛选：条件全部 AND，`since/until` 走 `triggered_at`。

    `limit/offset` 也要在这一个用例里成对出现——分页的错通常是「第二页重复第一页」，
    只测 limit 测不出来。
    """
    repo = AlertRepo(db)
    early = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    late = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
    mid = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
    kw = dict(
        rule_id="r1", rule_name="止损", account_id=None, account_name="",
        symbol_name="贵州茅台", stage="hit", value_text="1500",
        threshold_text="1550", action_note="减半",
    )
    repo.create("u1", market="A", code="600519", severity="strong", triggered_at=early, **kw)
    repo.create("u1", market="HK", code="00700", severity="mid", triggered_at=late,
                rule_id="r2", rule_name="目标价", account_id=None, account_name="",
                symbol_name="腾讯", stage="pre", value_text="380", threshold_text="400",
                action_note="观察")

    assert [a.code for a in repo.list_for("u1")] == ["00700", "600519"]
    assert [a.code for a in repo.list_for("u1", severity="strong")] == ["600519"]
    assert [a.code for a in repo.list_for("u1", market="HK")] == ["00700"]
    assert [a.code for a in repo.list_for("u1", code="600519")] == ["600519"]
    assert [a.code for a in repo.list_for("u1", since=mid)] == ["00700"]
    assert [a.code for a in repo.list_for("u1", until=mid)] == ["600519"]
    assert [a.code for a in repo.list_for("u1", rule_id="r2")] == ["00700"]
    assert repo.list_for("u1", limit=1, offset=1)[0].code == "600519"
    assert repo.list_for("u2") == []


def test_set_status_rejects_an_unknown_one(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="不接受"):
        repo.set_status(alert.id, "archieved", user_id=seeded_user)


def test_a_rule_id_may_be_absent(repo, seeded_user):
    """系统类告警没有规则：`rule_id` 在 0015 之后可空，这里要写得进去也读得回来。"""
    alert = repo.create(seeded_user, **{**CREATE_KW, "rule_id": None})
    assert alert.rule_id is None
    assert repo.get(alert.id).rule_id is None


# --- 冷却与去重的查询（§10.1 的键 / D94 的上一档）------------------------------------------


def seed_hit(
    repo, seeded_user, *, minutes_ago=0, rule="r1", code="601088", account="acc1", stage="pre"
):
    """按「距现在多少分钟」种一条告警：断言里不写日期，跑在什么时候都一样。"""
    from datetime import timedelta

    from sams.core.clock import SYSTEM_CLOCK

    moment = SYSTEM_CLOCK.now_utc() - timedelta(minutes=minutes_ago)
    return repo.create(
        seeded_user,
        **{
            **CREATE_KW,
            "rule_id": rule,
            "account_id": account,
            "code": code,
            "stage": stage,
            "triggered_at": moment,
        },
    )


def test_latest_for_picks_the_newest_row_on_the_dedupe_key(repo, seeded_user):
    older = seed_hit(repo, seeded_user, minutes_ago=120, stage="pre")
    newer = seed_hit(repo, seeded_user, minutes_ago=10, stage="hit")
    seed_hit(repo, seeded_user, minutes_ago=5, rule="r2")  # 别的规则，不算这条的上一档

    # 去重键里账户也占一维（默认种的是 acc1），漏了它问的就是另一条键
    got = repo.latest_for("r1", "A", "601088", account_id="acc1")

    assert got is not None
    assert got.id == newer.id, f"该拿最近那一条，拿到了 {older.id}"
    assert got.stage == "hit"


def test_latest_for_does_not_let_another_accounts_hit_silence_this_one(repo, seeded_user):
    """§1 那张表的第三行：同一只票两个账户，A 的破位不能把 B 的吃掉。"""
    seed_hit(repo, seeded_user, account="acc-a", minutes_ago=5, stage="hit")

    assert repo.latest_for("r1", "A", "601088", account_id="acc-b") is None
    assert repo.latest_for("r1", "A", "601088", account_id="acc-a") is not None


def test_latest_for_matches_a_missing_account(repo, seeded_user):
    """非持仓类规则的 `account_id` 是 NULL，而 SQL 里 `NULL = NULL` 不成立。

    这一枚钉的是 DuckDB 那一侧：写成 `account_id = ?` 时这类规则永远查不到自己的上一次
    命中，于是每一趟都「穿过冷却」——症状是同一条止损每跑一次重发一遍，而库里全是它
    自己刚写的那些行。
    """
    seed_hit(repo, seeded_user, account=None, minutes_ago=30, stage="pre")

    assert repo.latest_for("r1", "A", "601088") is not None
    assert repo.latest_for("r1", "A", "601088", account_id=None) is not None


def test_latest_for_is_empty_when_nothing_was_raised(repo, seeded_user):
    assert repo.latest_for("r1", "A", "000001") is None


# --- 刷屏的查询（§10.1 的 `no_open_alert`：同票同人，不管账户）------------------------------


def test_latest_open_finds_the_newest_unhandled_alert_on_that_symbol(repo, seeded_user) -> None:
    """三条各给不同时刻，并且**别只种一条 `new`**：只留一条时「取最新」这半句没被钉住。

    另一只票那一条是三者里最新的——漏了 `code` 这一维就会拿到它，而不是这一只票上那条。
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    first = repo.create(
        user_id=seeded_user,
        **{
            **CREATE_KW,
            "stage": "pre",
            "triggered_at": now - timedelta(minutes=30),
        },
    )
    repo.set_status(first.id, HANDLED, user_id=seeded_user)
    # 同票上**还有一条更早的 `new`**：只留一条 `new` 的话，删掉 `ORDER BY` 也全绿，
    # 「取最新」这半句根本没被钉住。
    repo.create(
        user_id=seeded_user,
        **{
            **CREATE_KW,
            "stage": "pre",
            "triggered_at": now - timedelta(days=3),
        },
    )
    second = repo.create(
        user_id=seeded_user,
        **{
            **CREATE_KW,
            "stage": "hit",
            "triggered_at": now - timedelta(minutes=10),
        },
    )
    # 另一只票的一条，且更新：不该被算进来
    repo.create(
        user_id=seeded_user,
        **{
            **CREATE_KW,
            "code": "600519",
            "stage": "urgent",
            "triggered_at": now,
        },
    )
    found = repo.latest_open(seeded_user, CREATE_KW["market"], CREATE_KW["code"])
    assert found is not None and found.id == second.id and found.stage == "hit"


def test_latest_open_returns_none_once_the_last_one_is_dismissed(repo, seeded_user) -> None:
    got = repo.create(user_id=seeded_user, **CREATE_KW)
    assert repo.latest_open(seeded_user, CREATE_KW["market"], CREATE_KW["code"]) is not None
    repo.set_status(got.id, DISMISSED, user_id=seeded_user, note="误报")
    assert repo.latest_open(seeded_user, CREATE_KW["market"], CREATE_KW["code"]) is None


def test_latest_open_keeps_an_acknowledged_alert_open(repo, seeded_user, db) -> None:
    """已读不等于已处置：点一下「已读」不能让同票的其他规则重新开火（M4-B §8.1）。"""
    got = repo.create(user_id=seeded_user, **CREATE_KW)
    db.execute("UPDATE alerts SET status = ? WHERE id = ?", [ACKNOWLEDGED, got.id])
    found = repo.latest_open(seeded_user, CREATE_KW["market"], CREATE_KW["code"])
    assert found is not None and found.id == got.id


def test_latest_open_ignores_another_users_alerts(repo, seeded_user, other_user) -> None:
    """`no_open_alert` 挡的是「这个人已经有一条没点掉的」：别人的那条不算（§10.1）。"""
    repo.create(user_id=other_user, **CREATE_KW)
    assert repo.latest_open(seeded_user, CREATE_KW["market"], CREATE_KW["code"]) is None


# ---------- 人做了什么：那三列第一次有人读 ----------


def test_the_three_columns_a_human_writes_come_back_out(repo, seeded_user):
    """`handled_at`/`handle_note`/`dismiss_reason` 从 0001 就在库里，而 `Alert` 读不出它们。

    读不出就等于屏幕上没有「什么时候按的、留了哪一句」，那一句只能靠记忆——而记忆撑不过第二次复盘。
    """
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="已挂条件单")

    assert moved.status == HANDLED
    assert moved.handle_note == "已挂条件单"
    assert moved.dismiss_reason is None
    assert moved.handled_at is not None


def test_a_dismissal_keeps_its_reason_in_its_own_column(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.set_status(
        alert.id, DISMISSED, user_id=seeded_user, note="停牌导致的假跌破"
    )

    assert moved.dismiss_reason == "停牌导致的假跌破"
    assert moved.handle_note is None, "两句不许同时写同一句话"


def test_dismissing_without_a_reason_is_refused(repo, seeded_user):
    """§5 那句「dismissed 必须带 reason（喂给忽略率统计）」，落点在仓库层。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="原因"):
        repo.set_status(alert.id, DISMISSED, user_id=seeded_user)

    assert repo.get(alert.id).status == NEW, "拒绝不许留下一半的变更"


def test_a_blank_reason_is_not_a_reason(repo, seeded_user):
    """空格与空串是同一种东西：那一列要能被统计，收下一句空白就是收下噪音。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="原因"):
        repo.set_status(alert.id, DISMISSED, user_id=seeded_user, note="   ")


def test_someone_elses_alert_is_not_yours_to_move(repo, seeded_user, other_user):
    """归属闸放在仓库层（D81）：调度器与后台都不经过命令层，挡在命令层对它们是空的。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(StorageError, match="不属于"):
        repo.set_status(alert.id, HANDLED, user_id=other_user, note="手滑")

    assert repo.get(alert.id).status == NEW


def test_changing_your_mind_replaces_the_previous_sentence(repo, seeded_user):
    """按错了要能改，但「人对这条做过什么」在屏幕上只能有一个答案。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="准备挂单")
    moved = repo.set_status(
        alert.id, DISMISSED, user_id=seeded_user, note="后来决定这轮不动"
    )

    assert moved.handle_note is None
    assert moved.dismiss_reason == "后来决定这轮不动"


def test_a_known_status_still_needs_a_known_alert(repo, seeded_user):
    with pytest.raises(StorageError, match="不存在"):
        repo.set_status("ghost", HANDLED, user_id=seeded_user, note="x")


def test_handling_does_not_stamp_a_moment_for_a_new_one(repo, seeded_user):
    """`new` 没有「处理时刻」：给它盖一个，屏幕上那句「何时处理的」就成了谎。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="看了")
    back = repo.set_status(alert.id, NEW, user_id=seeded_user)

    assert moved.handled_at is not None
    assert back.handled_at is None and back.status == NEW


# ---------- 按用户读（屏幕上那串 id 不能变成一个跨用户的读取口）----------


def test_get_can_scope_to_a_user(repo, seeded_user, other_user):
    """`alert show <id>` 的输入就是那串 id：不挡就等于任何人问任何人都能读。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    assert repo.get(alert.id, user_id=seeded_user) is not None
    assert repo.get(alert.id, user_id=other_user) is None


def test_get_without_a_user_still_reads_any_row(repo, seeded_user):
    """默认不挡，且这一条要测住：`record`/`set_status` 内部那次读靠的就是它。

    把默认改成「挡」不会报错，只会让那两处把自己的告警读成不存在。
    """
    alert = repo.create(seeded_user, **CREATE_KW)
    assert repo.get(alert.id) is not None


# ---------- 系统告警的冷却（D102：同组件同人一个窗口）----------


def test_has_system_alert_matches_one_admin_one_component(repo, seeded_user, other_user):
    system = {
        **CREATE_KW,
        "rule_id": None,
        "account_id": None,
        "market": "SYS",
        "code": "calendar",
    }
    repo.create(seeded_user, **system)

    since = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is True
    assert repo.has_system_alert(other_user, "calendar", since=since) is False
    assert repo.has_system_alert(seeded_user, "datasource:akshare", since=since) is False
    assert (
        repo.has_system_alert(
            seeded_user, "calendar", since=datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
        )
        is False
    )


def test_has_system_alert_counts_the_boundary_moment_as_inside(repo, seeded_user):
    """窗口在仓库层也是闭的：`triggered_at == since` 的那一行算「刚告过」。

    只在服务层钉这一条不够——那里隔着收件人筛选与消息构造，把 `>=` 写成 `>` 时
    屏幕上留下的是一条没有解释的沉默，而这一层的一问一答必须自己说清边界归谁。
    """
    moment = datetime(2026, 10, 20, 14, 30, tzinfo=UTC)
    repo.create(
        seeded_user,
        **{
            **CREATE_KW,
            "rule_id": None,
            "account_id": None,
            "market": "SYS",
            "code": "calendar",
            "triggered_at": moment,
        },
    )

    assert repo.has_system_alert(seeded_user, "calendar", since=moment) is True
    # 窗口右端挪一秒，那一行就落在外面：闭区间不是「全都算」
    assert (
        repo.has_system_alert(seeded_user, "calendar", since=moment + timedelta(seconds=1))
        is False
    )


def test_has_system_alert_does_not_mistake_a_stock_alert_for_a_component(repo, seeded_user):
    repo.create(seeded_user, **{**CREATE_KW, "code": "calendar"})

    since = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is False


def test_has_system_alert_needs_both_halves_of_the_system_shape(repo, seeded_user):
    """`market = 'SYS'` 与 `rule_id IS NULL` 是两个条件，所以要一行只缺半边的证据。

    只种形状完整的那一行时，删掉任一条件用例都还是绿的；缺一边的那两种行都会被当成
    冷却的起点，于是本该出声的那一次被按下——而屏幕上留下的是一行看不出来的沉默。
    """
    since = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    # SYS 市场、组件名对，但挂着 `rule_id`：那是规则告警，不是系统告警
    repo.create(seeded_user, **{**CREATE_KW, "market": "SYS", "code": "calendar"})
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is False

    # 反过来：没有 `rule_id`，但市场是普通的 A（一条恰好把 `code` 写成组件名的规则告警）
    repo.create(seeded_user, **{**CREATE_KW, "rule_id": None, "code": "calendar"})
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is False

    # 两半都齐才算：同一句查询的正例
    system = {"rule_id": None, "account_id": None, "market": "SYS", "code": "calendar"}
    repo.create(seeded_user, **{**CREATE_KW, **system})
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is True


# ---------- 四态与闭环六列（M4-B §8.1）----------


def test_status_vocabulary_grows_one_middle_state():
    """ack 与 acted 分开，才能回答「看到了但没做」与「根本没看到」（D124）。"""
    assert STATUS == ("new", "acknowledged", "handled", "dismissed")


def test_create_carries_the_thesis_snapshot(repo, seeded_user):
    """规则经观点展开时留下 `thesis_id`：推送上下文与详情跳转都要它，但它不是归属键。"""
    alert = repo.create(seeded_user, thesis_id="t1", **CREATE_KW)
    assert alert.thesis_id == "t1"


def test_list_for_accepts_the_new_status(repo, seeded_user, db):
    alert = repo.create(seeded_user, **CREATE_KW)
    db.execute("UPDATE alerts SET status = ? WHERE id = ?", [ACKNOWLEDGED, alert.id])
    assert [row.id for row in repo.list_for(seeded_user, status=ACKNOWLEDGED)] == [alert.id]


def test_acknowledge_marks_it_read_without_closing_it(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.acknowledge(alert.id, user_id=seeded_user)
    assert moved.status == ACKNOWLEDGED
    assert moved.acknowledged_at == clock.moment
    assert moved.handled_at is None
    assert [e.action for e in repo.events.list_for(alert.id)] == ["ack"]


def test_acknowledge_is_idempotent(repo, seeded_user, clock):
    """手机连点两下不该留下两条「已读」事件。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    first = repo.acknowledge(alert.id, user_id=seeded_user)
    clock.advance(seconds=9)
    second = repo.acknowledge(alert.id, user_id=seeded_user)
    assert second.acknowledged_at == first.acknowledged_at
    assert len(repo.events.list_for(alert.id)) == 1


def test_a_racing_ack_does_not_write_a_second_event(tmp_path, monkeypatch):
    """另一个连接先按掉了这一条：本连接的条件更新命中 0 行，不能再补一条历史。"""
    from sams.storage.db import Database
    from sams.storage.migrate import apply_migrations

    path = str(tmp_path / "race.duckdb")
    db = Database.open(path)
    apply_migrations(db)
    user = UserRepo(db).create("racer", "correct-horse-battery").id
    clock = FrozenClock(NOW)
    repo = AlertRepo(db, clock=clock)
    alert = repo.create(user, **CREATE_KW)
    other = Database.open(path)

    def racing():
        other.execute(
            "UPDATE alerts SET status = 'acknowledged', acknowledged_at = ? WHERE id = ?",
            [clock.moment, alert.id],
        )
        other.execute(
            "INSERT INTO alert_events"
            " (id, alert_id, user_id, action, from_status, to_status, note, ts)"
            " VALUES ('e-other', ?, ?, 'ack', 'new', 'acknowledged', '', ?)",
            [alert.id, user, clock.moment],
        )
        return clock.moment

    monkeypatch.setattr(clock, "now_utc", racing)
    moved = repo.acknowledge(alert.id, user_id=user)
    assert moved.status == ACKNOWLEDGED
    assert [e.id for e in repo.events.list_for(alert.id)] == ["e-other"]
    other.close()


def test_acknowledge_refuses_a_closed_alert(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="已挂单")
    with pytest.raises(Conflict, match="已经是 handled"):
        repo.acknowledge(alert.id, user_id=seeded_user)


def test_an_acknowledged_alert_still_blocks_a_second_fire(repo, seeded_user):
    """已读不等于已处置：同票第二条规则不该因为点了一下「已读」就重新开火。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.acknowledge(alert.id, user_id=seeded_user)
    assert repo.latest_open(seeded_user, "A", "601088").id == alert.id


def test_act_requires_an_action_kind(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="action_kind"):
        repo.act(alert.id, user_id=seeded_user, action_kind="done", note="看过了")


def test_act_with_trade_reference_closes_the_alert(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.act(
        alert.id, user_id=seeded_user, action_kind="trade", action_ref="o1", note="补记卖出"
    )
    assert moved.status == HANDLED
    assert moved.action_kind == "trade" and moved.action_ref == "o1"
    assert moved.handled_at == clock.moment
    assert moved.handle_note == "补记卖出"
    events = repo.events.list_for(alert.id)
    assert [(e.action, e.from_status, e.to_status, e.note) for e in events] == [
        ("act", "new", "handled", "trade:o1 补记卖出")
    ]


def test_act_from_acknowledged_records_both_events(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.acknowledge(alert.id, user_id=seeded_user)
    repo.act(alert.id, user_id=seeded_user, action_kind="watch",
             note="加自选等回踩")
    assert [e.action for e in repo.events.list_for(alert.id)] == ["ack", "act"]


def test_a_referencing_action_requires_its_reference(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    for kind in ("trade", "thesis", "rule"):
        with pytest.raises(ValidationError, match="引用"):
            repo.act(alert.id, user_id=seeded_user, action_kind=kind, note="x")


def test_other_needs_a_sentence(repo, seeded_user):
    """「已处置」如果只是一句话，复盘时仍然不知道做了什么（D125）。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="说明"):
        repo.act(alert.id, user_id=seeded_user, action_kind="other", note=" ")


def test_a_terminal_alert_refuses_a_second_action(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.act(alert.id, user_id=seeded_user, action_kind="other", note="已处理")
    with pytest.raises(Conflict):
        repo.act(alert.id, user_id=seeded_user, action_kind="other", note="再处理")


def test_a_terminal_alert_refuses_a_dismiss(repo, seeded_user):
    """忽略与处置是两种结论：关上之后换一个原因码再按一次不是改主意，是覆盖历史。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.dismiss(alert.id, user_id=seeded_user, code="duplicate")
    with pytest.raises(Conflict):
        repo.dismiss(alert.id, user_id=seeded_user, code="expected")


def test_dismiss_requires_a_code(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="dismiss_code"):
        repo.dismiss(alert.id, user_id=seeded_user, code="", reason="看过")


def test_misreport_and_other_need_a_reason(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    for code in ("misreport", "other"):
        with pytest.raises(ValidationError, match="说明"):
            repo.dismiss(alert.id, user_id=seeded_user, code=code, reason="")


def test_dismiss_records_the_code_and_reason(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.dismiss(
        alert.id, user_id=seeded_user, code="expected", reason="财报前的正常波动"
    )
    assert moved.status == DISMISSED
    assert moved.dismiss_code == "expected"
    assert moved.dismiss_reason == "财报前的正常波动"
    assert moved.handled_at is not None
    events = repo.events.list_for(alert.id)
    assert [(e.from_status, e.to_status, e.note) for e in events] == [
        ("new", "dismissed", "expected 财报前的正常波动")
    ]


def test_a_racing_terminal_action_writes_no_event(tmp_path, monkeypatch):
    """另一个连接先把这条关了：本连接的条件更新命中 0 行，不能再补一条历史。"""
    from sams.storage.db import Database
    from sams.storage.migrate import apply_migrations

    path = str(tmp_path / "race.duckdb")
    db = Database.open(path)
    apply_migrations(db)
    user = UserRepo(db).create("racer", "correct-horse-battery").id
    clock = FrozenClock(NOW)
    repo = AlertRepo(db, clock=clock)
    alert = repo.create(user, **CREATE_KW)
    other = Database.open(path)

    def racing():
        other.execute(
            "UPDATE alerts SET status = 'handled', handled_at = ? WHERE id = ?",
            [clock.moment, alert.id],
        )
        return clock.moment

    monkeypatch.setattr(clock, "now_utc", racing)
    with pytest.raises(Conflict, match="操作期间"):
        repo.act(alert.id, user_id=user, action_kind="other", note="已处理")
    assert repo.events.list_for(alert.id) == []
    other.close()


def test_act_after_a_racing_ack_does_not_record_a_stale_from_status(
    tmp_path, monkeypatch
):
    """另一个连接在 `_owned` 与 UPDATE 之间按了已读：条件是「观测到的那一态」。

    放宽成 `status IN ('new', 'acknowledged')` 时，UPDATE 照样命中，事件却写成
    new→handled——而历史里上一条说的是 new→acknowledged，两行互相矛盾。
    """
    from sams.storage.db import Database
    from sams.storage.migrate import apply_migrations

    path = str(tmp_path / "race.duckdb")
    db = Database.open(path)
    apply_migrations(db)
    user = UserRepo(db).create("racer", "correct-horse-battery").id
    clock = FrozenClock(NOW)
    repo = AlertRepo(db, clock=clock)
    alert = repo.create(user, **CREATE_KW)
    other = Database.open(path)

    def racing():
        other.execute(
            "UPDATE alerts SET status = 'acknowledged', acknowledged_at = ? WHERE id = ?",
            [clock.moment, alert.id],
        )
        other.execute(
            "INSERT INTO alert_events"
            " (id, alert_id, user_id, action, from_status, to_status, note, ts)"
            " VALUES ('e-ack', ?, ?, 'ack', 'new', 'acknowledged', '', ?)",
            [alert.id, user, clock.moment],
        )
        return clock.moment

    monkeypatch.setattr(clock, "now_utc", racing)
    with pytest.raises(Conflict, match="操作期间"):
        repo.act(alert.id, user_id=user, action_kind="watch", note="加自选")
    assert [e.id for e in repo.events.list_for(alert.id)] == ["e-ack"]
    other.close()


def test_ack_after_a_racing_close_raises_instead_of_returning_a_closed_alert(
    tmp_path, monkeypatch
):
    """输给终态时报错，不能把 handled 那一行当成「已读成功」返回。"""
    from sams.storage.db import Database
    from sams.storage.migrate import apply_migrations

    path = str(tmp_path / "race.duckdb")
    db = Database.open(path)
    apply_migrations(db)
    user = UserRepo(db).create("racer", "correct-horse-battery").id
    clock = FrozenClock(NOW)
    repo = AlertRepo(db, clock=clock)
    alert = repo.create(user, **CREATE_KW)
    other = Database.open(path)

    def racing():
        other.execute(
            "UPDATE alerts SET status = 'handled', handled_at = ? WHERE id = ?",
            [clock.moment, alert.id],
        )
        return clock.moment

    monkeypatch.setattr(clock, "now_utc", racing)
    with pytest.raises(Conflict, match="操作期间"):
        repo.acknowledge(alert.id, user_id=user)
    assert repo.events.list_for(alert.id) == []
    other.close()


# ---------- 稍后提醒与撤销终态（M4-B §8.1）----------


def test_defer_keeps_the_status_and_sets_a_wakeup(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    until = clock.moment + timedelta(hours=1)
    moved = repo.defer(alert.id, user_id=seeded_user, until=until)
    assert moved.status == NEW
    assert moved.deferred_until == until
    event = repo.events.list_for(alert.id)[0]
    assert event.action == "defer" and event.from_status == event.to_status == NEW


def test_a_past_wakeup_is_refused(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="未来"):
        repo.defer(alert.id, user_id=seeded_user, until=clock.moment)


def test_a_naive_wakeup_is_refused(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="时区"):
        repo.defer(alert.id, user_id=seeded_user, until=datetime(2026, 10, 26, 1, 0))


def test_defer_refuses_a_terminal_alert(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.dismiss(alert.id, user_id=seeded_user, code="expected")
    with pytest.raises(Conflict):
        repo.defer(alert.id, user_id=seeded_user, until=clock.moment + timedelta(hours=1))


def test_defer_from_acknowledged_keeps_the_middle_state(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.acknowledge(alert.id, user_id=seeded_user)
    until = clock.moment + timedelta(hours=1)
    moved = repo.defer(alert.id, user_id=seeded_user, until=until)
    assert moved.status == ACKNOWLEDGED
    event = repo.events.list_for(alert.id)[-1]
    assert (event.action, event.from_status, event.to_status) == (
        "defer", ACKNOWLEDGED, ACKNOWLEDGED,
    )
    assert event.note == until.isoformat()


def test_undo_returns_to_the_previous_state_and_clears_the_conclusion(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    acked_at = repo.acknowledge(alert.id, user_id=seeded_user).acknowledged_at
    repo.act(alert.id, user_id=seeded_user, action_kind="watch", note="等回踩")
    clock.advance(minutes=1)
    moved = repo.undo(alert.id, user_id=seeded_user)
    assert moved.status == ACKNOWLEDGED
    assert moved.acknowledged_at == acked_at
    assert moved.handled_at is None
    assert moved.action_kind is None and moved.action_ref is None
    assert moved.deferred_until is None
    assert [e.action for e in repo.events.list_for(alert.id)] == ["ack", "act", "undo"]


def test_undo_to_new_clears_the_acknowledgement(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.dismiss(alert.id, user_id=seeded_user, code="duplicate")
    moved = repo.undo(alert.id, user_id=seeded_user)
    assert moved.status == NEW
    assert moved.acknowledged_at is None
    assert moved.dismiss_code is None and moved.dismiss_reason is None
    assert moved.handled_at is None
    assert [e.action for e in repo.events.list_for(alert.id)] == ["dismiss", "undo"]


def test_undo_after_five_minutes_is_refused(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.dismiss(alert.id, user_id=seeded_user, code="expected")
    clock.advance(minutes=5)
    with pytest.raises(Conflict, match="5 分钟"):
        repo.undo(alert.id, user_id=seeded_user)


def test_undo_refuses_when_a_legacy_close_is_newer_than_the_recorded_action(
    repo, seeded_user
):
    """处置过、撤销过、又已读，然后旧入口关了它：最新事件是 ack，
    不能拿更早那条 act 的 from_status 猜上一态（旧实现会回退到 new）。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.act(alert.id, user_id=seeded_user, action_kind="watch", note="先处置")
    repo.undo(alert.id, user_id=seeded_user)
    repo.acknowledge(alert.id, user_id=seeded_user)
    repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="旧入口")
    with pytest.raises(Conflict, match="动作记录"):
        repo.undo(alert.id, user_id=seeded_user)


def test_undo_needs_a_recorded_transition(repo, seeded_user):
    """旧入口 set_status 不写事件：那条告警没有可信的上一态，不许猜。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="旧入口")
    with pytest.raises(Conflict, match="动作记录"):
        repo.undo(alert.id, user_id=seeded_user)


def test_undo_refuses_an_open_alert(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(Conflict):
        repo.undo(alert.id, user_id=seeded_user)


def test_a_racing_close_makes_defer_a_conflict_without_an_event(tmp_path, monkeypatch):
    """另一个连接在 `_owned` 与 UPDATE 之间把这条关了：条件更新命中 0 行，不补历史。"""
    from sams.storage.db import Database
    from sams.storage.migrate import apply_migrations

    path = str(tmp_path / "race.duckdb")
    db = Database.open(path)
    apply_migrations(db)
    user = UserRepo(db).create("racer", "correct-horse-battery").id
    clock = FrozenClock(NOW)
    repo = AlertRepo(db, clock=clock)
    alert = repo.create(user, **CREATE_KW)
    other = Database.open(path)

    def racing():
        other.execute(
            "UPDATE alerts SET status = 'handled', handled_at = ? WHERE id = ?",
            [clock.moment, alert.id],
        )
        return clock.moment

    monkeypatch.setattr(clock, "now_utc", racing)
    with pytest.raises(Conflict, match="操作期间"):
        repo.defer(alert.id, user_id=user, until=clock.moment + timedelta(hours=1))
    assert repo.events.list_for(alert.id) == []
    other.close()
