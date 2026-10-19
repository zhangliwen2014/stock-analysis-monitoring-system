from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl
import pytest
from support import StubActionsAdapter, StubAdapter, sample_bars

import sams.cli as cli
from sams.bootstrap import build_container
from sams.core.config import Settings
from sams.core.errors import DatasourceError, EmptyResponse, RateLimited
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.storage.db import Database
from sams.storage.migrate import apply_migrations, discover
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.symbols import SymbolRepo

PASSWORD = "correct horse battery"

CONFIG = """
[storage]
db_path = "{db_path}"

[general]
secret_key = "test-secret"

[markets.A]
tz = "Asia/Shanghai"
currency = "CNY"
post_close_min = 30
sources = ["akshare", "eastmoney"]

[markets.HK]
tz = "Asia/Hong_Kong"
currency = "HKD"
sources = ["yfinance"]
"""


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    path.write_text(CONFIG.format(db_path=tmp_path / "sams.duckdb"), encoding="utf-8")
    monkeypatch.setenv("SAMS_CONFIG", str(path))
    return path


def test_migrate_then_again_is_a_no_op(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    assert "0001_init.sql" in capsys.readouterr().out
    assert cli.run(["db", "migrate"]) == 0
    assert "已是最新" in capsys.readouterr().out
    # 数量取自迁移目录：加迁移不该改这个断言
    assert cli.run(["db", "status"]) == 0
    assert f"已应用 {len(discover())} 个" in capsys.readouterr().out


def test_user_lifecycle_through_cli(config_path, capsys):
    assert cli.run(["user", "add", "--username", "zhangsan", "--password", PASSWORD]) == 0
    assert "zhangsan" in capsys.readouterr().out

    assert cli.run(["user", "list"]) == 0
    assert "zhangsan" in capsys.readouterr().out

    assert cli.run(["login", "--username", "zhangsan", "--password", PASSWORD]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert len(token) > 20

    container = build_container(Settings.load(config_path))
    try:
        assert container.sessions.verify(token) is not None
    finally:
        container.close()


def test_bad_password_exits_1_without_leaking_a_token(config_path, capsys):
    cli.run(["user", "add", "--username", "zhangsan", "--password", PASSWORD])
    capsys.readouterr()
    assert cli.run(["login", "--username", "zhangsan", "--password", "totally-wrong-pw"]) == 1
    out = capsys.readouterr()
    assert "登录失败" in out.err and out.out == ""


def test_weak_password_and_unknown_user_are_reported(config_path, capsys):
    assert cli.run(["user", "add", "--username", "zhangsan", "--password", "short"]) == 1
    assert "至少" in capsys.readouterr().err
    assert cli.run(["user", "promote", "--username", "ghost"]) == 1
    assert "用户不存在" in capsys.readouterr().err


def test_disable_revokes_sessions(config_path, capsys):
    cli.run(["user", "add", "--username", "zhangsan", "--password", PASSWORD])
    cli.run(["login", "--username", "zhangsan", "--password", PASSWORD])
    token = capsys.readouterr().out.strip().splitlines()[-1]

    container = build_container(Settings.load(config_path))
    try:
        assert container.sessions.verify(token) is not None
        assert cli_run_on(container, ["user", "disable", "--username", "zhangsan"]) == 0
        assert container.sessions.verify(token) is None
    finally:
        container.close()


def cli_run_on(container, argv: list[str]) -> int:
    """复用已建好的容器跑一条命令：避免每个断言都重新打开库。"""
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def stub_adapters() -> dict:
    """每个替身都返回标了自己 source 的两日帧：能区分「谁取的数」。

    akshare 额外带公司行为能力（D38）：`sync actions` 的降级与跳过报告要有一条链可测。
    """
    built = {
        name: StubAdapter(name, markets, sample_bars(source=name))
        for name, markets in (
            ("akshare", ["A"]),
            ("eastmoney", ["A"]),
            ("yfinance", ["HK", "US"]),
        )
    }
    built["akshare"] = StubActionsAdapter("akshare", ["A"], sample_bars(source="akshare"))
    return built


def broken(name: str, markets) -> StubAdapter:
    """始终抛 429 的源：用来走降级分支。"""
    return StubAdapter(name, markets, RateLimited(name, "429 too many requests"))


@pytest.fixture
def container(config_path):
    settings = Settings.load(config_path)
    db = Database.open(settings.db_path)
    apply_migrations(db)
    built = build_container(settings, db=db, adapters=stub_adapters())
    # 退避重试是真 sleep，测试里把它换掉：降级路径要跑满 3 次重试才换源
    built.registry.sleep = lambda _seconds: None
    yield built
    built.close()


def sync_argv(code="600519"):
    return [
        "sync",
        "daily",
        "--market",
        "A",
        "--code",
        code,
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-03",
    ]


def test_sync_command_writes_bars_and_symbol(container, capsys):
    assert cli_run_on(container, sync_argv()) == 0
    out = capsys.readouterr().out
    assert "源=akshare" in out and "入库=2" in out
    assert BarRepo(container.db).count("A", "600519") == 2
    assert SymbolRepo(container.db).get("A", "600519")["currency"] == "CNY"


def test_sync_twice_keeps_row_count_stable(container):
    cli_run_on(container, sync_argv())
    cli_run_on(container, sync_argv())
    assert BarRepo(container.db).count() == 2, "验收 2：连跑两次行数一致"


def test_sync_falls_back_and_says_so(container, capsys):
    container.registry.adapters["akshare"] = broken("akshare", ["A"])
    assert cli_run_on(container, sync_argv()) == 0
    out = capsys.readouterr().out
    assert "源=eastmoney" in out and "降级: akshare✗ → eastmoney✓" in out
    assert container.registry.records["akshare"].consecutive_failures == 1


def test_source_failures_are_not_reported_as_a_stock_that_has_no_data(container, capsys):
    """交易员试用报告 P1：`sync daily 600028 2005→2026` 报「所有源均无数据」、退出码 0，
    而同期日志是 akshare/eastmoney 网络失败、sina 撞 1023 根上限、tencent 回空。
    「这个源挂了」与「这只票没数据」的下一步完全相反，屏幕上不能长一样。"""
    container.registry.adapters["akshare"] = broken("akshare", ["A"])
    container.registry.adapters["eastmoney"] = StubAdapter(
        "eastmoney", ["A"], EmptyResponse("eastmoney", "区间无数据")
    )
    assert cli_run_on(container, sync_argv()) == 1
    out = capsys.readouterr().out
    assert "429" in out and "不能断定" in out


def test_every_source_answering_no_data_is_a_clean_answer(container, capsys):
    """两只源都答「这段没有数据」（停牌、新股）：这是事实，不是故障，退出码 0。"""
    for name in ("akshare", "eastmoney"):
        container.registry.adapters[name] = StubAdapter(
            name, ["A"], EmptyResponse(name, "区间无数据")
        )
    assert cli_run_on(container, sync_argv()) == 0
    assert "所有源都回答" in capsys.readouterr().out


def actions_argv(code="601088"):
    return ["sync", "actions", "--market", "A", "--code", code]


def test_sync_actions_writes_rows_and_coverage(container, capsys):
    assert cli_run_on(container, actions_argv()) == 0
    out = capsys.readouterr().out
    assert "源=akshare" in out and "入库=1" in out and "全历史" in out
    assert CorpActionRepo(container.db).count() == 1
    claim = CorpActionsCoverageRepo(container.db).get("A", "601088", src="akshare")
    assert claim.full_history is True


def test_sync_actions_names_the_sources_it_skipped(container, capsys):
    """「0 条」与「这个源根本不给除权记录」必须长得不一样（D38）。"""
    container.registry.market_sources["A"] = ("eastmoney", "akshare")
    assert cli_run_on(container, actions_argv()) == 0
    assert "跳过 eastmoney：不提供公司行为" in capsys.readouterr().out


def test_sync_actions_refuses_a_chain_with_no_capability(container):
    """链上一个能答公司行为的源都没有：报错要说清是谁被跳过了，而不是「没有数据」。"""
    container.registry.market_sources["A"] = ("eastmoney",)
    with pytest.raises(DatasourceError, match="不提供公司行为"):
        cli_run_on(container, actions_argv())


def test_sync_actions_defaults_to_the_whole_history(container, capsys):
    """不给区间也要能跑：默认起点早于任何 A 股上市日，终点是今天。"""
    assert cli_run_on(container, actions_argv()) == 0
    assert "确认至=" in capsys.readouterr().out


def test_calendar_check_and_refresh(container, capsys):
    assert cli_run_on(container, ["calendar", "refresh", "--market", "HK", "--year", "2026"]) == 0
    assert "写入" in capsys.readouterr().out

    def check(day: str) -> str:
        assert cli_run_on(container, ["calendar", "check", "--market", "HK", "--date", day]) == 0
        return capsys.readouterr().out

    assert "交易日" in check("2026-12-23")
    assert "休市" in check("2026-01-01")
    half = check("2026-12-24")
    # 半日市 + UTC 换算：漏了 astimezone(UTC) 时这两行会打印同一个时间
    assert "半天" in half and "本地 09:30–12:00" in half and "UTC 12-24 01:30–12-24 04:00" in half
    assert CalendarRepo(container.db).year_covered("HK", 2026)


def test_health_command(container, capsys):
    assert cli_run_on(container, ["health"]) == 0
    out = capsys.readouterr().out
    assert "bars_daily" in out and "akshare" in out and "[日历覆盖]" in out


def test_health_reports_degraded_source(container, capsys):
    container.registry.adapters["akshare"] = broken("akshare", ["A"])
    container.registry.adapters["eastmoney"] = broken("eastmoney", ["A"])

    with pytest.raises(DatasourceError):
        container.registry.fetch_daily("A", "600519", date(2024, 1, 2), date(2024, 1, 3))
    assert cli_run_on(container, ["health"]) == 1
    assert "未恢复" in capsys.readouterr().out


def test_degraded_source_is_still_degraded_for_the_next_command(config_path, container, capsys):
    """健康状态必须跨进程：`sams health` 与采集不在同一个进程里跑。"""
    container.registry.adapters["akshare"] = broken("akshare", ["A"])
    result = container.registry.fetch_daily("A", "600519", date(2024, 1, 2), date(2024, 1, 3))
    assert result.degraded, "东财接了 akshare 的活，但 akshare 的失败要留下痕迹"
    capsys.readouterr()

    reopened = build_container(Settings.load(config_path))
    try:
        assert cli_run_on(reopened, ["health"]) == 1
    finally:
        reopened.close()
    out = capsys.readouterr().out
    assert "akshare" in out and "未恢复" in out


def test_unknown_config_file_is_reported_not_traced(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAMS_CONFIG", str(tmp_path / "missing.toml"))
    assert cli.run(["health"]) == 1
    assert "配置文件不存在" in capsys.readouterr().err


# ---------- `sams source ...`：M1 的数据源管理后台 ----------


def test_source_list_shows_the_seeded_chains(container, capsys):
    assert cli_run_on(container, ["source", "list"]) == 0
    out = capsys.readouterr().out
    assert "akshare → eastmoney → tencent → sina" in out
    assert "yfinance → tencent" in out, "验收：港美股有备源，且从 list 就能看出来"


def test_source_list_falls_back_to_the_config_when_the_table_is_empty(container, capsys):
    container.db.execute("DELETE FROM data_source_routes")
    container.db.execute("DELETE FROM data_sources")
    assert cli_run_on(container, ["source", "list"]) == 0
    out = capsys.readouterr().out
    assert "按配置文件" in out and "db migrate" in out
    assert "akshare → eastmoney" in out, "空表时要说清真正生效的是哪条链"


def test_source_kinds_lists_the_pluggable_implementations(container, capsys):
    assert cli_run_on(container, ["source", "kinds"]) == 0
    out = capsys.readouterr().out
    assert "tencent" in out and "sina" in out
    assert "参数 ['timeout']" in out, "可配参数要写在界面上，不然只能靠试"
    assert "来自 tencent.py" in out


def test_source_add_then_remove(container, capsys):
    add = ["source", "add", "tx2", "--kind", "tencent", "--markets", "HK"]
    assert cli_run_on(container, add) == 0
    assert "tx2" in capsys.readouterr().out
    assert cli_run_on(container, ["source", "list"]) == 0
    assert "yfinance → tencent → akshare_hk → tx2" in capsys.readouterr().out

    assert cli_run_on(container, ["source", "remove", "tx2"]) == 0
    assert "连带移除路由 1 条" in capsys.readouterr().out


def test_source_options_keep_their_type(container, capsys):
    add = ["source", "add", "sx", "--kind", "sina", "--markets", "A", "--opt", "timeout=7"]
    assert cli_run_on(container, add) == 0
    capsys.readouterr()
    assert cli_run_on(container, ["source", "list"]) == 0
    # 存成 "7" 会一路带到构造函数里：换成布尔参数时就不只是「侥幸能跑」的问题了
    assert "参数 {'timeout': 7}" in capsys.readouterr().out


def test_bad_add_is_reported_as_a_message_not_a_trace(config_path, capsys):
    assert cli.run(["source", "add", "evil", "--kind", "os.system", "--markets", "A"]) == 1
    assert "未注册" in capsys.readouterr().err
    capsys.readouterr()
    assert cli.run(["source", "add", "sx", "--kind", "sina", "--markets", "US"]) == 1
    assert "不支持市场" in capsys.readouterr().err


def test_disabling_the_last_source_of_a_market_is_refused(config_path, capsys):
    """这条命令的失败是它存在的理由：撤掉唯一的源在当下毫无症状，
    症状要到下一次采集才出现——那时已经没人知道是谁干的。
    """
    assert cli.run(["source", "move", "HK", "yfinance"]) == 0
    capsys.readouterr()
    assert cli.run(["source", "disable", "yfinance"]) == 1
    assert "完全取不到数" in capsys.readouterr().err
    assert cli.run(["source", "add", "tx9", "--kind", "tencent", "--markets", "HK"]) == 0
    capsys.readouterr()
    assert cli.run(["source", "disable", "yfinance"]) == 0
    assert "已停用 yfinance" in capsys.readouterr().out


def test_source_enable_puts_it_back_at_the_same_priority(config_path, capsys):
    cli.run(["source", "disable", "tencent"])
    capsys.readouterr()
    assert cli.run(["source", "enable", "tencent"]) == 0
    assert "yfinance → tencent" in capsys.readouterr().out


def test_probe_prints_the_failure_instead_of_raising(container, monkeypatch, capsys):
    from sams.datasource.sina import SinaAdapter

    monkeypatch.setattr(
        "sams.services.sources.build", lambda kind, options=None: SinaAdapter(get=_FakeSina())
    )
    assert cli_run_on(container, ["source", "probe", "sina", "--market", "A", "--code", "600519",
                                  "--start", "2024-01-02", "--end", "2024-01-05"]) == 0
    out = capsys.readouterr().out
    assert "OK 取到 2 根" in out, "试跑命令的退出码不该是网络状况的函数"


class _FakeSina:
    def __call__(self, url, params, *, timeout=15):
        return [
            {"day": f"2024-01-0{d}", "open": "1", "high": "2", "low": "0.5", "close": "1.5",
             "volume": "1000"}
            for d in (2, 3)
        ]


def test_health_shows_chains_and_flags_a_single_source_market(config_path, capsys):
    """health 要说的是「这个进程会用哪条链」，一个市场的单源状态要从这一屏看出来。"""
    # 走真实入口：cli.run 会先补齐迁移，链的权威来源是库里的记录
    assert cli.run(["source", "unroute", "HK", "tencent"]) == 0
    assert cli.run(["source", "unroute", "HK", "akshare_hk"]) == 0
    capsys.readouterr()

    assert cli.run(["health"]) == 0
    out = capsys.readouterr().out
    assert "[降级链]（库里的记录）" in out
    assert "akshare → eastmoney → tencent → sina" in out
    assert "yfinance  ⚠ 日线单源" in out, "每个源都健康 ≠ 有备源：这里必须看得见"


def test_a_lowercase_market_echoes_the_shape_that_was_written(config_path, capsys):
    """回显即归一（`calendar` 第四轮就定了这条规矩）：打印 `a` 的那一行会让人以为
    动的是另一个市场的链，而路由表里只有一份大写形态。
    """
    assert cli.run(["source", "unroute", "a", "sina"]) == 0
    out = capsys.readouterr().out
    assert "A 链里已移除 sina" in out and "a 链" not in out

    assert cli.run(["source", "route", "a", "sina", "--priority", "4"]) == 0
    assert "已把 sina 排到 A 的第 4 位" in capsys.readouterr().out

    assert cli.run(["source", "move", "a", "sina", "akshare", "eastmoney", "tencent"]) == 0
    assert "A 链已重排" in capsys.readouterr().out


def test_an_actions_only_source_is_not_counted_as_a_bars_backup(config_path, capsys):
    """akshare_hk 拉不到价格。把它当港股日线的第二个源，屏幕上就是「有两个源，正常」，
    而 ⚠ 日线单源永远不出现——D38 分能力记账就是为了这一屏不说假话。
    """
    assert cli.run(["source", "unroute", "HK", "tencent"]) == 0
    capsys.readouterr()

    assert cli.run(["health"]) == 0
    out = capsys.readouterr().out
    assert "yfinance → akshare_hk(仅公司行为)" in out
    assert "⚠ 日线单源" in out


def test_health_shows_when_the_last_bar_is_and_that_prices_are_raw(container, capsys):
    """行数说明不了能不能用：最后一天是哪天、是不是裸价，只有这一屏会说。"""
    assert cli_run_on(container, sync_argv()) == 0
    capsys.readouterr()

    assert cli_run_on(container, ["health"]) == 0
    out = capsys.readouterr().out
    assert "[数据]" in out
    assert "最新 2024-01-03" in out
    # 库里没有公司行为时跨除权日的区间涨跌是错的（专家复核 #1），必须在这里露头
    assert "corp_actions 0 行" in out and "裸价" in out


def test_health_names_the_stocks_whose_ex_rights_were_never_confirmed(container, capsys):
    """有 K 线、零覆盖确认的票在这一屏上必须与「一切正常」长得不一样。

    `corp_actions 47 行` 说明不了 600028 有没有除权：库里那 47 行可能一行都不属于它，
    而读取层的守卫对这种票只会拒绝出数。看不见这一格，用户以为自己有一库可用数据。
    """
    assert cli_run_on(container, sync_argv()) == 0
    assert cli_run_on(container, ["health"]) == 0
    out = capsys.readouterr().out
    assert "1 只有 K 线但除权记录从没确认过" in out

    # 确认过之后这一格就应当消失：它报的是缺口，不是一个恒定的口径提示
    assert cli_run_on(container, actions_argv("600519")) == 0
    assert cli_run_on(container, ["health"]) == 0
    assert "从没确认过" not in capsys.readouterr().out


def test_health_names_a_field_the_serving_sources_do_not_provide(container, capsys):
    """只有腾讯/新浪在服务时成交额整段为空——不是错误，但依赖它的规则永不触发。"""
    container.bars.upsert(
        sample_bars("HK", "00700").with_columns(pl.lit(None, dtype=pl.Float64).alias("amount"))
    )
    assert cli_run_on(container, ["health"]) == 0
    assert "成交额只有 0% 的行有值" in capsys.readouterr().out


def test_health_shows_negative_volume_rows(container, capsys):
    """负量原样入库是决定，量比会算出 62 倍是后果——中间这一环不能缺。"""
    container.bars.upsert(sample_bars().with_columns(pl.lit(-5.0).alias("volume")))
    assert cli_run_on(container, ["health"]) == 0
    assert "行成交量为负" in capsys.readouterr().out


def test_health_says_a_market_has_no_bars_yet(container, capsys):
    assert cli_run_on(container, ["health"]) == 0
    assert "（无日线，还没采集过）" in capsys.readouterr().out


# ---------------------------------------------------------------- 基本面覆盖


def _financial_row(**over) -> pl.DataFrame:
    row = {
        "market": "A",
        "code": "600519",
        "report_date": date(2023, 12, 31),
        "published_date": date(2024, 3, 20),
        "roe": 0.3,
        "revenue_yoy": None,
        "profit_yoy": None,
        "dividend_yield": None,
        "market_cap": None,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row], schema={
        "market": pl.Utf8, "code": pl.Utf8, "report_date": pl.Date,
        "published_date": pl.Date, "roe": pl.Float64, "revenue_yoy": pl.Float64,
        "profit_yoy": pl.Float64, "dividend_yield": pl.Float64,
        "market_cap": pl.Float64, "source": pl.Utf8,
    })


def _valuation_row(**over) -> pl.DataFrame:
    row = {
        "market": "A",
        "code": "600519",
        "date": date(2024, 7, 5),
        "pe_ttm": 20.0,
        "pb": 8.0,
        "dividend_yield": None,
        "market_cap": None,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row], schema={
        "market": pl.Utf8, "code": pl.Utf8, "date": pl.Date, "pe_ttm": pl.Float64,
        "pb": pl.Float64, "dividend_yield": pl.Float64, "market_cap": pl.Float64,
        "source": pl.Utf8,
    })


def test_health_reports_how_many_stocks_the_screening_rules_can_see(container, capsys):
    """`sams screen pr` 的输入是「同步过基本面的票」，不是全市场：这一屏要说这个数字。

    少了它，「今天没有一只满足 PR ≤ 0.4」会被读成「市场不便宜」，
    而真正的原因可能是只同步了 1 只。
    """
    container.financials_store.upsert(_financial_row())
    container.valuation_store.upsert(_valuation_row())
    assert cli_run_on(container, ["health"]) == 0
    out = capsys.readouterr().out
    assert "[基本面覆盖]" in out
    assert "财务 1 只" in out and "最新报告期 2023-12-31" in out
    assert "估值 1 只" in out and "最新 2024-07-05" in out
    assert "资金流 0 只" in out


def test_health_lists_a_market_whose_fundamentals_were_never_synced(container, capsys):
    """HK 一个数都没有时也要出现：缺席会被读成「覆盖不错」，而 0 只才是那条规则的输入边界。"""
    assert cli_run_on(container, ["health"]) == 0
    hk = [line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("HK")]
    assert any("财务 0 只" in line for line in hk)


def test_health_names_the_funding_flow_capability_gap(container, capsys):
    """D38 的续：能力比数据薄要出声。链上一家资金流源都没有时，`sync flow` 永远取不回东西。"""
    assert cli_run_on(container, ["health"]) == 0
    assert "没有资金流源" in capsys.readouterr().out


# --- `sams analysis return`：唯一一条过闸的读数路径 ---------------------------------


def analysis_argv(start="2024-07-01", end="2024-07-11", code="601088", market="A"):
    return [
        "analysis",
        "return",
        "--market",
        market,
        "--code",
        code,
        "--start",
        start,
        "--end",
        end,
    ]


def seed_crossing_ex_date(container, *, cover=True, cash=2.10):
    """601088 那一次：除息前收 42.00、每股派 2.10 ⇒ 参考价 39.90，除息当天收 40.40。

    裸价读作 −3.81%、交易所口径读作 +1.26% 的那一对数，就是这条命令要区分的两件事。
    """
    days = [date(2024, 7, 1) + timedelta(days=i) for i in range(11)]
    closes = [41.00, 41.20, 41.40, 41.60, 41.80, 42.00, 42.00, 40.40, 41.00, 41.40, 42.00]
    container.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * 11,
                "code": ["601088"] * 11,
                "date": days,
                "close": closes,
                "source": ["akshare"] * 11,
            }
        )
    )
    container.corp_actions.upsert_many(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "ex_date": [date(2024, 7, 8)],
                "kind": ["dividend"],
                "cash_per_share": [cash],
                "ratio": [None],
                "price": [None],
                "src": ["akshare"],
                "note": [f"10派{cash * 10:.2f}元(含税)"],
            }
        )
    )
    if cover:
        container.corp_actions_coverage.record(
            "A",
            "601088",
            covered_from=date(2024, 7, 1),
            covered_to=date(2024, 7, 11),
            full_history=False,
            src="akshare",
        )


