"""规则引擎（spec §10）：取数、模板、过滤、执行约束、一趟求值。

放在 `services` 而不是 `monitor` 是分层守卫决定的：引擎要读 `analysis/valuation/portfolio`
那些服务，而 `ALLOWED["monitor"] = {"core", "storage"}`。投递因此是一个注入进来的端口
（`core/notify.py::AlertSender`，T-14），`bootstrap` 把 `NotifyService.dispatch` 接上去——
与 D93 里 `HealthService` 收 `local_status` 同一个形状。

这里转出取数这一层的三个名字；其余逐个任务补（转出名单的终点见 T-17）。
"""

from sams.services.rules.engine import (
    Blocked,
    Raised,
    RuleEngine,
    RunReport,
    Skipped,
    Suppressed,
    action_for,
)
from sams.services.rules.filters import FILTERS, Gate, OpenAlert, run_filters
from sams.services.rules.notes import Fx, constraint_lines, holding_pnl
from sams.services.rules.snapshot import RuleData, Snapshot, Target
from sams.services.rules.templates import TABLE, Ask, Outcome

__all__ = [
    "Ask",
    "Blocked",
    "FILTERS",
    "Fx",
    "Gate",
    "OpenAlert",
    "Outcome",
    "Raised",
    "RuleData",
    "RuleEngine",
    "RunReport",
    "Skipped",
    "Snapshot",
    "Suppressed",
    "TABLE",
    "Target",
    "action_for",
    "constraint_lines",
    "holding_pnl",
    "run_filters",
]
