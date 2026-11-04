"""证伪台账的用例层（D232 P0-3）。

一条假设属于一条 thesis；归属先在 `theses` 验过，仓库只认行。判一次读的是
**截至那一天的库**（同 D61 的 PIT 纪律）：拿今天重算出来的数去解释昨天的决定，
正是这套系统一直在防的那种后见之明。

数据不够时是 `unknown`，不是 `refuted`：两者都会让人想动手，但只有后者是证据。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sams.core.assumptions import MET, REFUTED, UNKNOWN, holds, metric_label
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_market
from sams.core.errors import StorageError, ValidationError
from sams.services.actors import resolve_actor
from sams.services.fundamentals import FundamentalRead, FundamentalService
from sams.storage.repos.assumptions import Assumption, AssumptionRepo
from sams.storage.repos.theses import ARCHIVED, Thesis, ThesisRepo
from sams.storage.repos.users import UserRepo

_METRIC_READERS: dict[str, Callable[[FundamentalRead], float | None]] = {
    "roe": lambda read: read.roe,
    "revenue_yoy": lambda read: read.revenue_yoy,
    "profit_yoy": lambda read: read.profit_yoy,
    "dividend_yield": lambda read: read.dividend_yield,
    "pe_ttm": lambda read: read.pe_ttm,
    "pb": lambda read: read.pb,
    "pr": lambda read: read.pr.value,
}


@dataclass(frozen=True)
class Refutation:
    """这一趟**刚刚**变成证伪的那一条：时间、读数、阈值都带上，正文不用再去库里拼。"""

    thesis_id: str
    market: str
    code: str
    metric: str
    op: str
    threshold: float
    value: float
    on: date


@dataclass(frozen=True)
class EvaluationReport:
    """一趟扫完的结果。`checked` 是判过的条数，`refuted` 只装**状态变了**的那些。"""

    checked: int
    refuted: tuple[Refutation, ...] = ()


class AssumptionService:
    def __init__(
        self,
        assumptions: AssumptionRepo,
        theses: ThesisRepo,
        fundamentals: FundamentalService,
        users: UserRepo,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.assumptions = assumptions
        self.theses = theses
        self.fundamentals = fundamentals
        self.users = users
        self.clock = clock

    def add(
        self,
        username: str,
        thesis_id: str,
        *,
        metric: str,
        op: str,
        threshold: float,
        due_on: date | None = None,
        note: str | None = None,
    ) -> Assumption:
        thesis = self._own(username, thesis_id)
        return self.assumptions.create(
            thesis_id=thesis.id,
            metric=metric,
            op=op,
            threshold=threshold,
            due_on=due_on,
            note=note,
            now=self.clock.now_utc(),
        )

    def list(self, username: str, thesis_id: str) -> list[Assumption]:
        thesis = self._own(username, thesis_id)
        return self.assumptions.list_for(thesis.id)

    def remove(self, username: str, thesis_id: str, assumption_id: str) -> None:
        thesis = self._own(username, thesis_id)
        found = self.assumptions.get(assumption_id)
        if found is None or found.thesis_id != thesis.id:
            raise StorageError(
                f"这条 thesis 下没有 id 为 {assumption_id!r} 的假设（属于别条或不存在）"
            )
        self.assumptions.delete(assumption_id)

    def evaluate(
        self, username: str, thesis_id: str, *, on: date | None = None
    ) -> list[Assumption]:
        """按 `on`（缺省今天）的读数把每一条都判一次，并把结论落回行上。"""
        thesis = self._own(username, thesis_id)
        day = on if on is not None else self.clock.today()
        judged, _ = self._judge(thesis, day)
        return judged

    def evaluate_user(
        self, username: str, *, on: date | None = None, market: str | None = None
    ) -> EvaluationReport:
        """日常扫一遍：某用户名下（可按市场收窄）每条活着的 thesis 的假设都判一次。

        告警只收「这一趟刚从非证伪翻成证伪」的那几条：每趟都重报一遍，收件箱会先教会人
        忽略它，第一次真出事时也没人看。数据不足不进 `refuted`——那不是证据（同 `evaluate`）。
        """
        user = resolve_actor(self.users, username)
        day = on if on is not None else self.clock.today()
        wanted = canonical_market(market) if market is not None else None
        checked = 0
        refuted: list[Refutation] = []
        for thesis in self.theses.list(user.id):
            if thesis.status == ARCHIVED:
                continue
            if wanted is not None and thesis.market != wanted:
                continue
            judged, hits = self._judge(thesis, day)
            checked += len(judged)
            refuted.extend(hits)
        return EvaluationReport(checked=checked, refuted=tuple(refuted))

    def _judge(
        self, thesis: Thesis, day: date
    ) -> tuple[list[Assumption], list[Refutation]]:
        read = self.fundamentals.read(thesis.market, thesis.code, as_of=day)
        now = self.clock.now_utc()
        judged: list[Assumption] = []
        refuted: list[Refutation] = []
        for item in self.assumptions.list_for(thesis.id):
            value = _value_of(read, item.metric)
            if value is None:
                status = UNKNOWN
            elif holds(item.op, value, item.threshold):
                status = MET
            else:
                status = REFUTED
            judged.append(
                self.assumptions.record(
                    item.id, status=status, value=value, on=day, now=now
                )
            )
            if status == REFUTED and item.status != REFUTED:
                refuted.append(
                    Refutation(
                        thesis_id=thesis.id,
                        market=thesis.market,
                        code=thesis.code,
                        metric=item.metric,
                        op=item.op,
                        threshold=item.threshold,
                        value=value,
                        on=day,
                    )
                )
        return judged, refuted

    def _own(self, username: str, thesis_id: str) -> Thesis:
        user = resolve_actor(self.users, username)
        return self.theses.require(user.id, thesis_id)


def _value_of(read: FundamentalRead, metric: str) -> float | None:
    reader = _METRIC_READERS.get(metric)
    if reader is None:
        raise ValidationError(
            f"没有叫 {metric!r} 的指标：能用的是 {'、'.join(_METRIC_READERS)}"
        )
    metric_label(metric)
    return reader(read)