#: 锚点那条文案要说「这三天休市」，就得真有一条日历行撑着；空日历时它只能说「无法判断」
_ASESSIONS = (SessionWindow("regular", "09:30", "15:00"),)


def seed_calendar(container, *, closed=()):
    """把 2024 年的工作日铺成开市日，`closed` 里的那些例外（不写行 = 已覆盖且休市）。"""
    closed = set(closed)
    days = []
    cursor = date(2024, 1, 1)
    while cursor <= date(2024, 12, 31):
        if cursor.weekday() < 5 and cursor not in closed:
            days.append(CalendarDay("A", cursor, True, False, "Asia/Shanghai", _ASESSIONS))
        cursor += timedelta(days=1)
    container.calendar_store.save(days, source="test")


def test_analysis_return_prints_both_calibers_and_which_one_to_trust(container, capsys):
    """守卫再严，敲不出来就等于没有：这条命令是复权数唯一的出口。"""
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_argv()) == 0
    out = capsys.readouterr().out
    # 42.00/41.00-1 = +2.44%（裸价）；42.00×(42.00/39.90)/41.00-1 = +7.83%（复权）
    assert "+7.83%" in out and "+2.44%" in out
    assert "采信这个" in out and "覆盖确认：akshare" in out
    assert "错数" in out


