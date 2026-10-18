"""`sams sync financials|valuation|flow` 与 `sams analysis fundamentals` 的出口契约。

命令行是这些数第一次被人读到的地方，所以这里测的不是算式（在 `tests/analysis`）
也不是取数（在 `tests/datasource`），而是**屏幕上那句话有没有替数据说谎**：

* PR 那一行必须跟着「用的是哪一期的 ROE、哪一天的 PE」（D54、D60）。两个数并排印在
  同一行而不写日期，读的人就以为它们是同一天的。
* 算不出时打印 `PR = 无 ← 原因`，**不打印 0**（D59）。0 在 PR 的刻度上意思是「免费」，
  是这条公式最贵的一个误读。
* `--json` 的每个出口都是可解析 JSON 且带 `exit`（D57），参数错也是。
* 百分数写法的 `--roe-estimate 15` 必须被拒收：它会让 PR 静默小两百倍（D58）。
* 分母那一期是哪一种周期，跟着 PR 一起出门（D62）。实测同一只票同一个 PE，按最新一期
  能给到 8.41（一季报）与 3.51（半年报），按年报是 ~1.6——不写口径的那一行没有可比性。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars
from support_fundamentals import StubFundamentalsAdapter

import sams.cli as cli
from sams.bootstrap import build_container

FY = date(2023, 12, 31)
FY_PUBLISHED = date(2024, 3, 20)
H1 = date(2024, 6, 30)
H1_PUBLISHED = date(2024, 7, 5)
VALUATION_DAY = date(2024, 6, 28)


def financials_rows(**over) -> pl.DataFrame:
    """默认那一行是**年报**（12-31）：命令行不给 `--roe-basis` 时，PR 的分母就是它（D62）。"""
    row = {
        "market": "A",
        "code": "601088",
        "report_date": FY,
        "published_date": FY_PUBLISHED,
        "roe": 0.2,
        "revenue_yoy": 0.12,
        "profit_yoy": 0.08,
        "dividend_yield": 0.042,
        "market_cap": 6.4e11,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row])


def interim_rows(**over) -> pl.DataFrame:
    """比年报更新的一期（半年累计 ROE 0.08，已披露）：默认口径必须「说出来但不采信」。"""
    row = {
        "market": "A",
        "code": "601088",
        "report_date": H1,
        "published_date": H1_PUBLISHED,
        "roe": 0.08,
        "revenue_yoy": 0.12,
        "profit_yoy": 0.08,
        "dividend_yield": 0.042,
        "market_cap": 6.4e11,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row])


def valuation_rows(**over) -> pl.DataFrame:
    row = {
        "market": "A",
        "code": "601088",
        "date": VALUATION_DAY,
        "pe_ttm": 18.5,
        "pb": 2.1,
        "dividend_yield": None,
        "market_cap": 6.6e11,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row])


def flow_rows(**over) -> pl.DataFrame:
    row = {
        "market": "A",
        "code": "601088",
        "date": VALUATION_DAY,
        "main_net_in": 1.2e8,
        "north_net": None,
        "north_hold": None,
        "margin_bal": None,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row])


def make_container(db, settings, adapters) -> object:
    """时钟冻在 2024-07-09：命令的默认截至日必须问注入的时钟，不问真实今天。"""
    built = build_container(
        settings,
        db=db,
        adapters=adapters,
        clock=FrozenClock(datetime(2024, 7, 9, 15, tzinfo=UTC)),
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv: list[str]) -> int:
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def sync(container, kind: str, code: str = "601088") -> int:
    return run(container, ["sync", kind, "--market", "A", "--code", code])


def seeded(db, settings, *, financials=None, valuation=None, flow=None, eastmoney=None):
    """一个装好基本面替身的容器；`eastmoney` 用来在链上放一个「没有这个能力」的源。

    HK/US 链上的 `yfinance` 用普通桩填上：本文件只测 A 股基本面，但注册表要求
    配置里每个市场的源都能解析，缺一家是构造容器时就抛错，轮不到断言。
    """
    adapter = StubFundamentalsAdapter(
        ["A"], financials=financials, valuation=valuation, flow=flow
    )
    return make_container(
        db,
        settings,
        {
            "akshare": adapter,
            "eastmoney": eastmoney or adapter,
            "yfinance": StubAdapter("yfinance", ["HK", "US"], sample_bars(source="yfinance")),
        },
    )


@pytest.fixture
def wired(db, settings, capsys):
    """已经同步过一只好票的容器：`analysis fundamentals` 的直接输入。"""
    container = seeded(db, settings, financials=financials_rows(), valuation=valuation_rows())
    sync(container, "financials")
    sync(container, "valuation")
    capsys.readouterr()
    return container


@pytest.fixture
def two_periods(db, settings, capsys):
    """库里同时有年报与更新的半年期：D62 的两条口径在这个 fixture 里会给出两个数。"""
    container = seeded(
        db,
        settings,
        financials=pl.concat([financials_rows(), interim_rows()]),
        valuation=valuation_rows(),
    )
    sync(container, "financials")
    sync(container, "valuation")
    capsys.readouterr()
    return container


def argv(*extra: str, code: str = "601088") -> list[str]:
    return ["analysis", "fundamentals", "--market", "A", "--code", code, *extra]


# ---------- 采集命令 ----------


def test_sync_financials_reports_rows_written(db, settings, capsys):
    container = seeded(db, settings, financials=financials_rows())
    capsys.readouterr()
    assert sync(container, "financials") == 0
    out = capsys.readouterr().out
    assert "取回=1" in out and "入库=1" in out
    assert container.financials_store.count("A") == 1


def test_sync_valuation_and_flow_each_land_in_their_own_table(db, settings, capsys):
    container = seeded(db, settings, valuation=valuation_rows(), flow=flow_rows())
    capsys.readouterr()
    assert sync(container, "valuation") == 0
    assert sync(container, "flow") == 0
    assert container.valuation_store.count("A") == 1
    assert container.flow_store.count("A") == 1


def test_a_confirmed_absence_is_named_as_such_not_as_a_failure(db, settings, capsys):
    """「几家都查过，没有」与「取回 0 条所以同步坏了」是两句话（D43）。"""
    container = seeded(db, settings)
    capsys.readouterr()
    assert sync(container, "financials") == 0
    assert "查无记录" in capsys.readouterr().out
    assert container.financials_store.count() == 0


def test_sources_without_the_capability_are_reported_as_skipped(db, settings, capsys):
    """资金流在 A 股只有 akshare 一家能给：链上被跳过的源要说出来（D38），
    否则「0 行」看起来像源坏了。"""
    container = seeded(
        db, settings, flow=None, eastmoney=StubAdapter("eastmoney", ["A"], sample_bars())
    )
    capsys.readouterr()
    sync(container, "flow")
    printed = capsys.readouterr().out
    assert "跳过 eastmoney" in printed, printed


# ---------- 查询命令 ----------


def test_the_pr_line_carries_the_period_and_the_valuation_day(wired, capsys):
    capsys.readouterr()
    assert run(wired, argv()) == 0
    out = capsys.readouterr().out
    assert "PR = 0.925" in out, "18.5 / 0.2 / 100 = 0.925"
    assert str(FY) in out and str(VALUATION_DAY) in out, "两个粒度都要印出来（D60）"
    assert "已披露" in out, "ROE 是已披露的还是预估的，得跟着数走"


def test_the_caliber_line_ships_with_the_numbers(wired, capsys):
    capsys.readouterr()
    run(wired, argv())
    out = capsys.readouterr().out
    assert "小数" in out and "0.20 = 20%" in out


def test_ratios_are_printed_as_percentages_while_multiples_are_not(wired, capsys):
    """库里是小数，屏幕上是百分数——换算只发生在打印这一步，PE 不参与。"""
    capsys.readouterr()
    run(wired, argv())
    out = capsys.readouterr().out
    assert "20.00%" in out, "ROE 0.2 → 20.00%"
    assert "PE(TTM) 18.50" in out


def test_as_of_before_the_publication_date_loses_the_period(wired, capsys):
    """把截至日挪到披露日之前：那一期的 ROE 与由它算出的 PR 一起消失（D61）。"""
    capsys.readouterr()
    code = run(wired, argv("--as-of", "2024-03-19"))
    out = capsys.readouterr().out
    assert "采信期" in out and "无" in out
    assert "PR = 无" in out, "没有已披露的 ROE 就不该给出 PR"
    assert code == 1


def test_a_negative_pe_without_an_estimate_prints_no_pr_and_names_the_next_step(
    db, settings, capsys
):
    capsys.readouterr()
    container = seeded(
        db,
        settings,
        financials=financials_rows(roe=-0.1),
        valuation=valuation_rows(pe_ttm=-8.0),
    )
    sync(container, "financials")
    sync(container, "valuation")
    capsys.readouterr()
    code = run(container, argv())
    out = capsys.readouterr().out
    assert "PR = 无" in out and "PR = 0" not in out
    assert "roe-estimate" in out, "下一步是传预估，不是换个源"
    assert code == 1


def test_a_negative_pe_with_an_estimate_shows_that_it_is_estimated(db, settings, capsys):
    """预估 ROE 嵌在 PR 里，屏幕上必须跟着走：否则两个不同基准的数在同列里可比。"""
    capsys.readouterr()
    container = seeded(
        db, settings, financials=financials_rows(roe=-0.1), valuation=valuation_rows(pe_ttm=-8.0)
    )
    sync(container, "financials")
    sync(container, "valuation")
    capsys.readouterr()
    assert run(container, argv("--roe-estimate", "0.2")) == 0
    out = capsys.readouterr().out
    # PB 2.1 / 0.2 / 0.2 / 100 = 0.525
    assert "PR = 0.525" in out and "预估" in out


def test_a_percent_spelled_estimate_is_refused_before_it_silently_divides_twice(wired, capsys):
    capsys.readouterr()
    assert run(wired, argv("--roe-estimate", "15")) == 2
    assert "0.15" in capsys.readouterr().out, "拒收要说清该怎么写"


def test_an_unparsable_date_is_a_refusal_not_a_traceback(wired, capsys):
    capsys.readouterr()
    assert run(wired, argv("--as-of", "2024-7-1")) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().out


def test_a_code_with_nothing_in_the_database_names_the_sync_commands(wired, capsys):
    capsys.readouterr()
    code = run(wired, argv(code="600000"))
    out = capsys.readouterr().out
    assert "sams sync financials" in out
    assert code == 1


def test_json_carries_the_two_grains_the_pr_and_the_exit(wired, capsys):
    capsys.readouterr()
    assert run(wired, argv("--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit"] == 0 and payload["status"] == "ok"
    assert payload["period"] == str(FY) and payload["valuation_on"] == str(VALUATION_DAY)
    assert payload["pr"]["value"] == pytest.approx(0.925)
    assert payload["pr"]["basis"] == "pe"
    assert payload["roe"] == pytest.approx(0.2), "JSON 里给库里的口径，不给屏幕上的百分数"


def test_json_refusals_are_still_json(wired, capsys):
    """D57：管道里退出码会被下游覆盖，所以拒绝也得是 JSON——包括参数错。"""
    capsys.readouterr()
    assert run(wired, argv("--as-of", "2024-03-19", "--json")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit"] == 1 and payload["status"] == "refused"
    assert payload["pr"]["value"] is None and payload["pr"]["reason"]

    capsys.readouterr()
    assert run(wired, argv("--roe-estimate", "15", "--json")) == 2
    assert json.loads(capsys.readouterr().out)["exit"] == 2

# ---------- ROE 口径（D62）----------


def test_the_default_names_the_denominator_period_kind(wired, capsys):
    """默认口径那句话必须跟着 PR 出门：只印日期的那一行，读的人不知道周期多长。"""
    capsys.readouterr()
    assert run(wired, argv()) == 0
    out = capsys.readouterr().out
    assert "2023-12-31 年报" in out
    assert "分母口径：ROE 取最新已披露年报" in out


def test_a_newer_interim_period_is_announced_but_not_adopted(two_periods, capsys):
    """半年期的 ROE 是六个月累计，配十二个月的 PE 会给出跳动的 PR：不采信，但要说出来。"""
    capsys.readouterr()
    assert run(two_periods, argv()) == 0
    out = capsys.readouterr().out
    assert "PR = 0.925" in out, "18.5 / 0.2 / 100：用的是年报，不是那期半年数"
    assert f"库里另有更新的一期 {H1}" in out


def test_the_latest_basis_adopts_the_interim_period_and_renames_the_caliber(two_periods, capsys):
    capsys.readouterr()
    assert run(two_periods, argv("--roe-basis", "latest")) == 0
    out = capsys.readouterr().out
    assert "PR = 2.312" in out, "18.5 / 0.08 / 100 = 2.3125——同一个 PE，差了近三倍"
    assert "2024-06-30 6 月末期" in out
    assert "季报是三个月、半年报是六个月" in out
    assert "库里另有更新的一期" not in out, "已经采信了，就不是「放下了另一期」"


def test_a_stock_with_only_interim_periods_refuses_and_names_the_flag(db, settings, capsys):
    """只有半年报可采信时，默认口径给不出 PR——那句拒绝要指回 `--roe-basis`。"""
    capsys.readouterr()
    container = seeded(db, settings, financials=interim_rows(), valuation=valuation_rows())
    sync(container, "financials")
    sync(container, "valuation")
    capsys.readouterr()
    assert run(container, argv()) == 1
    out = capsys.readouterr().out
    assert "PR = 无" in out and "roe-basis latest" in out


def test_json_carries_the_basis_and_the_period_let_go(two_periods, capsys):
    """脚本按 PR 排序时，`roe_basis` 是它唯一能拿到的可比性证据（D57 + D62）。"""
    capsys.readouterr()
    assert run(two_periods, argv("--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["roe_basis"] == "annual"
    assert payload["period"] == str(FY)
    assert payload["interim_period"] == str(H1)
    assert payload["interim_roe"] == pytest.approx(0.08)
    assert payload["basis_note"].startswith("分母口径：ROE 取最新已披露年报")


def test_the_default_refusal_still_names_the_basis_when_nothing_is_annual(wired, capsys):
    """截至日报表还没公开：那一行说「annual 口径下没有已披露数据」，不是「库里没数据」。"""
    capsys.readouterr()
    assert run(wired, argv("--as-of", "2024-03-19")) == 1
    assert "annual" in capsys.readouterr().out
