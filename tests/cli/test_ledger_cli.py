"""`sams ledger` 的行为契约：文本给人，`--json` 给机器，拒绝也要给（§5.4、D57/D77）。

与 `tests/cli/test_thesis_cli.py` 同一跑法：不起子进程，直接 `build_parser()` 解析参数、
拿 `args.handler(container, args)` 的返回值当退出码——退出码与打印的那一行就是这个模块的
全部对外行为，起子进程只会把断言拖进 shell 引号里。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from support_valuation import DAYS, bars, make_container

import sams.cli as cli

USER = "trader"
PWD = "correct-horse-battery"


@pytest.fixture
def container(db, settings):
    c = make_container(db, settings)
    # 屏幕上的 `--user` 是用户名，库里认的是 id：没有这个用户，每一条记账都被拒
    c.users.create(USER, PWD)
    return c


def invoke(container, *extra: str) -> int:
    args = cli.build_parser().parse_args(["ledger", *extra])
    return args.handler(container, args)


def out(container, capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()
    code = invoke(container, *extra)
    return code, capsys.readouterr().out


def payload(container, capsys, *extra: str) -> tuple[int, dict]:
    code, text = out(container, capsys, *extra, "--json")
    return code, json.loads(text)


def _open_account(container, capsys, name: str = "主账户") -> str:
    code = invoke(container, "account", "add", "--user", USER, "--name", name, "--json")
    assert code == 0
    return json.loads(capsys.readouterr().out)["account"]["id"]


def _funded(container, capsys, *, name: str = "主账户", amount: str = "100000") -> str:
    account_id = _open_account(container, capsys, name)
    code = invoke(
        container,
        "cash",
        "--user",
        USER,
        "--account",
        account_id,
        "--currency",
        "CNY",
        "--amount",
        amount,
        "--reason",
        "初始",
    )
    assert code == 0
    capsys.readouterr()
    return account_id


def _buy_args(account_id: str, *extra: str) -> tuple[str, ...]:
    return (
        "buy",
        "--user",
        USER,
        "--account",
        account_id,
        "--market",
        "A",
        "--code",
        "601088",
        "--qty",
        "100",
        "--price",
        "40",
        *extra,
    )


def test_adding_an_account_prints_the_name_and_the_report_currency(container, capsys) -> None:
    code, body = out(container, capsys, "account", "add", "--user", USER, "--name", "主账户")
    assert code == 0
    assert "主账户" in body and "CNY" in body


def test_listing_accounts_keeps_one_line_each(container, capsys) -> None:
    _open_account(container, capsys, "主账户")
    _open_account(container, capsys, "副账户")
    code, body = out(container, capsys, "account", "list", "--user", USER)
    assert code == 0
    assert "主账户" in body and "副账户" in body


def test_a_duplicate_account_name_exits_1_not_2(container, capsys) -> None:
    """重名是库里那条约束挡下来的（业务拒绝），不是参数写错：脚本对两者的处置不一样。"""
    _open_account(container, capsys, "主账户")
    assert invoke(container, "account", "add", "--user", USER, "--name", "主账户") == 1


def test_a_bad_date_exits_2(container, capsys) -> None:
    """`2024-7-1` 是手滑：`fromisoformat` 抛裸 ValueError，CLI 只兜 SamsError。"""
    account_id = _funded(container, capsys)
    assert invoke(container, *_buy_args(account_id, "--on", "2024-7-1")) == 2


def test_an_unknown_user_exits_2_and_names_the_fix(container, capsys) -> None:
    """`--user trder` 是参数打错了（2），且屏幕上要说怎么补：库里那一列认的是 id。"""
    code, body = out(container, capsys, "account", "list", "--user", "trder")
    assert code == 2
    assert "sams user add" in body


def test_withdrawing_cash_decreases_the_balance(container, capsys) -> None:
    """方向由 `--withdraw` 说，不由数额的正负说：`--amount -100` 在两个方向里都能读。"""
    account_id = _funded(container, capsys, amount="100000")
    code, body = payload(
        container,
        capsys,
        "cash",
        "--user",
        USER,
        "--account",
        account_id,
        "--currency",
        "CNY",
        "--amount",
        "40000",
        "--withdraw",
        "--reason",
        "取出",
    )
    assert code == 0
    assert body["balance"] == pytest.approx(60_000.0)


def test_buying_without_on_is_refused_as_a_typo(container, capsys) -> None:
    """缺成交日是参数错（2），不是业务拒绝（1）：默认「今天」会在跨时区补录时静默记错一天。"""
    account_id = _funded(container, capsys)
    code, body = out(container, capsys, *_buy_args(account_id))
    assert code == 2
    assert "--on" in body


def test_every_refusal_still_emits_json(container, capsys) -> None:
    """D57：管道里读的人拿到的是一句原因加一个码，不是一个空 stdin。"""
    code, body = payload(
        container, capsys, *_buy_args("no-such-account", "--on", "2024-10-25", "--json")
    )
    # 拿到 1 而不是 2：`--on` 合法、日期能解析，拒的是「库里没有这笔账」这件业务
    assert code == 1
    assert body["status"] == "refused"
    assert "不存在" in body["reason"]


def test_a_buy_reports_the_order_and_the_cash_left(container, capsys) -> None:
    account_id = _funded(container, capsys)
    code, body = payload(
        container, capsys, *_buy_args(account_id, "--fee", "5", "--on", "2024-10-25", "--json")
    )
    assert code == 0
    assert body["order"]["currency"] == "CNY"
    assert body["cash"]["CNY"] == pytest.approx(95_995.0)


def test_the_buy_screen_labels_the_instant_in_utc(container, capsys) -> None:
    """D80 的同一个坑在账本上重犯一次就会留下「成交比立论晚八小时」那种假时间线。"""
    account_id = _funded(container, capsys)
    code, body = out(container, capsys, *_buy_args(account_id, "--on", "2024-10-25"))
    assert code == 0
    assert "07:00:00 UTC" in body


def test_summary_json_carries_both_pnl_pieces_as_separate_keys(container, capsys) -> None:
    """两个键必须同时存在，即使其中一个是 0：靠「有没有这个键」判断有没有换汇是错的。"""
    account_id = _funded(container, capsys)
    invoke(container, *_buy_args(account_id, "--on", "2024-10-25"))
    code, body = payload(container, capsys, "summary", "--user", USER, "--account", account_id)
    assert code == 0
    assert "price_pnl_report" in body and "fx_pnl" in body
    assert body["fx_pnl"] == 0.0
    assert body["cash_by_currency"]["CNY"] == pytest.approx(96_000.0)


def test_positions_json_carries_the_price_day(container, capsys) -> None:
    """盈亏那个数是哪一天的收盘，必须跟着它一起出门：不带的价格是「此时此刻」的错觉。"""
    account_id = _funded(container, capsys)
    invoke(container, *_buy_args(account_id, "--on", "2024-10-25"))
    code, body = payload(container, capsys, "positions", "--user", USER, "--account", account_id)
    assert code == 0
    row = body["positions"][0]
    assert row["code"] == "601088"
    assert row["qty"] == pytest.approx(100.0)
    assert row["convertible"] is True
    assert row["last_price_date"] is not None


def test_a_text_summary_prints_the_notes_after_the_numbers(container, capsys) -> None:
    """`notes` 与跨账户点名不是日志：它们是「这个合计为什么不完整」的出处，只进 JSON 等于没有。"""
    first = _funded(container, capsys)
    second = _funded(container, capsys, name="副账户", amount="50000")
    invoke(container, *_buy_args(first, "--on", "2024-10-25"))
    invoke(container, *_buy_args(second, "--on", "2024-10-25"))
    capsys.readouterr()
    code, body = out(container, capsys, "summary", "--user", USER, "--account", first)
    assert code == 0
    assert "副账户" in body


def test_an_unconvertable_cash_is_named_in_the_text_summary(container, capsys) -> None:
    """换不出的那笔现金：合计少了它，屏幕上必须说少了它。"""
    account_id = _funded(container, capsys)
    capsys.readouterr()
    invoke(
        container,
        "cash",
        "--user",
        USER,
        "--account",
        account_id,
        "--currency",
        "USD",
        "--amount",
        "1000",
        "--reason",
        "美元口袋",
    )
    capsys.readouterr()
    code, body = out(container, capsys, "summary", "--user", USER, "--account", account_id)
    assert code == 0
    assert "USD" in body and "未计入" in body


# ---------- 手工汇率：`sams ledger fx set`（采集链失败时的出口） ----------


def _fx_set(*extra: str) -> tuple[str, ...]:
    return ("fx", "set", "--day", "2024-10-25", "--base", "USD", "--quote", "CNY", *extra)


def test_a_manual_fx_rate_is_written_and_names_its_source(container, capsys) -> None:
    """手工填的那一行必须在库里**看得出是手工的**：否则下次同步覆盖它时，
    没人知道自己丢掉的是 Yahoo 的数还是自己手敲的数。"""
    code, body = payload(container, capsys, *_fx_set("--rate", "7.1"))
    assert code == 0
    assert body["rate"]["source"] == "manual"
    stored = container.fx_store.get(date(2024, 10, 25), "USD", "CNY")
    assert stored.rate == pytest.approx(7.1)
    assert stored.source == "manual"


def test_the_manual_rate_screen_says_which_direction_was_written(container, capsys) -> None:
    """屏幕上要说清「1 USD = 7.1 CNY」：只印一个 7.1，下一行就被读成倒数。"""
    code, body = out(container, capsys, *_fx_set("--rate", "7.1"))
    assert code == 0
    assert "USD→CNY" in body


def test_a_zero_rate_is_a_parameter_error(container) -> None:
    """0 不是「没有汇率」而是一个会毁掉换算的数：仓库层当场拒收，这里给出 2。"""
    assert invoke(container, *_fx_set("--rate", "0")) == 2


def test_the_same_currency_pair_is_refused_as_a_typo(container) -> None:
    """CNY→CNY 恒为 1，写进库会让下一次读它的人以为那天真的换过汇。"""
    assert (
        invoke(
            container, "fx", "set", "--day", "2024-10-25", "--base", "CNY",
            "--quote", "CNY", "--rate", "1",
        )
        == 2
    )


def test_positions_labels_the_two_pnl_halves_with_their_own_currency(
    container, capsys
) -> None:
    """`price_pnl` 用标的币种、`fx_pnl` 用报告币种（§5.4 规则 3）。

    共用一个后缀会在屏幕上造出一个不存在的数：1000.00 是美元、400.00 是人民币，
    打成 `1000.00400.00 CNY` 读起来像一笔 1400 的亏损，而那是两个币种的两个量。
    """
    account_id = _funded(container, capsys)
    assert (
        invoke(
            container, "cash", "--user", USER, "--account", account_id,
            "--currency", "USD", "--amount", "10000", "--reason", "美元口袋",
        )
        == 0
    )
    # 美股行情：库里的 K 线只有 A/601088，所以现场铺一条恒定 20 美元的 AAPL
    container.bars.upsert(bars(DAYS, [20.0] * len(DAYS), market="US", code="AAPL"))
    # 买入基准 7.0、行情日 7.2 ⇒ 换汇段 = 100 股 × 现价 20 × (7.2-7.0) = 400
    assert (
        invoke(
            container, "buy", "--user", USER, "--account", account_id,
            "--market", "US", "--code", "AAPL", "--qty", "100", "--price", "10",
            "--on", "2024-10-25", "--fx-rate", "7.0",
        )
        == 0
    )
    assert invoke(
        container, "fx", "set", "--day", "2024-10-26", "--base", "USD",
        "--quote", "CNY", "--rate", "7.2",
    ) == 0
    capsys.readouterr()

    code, body = payload(container, capsys, "positions", "--user", USER, "--account", account_id)
    assert code == 0
    row = body["positions"][0]
    assert row["report_currency"] == "CNY"  # 机器侧也要能问出这一段是什么币种
    _, text = out(container, capsys, "positions", "--user", USER, "--account", account_id)
    assert "1000.00 USD" in text and "400.00 CNY" in text
    assert "1000.00400.00" not in text  # 两半之间必须有东西隔着，不能靠列宽挤出来