def test_analysis_return_refuses_an_unconfirmed_window(container, capsys):
    """没确认过的区间一个字都不给：半截裸价数比拒绝危险得多。"""
    seed_crossing_ex_date(container, cover=False)
    assert cli_run_on(container, analysis_argv()) == 1
    out = capsys.readouterr().out
    assert "拒绝出数" in out and "sync actions" in out
    assert "+2.44%" not in out


def test_analysis_return_says_which_two_bars_it_anchored_on(container, capsys):
    """请求 7-15 而库里到 7-11 为止：数照样给（几天缺口是常态），但锚点要写在脸上。

    7-12 与 7-15 在日历上是开市日，库里却没有那两根 K 线——这正是「数不是请求
    那个区间的数」，退出码因此不是 0：脚本不能把它当成一次正常的读数。
    """
    seed_crossing_ex_date(container)
    seed_calendar(container)
    assert cli_run_on(container, analysis_argv(end="2024-07-15")) == 3
    out = capsys.readouterr().out
    assert "这两根出的数" in out and "2024-07-11" in out
    assert "开市日没有 K 线" in out and "2024-07-12" in out
    assert "周末" not in out, "没查过日历就不许把缺口归给周末"
    # 「采信这个」不能悬空：锚点挪走时，那行得当场说清它采信的是哪一段
    assert "采信这个，但它算的是 2024-07-01~2024-07-11 这一段" in out


