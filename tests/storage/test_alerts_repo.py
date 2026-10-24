"""alerts 的写入与 notify_log 的每一行。

两件事分得很开：`alerts` 是「发生了什么」的一条历史，`notify_log` 是「为了把它送出去
试了几次、每次什么结果」。合在一张表里的后果是一次告警三个通道变成三行 alert，
而 `sams alerts` 屏幕上会出现三条一模一样的事。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sams.core.errors import StorageError, ValidationError
from sams.core.notify import FAILED, NO_CHANNEL, SENT, SUPPRESSED
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.users import UserRepo

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
def repo(db):
    return AlertRepo(db)


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
    assert [r.outcome for r in rows] == [NO_CHANNEL, SUPPRESSED]


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
    assert got.status == "new"


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
    repo.set_status(alert.id, "handled", user_id=seeded_user, note="已手动复核")
    assert repo.list_for(seeded_user, status="new") == []
    handled = repo.list_for(seeded_user, status="handled")[0]
    assert handled.status == "handled"


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
    with pytest.raises(ValidationError, match="状态"):
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
    repo.set_status(first.id, "handled", user_id=seeded_user)
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
    repo.set_status(got.id, "dismissed", user_id=seeded_user, note="误报")
    assert repo.latest_open(seeded_user, CREATE_KW["market"], CREATE_KW["code"]) is None


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
    moved = repo.set_status(alert.id, "handled", user_id=seeded_user, note="已挂条件单")

    assert moved.status == "handled"
    assert moved.handle_note == "已挂条件单"
    assert moved.dismiss_reason is None
    assert moved.handled_at is not None


def test_a_dismissal_keeps_its_reason_in_its_own_column(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.set_status(
        alert.id, "dismissed", user_id=seeded_user, note="停牌导致的假跌破"
    )

    assert moved.dismiss_reason == "停牌导致的假跌破"
    assert moved.handle_note is None, "两句不许同时写同一句话"


def test_dismissing_without_a_reason_is_refused(repo, seeded_user):
    """§5 那句「dismissed 必须带 reason（喂给忽略率统计）」，落点在仓库层。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="原因"):
        repo.set_status(alert.id, "dismissed", user_id=seeded_user)

    assert repo.get(alert.id).status == "new", "拒绝不许留下一半的变更"


def test_a_blank_reason_is_not_a_reason(repo, seeded_user):
    """空格与空串是同一种东西：那一列要能被统计，收下一句空白就是收下噪音。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="原因"):
        repo.set_status(alert.id, "dismissed", user_id=seeded_user, note="   ")


def test_someone_elses_alert_is_not_yours_to_move(repo, seeded_user, other_user):
    """归属闸放在仓库层（D81）：调度器与后台都不经过命令层，挡在命令层对它们是空的。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(StorageError, match="不属于"):
        repo.set_status(alert.id, "handled", user_id=other_user, note="手滑")

    assert repo.get(alert.id).status == "new"


def test_changing_your_mind_replaces_the_previous_sentence(repo, seeded_user):
    """按错了要能改，但「人对这条做过什么」在屏幕上只能有一个答案。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.set_status(alert.id, "handled", user_id=seeded_user, note="准备挂单")
    moved = repo.set_status(
        alert.id, "dismissed", user_id=seeded_user, note="后来决定这轮不动"
    )

    assert moved.handle_note is None
    assert moved.dismiss_reason == "后来决定这轮不动"


def test_a_known_status_still_needs_a_known_alert(repo, seeded_user):
    with pytest.raises(StorageError, match="不存在"):
        repo.set_status("ghost", "handled", user_id=seeded_user, note="x")


def test_handling_does_not_stamp_a_moment_for_a_new_one(repo, seeded_user):
    """`new` 没有「处理时刻」：给它盖一个，屏幕上那句「何时处理的」就成了谎。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.set_status(alert.id, "handled", user_id=seeded_user, note="看了")
    back = repo.set_status(alert.id, "new", user_id=seeded_user)

    assert moved.handled_at is not None
    assert back.handled_at is None and back.status == "new"


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


