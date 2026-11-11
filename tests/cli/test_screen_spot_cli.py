"""`sams screen spot`：第一行是范围，每条件三个数，无数出局的票逐个点名。

这一屏最容易撒的谎不是算错 PR，是「无数」被当成「不通过」静默丢掉。所以断言分三段读：
范围行、条件行、无数段；命中与无数各自成段，同一只票只能出现在其中一段。

PR 现值 = PE(动)² ÷ (100×PB)：PB=1 时 PE 5 / 6 / 10 分别是 0.25 / 0.36 / 1.0，
所以门槛 0.5 下前两只命中、第三只落选。写成 PE=10/20/40 会让「两只通过」在公式上
根本不成立——PE=10 自己就已经是 1.0。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import polars as pl
from support import FrozenClock, StubAdapter
from support_valuation import financial_rows

import sams.cli as cli
from sams.bootstrap import build_container
from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.financials import FinancialRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)

# 夹具的 A 链是 akshare/eastmoney；装配期会校验「配置了就必须注册」，所以换成桩。
# 选股命令从不调用 registry，桩只需要名字与市场对得上
ADAPTERS = {
    name: StubAdapter(name, markets, None)
    for name, markets in (
        ("akshare", ["A"]),
        ("eastmoney", ["A"]),
        ("yfinance", ["HK", "US"]),
    )
}


def snap(code, *, pe_dyn=10.0, pb=1.0, turnover=1.0):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": DAY,
        "price": 10.0,
        "pct_chg": 1.0,
        "amount": 1e8,
        "turnover_rate": turnover,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": pe_dyn,
        "pe_dyn": pe_dyn,
        "pb": pb,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


def container_for(db, settings, *rows):
    codes = tuple(row["code"] for row in rows)
    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in codes
        ],
    )
    frame = pl.DataFrame(
        [{"market": "A", "taken_at": TAKEN, **row} for row in rows],
        schema={"market": pl.Utf8, "taken_at": pl.Datetime("us", "UTC"), **SPOT_SCHEMA},
    )
    SnapshotRepo(db).replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame)
    built = build_container(
        settings,
        db=db,
        clock=FrozenClock(datetime(2026, 10, 28, 8, 41, tzinfo=UTC)),
        adapters=ADAPTERS,
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv):
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def test_first_line_is_the_range_then_one_line_per_condition(db, settings, capsys):
    container = container_for(
        db,
        settings,
        snap("600519", pe_dyn=5.0),
        snap("000001", pe_dyn=6.0),
        snap("300750", pe_dyn=10.0),
    )
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "pr_now<=0.5"]) == 0
    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert "A 股" in first and "快照 2026-10-28" in first
    assert "票池 3 只" in out and "代码表 3 只" in out
    assert "pr_now <= 0.5" in out
    # 单位与标签必须出门（D58/D179）：光有 `pr_now` 三个字，0.5 是 0.5 还是 50 全靠猜
    assert "现值市赚率 PR" in out
    assert "通过 2 / 不通过 1 / 无数 0" in out
    assert "600519" in out and "000001" in out
    # 落选的票不进「命中」，也不冒充「无数」：只报数，不点名
    assert "300750" not in out


def test_missing_codes_are_named_in_their_own_section(db, settings, capsys):
    container = container_for(db, settings, snap("600519", pe_dyn=-5.0), snap("000001"))
    capsys.readouterr()
    run(container, ["screen", "spot", "--market", "A", "--where", "pr_now<=0.5"])
    out = capsys.readouterr().out
    missing = out.split("无数 1 只")[-1]
    assert "600519" in missing and "亏损" in missing


def test_no_snapshot_exits_1_with_the_next_command(db, settings, capsys):
    container = build_container(
        settings,
        db=db,
        clock=FrozenClock(datetime(2026, 10, 28, 8, 41, tzinfo=UTC)),
        adapters=ADAPTERS,
    )
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "price>=1"]) == 1
    out = capsys.readouterr().out
    assert "sams sync spot" in out


def test_unknown_field_is_a_parameter_error_with_the_vocabulary(db, settings, capsys):
    container = container_for(db, settings, snap("600519"))
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "prr<=0.5"]) == 2
    assert "可选的是" in capsys.readouterr().out


def test_between_is_written_as_a_range_and_backwards_is_refused(db, settings, capsys):
    container = container_for(
        db, settings, snap("600519", turnover=2.0), snap("000001", turnover=9.0)
    )
    capsys.readouterr()
    argv = ["screen", "spot", "--market", "A", "--where", "turnover_rate=1..5"]
    assert run(container, argv) == 0
    assert "600519" in capsys.readouterr().out
    argv[-1] = "turnover_rate=5..1"
    assert run(container, argv) == 2


def test_limit_must_be_positive(db, settings, capsys):
    container = container_for(db, settings, snap("600519", pe_dyn=5.0))
    capsys.readouterr()
    argv = ["screen", "spot", "--market", "A", "--where", "pr_now<=0.5", "--limit", "0"]
    assert run(container, argv) == 2
    assert "正整数" in capsys.readouterr().out


def test_the_coverage_sentence_rides_with_the_annual_condition_and_in_json(
    db, settings, capsys
):
    """只看快照列时不背年报覆盖；读到年报列时，命中数旁边必须有「覆盖 x / y」（D227）。"""
    container = container_for(db, settings, snap("600519"), snap("000001"))
    FinancialRepo(db).upsert(financial_rows(code="600519"))
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "roe>=0.1"]) == 0
    out = capsys.readouterr().out
    assert "年报覆盖 1 / 2 只" in out
    assert "不是落选" in out

    run(container, ["screen", "spot", "--market", "A", "--where", "pe_ttm<=20", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["financial_coverage"] is None


def test_negative_valuations_and_st_rows_sit_out_unless_asked(db, settings, capsys):
    """R-09：命令行与网页同一个默认——负 PE 不是便宜，ST/退市默认不出现。"""
    container = container_for(
        db,
        settings,
        snap("600519", pe_dyn=5.0),
        snap("000001", pe_dyn=-72980.5),
        {**snap("600001", pe_dyn=4.0), "name": "*ST海医", "is_st": True},
    )
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "pe_ttm<=8"]) == 0
    out = capsys.readouterr().out
    assert "通过 1 / 不通过 0 / 无数 1" in out
    assert "估值不为正" in out
    assert "ST/退市 1 只未参与" in out
    assert "*ST海医" not in out

    capsys.readouterr()
    argv = [
        "screen",
        "spot",
        "--market",
        "A",
        "--where",
        "pe_ttm<=8",
        "--include-negatives",
        "--include-st",
    ]
    assert run(container, argv) == 0
    out = capsys.readouterr().out
    assert "通过 3 / 不通过 0 / 无数 0" in out
    assert "*ST海医" in out
    assert "ST/退市" not in out


def test_the_cli_appends_its_own_switch_to_the_nonpositive_reason(db, settings, capsys):
    """共享串不偏袒任何界面（D283）：命令行自己把开关名接在后面。"""
    container = container_for(db, settings, snap("600519", pe_dyn=5.0), snap("000001", pe_dyn=-5.0))
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "pe_ttm<=8"]) == 0
    out = capsys.readouterr().out
    assert "估值不为正" in out
    assert "--include-negatives" in out


def test_the_cli_appends_the_sync_command_to_a_financials_gap(db, settings, capsys):
    """D336：共享理由只讲事实；命令行这一间房自己接上「先跑什么」。

    网页那边同一组缺口给的是排队按钮——两个房间的下一步不同，串本身不偏袒。
    """
    container = container_for(db, settings, snap("600519"), snap("000001"))
    FinancialRepo(db).upsert(financial_rows(code="600519"))
    capsys.readouterr()
    assert run(container, ["screen", "spot", "--market", "A", "--where", "roe>=0.1"]) == 0
    out = capsys.readouterr().out
    assert "没有已披露年报数据（年报 ROE）" in out
    assert "sams sync financials --market A --code <代码>" in out


def test_json_carries_three_counts_and_the_missing_groups(db, settings, capsys):
    container = container_for(
        db, settings, snap("600519", pe_dyn=5.0), snap("000001", pe_dyn=-5.0)
    )
    capsys.readouterr()
    run(container, ["screen", "spot", "--market", "A", "--where", "pr_now<=0.5", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit"] == 0
    assert payload["fields"][0]["passed"] == 1
    assert payload["fields"][0]["missing"] == 1
    assert payload["missing"][0]["codes"] == ["000001"]
    assert payload["hits"][0]["code"] == "600519"


def test_field_names_with_digits_are_not_rejected_as_bad_syntax(db, settings, capsys):
    """`ma20_gap` / `rsi14` 带数字：解析器把它们当非法写法，CLI 就进不了技术面字段。"""
    container = container_for(db, settings, snap("600519", pe_dyn=5.0))
    capsys.readouterr()
    code = run(container, ["screen", "spot", "--market", "A", "--where", "ma20_gap>0"])
    out = capsys.readouterr().out
    assert code == 0
    assert "读不出来" not in out
    # 没有日线可算：技术面字段记「无数」，不静默丢掉
    assert "通过 0 / 不通过 0 / 无数 1" in out
    # D337：命令行要的是能当场跑的那一条；共享理由本身只说事实
    assert "日线不足 20 根" in out
    assert "sams sync bars --market A --code <代码>" in out


def _seed_bars(db, code="600519", n=260):
    days = []
    cursor = DAY
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    closes = [100.0 + i * 0.5 for i in range(n)]
    BarRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A"] * n,
                "code": [code] * n,
                "date": days,
                "open": closes,
                "high": [value + 1 for value in closes],
                "low": [value - 1 for value in closes],
                "close": closes,
                "volume": [100.0] * n,
                "amount": [value * 100 for value in closes],
                "source": ["akshare"] * n,
            }
        )
    )


def test_a_stock_without_coverage_gets_the_actions_command(db, settings, capsys):
    _seed_bars(db)
    container = container_for(db, settings, snap("600519"))
    capsys.readouterr()
    run(container, ["screen", "spot", "--market", "A", "--where", "ma20_gap>0"])
    out = capsys.readouterr().out
    assert "公司行为覆盖未确认，无法复权" in out
    assert "sams sync actions --market A --code <代码>" in out
    assert "sams sync bars" not in out