def test_analysis_return_charges_nothing_when_the_gap_is_only_rest_days(container, capsys):
    """缺口全是休市日时，这就是请求的那个区间：0 退出，且要说清依据来自日历。"""
    seed_crossing_ex_date(container)
    seed_calendar(container, closed=[date(2024, 7, 12), date(2024, 7, 15)])
    assert cli_run_on(container, analysis_argv(end="2024-07-15")) == 0
    out = capsys.readouterr().out
    assert "这两根出的数" in out and "都是休市日" in out


def test_analysis_return_admits_the_calendar_cannot_answer(container, capsys):
    """日历没铺到那一年时，「（周末、长假、停牌）」是猜的：猜就要说不知道。"""
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_argv(end="2024-07-15")) == 3
    out = capsys.readouterr().out
    assert "无法判断" in out and "calendar refresh" in out


def test_analysis_return_declares_the_adjustment_convention(container, capsys):
    """两个数并列而不说口径，读的人不知道自己采信的是哪一种复权。

    D48：那句话的名字必须由算式决定。因子法（D34）等价于**分红在除权日按交易所参考价
    买回该股**，而「现金到手不再投资」是另一个数——以前这里印的正是后者那句话，
    于是给的是再投资口径、承诺的是到手口径，两个都在屏幕上，没人知道自己信的是哪个。
    """
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_argv()) == 0
    out = capsys.readouterr().out
    assert "口径" in out and "除权参考价" in out and "再投资" in out
    # 起点 41.00、除权前收 42.00、每股派 2.10 ⇒ 参考价 39.90、终点 42.00
    reinvested = 42.00 * (42.00 / (42.00 - 2.10)) / 41.00 - 1
    cash_in_hand = (42.00 + 2.10) / 41.00 - 1
    assert f"{reinvested:+.2%}" in out
    assert f"{cash_in_hand:+.2%}" not in out
    # 放行这个数的那条确认做到哪天为止：不给日期，「覆盖确认」这四个字没有可核对的内容
    assert "记录截至 2024-07-11" in out


