from __future__ import annotations

from datetime import date

import polars as pl
import pytest
from support import StubActionsAdapter, StubAdapter, sample_bars

import sams.cli as cli
from sams.bootstrap import build_container
from sams.core.config import Settings
from sams.core.errors import DatasourceError, RateLimited
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
