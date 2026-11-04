"""方案自动监控（S4-B / D254–D257）：第一次只记基线，之后每只新票一条告警。

监控与「重跑」不是一件事：重跑只回答「今天这一篮是谁」，监控还要记住上一篮是谁。
把第一天也当变化，会让刚打开开关的人当晚收到几十条「新命中」——那正是提醒失去
可信度的方式。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock

from sams.core.notify import AlertMessage
from sams.core.screener_fields import Condition
from sams.datasource.spot import SPOT_SCHEMA
from sams.services.screener import ScreenerService, preset_strategy_id
from sams.services.screener_monitor import ScreenerMonitorService
from sams.storage.repos.alert_mutes import MuteRepo
from sams.storage.repos.financials import FinancialRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.screener_hits import ScreenerHitRepo
from sams.storage.repos.screener_presets import ScreenerPresetRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo
from sams.storage.repos.users import UserRepo

DAY = date(2026, 10, 28)
NEXT = date(2026, 10, 29)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
CONDITIONS = [Condition("pr_now", "<=", 0.5)]


def _snap(code: str, *, pe_dyn: float = 5.0):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": DAY,
        "price": 10.0,
        "pct_chg": 1.0,
        "amount": 1e8,
        "turnover_rate": 1.0,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": pe_dyn,
        "pe_dyn": pe_dyn,
        "pb": 1.0,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


class Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[AlertMessage, str, tuple[str, ...], str | None]] = []

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
        is_test=False,
    ) -> str:
        self.calls.append((message, user_id, tuple(channel_ids), rule_id))
        return f"alert-{len(self.calls)}"


@dataclass
class World:
    db: object
    user_id: str
    presets: ScreenerPresetRepo
    screener: ScreenerService
    hits: ScreenerHitRepo
    scans: ScanResultRepo
    mutes: MuteRepo
    sender: Sender
    clock: FrozenClock
    service: ScreenerMonitorService

    def seed(self, *codes: str, day: date = DAY) -> None:
        SymbolListRepo(self.db).upsert_batch(
            "A",
            [
                {
                    "code": code,
                    "name": code,
                    "py_initials": code,
                    "py_full": code,
                    "currency": "CNY",
                }
                for code in codes
            ],
        )
        frame = pl.DataFrame(
            [{"market": "A", "taken_at": TAKEN, **_snap(code)} for code in codes],
            schema={
                "market": pl.Utf8,
                "taken_at": pl.Datetime("us", "UTC"),
                **SPOT_SCHEMA,
            },
        )
        SnapshotRepo(self.db).replace_day(market="A", day=day, taken_at=TAKEN, frame=frame)

    def preset(self, *, name: str = "便宜", enabled: bool = True, channels=("ch1",)):
        preset = self.presets.create(self.user_id, name, "A", CONDITIONS)
        if enabled:
            self.presets.set_monitor(
                self.user_id, preset.id, enabled=True, channel_ids=channels
            )
        return self.presets.get(self.user_id, preset.id)

    def run(self, preset):
        return self.service.run_preset(preset)


@pytest.fixture
def world(db):
    clock = FrozenClock(NOW)
    user = UserRepo(db).create("trader", "correct-horse-battery")
    presets = ScreenerPresetRepo(db, clock=clock)
    hits = ScreenerHitRepo(db)
    scans = ScanResultRepo(db)
    mutes = MuteRepo(db, clock=clock)
    sender = Sender()
    screener = ScreenerService(SnapshotRepo(db), SymbolListRepo(db), FinancialRepo(db))
    service = ScreenerMonitorService(
        presets, screener, hits, scans, mutes, sender, clock=clock
    )
    return World(
        db=db,
        user_id=user.id,
        presets=presets,
        screener=screener,
        hits=hits,
        scans=scans,
        mutes=mutes,
        sender=sender,
        clock=clock,
        service=service,
    )


def test_the_first_run_records_a_baseline_and_sends_nothing(world):
    world.seed("600519", "000001")
    preset = world.preset()

    run = world.run(preset)

    assert run.baseline is True
    assert run.new == ("000001", "600519")
    assert run.alerts == 0
    assert world.sender.calls == []
    assert world.hits.count(world.user_id, preset.id) == 2
    saved = world.scans.recent(
        market="A", strategy_id=preset_strategy_id(preset.id, preset.conditions), limit=1
    )
    assert saved and saved[0]["date"] == DAY


def test_the_second_run_alerts_once_for_each_new_code(world):
    world.seed("600519", "000001")
    preset = world.preset()
    world.run(preset)
    world.seed("600519", "000001", "300750", day=NEXT)
    world.clock.advance(days=1)

    run = world.run(preset)

    assert run.baseline is False
    assert run.new == ("300750",)
    assert run.alerts == 1
    message, user_id, channels, rule_id = world.sender.calls[0]
    assert user_id == world.user_id
    assert rule_id is None
    assert channels == ("ch1",)
    assert message.code == "300750" and message.market == "A"
    assert message.rule_name == "方案《便宜》"
    assert message.facts["kind"] == "screen"
    assert message.facts["preset_id"] == preset.id
    assert message.facts["preset_name"] == "便宜"
    assert message.facts["first_seen"] == NEXT.isoformat()
    assert "现值市赚率" in message.facts["conditions"]
    assert "/symbol/A/300750" in message.body


def test_running_the_same_day_again_sends_nothing(world):
    world.seed("600519", "000001")
    preset = world.preset()
    world.run(preset)
    world.seed("600519", "000001", "300750", day=NEXT)
    world.clock.advance(days=1)
    world.run(preset)
    assert len(world.sender.calls) == 1

    again = world.run(preset)

    assert again.new == () and again.alerts == 0
    assert len(world.sender.calls) == 1


def test_a_screen_muted_symbol_is_not_pushed(world):
    world.seed("600519", "000001")
    preset = world.preset()
    world.run(preset)
    world.seed("600519", "000001", "300750", day=NEXT)
    world.clock.advance(days=1)
    world.mutes.add(world.user_id, "A", "300750", kind="screen", note="这只我不看")

    run = world.run(preset)

    assert run.new == ("300750",)
    assert run.alerts == 0
    assert run.muted == 1
    assert world.sender.calls == []


def test_no_channels_still_writes_the_alert_for_the_inbox(world):
    world.seed("600519")
    preset = world.preset(channels=())
    world.run(preset)
    world.seed("600519", "300750", day=NEXT)
    world.clock.advance(days=1)

    run = world.run(preset)

    assert run.alerts == 1
    assert world.sender.calls[0][2] == ()


def test_changing_the_conditions_makes_the_next_run_a_baseline_again(world):
    world.seed("600519", "000001")
    preset = world.preset()
    world.run(preset)
    saved = world.presets.get(world.user_id, preset.id)
    assert saved is not None and saved.last_run_at is not None

    world.presets.update_conditions(
        world.user_id, preset.id, [Condition("pr_now", "<=", 0.4)]
    )
    fresh = world.presets.get(world.user_id, preset.id)
    assert fresh is not None and fresh.last_run_at is None

    world.seed("600519", "000001", "300750", day=NEXT)
    world.clock.advance(days=1)
    run = world.run(fresh)

    assert run.baseline is True
    assert run.alerts == 0
    assert world.sender.calls == []


def test_no_snapshot_is_skipped_and_does_not_become_a_baseline(world):
    preset = world.preset()

    run = world.run(preset)

    assert run.skipped
    assert "没有" in run.skipped
    assert world.presets.get(world.user_id, preset.id).last_run_at is None


def test_market_run_covers_only_enabled_presets(world):
    world.seed("600519")
    world.preset(name="开着")
    world.preset(name="关着", enabled=False)

    summary = world.service.run_market("A")

    assert [run.name for run in summary.runs] == ["开着"]
    assert summary.new_codes == 1


def test_the_baseline_run_says_baseline_in_the_note_not_new_hits(world):
    """第一次运行只记名单：把基线报成「新命中」，开开关那晚的这句话就与承诺相反。"""
    world.seed("600519")
    world.preset(name="开着")

    summary = world.service.run_market("A")

    assert "基线 1 只" in summary.note
    assert "新命中" not in summary.note


def test_the_note_counts_new_hits_only_for_runs_that_had_a_baseline(world):
    world.seed("600519")
    world.preset(name="开着")
    world.service.run_market("A")  # 记基线
    world.seed("000001")

    summary = world.service.run_market("A")

    assert "新命中 1 只" in summary.note
