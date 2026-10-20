"""`sams channel` 的行为契约：文本给人，`--json` 给机器，拒绝也要给（§4.2、D57/D77）。

屏幕上永远不许出现明文凭据——这条在本文件里被断言三次，因为它是三种不同的
路径：列表、单看、以及**添加成功后的那一行回显**。添加后回显是最后会被写进去的路径，
也是最容易漏的一处。
"""

from __future__ import annotations

import json

import pytest
from support_valuation import make_container

import sams.cli as cli
from sams.core.notify import NotifyResult

USER = "trader"
PWD = "correct-horse-battery"
SECRET = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=super-secret-token"


@pytest.fixture
def container(db, settings):
    c = make_container(db, settings)
    c.users.create(USER, PWD)
    return c


def invoke(container, *extra: str) -> int:
    args = cli.build_parser().parse_args(["channel", *extra])
    return args.handler(container, args)


def out(container, capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()
    code = invoke(container, *extra)
    return code, capsys.readouterr().out


def payload(container, capsys, *extra: str) -> tuple[int, dict]:
    code, text = out(container, capsys, *extra, "--json")
    return code, json.loads(text)


def add(container, capsys, *extra: str, kind="wecom", name="主群", url=SECRET) -> str:
    code, row = payload(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--kind",
        kind,
        "--name",
        name,
        "--config",
        f"url={url}",
        *extra,
    )
    assert code == 0
    return row["channel"]["id"]


# ---------- 探针走的是另一条装配 ----------


class _PostSpy:
    """探针那条用例要的只是「outcome 是三种之一」，不是真发一个包。

    容器的默认工厂会打真实网络：`sams channel probe` 的屏幕形状在这里是被验的对象，
    而「企微收到了一条标题为探针的消息」属于 `@pytest.mark.live` 那一类断言。
    """

    name = "wecom"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def send(self, message) -> NotifyResult:
        return NotifyResult(ok=True)


@pytest.fixture
def quiet(db, settings):
    c = make_container(db, settings, notifier_factory=lambda kind, config: _PostSpy())
    c.users.create(USER, PWD)
    return c


# ---------- 用例 ----------


def test_add_returns_the_id_and_masks_the_config(container, capsys):
    code, row = payload(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--kind",
        "wecom",
        "--name",
        "主群",
        "--config",
        f"url={SECRET}",
    )
    assert code == 0
    assert SECRET not in json.dumps(row, ensure_ascii=False)
    assert "***" in json.dumps(row, ensure_ascii=False)


def test_add_echo_does_not_leak_the_secret(container, capsys):
    code, text = out(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--kind",
        "wecom",
        "--name",
        "主群",
        "--config",
        f"url={SECRET}",
    )
    assert code == 0
    assert SECRET not in text


def test_list_carries_names_and_flags(container, capsys):
    add(container, capsys)
    code, rows = payload(container, capsys, "list", "--user", USER)
    assert code == 0
    row = rows["channels"][0]
    assert row["name"] == "主群" and row["enabled"] and row["is_default"]
    assert SECRET not in json.dumps(rows, ensure_ascii=False)


def test_show_prints_the_masked_config_and_the_last_error(container, capsys):
    channel_id = add(container, capsys)
    code, row = payload(container, capsys, "show", "--user", USER, "--channel", channel_id)
    assert code == 0
    assert row["channel"]["last_error"] == ""
    assert SECRET not in json.dumps(row, ensure_ascii=False)


def test_show_text_lists_every_config_key(container, capsys):
    channel_id = add(container, capsys, url=SECRET)
    code, text = out(container, capsys, "show", "--user", USER, "--channel", channel_id)
    assert code == 0
    assert "url" in text and SECRET not in text


def test_unknown_channel_is_a_refusal_with_exit_2(container, capsys):
    code, text = out(container, capsys, "show", "--user", USER, "--channel", "nope")
    assert code == 2
    assert "不存在" in text


def test_missing_user_is_refused(container, capsys):
    code, text = out(
        container,
        capsys,
        "add",
        "--user",
        "ghost",
        "--kind",
        "wecom",
        "--name",
        "n",
        "--config",
        "url=https://x",
    )
    assert code == 2
    assert "user add" in text


def test_unknown_kind_names_the_six(container, capsys):
    code, text = out(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--kind",
        "telegram",
        "--name",
        "n",
        "--config",
        "url=https://x",
    )
    assert code == 2
    assert "wecom" in text


def test_first_channel_cannot_decline_to_be_the_default(container, capsys):
    code, text = out(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--kind",
        "wecom",
        "--name",
        "主群",
        "--config",
        f"url={SECRET}",
        "--no-default",
    )
    assert code == 2 and "兜底" in text


def test_enable_disable_and_default(container, capsys):
    a = add(container, capsys)
    b = add(container, capsys, name="副群")
    assert invoke(container, "default", "--user", USER, "--channel", b) == 0
    assert invoke(container, "disable", "--user", USER, "--channel", a) == 0
    code, rows = payload(container, capsys, "list", "--user", USER)
    flags = {r["id"]: (r["enabled"], r["is_default"]) for r in rows["channels"]}
    assert flags == {a: (False, False), b: (True, True)}


def test_disabling_the_only_default_is_refused(container, capsys):
    a = add(container, capsys)
    code, text = out(container, capsys, "disable", "--user", USER, "--channel", a)
    assert code == 2 and "默认" in text


def test_remove_needs_a_confirm(container, capsys):
    a = add(container, capsys)
    code, text = out(container, capsys, "remove", "--user", USER, "--channel", a)
    assert code == 2 and "--yes" in text
    assert invoke(container, "remove", "--user", USER, "--channel", a, "--yes") == 0
    _, rows = payload(container, capsys, "list", "--user", USER)
    assert rows["channels"] == []


def test_probe_reports_the_channel_by_name(quiet, capsys):
    channel_id = add(quiet, capsys)
    code, row = payload(quiet, capsys, "probe", "--user", USER, "--channel", channel_id)
    assert code == 0
    assert row["probe"]["outcome"] in ("sent", "failed", "suppressed")
    assert row["probe"]["kind"] == "wecom"


class _Failing(_PostSpy):
    def send(self, message) -> NotifyResult:
        return NotifyResult(ok=False, error="企微 errcode=93000 errmsg=key 不合法")


def test_probe_exits_zero_even_when_it_did_not_go_out(db, settings, capsys):
    c = make_container(db, settings, notifier_factory=lambda kind, config: _Failing())
    c.users.create(USER, PWD)
    # 容器的默认工厂真会退避等待；这一枚用例问的是屏幕，不是重试（重试在 router 那边验）
    c.notify.sleep = lambda _seconds: None
    channel_id = add(c, capsys)
    code, text = out(c, capsys, "probe", "--user", USER, "--channel", channel_id)
    assert code == 0
    assert "没送出去" in text and "93000" in text


def test_dnd_set_and_clear(container, capsys):
    code, row = payload(
        container,
        capsys,
        "dnd",
        "set",
        "--user",
        USER,
        "--start",
        "23:00",
        "--end",
        "07:00",
        "--tz",
        "Asia/Shanghai",
    )
    assert code == 0
    assert row["dnd"]["start"] == "23:00" and row["dnd"]["tz"] == "Asia/Shanghai"
    assert invoke(container, "dnd", "show", "--user", USER, "--json") == 0
    assert invoke(container, "dnd", "clear", "--user", USER, "--json") == 0
    _, row = payload(container, capsys, "dnd", "show", "--user", USER)
    assert row["dnd"]["start"] is None


def test_dnd_partial_is_refused(container, capsys):
    code, text = out(container, capsys, "dnd", "set", "--user", USER, "--start", "23:00")
    assert code == 2 and "三个一起" in text


def test_dnd_show_names_the_window_in_text(container, capsys):
    assert (
        invoke(
            container,
            "dnd",
            "set",
            "--user",
            USER,
            "--start",
            "23:00",
            "--end",
            "07:00",
            "--tz",
            "Asia/Shanghai",
        )
        == 0
    )
    code, text = out(container, capsys, "dnd", "show", "--user", USER)
    assert code == 0
    assert "23:00" in text and "strong" in text


def test_bad_clock_format_is_a_refusal(container, capsys):
    code, text = out(
        container, capsys, "dnd", "set", "--user", USER, "--start", "11pm", "--tz", "UTC"
    )
    assert code == 2 and "HH:MM" in text


def test_text_output_has_a_header_and_no_secrets(container, capsys):
    add(container, capsys)
    code, text = out(container, capsys, "list", "--user", USER)
    assert code == 0
    assert "主群" in text and "wecom" in text and SECRET not in text


def test_empty_list_says_how_to_add_one(container, capsys):
    code, text = out(container, capsys, "list", "--user", USER)
    assert code == 0
    assert "channel add" in text
