"""M3-D 的验收：一个模拟交易日，从日线到告警、从心跳到健康页走一遍。

这一枚验的不是「每个零件都对」（那由前面各自的用例回答），而是**它们排成一晚之后
仍然对**：数据 job 写进去的行被规则读到、失败的 job 不挡住后面的市场、常驻循环认领了
心跳、同一晚再一拍不重复写账也不重复打扰。这类事故只在「拼起来」的粒度上露头。

时钟冻在 2024-10-25 23:00 UTC。那一瞬间三个市场各自拿到的到期日都是 10-25，但**不
是同一条路**：A 与 HK 的本地已经是 10-26（周六），靠 `daily_due` 的回看落到周五；
美股本地还是 10-25。触发时点由 `WeekdayProvider` 的常规时段（对所有市场都是
09:30–15:00）加上各市场配置的盘后偏移算出：A 与 HK 是 15:30 本地（= 07:30 UTC），
美股是 16:00 EDT（= 20:00 UTC）——23:00 UTC 三个都已过了，`fx` 要的「当天开市的市
场都收盘」也满足（D108）。10-25 又是那一 ISO 周的最后一个交易日（下一个 10-28 已进
第 44 周），所以周频的复检也到期。

库里只铺到 **10-24**，且第一个用例里只有 A 股挂了标的：A 的那五条数据 job 与 `rules@A`
都有真正要做的事，不是「全都已跑过」的空转。HK/US 的 job 照样登记（D106 看的是市场
配了什么源，不是谁的自选股里有什么），它们在第一枚里是「跑到点、没标的可收」也记为
ok，在第二枚里才真的带着标的去撞一个被限流的源。

数据源替身必须**五个方法都在**。`chain_for` 按方法存在与否路由（D38）：只有日线的桩
会让 `financials`/`valuation`/`flow` 三个 job 根本不登记，验收就悄悄退化成「只验了日线」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import support_valuation as sample
from support import StubAdapter, sample_bars
from support_scheduler import WeekdayProvider

from sams.core.errors import RateLimited
from sams.core.notify import NotifyResult
from sams.services.scheduler.heartbeat import HEARTBEAT_STALE_S
from sams.services.scheduler.runner import SKIPPED
from sams.storage.repos.bars import BarRepo

MOMENT = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
AS_OF = date(2024, 10, 25)
USER = "trader"
PWD = "correct-horse-battery"
#: 样本库只铺到 10-24：今晚那一条得由 job 自己去取，否则「规则读到刚写进去的那根」
#: 这句话就没有反例可挡
HISTORY = [day for day in sample.DAYS if day <= date(2024, 10, 24)]
HISTORY_CLOSES = sample.CLOSES[: len(HISTORY)]
#: 昨晚那根的收盘（样本序列走到这里正在下滑）与今晚 `WorldAdapter` 给的那根（恒定
#: `PRICE`）之间的中线。光看「有没有告警」分不开这两根：`stop.fixed` 的 `pre` 档带宽
#: 是 3%（`PRE_BAND`），17.06 与 17.00 都够得到，所以线卡在中间还不够——真正钉死「读的
#: 是今晚那根」的是下面那条 `stage == "hit"`：昨晚那根在线上方，只给得出 `pre`
SEED_LAST_CLOSE = HISTORY_CLOSES[-1]
STOP_LINE = (SEED_LAST_CLOSE + sample.PRICE) / 2


def weekdays(start: date, end: date) -> list[date]:
    days, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


class WorldAdapter:
    """A 股的五能力源：每个方法都要在，`chain_for` 才登记对应的 job。"""

    name = "world"
    markets = frozenset({"A"})
    corp_actions_full_history = True

    def daily_bars(self, market, code, start, end):
        days = weekdays(start, end)
        return sample.bars(days, [sample.PRICE] * len(days), market=market, code=code)

    def corp_actions(self, market, code, start, end):
        return sample.dividend(AS_OF, market=market, code=code)

    def financials(self, market, code, start, end):
        return sample.financial_rows(market=market, code=code)

    def valuation(self, market, code, start, end):
        return sample.valuation(weekdays(start, end), market=market, code=code)

    def funding_flow(self, market, code, start, end):
        return sample.flow_rows(
            weekdays(start, end), net=1.0e8, market=market, code=code
        )


def providers_for(settings) -> dict:
    """每个市场一张不看网络的日历：工作日开市，收盘时点由 `WeekdayProvider` 给。"""
    return {
        market: WeekdayProvider(market, cfg.tz)
        for market, cfg in settings.markets.items()
    }


def recording_factory(kind, config):
    """通知出口的后门：真通道会打网络，而这一枚要断言的是「发出去了」这个结论。"""

    class Node:
        name = kind

        def available(self):
            return True

        def unavailable_reason(self):
            return ""

        def send(self, message):
            return NotifyResult(ok=True, attempts=1)

    return Node()


def build_world(db, settings, *, yfinance=None, notifier_factory=None):
    world = WorldAdapter()
    return sample.make_container(
        db,
        settings,
        moment=MOMENT,
        adapters={
            "akshare": world,
            "eastmoney": world,
            "yfinance": yfinance
            or StubAdapter(
                "yfinance", ["HK", "US"], sample_bars("HK", "00700", source="yfinance")
            ),
        },
        providers=providers_for(settings),
        notifier_factory=notifier_factory,
        days=HISTORY,
        closes=HISTORY_CLOSES,
    )


def test_one_trading_day_converges_to_one_alert_and_a_fresh_heartbeat(db, settings):
    container = build_world(db, settings, notifier_factory=recording_factory)
    user = container.users.create(USER, PWD)
    container.watchlists.add(user.id, "A", sample.CODE)
    node = container.notify.add(user.id, "wecom", "主群", {"url": "https://x/key=k"})
    container.rule_service.add(
        user.id,
        kind="stop.fixed",
        name="跌破止损",
        target_type="symbol",
        target_ref=f"A/{sample.CODE}",
        params={"price": STOP_LINE},
        channel_ids=[node.id],
    )

    reports = []
    assert container.scheduler.run_forever(on_tick=reports.append, max_ticks=1) == 1
    report = reports[0]

    # 五个能力都在的源才登记得出 A 的那五条数据 job：`WorldAdapter` 少一个方法，
    # 这一晚会安静地退化成「只验了日线」（D38 的按方法路由 + D106 的按能力登记）
    assert [job.title for job in container.scheduler.jobs] == [
        "bars@A",
        "actions@A",
        "financials@A",
        "valuation@A",
        "flow@A",
        "recheck@A",
        "rules@A",
        "bars@HK",
        "recheck@HK",
        "rules@HK",
        "bars@US",
        "recheck@US",
        "rules@US",
        "fx@全局",
        "calendar@A",
        "calendar@HK",
        "calendar@US",
    ]

    # 没有「还没到点」，也没有「算不出到期日」：这一晚每一个登记的 job 都真的到期了
    assert report.waiting == () and report.errors == ()
    assert [result.status for result in report.results] == ["ok"] * len(report.results)
    assert [(r.job, r.market) for r in report.results] == [
        (spec.name, spec.market) for spec in container.scheduler.jobs
    ]

    # 库里铺到 10-24，所以今晚这根是新的——「全都已跑过」的空转跑不出这个数
    by_key = {(r.job, r.market): r for r in report.results}
    assert by_key[("bars", "A")].rows_affected == 1
    assert BarRepo(db).latest_date("A", sample.CODE) == AS_OF

    # 告警的档位与数值都只能来自今晚刚写进去的那一根：昨晚那根（17.06）在止损线上方，
    # 只给得出 `pre`；线本身又在线下方，`hit` 加 `17.00` 才是「规则读到今晚」的形状
    alerts = container.alerts_store.list_for(user.id)
    assert len(alerts) == 1
    assert alerts[0].code == sample.CODE
    assert alerts[0].stage == "hit"
    assert alerts[0].payload["value_text"].startswith(f"{sample.PRICE:.2f}")
    assert container.alerts_store.deliveries(alerts[0].id)[0].outcome == "sent"

    health = container.health.report()
    assert health.schedule is not None
    assert health.schedule.beat_age_s == 0
    assert not health.schedule.stale
    assert len(health.schedule.jobs) == len(container.scheduler.jobs)
    assert all(
        job.latest is not None and job.latest.status == "ok"
        for job in health.schedule.jobs
    )

    # 同一晚再一拍（把钟推过「失联」阈值再多一分钟，让「这一拍留没留痕迹」可分辨）：
    # 成功的周期不重跑，不写新账，也不重复打扰
    run_count = container.scheduler.runs.count()
    container.clock.advance(seconds=HEARTBEAT_STALE_S + 60)
    again = container.scheduler.tick()
    assert len(again.results) == len(container.scheduler.jobs)
    assert [result.status for result in again.results] == [SKIPPED] * len(again.results)
    assert container.scheduler.runs.count() == run_count
    assert len(container.alerts_store.list_for(user.id)) == 1
    # 心跳是每一拍写的，不是只有认领那一次：推过阈值再多一分钟，读到的仍是「刚打过」
    beat = container.health.report().schedule
    assert beat is not None and beat.beat_age_s == 0 and not beat.stale


def test_one_market_failure_does_not_stop_the_rest(db, settings):
    """港股的源被限流，带走的是港股自己那几条：A 的日线、A 的规则与全局的汇率照跑。

    这一条是「隔离」这件事的端到端版本——`runner.py` 那一层接住异常不往外抛，
    只有拼上一晚才看得见它挡住了后面的市场。
    """
    broken = StubAdapter(
        "yfinance", ["HK", "US"], RateLimited("yfinance", "429 too many requests")
    )
    container = build_world(db, settings, yfinance=broken)
    user = container.users.create(USER, PWD)
    container.watchlists.add(user.id, "A", sample.CODE)
    container.watchlists.add(user.id, "HK", "00700")

    report = container.scheduler.tick()

    by_key = {(result.job, result.market): result for result in report.results}
    assert report.errors == (), "到期计算本身不该坏；坏的是港股的源"
    assert by_key[("bars", "A")].status == "ok"
    assert by_key[("bars", "HK")].status == "failed"
    assert "429" in by_key[("bars", "HK")].error
    assert by_key[("rules", "A")].status == "ok"
    assert by_key[("fx", None)].status == "ok"
    assert BarRepo(db).latest_date("A", sample.CODE) == AS_OF
    # 账本那一行也得是失败：健康页的 `[调度]` 与 `sams run jobs` 读的是它，不是屏幕上这一条
    ledger = container.scheduler.runs.latest_per_period("bars", "HK", limit=1)
    assert ledger[0].status == "failed"
    assert "429" in ledger[0].error
