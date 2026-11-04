"""方案自动监控（S4-B / D254–D257）：按日重跑启用的方案，只把新进名单的票推出去。

监控与「重跑」分管两件事：重跑回答「今天这一篮是谁」，监控还要记住上一篮是谁。
第一次运行（新启用、刚改条件）只写名单与存档，不发通知：把基线当变化，会让打开
开关的当晚收到几十条「新命中」，而那只是第一次看。

告警复用告警中心（`rule_id=None` + `payload.kind=screen`），因此去重、免打扰、
`notify_log` 与静音都只有一套。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import SamsError
from sams.core.notify import MID, AlertMessage, AlertSender
from sams.core.screener_fields import describe_all, field
from sams.services.screener import (
    ScreenerService,
    preset_strategy_id,
)
from sams.storage.repos.alert_mutes import MuteRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.screener_hits import ScreenerHitRepo
from sams.storage.repos.screener_presets import Preset, ScreenerPresetRepo

#: 告警列表与摘要按这个 `kind` 认出「选股新命中」（D256）
SCREEN_KIND = "screen"
SCREEN_RULE_PREFIX = "方案"


@dataclass(frozen=True)
class MonitorRun:
    preset_id: str
    name: str
    market: str
    day: date | None = None
    #: 这一趟只记基线、不推送（`last_run_at` 原本为 NULL）
    baseline: bool = False
    new: tuple[str, ...] = ()
    removed: int = 0
    alerts: int = 0
    muted: int = 0
    #: 非空 = 这一趟没跑（没有快照 / 票池为空），原因就在这里
    skipped: str = ""
    error: str = ""


@dataclass(frozen=True)
class MonitorSummary:
    market: str
    runs: tuple[MonitorRun, ...] = ()

    @property
    def new_codes(self) -> int:
        return sum(len(run.new) for run in self.runs)

    @property
    def alerts(self) -> int:
        return sum(run.alerts for run in self.runs)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(
            f"{run.market} {run.name}: {run.error}" for run in self.runs if run.error
        )

    @property
    def note(self) -> str:
        if not self.runs:
            return f"{self.market} 没有启用监控的方案，跳过"
        skipped = sum(1 for run in self.runs if run.skipped)
        # 基线趟数单独说话：第一次运行进来的票不是「变化」，把 36 只报成「新命中」
        # 会让刚打开开关的人以为系统替他做了决定（D255）
        baselines = sum(len(run.new) for run in self.runs if run.baseline)
        fresh = self.new_codes - baselines
        changes = []
        if fresh:
            changes.append(f"新命中 {fresh} 只")
        if baselines:
            changes.append(f"基线 {baselines} 只（第一次运行不推送）")
        if not changes:
            changes.append("新命中 0 只")
        note = (
            f"{self.market} 方案 {len(self.runs)} 个："
            + "、".join(changes)
            + f"，推送 {self.alerts} 条"
        )
        if skipped:
            note += f"，{skipped} 个跳过"
        if self.errors:
            note += f"，{len(self.errors)} 个失败"
        return note


class ScreenerMonitorService:
    def __init__(
        self,
        presets: ScreenerPresetRepo,
        screener: ScreenerService,
        hits: ScreenerHitRepo,
        scans: ScanResultRepo,
        mutes: MuteRepo,
        sender: AlertSender | None,
        *,
        base_url: str = "",
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.presets = presets
        self.screener = screener
        self.hits = hits
        self.scans = scans
        self.mutes = mutes
        self.sender = sender
        self.base_url = base_url.rstrip("/")
        self.clock = clock

    def run_market(self, market: str) -> MonitorSummary:
        """调度入口：一个方案的失败不带走其余方案，也不带走这一晚的摘要。"""
        runs: list[MonitorRun] = []
        for preset in self.presets.list_enabled(market=market):
            try:
                runs.append(self.run_preset(preset))
            except SamsError as exc:
                runs.append(
                    MonitorRun(
                        preset_id=preset.id,
                        name=preset.name,
                        market=preset.market,
                        error=str(exc),
                    )
                )
        return MonitorSummary(market=market, runs=tuple(runs))

    def run_preset(self, preset: Preset, *, notify: bool = True) -> MonitorRun:
        """`preset` 是调用方手里的那一份；监控状态（`last_run_at`、渠道）以库里的为准。

        每天由 `run_market` 重新读出来时两者一样；手动重跑拿着页面打开时的旧对象进来，
        不重读就会把「今晚已经跑过」看成第一次运行，整篮票再推一遍。
        """
        fresh = self.presets.get(preset.user_id, preset.id)
        if fresh is not None:
            preset = fresh
        result = self.screener.run(preset.market, list(preset.conditions))
        if result.day is None or result.universe_size == 0:
            return MonitorRun(
                preset_id=preset.id,
                name=preset.name,
                market=preset.market,
                day=result.day,
                skipped=result.note or f"{preset.market} 票池为空，跳过",
            )

        self.scans.record(
            on=result.day,
            market=preset.market,
            strategy_id=preset_strategy_id(preset.id, preset.conditions),
            universe_size=result.universe_size,
            hits=[
                {"code": hit.code, "name": hit.name, **hit.values}
                for hit in result.hits
            ],
        )
        synced = self.hits.sync(
            preset.user_id,
            preset.id,
            preset.market,
            [hit.code for hit in result.hits],
            result.day,
        )
        baseline = preset.last_run_at is None
        alerts = 0
        muted = 0
        if notify and not baseline and self.sender is not None:
            by_code = {hit.code: hit for hit in result.hits}
            for code in synced.new:
                if (
                    self.mutes.get(
                        preset.user_id, preset.market, code, kind=SCREEN_KIND
                    )
                    is not None
                ):
                    muted += 1
                    continue
                hit = by_code.get(code)
                if hit is None:
                    continue
                self._dispatch(preset, hit.code, hit.name, hit.values, result.day)
                alerts += 1
        self.presets.mark_run(preset.user_id, preset.id, self.clock.now_utc())
        return MonitorRun(
            preset_id=preset.id,
            name=preset.name,
            market=preset.market,
            day=result.day,
            baseline=baseline,
            new=synced.new,
            removed=synced.removed,
            alerts=alerts,
            muted=muted,
        )

    def _dispatch(
        self,
        preset: Preset,
        code: str,
        name: str | None,
        values: dict[str, float | None],
        day: date,
    ) -> None:
        assert self.sender is not None  # 调用方已经挡过一次
        conditions = describe_all(preset.conditions)
        readings = "、".join(
            f"{field(cond.field).label} {value:g}"
            for cond in preset.conditions
            if (value := values.get(cond.field)) is not None
        )
        label = f"{name}（{code}）" if name else code
        symbol_path = f"/symbol/{preset.market}/{code}"
        link = f"{self.base_url}{symbol_path}" if self.base_url else symbol_path
        body = "\n".join(
            part
            for part in (
                f"方案《{preset.name}》新命中：{label}",
                f"条件：{conditions}",
                f"本次读数：{readings}" if readings else "",
                f"个股页：{link}",
                "这是筛选结果，不是买卖建议。",
            )
            if part
        )
        message = AlertMessage(
            market=preset.market,
            code=code,
            severity=MID,
            triggered_at=self.clock.now_utc(),
            name=name or "",
            rule_name=f"{SCREEN_RULE_PREFIX}《{preset.name}》",
            value_text=label,
            threshold_text="自动监控新命中",
            action_note="点开个股页核对；这不是买卖建议",
            stage="hit",
            body=body,
            facts={
                "kind": SCREEN_KIND,
                "preset_id": preset.id,
                "preset_name": preset.name,
                "first_seen": day.isoformat(),
                "conditions": conditions,
                "symbol_url": symbol_path,
            },
        )
        self.sender.dispatch(
            message,
            user_id=preset.user_id,
            channel_ids=list(preset.channel_ids),
            rule_id=None,
        )