def seed_tiny_dividend(container, cash: float = 0.001):
    """一笔小到两位小数看不出的派息：两个数其实是不同的数，只是打印成了同一个。"""
    seed_crossing_ex_date(container, cash=cash)


def test_analysis_return_shows_more_digits_when_the_two_calibers_look_identical(
    container, capsys
):
    """「+2.44% vs +2.44%，采信复权」这句话没有信息量：那位小数正是差别所在。"""
    seed_tiny_dividend(container)
    assert cli_run_on(container, analysis_argv()) == 0
    out = capsys.readouterr().out
    assert "+2.4390%" in out and "+2.4415%" in out
    assert "四位小数" in out


def test_analysis_return_names_a_window_the_bars_do_not_cover(container, capsys):
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_argv(end="2024-12-31")) == 1
    assert "只覆盖" in capsys.readouterr().out


def test_analysis_return_takes_a_lowercase_market(container, capsys):
    """`--market a` 不许关掉 D37，也不许把三个仓库都查成空表。"""
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_argv(market="a")) == 0
    out = capsys.readouterr().out
    assert "+7.83%" in out and "A/601088" in out


def seed_hk_crossing_ex_date(container):
    """00700：除净前收 400.00、每股派 20.00 ⇒ 参考价 380.00、除净当天收 380.00。

    裸价 −5.00%、复权 0.00%。而港交所不为现金分红复权前收盘价，板上那天就是 −5.00%：
    同一句「与交易所公布的当日涨跌幅同一算法」在 A 股是真、在这里是假（D53）。
    """
    days = [date(2024, 7, 1) + timedelta(days=i) for i in range(11)]
    container.bars.upsert(
        pl.DataFrame(
            {
                "market": ["HK"] * 11,
                "code": ["00700"] * 11,
                "date": days,
                "close": [400.00] * 7 + [380.00] * 4,
                "source": ["yfinance"] * 11,
            }
        )
    )
    container.corp_actions.upsert_many(
        pl.DataFrame(
            {
                "market": ["HK"],
                "code": ["00700"],
                "ex_date": [date(2024, 7, 8)],
                "kind": ["dividend"],
                "cash_per_share": [20.0],
                "ratio": [None],
                "price": [None],
                "src": ["akshare_hk"],
                "note": ["每股派港币20.00元"],
            }
        )
    )
    container.corp_actions_coverage.record(
        "HK",
        "00700",
        covered_from=date(2024, 7, 1),
        covered_to=date(2024, 7, 11),
        full_history=False,
        src="akshare_hk",
    )


