"""`sams screen pr` 的出口契约：第一行是范围，不是结果。

选股命令最容易撒的一种谎不是算错 PR，是**让人以为那就是全市场**。库里同步过基本面的
只有 137 只，A 股主档有 5000 多只；「今天一只都没有」在前者意思是「再去同步几只」，
在后者才意思接近「市场不便宜」。所以这里第一批断言打的就是那句范围话是否出门、
两个数是否都在（D43 的「查过且没有」与「没查」、D60 的两个粒度，在选股里同样是两件事）。

另一半是「一只都没扫成」绝不能长得像「扫了，没有」：那时退出码是 1，
而 `--json` 里 exit 同值（D57——退出码进管道就没了，脚本只能读 payload）。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars

import sams.cli as cli
from sams.bootstrap import build_container
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.symbols import SymbolRepo

FY = date(2023, 12, 31)
FY_PUBLISHED = date(2024, 3, 20)
FRESH = date(2024, 7, 5)

FINANCIAL_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "report_date": pl.Date,
    "published_date": pl.Date,
    "roe": pl.Float64,
    "revenue_yoy": pl.Float64,
    "profit_yoy": pl.Float64,
    "dividend_yield": pl.Float64,
    "market_cap": pl.Float64,
    "source": pl.Utf8,
}
VALUATION_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "date": pl.Date,
    "pe_ttm": pl.Float64,
    "pb": pl.Float64,
    "dividend_yield": pl.Float64,
    "market_cap": pl.Float64,
    "source": pl.Utf8,
}


def container_for(db, settings) -> object:
    built = build_container(
        settings,
        db=db,
        clock=FrozenClock(datetime(2024, 7, 9, 15, tzinfo=UTC)),
        adapters={
            name: StubAdapter(name, markets, sample_bars(source=name))
            for name, markets in (
                ("akshare", ["A"]),
                ("eastmoney", ["A"]),
                ("yfinance", ["HK", "US"]),
            )
        },
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv: list[str]) -> int:
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def scan(argv: list[str]) -> str:
    return " ".join(["screen", "pr", "--market", "A", "--pr-max", "0.4", *argv])


def block(out: str, head: str) -> list[str]:
    """段名顶格的那一段，含标题行；下一个顶格行结束它。

    测试按段落读输出，不按「整屏包含某个子串」读——后者会让「命中 600001」与
    「排除 600001」在断言里长得一样，而那只票只能出现在其中一段。
    """
    rows: list[str] = []
    inside = False
    for line in out.splitlines():
        if line.startswith(head):
            inside = True
            rows.append(line)
            continue
        if inside:
            if line.strip() and not line.startswith(" "):
                break
            rows.append(line)
    return rows


def codes_in(out: str, head: str) -> list[str]:
    return [line.strip().split()[0] for line in block(out, head)[1:] if line.strip()]


def add_financial(db, code: str, *, roe: float = 0.5, period: date = FY) -> None:
    FinancialRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "report_date": period,
                    "published_date": (
                        FY_PUBLISHED if period == FY else date(2024, 4, 28)
                    ),
                    "roe": roe,
                    "revenue_yoy": None,
                    "profit_yoy": None,
                    "dividend_yield": None,
                    "market_cap": None,
                    "source": "akshare",
                }
            ],
            # 显式 schema：仓库层对「日期列不是 pl.Date」当场拒绝，而 None 会让整列推成 Null 类型
            schema=FINANCIAL_SCHEMA,
        )
    )


def add_valuation(db, code: str, *, pe: float = 15.0, pb: float = 2.0, on: date = FRESH) -> None:
    ValuationRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "date": on,
                    "pe_ttm": pe,
                    "pb": pb,
                    "dividend_yield": None,
                    "market_cap": None,
                    "source": "akshare",
                }
            ],
            schema=VALUATION_SCHEMA,
        )
    )


def add(db, code: str, *, pe: float = 15.0, pb: float = 2.0, roe: float = 0.5) -> None:
    """一只「好票」：年报 ROE 与当日 PE 都齐，PR = pe ÷ roe ÷ 100。"""
    add_financial(db, code, roe=roe)
    add_valuation(db, code, pe=pe, pb=pb)


def add_master(db, count: int) -> None:
    SymbolRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": f"60000{i}",
                    "name": f"票{i}",
                    "list_status": "L",
                    "currency": "CNY",
                }
                for i in range(count)
            ]
        )
    )


# ---------- 第一行是范围 ----------


def test_the_first_lines_are_the_universe_then_the_hits(db, settings, capsys):
    add(db, "600000")
    add(db, "600001", pe=10.0)
    add_master(db, 9)
    capsys.readouterr()
    container = container_for(db, settings)
    assert run(container, scan([]).split()) == 0
    out = capsys.readouterr().out
    assert "已同步基本面的 2 只" in out
    assert "主档 9 只" in out, "universe 与主档总数都要出门，否则「没有命中」会被读成「市场不贵」"
    assert "sams sync financials" in out, "范围之外的那些票怎么进样本，得在同一屏说清"
    assert codes_in(out, "命中") == ["600001", "600000"]


def test_synced_codes_outside_the_master_are_not_counted_as_negative_range(db, settings, capsys):
    """库里同步过基本面的票**比主档在市的还多**时，那句范围话不能说反。

    `sync financials --code` 不要求主档有行，所以这状态是会发生的（退市票、或票池没同步）。
    「范围外的 -1 只」不只是难看：负号在这里意味着「主档里还有 -1 只没覆盖」，而真相是
    样本里有主档不认识的票——那句话把范围的反方向说成了同一个方向。
    """
    add_master(db, 1)  # 只有 600000 在市
    add(db, "600000")
    add(db, "600001")  # 有基本面与估值，主档里却没有
    capsys.readouterr()
    assert run(container_for(db, settings), scan([]).split()) == 0
    out = capsys.readouterr().out
    assert "-1 只" not in out, "负数的「范围外」是一句反话"
    assert "范围外的 1 只" not in out, "主档里没有的那一只不是「范围外等着被同步」的票"
    assert "主档里查不到" in out
    assert "已同步基本面的 2 只" in out


def test_an_empty_master_admits_it_does_not_know_the_denominator(db, settings, capsys):
    """主档为空时不给 0：0 只会被读成「这个市场没有票」，而真正的原因是没同步主档。"""
    add(db, "600000")
    capsys.readouterr()
    assert run(container_for(db, settings), scan([]).split()) == 0
    out = capsys.readouterr().out
    assert "已同步基本面的 1 只" in out
    assert "主档 0 只" not in out


def test_nothing_scanned_at_all_is_a_failure_not_an_empty_answer(db, settings, capsys):
    capsys.readouterr()
    assert run(container_for(db, settings), scan([]).split()) == 1
    out = capsys.readouterr().out
    assert "sams sync financials" in out


def test_nothing_scanned_at_all_is_json_too(db, settings, capsys):
    code = run(container_for(db, settings), scan(["--json"]).split())
    payload = json.loads(capsys.readouterr().out)
    assert (payload["status"], payload["exit"], payload["universe_size"]) == ("empty", 1, 0)
    assert code == 1


# ---------- 排除要点名 ----------


def test_every_excluded_code_is_named_with_its_reason(db, settings, capsys):
    add(db, "600000")
    add_financial(db, "600001", period=date(2024, 3, 31))
    add_valuation(db, "600001")
    capsys.readouterr()
    run(container_for(db, settings), scan([]).split())
    out = capsys.readouterr().out
    assert codes_in(out, "排除") == ["600001"]
    assert "roe-basis latest" in out, "下一步命令要真的能让那只票进结果"


def test_a_negative_pe_stock_is_excluded_by_default_and_named(db, settings, capsys):
    add(db, "600000")
    add(db, "600001", pe=-8.0, pb=1.0)
    capsys.readouterr()
    run(container_for(db, settings), scan([]).split())
    out = capsys.readouterr().out
    assert codes_in(out, "命中") == ["600000"]
    assert codes_in(out, "排除") == ["600001"]
    assert "预估" in " ".join(block(out, "排除"))


def test_include_estimated_puts_the_pb_branch_back_with_its_basis_labelled(db, settings, capsys):
    add(db, "600000")
    add(db, "600001", pe=-8.0, pb=1.0)  # PB 支：1 / 0.5 / 0.5 / 100 = 0.04
    capsys.readouterr()
    run(container_for(db, settings), scan(["--include-estimated"]).split())
    out = capsys.readouterr().out
    assert codes_in(out, "命中") == ["600001", "600000"]
    assert any("pb_estimate" in row for row in block(out, "命中")[1:]), (
        "混进同一张表时，那一行必须自己声明它是预估那一支"
    )


def test_the_staleness_of_a_valuation_row_is_printed_with_its_date(db, settings, capsys):
    """停牌两个月的票「有 PE」，但那个 PE 不属于今天：给它的 PR 是一篮子不同日期的数。"""
    add(db, "600000")
    add_financial(db, "600001")
    add_valuation(db, "600001", on=date(2024, 5, 1))
    capsys.readouterr()
    run(container_for(db, settings), scan([]).split())
    out = capsys.readouterr().out
    assert codes_in(out, "排除") == ["600001"]
    assert "2024-05-01" in " ".join(block(out, "排除"))


def test_a_code_with_no_valuation_row_names_the_sync_command(db, settings, capsys):
    add_financial(db, "600000")
    capsys.readouterr()
    run(container_for(db, settings), scan([]).split())
    assert "sams sync valuation" in " ".join(block(capsys.readouterr().out, "排除"))


# ---------- 口径跟着数出门 ----------


def test_the_denominator_basis_is_stated_next_to_the_results(db, settings, capsys):
    add(db, "600000")
    capsys.readouterr()
    run(container_for(db, settings), scan([]).split())
    out = capsys.readouterr().out
    assert "十二个月" in " ".join(block(out, "分母口径")), (
        "D62：不说分母是哪一种周期，排序就没有可比性"
    )


def test_the_basis_flag_changes_both_the_answer_and_its_claim(db, settings, capsys):
    add_financial(db, "600000", period=date(2024, 3, 31))
    add_valuation(db, "600000")
    capsys.readouterr()
    assert run(container_for(db, settings), scan(["--roe-basis", "latest"]).split()) == 0
    out = capsys.readouterr().out
    assert "三个月" in " ".join(block(out, "分母口径")), "换口径就要换那句声明，它不是脚注"
    assert codes_in(out, "命中") == ["600000"]


def test_each_hit_row_carries_its_period_and_valuation_date(db, settings, capsys):
    add(db, "600000")
    capsys.readouterr()
    run(container_for(db, settings), scan([]).split())
    row = block(capsys.readouterr().out, "命中")[1]
    assert str(FY) in row and str(FRESH) in row, "两个粒度各说各的日期，跟每一行走（D60）"


def test_the_fallouts_are_counted_separately_from_the_exclusions(db, settings, capsys):
    add(db, "600000")
    add(db, "600001", pe=100.0)  # PR = 2.0：算得出，只是不便宜
    capsys.readouterr()
    run(container_for(db, settings), scan([]).split())
    out = capsys.readouterr().out
    assert "落选 1 只" in out, "落选与排除是两件事：前者是「不便宜」，后者是「不知道」"
    assert block(out, "排除") == []


# ---------- JSON ----------


def test_the_json_payload_is_the_same_scan_as_the_text(db, settings, capsys):
    add(db, "600000")
    add_financial(db, "600001", period=date(2024, 3, 31))
    add_valuation(db, "600001")
    capsys.readouterr()
    code = run(container_for(db, settings), scan(["--json"]).split())
    payload = json.loads(capsys.readouterr().out)
    assert code == payload["exit"] == 0
    assert (payload["universe_size"], payload["market_size"]) == (2, None)
    assert (payload["strategy_id"], payload["roe_basis"]) == ("pr<=0.4|annual", "annual")
    assert [h["code"] for h in payload["hits"]] == ["600000"]
    assert payload["hits"][0]["basis"] == "pe"
    assert payload["hits"][0]["value"] == pytest.approx(0.3)
    assert payload["hits"][0]["period"] == str(FY)
    assert payload["hits"][0]["valuation_on"] == str(FRESH)
    assert [e["code"] for e in payload["excluded"]] == ["600001"]
    assert payload["caliber"] and payload["basis_note"]


def test_codes_narrow_the_universe_and_the_json_reports_the_narrowed_size(
    db, settings, capsys
):
    add(db, "600000")
    add(db, "600001")
    capsys.readouterr()
    run(container_for(db, settings), scan(["--codes", "600001,600000", "--json"]).split())
    assert json.loads(capsys.readouterr().out)["universe_size"] == 2
    capsys.readouterr()
    run(container_for(db, settings), scan(["--codes", "600001", "--json"]).split())
    payload = json.loads(capsys.readouterr().out)
    assert payload["universe_size"] == 1
    assert [h["code"] for h in payload["hits"]] == ["600001"]


def test_the_key_spellings_do_not_change_the_answer(db, settings, capsys):
    """`a`/`601088` 与 `A`/`601088` 给同一个结果（D50）：漏归一的键会让 universe 静默少一只。"""
    add_financial(db, "601088")
    add_valuation(db, "601088")
    capsys.readouterr()
    args = cli.build_parser().parse_args(
        ["screen", "pr", "--market", "a", "--pr-max", "0.4", "--codes", "601088", "--json"]
    )
    assert args.handler(container_for(db, settings), args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["market"], payload["universe_size"]) == ("A", 1)
    assert [h["code"] for h in payload["hits"]] == ["601088"]


def test_a_threshold_that_is_not_a_number_is_refused_as_json(db, settings, capsys):
    code = run(
        container_for(db, settings),
        ["screen", "pr", "--market", "A", "--pr-max", "零点四", "--json"],
    )
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["exit"], payload["status"]) == (2, 2, "refused")
    assert "0.4" in payload["reason"]


def test_the_scan_is_archived_under_its_own_strategy_id(db, settings, capsys):
    add(db, "600000")
    capsys.readouterr()
    container = container_for(db, settings)
    run(container, scan([]).split())
    repo = ScanResultRepo(db)
    assert repo.count() == 1
    run(container, scan([]).split())
    assert repo.count() == 1, "同一天同一条规则重跑覆盖，不留三份「今天扫出什么」"
    run(container, ["screen", "pr", "--market", "A", "--pr-max", "0.2"])
    assert repo.count() == 2, "门槛不同是另一条规则；覆盖掉前一次等于抹掉那个答案"
