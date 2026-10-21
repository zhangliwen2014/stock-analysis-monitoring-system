"""alerts 的写入与 notify_log 的每一行。

两件事分得很开：`alerts` 是「发生了什么」的一条历史，`notify_log` 是「为了把它送出去
试了几次、每次什么结果」。合在一张表里的后果是一次告警三个通道变成三行 alert，
而 `sams alerts` 屏幕上会出现三条一模一样的事。
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    repo.set_status(alert.id, "handled", note="已手动复核")
    assert repo.list_for(seeded_user, status="new") == []
    handled = repo.list_for(seeded_user, status="handled")[0]
    assert handled.status == "handled"


def test_set_status_rejects_an_unknown_one(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="状态"):
        repo.set_status(alert.id, "archieved")


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
    repo.set_status(first.id, "handled")
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
    repo.set_status(got.id, "dismissed", note="误报")
    assert repo.latest_open(seeded_user, CREATE_KW["market"], CREATE_KW["code"]) is None


def test_latest_open_ignores_another_users_alerts(repo, seeded_user, other_user) -> None:
    """`no_open_alert` 挡的是「这个人已经有一条没点掉的」：别人的那条不算（§10.1）。"""
    repo.create(user_id=other_user, **CREATE_KW)
    assert repo.latest_open(seeded_user, CREATE_KW["market"], CREATE_KW["code"]) is None
