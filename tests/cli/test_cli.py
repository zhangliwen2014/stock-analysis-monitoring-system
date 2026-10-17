from __future__ import annotations

from datetime import date

import pytest
from support import StubAdapter, sample_bars

import sams.cli as cli
from sams.bootstrap import build_container
from sams.core.config import Settings
from sams.core.errors import DatasourceError, RateLimited
from sams.storage.db import Database
from sams.storage.migrate import apply_migrations, discover
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
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
    """每个替身都返回标了自己 source 的两日帧：能区分「谁取的数」。"""
    return {
        name: StubAdapter(name, markets, sample_bars(source=name))
        for name, markets in (
            ("akshare", ["A"]),
            ("eastmoney", ["A"]),
            ("yfinance", ["HK", "US"]),
        )
    }


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