def test_the_exchange_claim_only_prints_where_that_exchange_actually_does_it(container, capsys):
    """同一句核对承诺，在 A 股能核对、在港股永远核对不上——而它以前无条件打印。

    交易员闸的 BLOCKER #1：`HK/00700` 那天板上就是裸价那个数（港交所不复权前收盘价），
    屏幕却说「复权这一行与交易所公布的当日涨跌幅同一算法」，把**对的**那个数标成错数。
    """
    seed_crossing_ex_date(container)
    seed_hk_crossing_ex_date(container)

    assert cli_run_on(container, analysis_argv()) == 0
    a_share = capsys.readouterr().out
    assert "同一算法" in a_share and "错数" in a_share

    assert cli_run_on(container, analysis_argv(market="HK", code="00700")) == 0
    hk = capsys.readouterr().out
    assert "同一算法" not in hk
    assert "-5.00%" in hk and "+0.00%" in hk
    assert "不为现金分红复权前收盘价" in hk
    assert "交易所公布的那个数就是它" in hk
    # A 股那句「含税」的丑话讲的是红利税，港股没有这件事
    assert "红利税" not in hk


def test_the_pair_names_the_ex_date_that_made_it_move(container, capsys):
    """「复权 −1.44% / 裸价 −6.38%」这一对数以前从不说是哪件事造成的（D54）。

    参考价、每股现金、因子——这三步守卫全都算过，不出去等于让读的人自己再去查一遍，
    而他查的那一遍通常就是绕过守卫的那一遍。
    """
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_argv()) == 0
    out = capsys.readouterr().out
    assert "除权  窗口内 1 个除权日：2024-07-08" in out
    assert "前收 42.00 → 参考价 39.90" in out
    assert "每股现金 2.1000" in out
    assert "因子 1.0526" in out


def test_a_cash_dividend_says_the_number_is_gross_of_tax(container, capsys):
    """源给的派息是**税前**额（`10派22.60元(含税,扣税后20.34元)`），复权数因此不是到手收益。

    持有不满一年要缴 10–20% 红利税：这句话不改算式（改了就丢掉与交易所公布值同一算法那条
    外部证据，D48），但它必须跟着数出门，否则高派息票的这个数会被直接当成交易员的 P&L。
    """
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_argv()) == 0
    out = capsys.readouterr().out
    assert "因子按含税派息折股" in out and "每股 2.1000" in out and "红利税" in out


