"""§9.3 周复检：给每条 `opened`/`tracking` 的论重算一次分，再决定这一次能不能比。

判据不在这里——掉档的判定是 `templates.thesis_drift`，复检调的是同一个函数、同一个 `Ask`。
两份「掉一档」的定义早晚分叉，而分叉的症状是每周复检说没掉、规则说掉了。

复检**只发事件，不发消息**：`ThesisDrifted` 是一条记录，「观点漂移，请复核」那句话由
§9.2 派生的 `thesis.drift` 规则在 `rule check` 那一趟命中后投出去（走过滤、走冷却、走 D95
那个端口）。两边都投就是同一件事每周响两遍，而防刷屏是这套系统的生死线（§10.1）。

第一次跑必须**建**基线而不是比：基线是「建仓时那一眼」，而那三列（`baseline_*`）是 0016
才加上的，加上的时候每一行都是空的。不写基线，`thesis.drift` 就永远停在「还没有基线」，
那条派生规则从此不响，而派生给一条论时它默认是启用的（`RuleRepo.create` 的 `enabled`）。

两句谎都不许说（D96）：没有基线 ≠ 没漂移；这一趟没算出分（覆盖不到地板，D70）也 ≠ 没掉档。
前者这一趟只建、不比。没有分的时候不建基线——那三列写一次，钉错了改不回来；但分项照样比，
因为 `thesis_drift` 判的是分项：一条已有基线的论即使这趟没出总分，也比，也可能报漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import NotAdjustable, ValidationError
from sams.core.events import EventBus, ThesisDrifted
from sams.services.rules.snapshot import RuleData, Snapshot, Target
from sams.services.rules.templates import Ask, thesis_drift
from sams.storage.repos.theses import OPENED, TRACKING, Thesis, ThesisRepo

#: 复检只走这两档：`proposed`/`observing` 还没有「建仓时那一眼」，
#: `hit_*`/`invalidated`/`archived` 已经过去了（`set_baseline` 那头也拒归档的那条）
RECHECK_STATUSES = (OPENED, TRACKING)

BASELINE = "baseline"
DRIFT = "drift"
CLEAN = "clean"
PARTIAL = "partial"
NO_SCORE = "no_score"

#: 屏幕上那一列的词（T-18 的 `rule recheck` 打的是这张表）。五个状态说的都是「这一趟
#: 做了什么」，不是「这条论现在怎么样」——后者是那条论的状态机在回答，这里不越权
STATE_NAMES = {
    BASELINE: "建立基线",
    DRIFT: "观点漂移",
    CLEAN: "比过了",
    PARTIAL: "比不全",
    NO_SCORE: "还没有分",
}


@dataclass(frozen=True)
class RecheckRow:
    """一条论的一次复检。`message` 是屏幕上那一行，`state` 是它属于哪一类。"""

    thesis_id: str
    user_id: str
    market: str
    code: str
    name: str
    status: str
    state: str
    #: 复检**之后**库里那条基线（这一趟没写它就是原值；写了 BASELINE 那一行的就是刚落库的数）
    baseline_score: float | None
    score: float | None
    message: str
    #: 这一趟有没有写库（发事件不算）
    written: bool = False


@dataclass(frozen=True)
class RecheckReport:
    user_id: str
    as_of: date
    dry_run: bool
    rows: tuple[RecheckRow, ...] = ()

    def count(self, state: str) -> int:
        return sum(1 for row in self.rows if row.state == state)

    @property
    def drifted(self) -> tuple[RecheckRow, ...]:
        return tuple(row for row in self.rows if row.state == DRIFT)


class Rechecker:
    """一周一次的维护动作：建基线、比基线、把掉档记成事件。"""

    def __init__(
        self,
        *,
        theses: ThesisRepo,
        data: RuleData,
        bus: EventBus,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.theses = theses
        self.data = data
        self.bus = bus
        self.clock = clock

    def run(
        self,
        user_id: str,
        *,
        as_of: date | None = None,
        dry_run: bool = False,
        thesis_id: str | None = None,
    ) -> RecheckReport:
        day = as_of if as_of is not None else self.clock.today()
        targets = self._select(user_id, thesis_id)
        rows = tuple(self._one(thesis, day, dry_run) for thesis in targets)
        return RecheckReport(user_id=user_id, as_of=day, dry_run=dry_run, rows=rows)

    def _select(self, user_id: str, thesis_id: str | None) -> list[Thesis]:
        if thesis_id is None:
            found = list(self.theses.list(user_id, status=OPENED))
            found += list(self.theses.list(user_id, status=TRACKING))
            return found
        thesis = self.theses.require(user_id, thesis_id)
        if thesis.status not in RECHECK_STATUSES:
            allowed = "/".join(RECHECK_STATUSES)
            raise ValidationError(
                f"{thesis.id} 现在是 {thesis.status}，不在复检范围内（只比 {allowed}）："
                "没建过仓的论没有「建仓时那一眼」可比，静默返回零行会被读成复检过了"
            )
        return [thesis]

    def _one(self, thesis: Thesis, as_of: date, dry_run: bool) -> RecheckRow:
        target = Target(thesis.user_id, thesis.market, thesis.code, None, thesis.id)
        try:
            snapshot = self.data.read(target, as_of=as_of)
        except NotAdjustable as exc:
            # 与引擎同一句理由（T-14 那一轮补的）：一条读不到的论不该把整趟周复检打掉——
            # 打掉的代价是剩下的那些论这一周没人看。这一条记成「还没有分」，把读不出分的原因
            # 原样带出去，而不是留下一个空白行让人以为复检过
            return _row(
                thesis,
                NO_SCORE,
                f"这一趟读不到这个对象（{exc}）：基线与比较都等读到再说，"
                "「读不到」不是「没漂移」",
                name=thesis.code,
                score=None,
            )
        if thesis.baseline_score is None:
            return self._lay_baseline(thesis, snapshot, as_of, dry_run)
        return self._compare(thesis, snapshot, dry_run)

    def _lay_baseline(
        self, thesis: Thesis, snapshot: Snapshot, as_of: date, dry_run: bool
    ) -> RecheckRow:
        if snapshot.score is None:
            return _row(
                thesis,
                NO_SCORE,
                "这一趟没合成出分（分项覆盖不到地板，D70）：基线与比较都等它有分再说——"
                "现在写下的「基线」会把一条论钉死在一个永远达不到的数上",
                name=snapshot.name,
                score=None,
            )
        if not dry_run:
            self.theses.set_baseline(
                thesis.user_id,
                thesis.id,
                score=snapshot.score,
                items=dict(snapshot.items),
                on=as_of,
                now=self.clock.now_utc(),
            )
        return _row(
            thesis,
            BASELINE,
            f"基线建立：{snapshot.score:g} 分（{as_of}，{len(snapshot.items)} 个分项）。"
            "这一趟只记下「当时那一眼」，下一次复检才有得比"
            + ("（试运行：没有写库）" if dry_run else ""),
            name=snapshot.name,
            score=snapshot.score,
            written=not dry_run,
            # 复检之后库里那条：写了就是刚落库这个数；干跑什么都没写，还是那条空的
            baseline_score=None if dry_run else snapshot.score,
        )

    def _compare(self, thesis: Thesis, snapshot: Snapshot, dry_run: bool) -> RecheckRow:
        got = thesis_drift(Ask(snapshot=snapshot, params={}, thesis=thesis))
        if got.hit is not None:
            if not dry_run:
                self.bus.publish(
                    ThesisDrifted(
                        thesis_id=thesis.id,
                        user_id=thesis.user_id,
                        detail=got.hit.why,
                        ts=self.clock.now_utc(),
                    )
                )
            return _row(
                thesis,
                DRIFT,
                got.hit.why + ("（试运行：没有发事件）" if dry_run else ""),
                name=snapshot.name,
                score=snapshot.score,
                written=False,
            )
        if got.why_not:
            return _row(
                thesis,
                PARTIAL,
                got.why_not,
                name=snapshot.name,
                score=snapshot.score,
            )
        now_text = (
            f"{snapshot.score:g} 分" if snapshot.score is not None else "总分这一趟没出"
        )
        return _row(
            thesis,
            CLEAN,
            f"比过了：基线 {thesis.baseline_score:g} 分，{now_text}，分项没有掉档",
            name=snapshot.name,
            score=snapshot.score,
        )


def _row(
    thesis: Thesis,
    state: str,
    message: str,
    *,
    name: str,
    score: float | None,
    written: bool = False,
    baseline_score: float | None = None,
) -> RecheckRow:
    # `baseline_score` 不给就用论上那条：这一趟没动基线的行，库里那条就是复检之后那条
    return RecheckRow(
        thesis_id=thesis.id,
        user_id=thesis.user_id,
        market=thesis.market,
        code=thesis.code,
        name=name,
        status=thesis.status,
        state=state,
        baseline_score=(
            thesis.baseline_score if baseline_score is None else baseline_score
        ),
        score=score,
        message=message,
        written=written,
    )
