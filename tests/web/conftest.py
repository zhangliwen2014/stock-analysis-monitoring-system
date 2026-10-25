"""Web 测试基座：不联网的适配器、每市场一份日历替身、每线程一条上下文的容器。

不建 tests/web/support.py：`pythonpath=["tests"]` 下它会遮住根目录的 `support`
（任何 `import support` 都会拿错文件）。替身与夹具全住这里。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import support
from fastapi.testclient import TestClient

from sams.bootstrap import build_core
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.web.app import create_app
from sams.web.context import ThreadContexts


class FakeRuntime:
    """/healthz 读数与维护按钮的替身：web 层不该为页面去起真线程。"""

    def __init__(self) -> None:
        self.prune_calls = 0
        self.last_prune: str | None = None
        self.last_prune_at: datetime | None = None
        self.last_prune_error = ""

    def healthz(self) -> dict:
        reading = {"last_poll_at": None, "age_s": None, "source": None, "ok": True}
        return {
            "status": "ok",
            "db": {"ok": True, "error": None},
            "scheduler": {"beat_age_s": 1, "stale": False},
            "intraday": {"A": dict(reading), "HK": dict(reading), "US": dict(reading)},
            "version": "0.0.0+test",
            "uptime_s": 0,
        }

    def request_prune(self) -> bool:
        self.prune_calls += 1
        self.last_prune = "1 步、合计 3 行"
        self.last_prune_at = datetime.now(UTC)
        self.last_prune_error = ""
        return True


class StubCalendar:
    """工作日 09:30–15:00 全开：够回答「现在开不开市」，不联网。"""

    source = "stub"
    _TZ = {"A": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}

    def __init__(self, market: str) -> None:
        self.market = market
        self.tz = self._TZ[market]
        self.typical_windows = (SessionWindow("day", "09:30", "15:00"),)

    def days_for_year(self, year: int) -> list[CalendarDay]:
        days: list[CalendarDay] = []
        day = date(year, 1, 1)
        while day.year == year:
            if day.weekday() < 5:
                days.append(
                    CalendarDay(self.market, day, True, False, self.tz, self.typical_windows)
                )
            day += timedelta(days=1)
        return days


@pytest.fixture
def stub_sources():
    """与 tests/test_adversarial_services.py 的 stub_all 同一形状。"""
    return {
        "akshare": support.StubAdapter("akshare", ["A"], support.sample_bars(source="akshare")),
        "eastmoney": support.StubAdapter(
            "eastmoney", ["A"], support.sample_bars(source="eastmoney")
        ),
        "yfinance": support.StubAdapter(
            "yfinance", ["US", "HK"], support.sample_bars("US", "AAPL", "yfinance")
        ),
    }


@pytest.fixture
def make_web_app(migrated, stub_sources):
    made: list[tuple] = []

    def make_app(clock=None):
        core = build_core(migrated, adapters=stub_sources, clock=clock)
        providers = {market: StubCalendar(market) for market in core.settings.markets}
        contexts = ThreadContexts(core, providers=providers)
        app = create_app(FakeRuntime(), core=core, contexts=contexts)
        made.append((core, contexts))
        return SimpleNamespace(app=app, core=core, contexts=contexts)

    yield make_app
    for core, contexts in made:
        contexts.close_all()
        core.close()


@pytest.fixture
def make_app(make_web_app):
    """`make_web_app` 那个工厂的别名，只多了 `clock=`：要按注入的时钟装一台 app 的用例
    才证得出「今天」读的是时钟而不是 `date.today()`（两者在同一个进程里差一天是常态）。
    """
    return make_web_app


@pytest.fixture
def web_app(make_web_app):
    web = make_web_app()
    context = web.contexts.current()
    context.users.create("alice", "correct-horse-battery", is_admin=True)
    with TestClient(web.app) as client:
        yield SimpleNamespace(
            client=client,
            app=web.app,
            core=web.core,
            contexts=web.contexts,
            context=context,
            runtime=web.app.state.web.runtime,
        )


@pytest.fixture
def logged(web_app):
    """已登录的 alice：client 里带着三枚 cookie，csrf 取签名前的那段。"""
    response = web_app.client.post(
        "/login",
        data={"username": "alice", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    web_app.csrf = web_app.client.cookies.get("sams_csrf").rpartition(".")[0]
    return web_app