def test_a_bonus_issue_says_the_share_count_changed(container, capsys):
    """送转给的是股数不是钱：跨那天的「每股成本」不再与之前可比，这一点没人说。"""
    days = [date(2024, 7, 1) + timedelta(days=i) for i in range(11)]
    container.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * 11,
                "code": ["601999"] * 11,
                "date": days,
                "close": [10.0] * 8 + [9.09, 9.09, 9.09],
                "source": ["akshare"] * 11,
            }
        )
    )
    container.corp_actions.upsert_many(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601999"],
                "ex_date": [days[8]],
                "kind": ["bonus"],
                "cash_per_share": [None],
                "ratio": [1.1],
                "price": [None],
                "src": ["akshare"],
                "note": ["10送1股"],
            }
        )
    )
    container.corp_actions_coverage.record(
        "A",
        "601999",
        covered_from=days[0],
        covered_to=days[10],
        full_history=False,
        src="akshare",
    )
    assert cli_run_on(container, analysis_argv(code="601999")) == 0
    out = capsys.readouterr().out
    assert "股数 ×1.1000" in out and "这段里股数变了" in out
    assert "每股成本" in out


def test_a_window_that_stops_just_short_of_an_unpriceable_action_says_so(container, capsys):
    """把窗口收窄到那条 `unknown` 之前，就得到一个「干净」的数——躲开拒绝不等于穿过守卫。

    00700 实测：止日 2023-01-04 ⇒ 复权=裸价 +16.68%、rc 0，而 01-05 那天躺着一条
    折不出数的实物分派。这一段确实没有需要折算的日子，数可以给（D39 的判据是按窗口），
    但「再往后一天就给不出了」必须跟着打印，否则屏幕在教人用收窄窗口换取一个没有守卫的数。
    """
    seed_crossing_ex_date(container)
    container.corp_actions.upsert_many(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "ex_date": [date(2024, 7, 12)],
                "kind": ["unknown"],
                "cash_per_share": [None],
                "ratio": [None],
                "price": [None],
                "src": ["akshare"],
                "note": ["以股代息（选股权）"],
            }
        )
    )
    assert cli_run_on(container, analysis_argv(end="2024-07-11")) == 0
    out = capsys.readouterr().out
    assert "止日之后 2024-07-12 有一条无法折算的公司行为" in out
    assert "以股代息（选股权）" in out


def test_a_refused_unpriceable_action_names_the_stock_and_the_way_out(container, capsys):
    """拒绝文案的两半：这句说的是谁（同时筛几只票时认得出主语），以及怎么出去。

    交易员按以前那句建议跑了 `sync actions`，回来还是同一条拒绝——`unknown` 这一行
    就是采集的结果，重采不会让它变成能折算的数；唯一的路是把窗口挪开，而它没人说过。
    """
    seed_crossing_ex_date(container)
    container.corp_actions.upsert_many(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "ex_date": [date(2024, 7, 10)],
                "kind": ["unknown"],
                "cash_per_share": [None],
                "ratio": [None],
                "price": [None],
                "src": ["akshare"],
                "note": ["以股代息"],
            }
        )
    )
    assert cli_run_on(container, analysis_argv(end="2024-07-11")) == 1
    out = capsys.readouterr().out
    assert "A/601088：" in out
    assert "止日 ≤ 2024-07-09" in out and "起日 ≥ 2024-07-10" in out
    assert "重新采集不会让它变成能折算的数" in out


def test_the_json_flag_is_the_machine_path_that_survives_a_pipe(container, capsys):
    """退出码 3 进了管道就没了（`echo $?` 拿的是 `grep` 的码），而筛选循环处处是管道。

    没有 `--json` 之前，机器唯一的路是正则中文文案，而 0 与 3 在那里分辨不出来。
    """
    seed_crossing_ex_date(container)
    seed_calendar(container)
    assert cli_run_on(container, analysis_argv(end="2024-07-15") + ["--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "anchor_outside_request" and payload["exit"] == 3
    assert payload["raw"] == pytest.approx(42.00 / 41.00 - 1)
    assert payload["ex_dates"][0]["date"] == "2024-07-08"
    assert payload["ex_dates"][0]["cash_per_share"] == pytest.approx(2.10)
    assert payload["exchange_folds_dividend"] is True
    assert "2024-07-12" in payload["gap"]["trading"]


def test_the_json_flag_reports_a_refusal_as_an_answer(container, capsys):
    """拒绝也是一种答案：机器读得到原因，才不必在 stderr 里找一行「错误:」。"""
    seed_crossing_ex_date(container, cover=False)
    assert cli_run_on(container, analysis_argv() + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused" and payload["exit"] == 1
    assert "覆盖确认" in payload["reason"]


def seed_ipo_jump(container):
    """上市第二天的 −30%：主板不可能，D37 因此拒绝——而它以前给的下一步根本跑不通。"""
    days = [date(2024, 7, 1) + timedelta(days=i) for i in range(11)]
    container.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * 11,
                "code": ["601999"] * 11,
                "date": days,
                "close": [10.0, 7.0] + [7.0] * 9,
                "source": ["akshare"] * 11,
            }
        )
    )
    container.corp_actions_coverage.record(
        "A",
        "601999",
        covered_from=days[0],
        covered_to=days[10],
        full_history=False,
        src="akshare",
    )


def test_the_listing_date_exemption_the_guard_offers_is_actually_a_flag(container, capsys):
    """D37 的拒绝写着「请传 listed_on 豁免」，而命令行没有这个参数（U18）：假建议比没建议更坏。"""
    seed_ipo_jump(container)
    assert cli_run_on(container, analysis_argv(code="601999")) == 1
    out = capsys.readouterr().out
    assert "--listed-on" in out

    assert cli_run_on(container, analysis_argv(code="601999") + ["--listed-on", "2024-07-01"]) == 0
    out = capsys.readouterr().out
    assert "-30.00%" in out
    assert "豁免了它起 10 个日历日内的跳变检查" in out
    assert "你传的" in out
    assert "没有 D37 那条" in out


