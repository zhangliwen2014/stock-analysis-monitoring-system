"""`sams alert ...` 那一屏（§5 复盘闭环里命令行能做的那一半）。

这一整个模块只读 `alerts`、`notify_log`、`channels`、`users` 四张表：它不碰行情、不碰观点，
所以容器是那个「空世界」（`with_bars=False, with_fundamentals=False`）——用例里出现的每一个数
都是这一条链自己写进库的，没有一处靠「样本库恰好是那串 K 线」。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from support_valuation import make_container

import sams.cli as cli

USER = "trader"
OTHER = "keeper"
PWD = "correct-horse-battery"
T0 = datetime(2024, 10, 26, 15, 0, tzinfo=UTC)
LATER = datetime(2024, 10, 28, 9, 30, tzinfo=UTC)


@pytest.fixture
def container(db, settings):
    c = make_container(db, settings, with_bars=False, with_fundamentals=False)
    c.users.create(USER, PWD)
    c.users.create(OTHER, PWD)
    return c


def uid(container, name: str = USER) -> str:
    return container.users.get_by_username(name).id


def invoke(container, *extra: str) -> int:
    args = cli.build_parser().parse_args(["alert", *extra])
    return args.handler(container, args)


def out(container, capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()
    code = invoke(container, *extra)
    return code, capsys.readouterr().out


def payload(container, capsys, *extra: str) -> tuple[int, dict]:
    code, text = out(container, capsys, *extra, "--json")
    return code, json.loads(text)


def make_alert(container, *, user: str = USER, **over):
    """默认建一条「刚跌破止损、没人处理过」的告警：用例只改自己要改的那一两个键。"""
    kw: dict = {
        "rule_id": "rule-1",
        "rule_name": "跌破止损",
        "account_id": "acct-1",
        "account_name": "模拟盘",
        "market": "A",
        "code": "000001",
        "symbol_name": "平安银行",
        "severity": "strong",
        "stage": "破位",
        "value_text": "9.80 CNY",
        "threshold_text": "10.00 CNY",
        "action_note": "按纪律减半仓",
        "triggered_at": T0,
        "extra": {"body": "绝对价止损：现价 9.80 CNY 已跌破 10.00 CNY\n这笔浮亏 -2.0%"},
    }
    kw.update(over)
    return container.alerts_store.create(uid(container, user), **kw)


def add_channel(container, name: str = "桌面通知"):
    return container.channels_store.create(uid(container), "desktop", name, {})


# ---------- list ----------


def test_list_shows_only_unhandled_by_default(container, capsys) -> None:
    """默认只看 `new`：复盘时手里要的是一份「还欠谁的」，不是全部历史。"""
    first = make_alert(container, code="000001")
    make_alert(container, code="601088", symbol_name="中国神华")
    container.alerts_store.set_status(first.id, "handled", user_id=uid(container), note="已复核")
    code, text = out(container, capsys, "list", "--user", USER)
    assert code == 0
    assert "A/601088" in text and "A/000001" not in text
    assert "未处理的告警 1 条" in text


def test_list_status_all_shows_every_state(container, capsys) -> None:
    make_alert(container, code="000001")
    make_alert(container, code="601088")
    code, text = out(container, capsys, "list", "--user", USER, "--status", "all")
    assert code == 0
    assert "名下全部状态的告警 2 条" in text


def test_list_status_handled_says_so_in_chinese(container, capsys) -> None:
    """库里那一列是英文，屏幕上是中文：两边都要——人靠中文读，机器靠英文查。"""
    alert = make_alert(container)
    container.alerts_store.set_status(alert.id, "handled", user_id=uid(container))
    code, text = out(container, capsys, "list", "--user", USER, "--status", "handled")
    assert code == 0
    assert "名下已处理的告警 1 条" in text
    assert alert.id in text


def test_list_json_filter_names_the_state_it_read(container, capsys) -> None:
    """`--status` 在命令层折过一次（`all`→None、没给→new）：JSON 里要说回折完的那一个。

    机器读的人靠 `filter` 判断这一批是不是他要的那一批：说成默认的那个，
    一份「已处理」的清单会被读成「没人看过」。
    """
    alert = make_alert(container)
    container.alerts_store.set_status(alert.id, "handled", user_id=uid(container))
    _, body = payload(container, capsys, "list", "--user", USER, "--status", "handled")
    assert body["filter"] == "handled"
    _, body = payload(container, capsys, "list", "--user", USER, "--status", "all")
    assert body["filter"] == "all"


def test_list_unknown_status_is_refused_with_two(container, capsys) -> None:
    """`--status archieved` 是敲错，不是空结果：exit 2（D77），不是 0 那句「没有」。"""
    code, text = out(container, capsys, "list", "--user", USER, "--status", "archieved")
    assert code == 2
    assert "只有" in text
    _, body = payload(container, capsys, "list", "--user", USER, "--status", "archieved")
    assert body["status"] == "refused" and body["exit"] == 2


def test_list_the_backlog_line_on_health_advertises_flags_that_exist(container, capsys) -> None:
    """健康页那句积压行写的是 `sams alert list --user … --status new`：屏幕上的命令必须真跑得通。

    这一枚钉的是两句屏之间的接缝：健康页改了措辞、这一屏改了旗标名，任一边单独看都是绿的。
    """
    make_alert(container)
    code, text = out(container, capsys, "list", "--user", USER, "--status", "new")
    assert code == 0
    assert "名下未处理的告警 1 条" in text


def test_list_truncates_and_says_there_is_more(container, capsys) -> None:
    for index in range(3):
        make_alert(container, code=f"00000{index}")
    code, text = out(container, capsys, "list", "--user", USER, "--limit", "2")
    assert code == 0
    assert "告警 2 条（最新 2 条之外还有更多）" in text


def test_list_without_truncation_does_not_mention_more(container, capsys) -> None:
    """那句「还有更多」不许白说：读的人据此决定要不要加大 `--limit`。"""
    make_alert(container)
    _, text = out(container, capsys, "list", "--user", USER, "--limit", "2")
    assert "还有更多" not in text


def test_list_limit_must_be_positive(container, capsys) -> None:
    """0 会让屏幕上那句「0 条」与「没有告警」混成一句谎，所以它不是「不限」的写法。"""
    code, _ = out(container, capsys, "list", "--user", USER, "--limit", "0")
    assert code == 2


def test_list_limit_is_an_integer_argparse_rejects(container, capsys) -> None:
    """`type=int` 的失败由 argparse 兜（`SystemExit` 2），不进我们的 handler。"""
    with pytest.raises(SystemExit) as got:
        invoke(container, "list", "--user", USER, "--limit", "很多")
    assert got.value.code == 2


def test_list_empty_tells_you_two_ways_out(container, capsys) -> None:
    """「没有」有两种：没跑过、全处理完了。这一屏认不出是哪一种，所以两种出路都给。"""
    code, text = out(container, capsys, "list", "--user", USER)
    assert code == 0
    assert "没有未处理的告警" in text
    assert "sams rule check" in text and "--status all" in text


def test_list_does_not_show_another_users_alert(container, capsys) -> None:
    """跨用户读取在这条链上没有任何合法场景，而它是默认形状。"""
    make_alert(container, user=OTHER, code="000001")
    _, mine = out(container, capsys, "list", "--user", USER)
    assert "A/000001" not in mine
    _, theirs = out(container, capsys, "list", "--user", OTHER)
    assert "A/000001" in theirs


def test_list_unknown_user_is_refused(container, capsys) -> None:
    code, text = out(container, capsys, "list", "--user", "ghost")
    assert code == 2
    assert "没有用户" in text


def test_list_json_shape(container, capsys) -> None:
    alert = make_alert(container)
    code, body = payload(container, capsys, "list", "--user", USER)
    assert code == 0 and body["exit"] == 0
    assert body["filter"] == "new" and body["limit"] == 50 and body["more"] is False
    got = body["alerts"][0]
    assert got["id"] == alert.id
    assert got["value_text"] == "9.80 CNY"
    # 库里的 TIMESTAMPTZ 读回来是带时区偏移的同一时刻，串未必等于写进去的那一份，所以比时刻
    assert datetime.fromisoformat(got["triggered_at"]) == T0


def test_list_is_newest_first(container, capsys) -> None:
    """那一列的排序就是读的顺序：最新的一条离「现在该怎么办」最近。"""
    make_alert(container, code="000001", triggered_at=T0)
    make_alert(container, code="601088", triggered_at=LATER)
    _, text = out(container, capsys, "list", "--user", USER)
    assert text.index("A/601088") < text.index("A/000001")


# ---------- show ----------


def test_show_prints_the_three_sentences_written_then(container, capsys) -> None:
    """`show` 只念 `payload_json` 里那三句，不按今天的模板重渲染。

    这一枚钉的是整个模块那条原则：重渲染会在措辞改版之后说谎，而复盘要看的就是当时看到什么。
    """
    alert = make_alert(container)
    code, text = out(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    assert alert.id in text
    assert "看到    9.80 CNY" in text
    assert "阈值    10.00 CNY" in text
    assert "建议    按纪律减半仓" in text


def test_show_prints_the_body_lines(container, capsys) -> None:
    alert = make_alert(container)
    code, text = out(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    assert "正文    绝对价止损" in text
    assert "这笔浮亏 -2.0%" in text
    assert "已跌破 10.00 CNY" in text


def test_show_names_the_channel_of_each_delivery(container, capsys) -> None:
    """那一行要说通道的**名字**：一段 hex 既认不出是哪条，也看不出它现在还在不在。"""
    alert = make_alert(container)
    channel = add_channel(container, name="桌面通知")
    container.alerts_store.record(alert.id, channel.id, "sent", attempts=2)
    code, text = out(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    assert "桌面通知" in text and "sent" in text and "2 次" in text


def test_show_marks_a_deleted_channel(container, capsys) -> None:
    """通道可以删、投递历史不能改：屏幕上要说「已删除」，不能报 KeyError，也不能假装没发过。"""
    alert = make_alert(container)
    channel = add_channel(container, name="临时")
    container.alerts_store.record(alert.id, channel.id, "failed", error="对方 503")
    container.channels_store.remove(channel.id)
    code, text = out(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    assert "已删除" in text and "503" in text


def test_show_says_the_no_channel_case(container, capsys) -> None:
    """`channel_id=None` 的那一行是「一个通道都没选到」：屏幕上不许留成空白。"""
    alert = make_alert(container)
    container.alerts_store.record(alert.id, None, "no_channel", error="没有启用的通道")
    code, text = out(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    assert "（没选到通道）" in text


def test_show_says_nothing_was_delivered(container, capsys) -> None:
    alert = make_alert(container)
    code, text = out(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    assert "还没出发过" in text


def test_show_missing_alert_is_exit_one(container, capsys) -> None:
    """读不到不是敲错参数：exit 1（D77），那一串 id 有可能刚刚还在。"""
    code, text = out(container, capsys, "show", "deadbeef" * 4, "--user", USER)
    assert code == 1
    assert "不存在" in text


def test_show_other_users_alert_is_refused(container, capsys) -> None:
    """那一条存在、那一列也读得回来，唯独不属于问的人——屏幕上不能把前三样吐出去。"""
    alert = make_alert(container, user=OTHER, symbol_name="别人的票")
    code, text = out(container, capsys, "show", alert.id, "--user", USER)
    assert code == 1
    assert "别人的票" not in text


def test_show_json_carries_payload_and_deliveries(container, capsys) -> None:
    alert = make_alert(container)
    channel = add_channel(container)
    container.alerts_store.record(alert.id, channel.id, "sent")
    code, body = payload(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    assert body["alert"]["status"] == "new"
    assert body["alert"]["body"].startswith("绝对价止损")
    assert body["alert"]["extra"] == {}
    got = body["deliveries"][0]
    assert got["outcome"] == "sent" and got["channel"] == "桌面通知"


# ---------- handle / dismiss ----------


def test_handle_moves_the_state_and_prints_it(container, capsys) -> None:
    alert = make_alert(container)
    code, text = out(container, capsys, "handle", alert.id, "--user", USER, "--note", "已挂条件单")
    assert code == 0
    assert "已标成 handled：已挂条件单" in text
    assert "no_open_alert" in text


def test_dismiss_without_reason_is_refused_with_two(container, capsys) -> None:
    """拒在仓库层、不在 argparse：那里能给 JSON，`--json` 的人拿得到同一句话。"""
    alert = make_alert(container)
    code, text = out(container, capsys, "dismiss", alert.id, "--user", USER)
    assert code == 2
    assert "必须带原因" in text
    _, body = payload(container, capsys, "dismiss", alert.id, "--user", USER)
    assert body["status"] == "refused" and body["exit"] == 2


def test_dismiss_writes_only_the_reason_column(container, capsys) -> None:
    """两列问两件事：混写一次，忽略率就再也回答不了「这条规则是不是在瞎报」。"""
    alert = make_alert(container)
    code, body = payload(
        container, capsys, "dismiss", alert.id, "--user", USER, "--reason", "除权造成的假跌破"
    )
    assert code == 0
    got = body["alert"]
    assert got["status"] == "dismissed"
    assert got["dismiss_reason"] == "除权造成的假跌破"
    assert got["handle_note"] is None


def test_switching_your_mind_replaces_the_previous_sentence(container, capsys) -> None:
    """`handled → dismissed` 允许，但上一次那句要清掉：留两句就有两个答案。"""
    alert = make_alert(container)
    invoke(container, "handle", alert.id, "--user", USER, "--note", "已挂条件单")
    code, body = payload(
        container, capsys, "dismiss", alert.id, "--user", USER, "--reason", "其实是误报"
    )
    assert code == 0
    assert body["alert"]["handle_note"] is None
    assert body["alert"]["dismiss_reason"] == "其实是误报"


def test_handle_unknown_alert_is_exit_one(container, capsys) -> None:
    code, _ = out(container, capsys, "handle", "deadbeef" * 4, "--user", USER)
    assert code == 1


def test_move_other_users_alert_is_refused(container, capsys) -> None:
    """写那一列的门槛比读更高：`user_id` 必填，所以「顺手标一下」也越不过去。"""
    alert = make_alert(container, user=OTHER)
    code, text = out(container, capsys, "handle", alert.id, "--user", USER)
    assert code == 1
    assert "不属于" in text
    assert container.alerts_store.get(alert.id).status == "new"


def test_handle_json_shape(container, capsys) -> None:
    alert = make_alert(container)
    code, body = payload(container, capsys, "handle", alert.id, "--user", USER)
    assert code == 0 and body["exit"] == 0
    assert body["alert"]["status"] == "handled"
    # JSON 里的时刻必须解析得开（屏幕上那种短横是给机器读不了的写法），
    # 而且按下的就是这一趟的那一枚：夹具的钟冻在哪个时刻，那一列就是哪个时刻
    stamped = datetime.fromisoformat(body["alert"]["handled_at"])
    assert stamped == container.alerts_store.clock.now_utc()


# ---------- 四态：多一个状态，屏幕上就得有那个词 ----------


def test_acknowledged_is_spoken_in_chinese(container, capsys, db) -> None:
    """状态词表是封闭枚举：多一个状态而 CLI 没这个词，`alert show` 会 KeyError。"""
    from sams.storage.repos.alerts import ACKNOWLEDGED

    assert ACKNOWLEDGED == "acknowledged"
    alert = make_alert(container)
    db.execute("UPDATE alerts SET status = 'acknowledged' WHERE id = ?", [alert.id])
    code, text = out(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    assert "已读" in text
