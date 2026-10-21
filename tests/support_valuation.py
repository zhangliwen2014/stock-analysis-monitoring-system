"""估值合成的样本数据：一份「库里铺了 300 天」的最小世界，读数用例与命令行用例共用。

`make_container` 把这份世界接到一个冻了时钟的容器上——命令行与服务用例都要对着
同一个世界跑，两份装配分开写，改了一边的数就再也对不上另一边说的话。

放这里而不是各自抄一份，是因为两边要验的是同一件事的两端（取哪些数 / 那句话怎么说），
而样本一旦分叉，两边就各自对着自己的数点头。形状与出处：

* 300 个**按日历日排开**的交易日：交易日历不在这条路上，`AnalysisService` 读的是库里的行。
  长度按 PR 分位的样本地板（250 天）倒推——短于它，分位用例测的就不是「排第几」。
* 收盘先涨后跌：单调上涨的序列里「前高 == 现价」，`pick_target` 会把那一路整个排除，
  「二者取近」这句话就永远测不到。
* 两期**年报**：只播一期的话，窗口前三分之二没有可采信的 ROE，可比样本掉到地板以下。
* 市值 6000 亿：资金流那一路按「净流入 ÷ 市值」打分，缺市值它就只能拒。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import support
from support_fundamentals import StubFundamentalsAdapter

from sams.bootstrap import build_container
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo

CODE = "601088"
DAYS = [date(2024, 1, 1) + timedelta(days=i) for i in range(300)]
TODAY = DAYS[-1]
RISE, FALL = 200, 100
CLOSES = [10.0 + 0.05 * i for i in range(RISE)] + [20.0 - 0.03 * i for i in range(1, FALL + 1)]
PRICE = CLOSES[-1]
PE = [30.0 - 0.03 * i for i in range(len(DAYS))]
PB = [3.0 - 0.003 * i for i in range(len(DAYS))]
MARKET_CAP = 6.0e11
FY22 = date(2022, 12, 31)
FY22_PUBLISHED = date(2023, 3, 20)
FY23 = date(2023, 12, 31)
FY23_PUBLISHED = date(2024, 3, 20)
ROE_NOW = 0.20
#: PE 一路走低、ROE 在窗口中段抬到 0.20 ⇒ 现在这一天是整段的最小值 ⇒ 分位 0，
#: 入场闸门（< 20%）过
PR_NOW = PE[-1] / ROE_NOW / 100.0


def bars(
    days: list[date], closes: list[float], *, market: str = "A", code: str = CODE
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": [market] * len(days),
            "code": [code] * len(days),
            "date": days,
            "open": closes,
            "high": [c + 0.10 for c in closes],
            "low": [c - 0.10 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(days),
            "amount": [c * 1000.0 for c in closes],
            "source": ["akshare"] * len(days),
        }
    )


def valuation(days: list[date], *, market: str = "A", code: str = CODE):
    return pl.DataFrame(
        {
            "market": [market] * len(days),
            "code": [code] * len(days),
            "date": days,
            "pe_ttm": PE[: len(days)],
            "pb": PB[: len(days)],
            "dividend_yield": [None] * len(days),
            "market_cap": [MARKET_CAP] * len(days),
            "source": ["akshare"] * len(days),
        }
    )


def financial_rows(*, market: str = "A", code: str = CODE) -> pl.DataFrame:
    """两期年报：2022 年那期在窗口开始之前就公开了，所以曲线上的每一天都有分母。"""
    return pl.DataFrame(
        [
            {
                "market": market,
                "code": code,
                "report_date": FY22,
                "published_date": FY22_PUBLISHED,
                "roe": 0.18,
                "revenue_yoy": 0.10,
                "profit_yoy": 0.10,
                "dividend_yield": 0.04,
                "market_cap": 5.0e11,
                "source": "akshare",
            },
            {
                "market": market,
                "code": code,
                "report_date": FY23,
                "published_date": FY23_PUBLISHED,
                "roe": ROE_NOW,
                "revenue_yoy": 0.10,
                "profit_yoy": 0.10,
                "dividend_yield": 0.04,
                "market_cap": MARKET_CAP,
                "source": "akshare",
            },
        ]
    )


def dividend(day: date, cash: float = 1.0, *, market: str = "A", code: str = CODE):
    return pl.DataFrame(
        {
            "market": [market],
            "code": [code],
            "ex_date": [day],
            "kind": ["dividend"],
            "cash_per_share": [cash],
            "ratio": [None],
            "price": [None],
            "src": ["akshare"],
            "note": [f"10派{cash * 10:.2f}元(含税)"],
        }
    )


def flow_rows(
    days: list[date],
    *,
    net: float,
    north: float | None = None,
    market: str = "A",
    code: str = CODE,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": [market] * len(days),
            "code": [code] * len(days),
            "date": days,
            "main_net_in": [net] * len(days),
            "north_net": [north] * len(days),
            "north_hold": [None] * len(days),
            "margin_bal": [None] * len(days),
            "source": ["akshare"] * len(days),
        }
    )


def make_container(db, settings, *, today: date = TODAY, notifier_factory=None, **seeded):
    """一个铺好样本库、时钟冻在 `today` 的容器：命令行与服务用例都对着同一个世界跑。

    放这里而不是各测试文件里抄一份，是因为两边要验的是同一条链的两端（服务给的数 /
    屏幕上那句怎么说）；两份装配一旦分叉，一边改的数就再也对不上另一边说的话。

    `notifier_factory` 是给通知那一路的后门：真通道会打网络，而这条链上每一个用例
    要断言的都是「路由把这条告警交给了哪个 kind」。
    """
    adapter = StubFundamentalsAdapter(["A"])
    container = build_container(
        settings,
        db=db,
        adapters={
            "akshare": adapter,
            "eastmoney": adapter,
            "yfinance": support.StubAdapter(
                "yfinance", ["HK", "US"], support.sample_bars(source="yfinance")
            ),
        },
        clock=support.FrozenClock(datetime(today.year, today.month, today.day, 15, tzinfo=UTC)),
        notifier_factory=notifier_factory,
    )
    container.registry.sleep = lambda _seconds: None
    seed(db, **seeded)
    return container


def seed(
    db,
    *,
    market: str = "A",
    code: str = CODE,
    days: list[date] | None = None,
    closes: list[float] | None = None,
    actions: pl.DataFrame | None = None,
    flow: pl.DataFrame | None = None,
    with_bars: bool = True,
    with_fundamentals: bool = True,
) -> None:
    """把上面那几帧写进库里：K 线 + 覆盖确认 + 两期年报 + 逐日估值（+ 可选资金流、除权）。

    覆盖确认是 `AnalysisService` 往前预热的边界（D36）：不播它，读到的序列会被截到请求段，
    表头那几行的均线就成了空的。
    """
    days = DAYS if days is None else days
    closes = CLOSES if closes is None else closes
    if with_bars:
        BarRepo(db).upsert(bars(days, closes, market=market, code=code))
        CorpActionsCoverageRepo(db).record(
            market,
            code,
            covered_from=days[0],
            covered_to=days[-1],
            full_history=True,
            src="akshare",
        )
    if actions is not None:
        CorpActionRepo(db).upsert_many(actions)
    if with_fundamentals:
        FinancialRepo(db).upsert(financial_rows(market=market, code=code))
        ValuationRepo(db).upsert(valuation(days, market=market, code=code))
    if flow is not None:
        FundingFlowRepo(db).upsert(flow)


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