def seed_master_listing_date(container, code="601999", *, listed_on=date(2024, 7, 1)):
    """把上市日写进主档：U18 之后这是「采集时填的」那一格，读数不再要求人重敲一遍。"""
    SymbolRepo(container.db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "name": "某新股",
                    "currency": "CNY",
                    "listed_on": listed_on,
                }
            ]
        )
    )


def test_the_masters_listing_date_exempts_without_any_flag(container, capsys):
    seed_ipo_jump(container)
    seed_master_listing_date(container)
    assert cli_run_on(container, analysis_argv(code="601999")) == 0
    out = capsys.readouterr().out
    assert "-30.00%" in out
    assert "主档里那格" in out
    assert "--listed-on" not in out, "没说「你给的」就别让人以为自己传过参数"


def test_the_listing_date_origin_travels_into_the_json(container, capsys):
    """`--json` 里那个日期得连来源一起给：豁免掉 D37 这件事在机器读的那一屏也是口径变更。"""
    seed_ipo_jump(container)
    seed_master_listing_date(container)
    assert cli_run_on(container, analysis_argv(code="601999") + ["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["listed_on"], payload["listed_on_source"]) == ("2024-07-01", "master")


def test_the_series_command_names_the_listing_date_origin(container, capsys):
    """两条出口同一个来源标记：只有区间那头说清，series 那头就是暗改口径。"""
    seed_crossing_ex_date(container)
    seed_master_listing_date(container, "601088")
    # 只点 `ma`：那 11 根没有 high/low，默认那套指标会先因缺列被拒（本用例测的是豁免的来源）
    argv = analysis_series_argv(extra=["--indicators", "ma", "--json"])
    assert cli_run_on(container, argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["listed_on"], payload["listed_on_source"]) == ("2024-07-01", "master")


def test_a_malformed_date_is_a_message_not_a_trace(config_path, capsys):
    """手滑打成 2024-7-1 不该看到一段栈：这是最常见的输入错误，不是程序错误。"""
    assert cli.run(analysis_argv(start="2024-7-1")) == 1
    err = capsys.readouterr().err
    assert "错误:" in err and "YYYY-MM-DD" in err



# --- `sams analysis series`：指标那条出口的命令行 -------------------------------------


def analysis_series_argv(start="2024-07-01", end="2024-07-11", code="601088", market="A",
                         extra=()):
    return [
        "analysis",
        "series",
        "--market",
        market,
        "--code",
        code,
        "--start",
        start,
        "--end",
        end,
        *extra,
    ]


def test_analysis_series_prints_the_table_with_its_anchor_and_warm_up(container, capsys):
    """闸在服务层再严，命令敲不出那三行口径也等于没有：锚、预热、`–` 的含义都要出门。"""
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_series_argv(extra=["--indicators", "ma,rsi"])) == 0
    out = capsys.readouterr().out
    assert "A/601088  day  2024-07-01 ~ 2024-07-11  表里 11 行" in out
    assert "后复权倍数锚在 2024-07-01" in out
    assert "覆盖确认：akshare，记录截至 2024-07-11" in out
    assert "样本不足，不是 0" in out
    # 请求从库里第一根起：没有预热根，递推指标的种子就是这一天——这条警告必须出声
    assert "往回多读 0 根" in out
    assert "没有预热根数" in out


def test_analysis_series_counts_the_warm_up_it_read(container, capsys):
    """请求 7-05 起：往回多读的 4 根要打印出来，屏幕必须说清指标吃到了哪一段。"""
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_series_argv(
        start="2024-07-05", extra=["--indicators", "ma"])) == 0
    out = capsys.readouterr().out
    assert "往回多读 4 根" in out
    assert "表里 7 行" in out


def test_analysis_series_shows_null_as_a_dash_not_a_zero(container, capsys):
    """11 根凑不满 ma20：屏幕上必须是 `–`。涂成 0 的话，均线会在开头几天画出假的金叉。"""
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_series_argv(extra=["--indicators", "ma"])) == 0
    out = capsys.readouterr().out
    assert "–" in out


def test_analysis_series_refuses_the_same_way_return_does(container, capsys):
    """同一道闸、同一个退出码：换一条出口，「没确认过就不出数」不能变成「照常给」。"""
    seed_crossing_ex_date(container, cover=False)
    assert cli_run_on(container, analysis_series_argv(extra=["--indicators", "ma"])) == 1
    out = capsys.readouterr().out
    assert "拒绝出数" in out and "sync actions" in out


def test_analysis_series_json_carries_the_provenance_and_keeps_nulls_null(container, capsys):
    """`--json` 是管道里那条机器路：口径字段要在 payload 里，null 不许被涂成 0。"""
    seed_crossing_ex_date(container)
    assert cli_run_on(container, analysis_series_argv(
        extra=["--json", "--indicators", "ma"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok" and payload["exit"] == 0
    assert payload["anchored_on"] == "2024-07-01"
    assert payload["warmup_bars"] == 0
    assert payload["covered_by"] == "akshare"
    assert payload["indicators"] == ["ma"]
    assert payload["rows"] == 11
    # ma20 在前 19 根都没有值：JSON 里也必须逐位是 null，而不是 0.0
    assert payload["table"][0]["ma20"] is None
    assert payload["table"][-1]["close_hfq"] is not None


def test_analysis_series_reports_an_unmarked_jump_on_this_path(container, capsys):
    """D37 在这条出口同样拦得下来：命令行给的是拒绝，不是一条「看起来正常」的错曲线。"""
    seed_ipo_jump(container)
    assert cli_run_on(container, analysis_series_argv(
        code="601999", extra=["--indicators", "ma"])) == 1
    out = capsys.readouterr().out
    assert "裸价变动" in out and "-30.0%" in out
