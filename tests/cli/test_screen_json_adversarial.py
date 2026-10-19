"""对抗性测试（M2-C 第二道闸 · CLI/JSON 出口）：`--json` 是机器出口，不是"能印中文"。

`D57` 那句话在这里被逐路验：每一个进程退出码都必须在 payload 里有同值的 `exit`；
参数错在 `--json` 下也要 JSON；空篮子 ≠ 没数据。这几件事任何一件漏，管道下游拿到的
是 `NaN`、`Infinity`、非零 `$?` 但没有 payload，或者一段中文——四种都会静默毁掉读取方。

`json.loads`（默认）能吃下 `NaN`/`Infinity`——所以本文件把 `parse_constant` 也接上，
把「不合法 JSON」当场炸出来（RFC 8259 的合法值里不含这两个，jq/JS 都会崩）。

写法与本仓库既有的 adversarial 文件一致：`test_should_*` 允许红，红的就是 finding。
全部离线。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
from support import FrozenClock, StubAdapter, sample_bars

import sams.cli as cli
from sams.bootstrap import build_container
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.symbols import SymbolRepo

FY = date(2023, 12, 31)
FY_PUBLISHED = date(2024, 3, 20)
Q1 = date(2024, 3, 31)
Q1_PUBLISHED = date(2024, 4, 26)
FRESH = date(2024, 7, 5)
TODAY = date(2024, 7, 9)

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


def scan(argv: list[str]) -> list[str]:
    return ["screen", "pr", "--market", "A", "--pr-max", "0.4", *argv]


def parse(out: str):
    """严格 JSON：Python 默认能吃下 `NaN`/`Infinity`，而 jq / JSON.parse / Go 会当场崩。

    `parse_constant` 让这两个非标准字面量在这里也炸——「payload 里出现了机器读不懂的 token」
    是这条命令的 D57 承诺里最贵的一种违约。
    """

    def _reject(name: str) -> None:
        raise AssertionError(f"payload 含非标准 JSON 常量 {name!r}：机器读者会崩")

    return json.loads(out, parse_constant=_reject)


_UNSET = object()


def add_financial(
    db,
    code: str,
    *,
    roe: float = 0.5,
    period: date = FY,
    published: date | None | object = _UNSET,
) -> None:
    """`published` 三态：默认按 period 补；显式 None 写 NULL 披露日；给日期用给的日期。"""
    if published is _UNSET:
        pub = FY_PUBLISHED if period == FY else date(2024, 4, 28)
    else:
        pub = published
    FinancialRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "report_date": period,
                    "published_date": pub,
                    "roe": roe,
                    "revenue_yoy": None,
                    "profit_yoy": None,
                    "dividend_yield": None,
                    "market_cap": None,
                    "source": "akshare",
                }
            ],
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


def add_good(db, code: str, *, pe: float = 15.0, roe: float = 0.5) -> None:
    add_financial(db, code, roe=roe)
    add_valuation(db, code, pe=pe)


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


# ---------- 1. 严格 JSON：`NaN`/`Infinity` 是这条闸的第一线 ----------


def test_should_refuse_nan_as_a_threshold(db, settings, capsys):
    """`--pr-max nan`：`float("nan")` 不抛、`nan <= 0` 是 False，所以当前通过；
    然后 payload 里的 `"threshold": NaN` 不是合法 JSON。
    参数错应当 rc=2 且 JSON 里的 reason 说清。"""
    add_good(db, "600000", pe=15.0, roe=0.5)
    capsys.readouterr()
    argv = ["screen", "pr", "--market", "A", "--pr-max", "nan", "--json"]
    code = run(container_for(db, settings), argv)
    cap = capsys.readouterr().out
    assert code == 2, (
        f"nan 不是合法门槛；现状 rc={code}，payload 里的 threshold 会变成 NaN（非标准 JSON）"
    )
    payload = parse(cap)
    assert payload["exit"] == code == 2


def test_should_refuse_infinity_as_a_threshold(db, settings, capsys):
    """`--pr-max inf`：同上一条。`inf <= 0` 是 False，通过；然后 payload 出现 Infinity。"""
    add_good(db, "600000", pe=15.0, roe=0.5)
    capsys.readouterr()
    argv = ["screen", "pr", "--market", "A", "--pr-max", "inf", "--json"]
    code = run(container_for(db, settings), argv)
    cap = capsys.readouterr().out
    assert code == 2, f"inf 不是合法门槛；现状 rc={code}"
    payload = parse(cap)
    assert payload["exit"] == code == 2


def test_nan_threshold_does_not_break_the_machine_reader_when_accepted(db, settings, capsys):
    """现状刻画：nan 通过校验、进入 payload、`parse_constant` 拦下——机器读者会当场崩。
    若上一条被修复，这条会跟着一起绿；现在它是红的，就是 finding。"""
    add_good(db, "600000", pe=15.0, roe=0.5)
    capsys.readouterr()
    argv = ["screen", "pr", "--market", "A", "--pr-max", "nan", "--json"]
    code = run(container_for(db, settings), argv)
    cap = capsys.readouterr().out
    # 若 status=refused 且 payload 合法，本条通过；若 payload 含 NaN/Infinity，parse 抛。
    parse(cap)
    payload = json.loads(cap)
    assert payload["exit"] == code, "每个 exit 都要有同值 return code（D57）"


def test_hits_value_cannot_be_infinity(db, settings, capsys):
    """极端输入下 PR 会溢出成 inf（pe=1e308 / roe=1e-9 / 100 = inf）；
    若那条 hit 进 payload，值就是 Infinity——机器读者会崩。"""
    add_financial(db, "600000", roe=1e-9)
    add_valuation(db, "600000", pe=1e308)
    capsys.readouterr()
    # threshold 用 inf：inf > inf 为 False，那条溢出的 hit 会进 hits（现状）
    argv = ["screen", "pr", "--market", "A", "--pr-max", "inf", "--json"]
    code = run(container_for(db, settings), argv)
    cap = capsys.readouterr().out
    if code == 2:
        # 若 inf 被拒（上一条 finding 修好之后），本条不需要看 hits
        return
    parse(cap)


# ---------- 2. 空篮子 / 空 universe：rc 与 payload 都要区分 ----------


def test_empty_universe_is_rc_1_and_json_says_empty(db, settings, capsys):
    """一条都没扫成（universe 为空）：rc 1，payload status="empty"，exit=1。"""
    capsys.readouterr()
    code = run(container_for(db, settings), scan(["--json"]))
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"], payload["status"]) == (1, 1, "empty")
    assert payload["universe_size"] == 0


def test_empty_universe_text_prints_the_honest_line(db, settings, capsys):
    """非 JSON 路径也要出「一条都没扫成」那句，让读者知道下一步是同步，不是市场贵。"""
    capsys.readouterr()
    code = run(container_for(db, settings), scan([]))
    out = capsys.readouterr().out
    assert code == 1
    assert "sams sync financials" in out, "下一步命令必须当场能跑"
    assert "一只都没扫成" in out


def test_scanned_but_empty_basket_is_rc_0_and_prints_the_empty_line(db, settings, capsys):
    """扫过了、只是没命中：rc=0，且屏幕上必须有那句「空篮子不等于「市场不便宜」」。"""
    add_good(db, "600000", pe=100.0, roe=0.5)  # PR = 2.0，算得出、只是贵
    capsys.readouterr()
    code = run(container_for(db, settings), scan([]))
    out = capsys.readouterr().out
    assert code == 0, "扫过而空 ≠ 没得扫；rc 应当是 0"
    assert "空篮子不等于" in out, "D64 那句必须打印：0 只命中不意味着市场不便宜"


def test_scanned_but_empty_basket_json_still_exit_zero(db, settings, capsys):
    add_good(db, "600000", pe=100.0, roe=0.5)
    capsys.readouterr()
    code = run(container_for(db, settings), scan(["--json"]))
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"]) == (0, 0)
    assert payload["status"] == "ok", "status 区分 empty（universe 为空）与 ok 但 hits 为空"
    assert payload["hits"] == []
    assert payload["priced_out"] == 1, "那只票是 priced_out 而不是 excluded"


# ---------- 3. 参数错：--json 下也是 JSON，rc=2 ----------


def test_bad_pr_max_is_valid_json_with_exit_2(db, settings, capsys):
    capsys.readouterr()
    code = run(container_for(db, settings), scan(["--pr-max", "零点四", "--json"]))
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"], payload["status"]) == (2, 2, "refused")


def test_zero_or_negative_threshold_is_refused(db, settings, capsys):
    capsys.readouterr()
    code = run(container_for(db, settings), scan(["--pr-max", "0", "--json"]))
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"]) == (2, 2)
    capsys.readouterr()
    code = run(container_for(db, settings), scan(["--pr-max", "-0.4", "--json"]))
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"]) == (2, 2)


def test_empty_codes_list_is_refused_as_json(db, settings, capsys):
    """`--codes ,,,` 拆不出任何代码 → ValidationError → 参数错路径。"""
    capsys.readouterr()
    code = run(container_for(db, settings), scan(["--codes", ",,", "--json"]))
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"], payload["status"]) == (2, 2, "refused")


def test_malformed_as_of_is_refused_as_json(db, settings, capsys):
    """`iso_date` 抛 ValidationError 且 run_pr 在 try 内调用它 ⇒ rc=2 + JSON。"""
    capsys.readouterr()
    code = run(container_for(db, settings), scan(["--as-of", "2024-7-9", "--json"]))
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"]) == (2, 2)


# ---------- 4. 每一路 exit 都与 return code 同值（D57） ----------


def test_analysis_fundamentals_json_exit_matches_return_code_for_every_path(
    db, settings, capsys
):
    """三只票：能算 PR 的（rc=0）、算不出的（rc=1）、参数错的（rc=2）——三路的 exit 字段
    都必须等于进程 return code。"""
    # rc=0
    add_good(db, "600000", pe=15.0, roe=0.5)
    capsys.readouterr()
    code = run(
        container_for(db, settings),
        ["analysis", "fundamentals", "--market", "A", "--code", "600000", "--json"],
    )
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"]) == (0, 0)
    assert payload["status"] == "ok"

    # rc=1（只有季报，annual 口径下拒绝）
    add_financial(db, "600002", roe=0.5, period=Q1, published=Q1_PUBLISHED)
    add_valuation(db, "600002", pe=15.0)
    capsys.readouterr()
    code = run(
        container_for(db, settings),
        ["analysis", "fundamentals", "--market", "A", "--code", "600002", "--json"],
    )
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"]) == (1, 1)
    assert payload["status"] == "refused"
    assert payload["pr"]["value"] is None
    assert payload["pr"]["reason"], "拒绝必须带原因（D59：0 与拒绝在 PR 刻度上差一个世界）"

    # rc=2（`--roe-estimate` 传百分数）
    capsys.readouterr()
    code = run(
        container_for(db, settings),
        [
            "analysis",
            "fundamentals",
            "--market",
            "A",
            "--code",
            "600000",
            "--roe-estimate",
            "15",
            "--json",
        ],
    )
    payload = parse(capsys.readouterr().out)
    assert (code, payload["exit"]) == (2, 2)
    assert payload["status"] == "refused"


# ---------- 5. universe 那句诚实话：读者从屏幕上能核对的数 ----------


def test_universe_line_uses_the_actual_scanned_count_not_the_master_size(
    db, settings, capsys
):
    add_good(db, "600000", pe=15.0, roe=0.5)
    add_good(db, "600001", pe=15.0, roe=0.5)
    add_master(db, 9)
    capsys.readouterr()
    run(container_for(db, settings), scan([]))
    out = capsys.readouterr().out
    assert "已同步基本面的 2 只" in out
    assert "主档 9 只" in out


def test_synced_codes_with_no_publication_date_reach_the_payload(db, settings, capsys):
    """D64 的出口：「已同步基本面的 N 只」里的 N 必须是库里真的同步过的只数。

    原来 universe 只数有已披露一期的票，于是另一只「同步了但披露日全为 NULL」的票
    在屏幕上彻底消失——既不在 N、也不在 dropped、也不在 excluded。
    """
    add_good(db, "600000", pe=15.0, roe=0.5)
    add_financial(db, "600001", roe=0.4, published=None)  # 全部 NULL 披露日
    capsys.readouterr()
    run(container_for(db, settings), scan(["--json"]))
    payload = parse(capsys.readouterr().out)
    assert payload["universe_size"] == 2, "同步过就得算进范围，一期都没披露也一样"
    assert payload["dropped_unpublished"] >= 1, (
        "NULL 披露日的同步行至少要被数出来一次；"
        "否则屏幕上『已同步基本面的 1 只』是谎话"
    )
    assert [e["code"] for e in payload["excluded"]] == ["600001"]
    assert "披露日" in payload["excluded"][0]["reason"]


def test_a_requested_code_that_is_not_in_the_library_is_named(db, settings, capsys):
    """`--codes` 打错一位：过去它被交集静默吃掉，屏幕上既不是命中也不是排除。"""
    add_good(db, "600000", pe=15.0, roe=0.5)
    capsys.readouterr()
    run(container_for(db, settings), scan(["--codes", "600000,600999", "--json"]))
    payload = parse(capsys.readouterr().out)
    assert payload["universe_size"] == 1
    assert payload["unmatched"] == ["600999"]
    capsys.readouterr()
    run(container_for(db, settings), scan(["--codes", "600000,600999"]))
    out = capsys.readouterr().out
    assert "600999" in out and "sync financials" in out, "文本出口也要点名并给出下一步"


# ---------- 6. archive 覆盖：不同 codes 子集撞同一条 strategy_id ----------


def test_a_subset_scan_says_out_loud_it_was_not_archived(db, settings, capsys):
    """屏幕上不能让人以为这次试跑也进了「今天那一篮子」（契约：子集扫描根本不存档）。

    `strategy_id` 里刻意不含 `--codes`：把 codes 编进 id 会让存档变成 N 行试跑，
    M3 的通知层不知道读哪一行。所以子集不存档，而这句话得当场说出来，不能沉默。
    """
    add_good(db, "600000", pe=15.0, roe=0.5)
    add_good(db, "600001", pe=10.0, roe=0.5)
    container = container_for(db, settings)
    capsys.readouterr()
    run(container, scan(["--codes", "600000", "--json"]))
    subset = parse(capsys.readouterr().out)
    assert subset["archived"] is False
    assert ScanResultRepo(db).count() == 0, (
        f"子集扫描写了存档（{ScanResultRepo(db).count()} 行）：全量那一条被它覆盖了"
    )
    capsys.readouterr()
    run(container, scan(["--json"]))
    full = parse(capsys.readouterr().out)
    assert full["archived"] is True
    assert ScanResultRepo(db).count() == 1

    capsys.readouterr()
    run(container, scan(["--codes", "600000"]))
    text = capsys.readouterr().out
    assert "不存档" in text, f"屏幕上没说这次试跑没进存档：{text}"


# ---------- 7. hits 每一行的字段都得是数（不能给字符串冒充） ----------


def test_hit_rows_do_not_have_prose_where_numbers_are_expected(db, settings, capsys):
    add_good(db, "600000", pe=15.0, roe=0.5)
    capsys.readouterr()
    run(container_for(db, settings), scan(["--json"]))
    payload = parse(capsys.readouterr().out)
    hit = payload["hits"][0]
    # 这些字段按 spec §8.2 都是「数值/日期/布尔」；文案（如「无」）冒进来就是违约
    for key in ("value", "roe_used", "pe_ttm", "pb"):
        v = hit[key]
        assert v is None or isinstance(v, (int, float)), f"{key} 必须是数或 null，收到 {v!r}"
    assert isinstance(hit["estimated"], bool)
    # 日期字段是 str 或 None，且 str 是 ISO 格式
    for key in ("period", "valuation_on"):
        v = hit[key]
        if v is not None:
            assert isinstance(v, str) and len(v) == 10, f"{key} 要 ISO 日期字符串，收到 {v!r}"
