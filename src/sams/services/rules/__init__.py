"""规则引擎（spec §10）：取数、模板、过滤、执行约束、一趟求值。

放在 `services` 而不是 `monitor` 是分层守卫决定的：引擎要读 `analysis/valuation/portfolio`
那些服务，而 `ALLOWED["monitor"] = {"core", "storage"}`。投递因此是一个注入进来的端口
（`core/notify.py::AlertSender`，T-14），`bootstrap` 把 `NotifyService.dispatch` 接上去——
与 D93 里 `HealthService` 收 `local_status` 同一个形状。

这里只转出 `RuleData`，其余逐个任务补（转出名单的终点见 T-17）。
"""

from sams.services.rules.snapshot import RuleData, Snapshot, Target

__all__ = ["RuleData", "Snapshot", "Target"]
