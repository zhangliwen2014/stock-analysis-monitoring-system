"""`sams attribution`（S5-D §6）：把持仓页那张归因卡原样打成文本。

这一层只做两件事：把 `--window` 折成一个起点日期，把服务给的数说一遍。所以用例断言的
是「两种介质读的是同一份报告」（JSON 逐项等于 `evaluate` 的返回），加上算不出来时那句
话术与退出码（D77：1 是账户不存在，2 是参数不对）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from support_valuation import TODAY, make_container

import sams.cli as cli

USER = "trader"
PWD = "correct-horse-battery"
#: 开在样本库第一天之后一天：`--window 0` 要说「从开户那天算起」，起点得有个可核对的日期
OPENED_AT = datetime(2024, 1, 2, 1, 0, tzinfo=UTC)
BUY_AT = datetime(2024, 1, 2, 6, 30, tzinfo=UTC)


@pytest.fixture
def world(db, settings):
    container = make_container(db, settings)
    user = container.users.create(USER, PWD)
    # 标的名与行业要能印出来：归因表按名字排，缺主档时会退回代码，两种都得见过
    container.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "name": ["中国神华"],
                "currency": ["CNY"],
                "sector": ["煤炭"],
            }
        )
    )
    account = container.sim_accounts.open(user.id, "主账户")
    db.execute("UPDATE sim_accounts SET created_at = ? WHERE id = ?", [OPENED_AT, account.id])
    container.sim_accounts.adjust_cash(
        account.id,
        "CNY",
        100_000.0,
        reason="存入 初始",
        actor_id=USER,
        ts=OPENED_AT,
        kind="deposit",
    )
    container.ledger.buy(
        account.id, USER, "A", "601088", qty=4_000, price=10.0, ts=BUY_AT
    )
    return SimpleNamespace(container=container, account_id=account.id)


def invoke(world, *extra: str) -> int:
    args = cli.build_parser().parse_args(list(extra))
    return args.handler(world.container, args)


def out(world, capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()
    code = invoke(world, *extra)
    return code, capsys.readouterr().out


def payload(world, capsys, *extra: str) -> tuple[int, dict]:
    code, text = out(world, capsys, *extra, "--json")
    return code, json.loads(text)


def rebuild(world) -> None:
    """归因只读已落库的净值：先跑 nav，才是这条命令的真实使用顺序。"""
    assert invoke(world, "nav", "rebuild", "--account", world.account_id, "--days", "300") == 0


def attribution(*extra: str) -> tuple[str, ...]:
    return ("attribution", *extra)


def test_attribution_says_what_to_run_before_any_nav_exists(world, capsys):
    code, text = out(world, capsys, *attribution("--account", world.account_id))

    assert code == 0
    assert f"sams nav rebuild --account {world.account_id}" in text
    # 算不出来时不给表头：空的归因表比一句「跑平」更容易被读成结论
    assert "价格段" not in text


def test_attribution_prints_the_three_parts_and_the_caliber_lines(world, capsys):
    rebuild(world)
    code, text = out(
        world, capsys, *attribution("--account", world.account_id, "--window", "20")
    )

    assert code == 0
    assert "中国神华" in text
    assert "按行业：煤炭" in text
    for header in ("区间均权", "贡献", "价格段", "换汇段", "分红"):
        assert header in text
    for line in ("区间收益", "基准", "超额", "残差"):
        assert line in text
    # 库里没有指数收盘：超额是「不给」，不是 0.00%（那会被读成跑平大盘）
    assert "超额 —" in text
    assert "没有收盘数据" in text


def test_the_json_is_the_same_report_the_page_prints(world, capsys):
    rebuild(world)
    code, body = payload(
        world, capsys, *attribution("--account", world.account_id, "--window", "20")
    )

    assert code == 0
    report = world.container.attribution.evaluate(
        USER, world.account_id, TODAY - timedelta(days=20), TODAY
    )
    assert body["account"] == world.account_id
    assert body["start"] == report.start.isoformat()
    assert body["end"] == report.end.isoformat()
    assert body["days"] == report.days
    assert body["window_return"] == pytest.approx(report.window_return)
    assert body["residual"] == pytest.approx(report.residual)
    assert [row["contribution"] for row in body["rows"]] == pytest.approx(
        [row.contribution for row in report.rows]
    )
    assert body["rows"][0]["price_part"] == pytest.approx(report.rows[0].price_part)
    assert body["benchmark"]["value"] == report.benchmark.value
    assert body["benchmark"]["label"] == "上证指数"
    assert body["excess"] == report.excess
    assert body["skipped"] == ""


def test_the_window_counts_calendar_days_back_from_today_and_zero_means_since_opening(
    world, capsys
):
    rebuild(world)
    _, twenty = payload(
        world, capsys, *attribution("--account", world.account_id, "--window", "20")
    )
    assert twenty["start"] == (TODAY - timedelta(days=20)).isoformat()

    _, body = payload(
        world, capsys, *attribution("--account", world.account_id, "--window", "0")
    )
    assert body["start"] == "2024-01-02"

    code, text = out(world, capsys, *attribution("--account", world.account_id, "--window", "0"))
    assert code == 0
    # 「窗口 0 天」不是「全部」：屏幕上那三个按钮里它是第三种说法
    assert "窗口 全部" in text


def test_a_holding_without_fx_or_dividend_prints_a_dash_not_a_zero(world, capsys):
    rebuild(world)
    _, text = out(
        world, capsys, *attribution("--account", world.account_id, "--window", "20")
    )

    row = next(line for line in text.splitlines() if "中国神华" in line)
    # A 股持仓的换汇段与分红本来就取不到：印 0.00% 会被读成「查过了，是零」
    assert row.endswith("\t—\t—")


def test_attribution_refuses_an_unknown_account_and_a_negative_window(world, capsys):
    code, text = out(world, capsys, *attribution("--account", "nope"))
    assert code == 1
    assert "不存在" in text

    code, text = out(
        world, capsys, *attribution("--account", world.account_id, "--window", "-1")
    )
    assert code == 2
    assert "--window" in text
