"""`task_runs` 与 `scheduler_heartbeats`（spec §12）。

两个表放在一个模块里，因为它们是同一个问题的两半：
「上一轮跑成了什么」（task_runs）与「现在还有没有人在跑」（heartbeats）。
分开放的话，单实例守卫与健康页要同时认识两个模块，而它们读的是同一次时钟。

状态只有三种：`running` 是「这一跑还没收尾」；同一行超过六小时仍未收尾才是崩溃留下的
证据（D105），读取侧不能把每个 `running` 都当成死进程。`ok` 与 `failed` 是两种结论。
没有 `skipped`：「这一轮不该跑」由到期计算决定，不会先写一行再跳过——那会让「没跑过」
与「跑过但没做事」在健康页上长得一样。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

RUNNING, OK, FAILED = "running", "ok", "failed"
_FINISHED = (OK, FAILED)


@dataclass(frozen=True)
class TaskRun:
    id: str
    job_name: str
    market: str | None
    period_key: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    rows_affected: int | None
    error: str | None
    note: str | None


@dataclass(frozen=True)
class Heartbeat:
    id: str
    started_at: datetime
    beat_at: datetime
    note: str | None


_COLUMNS = (
    "id, job_name, market, period_key, started_at, finished_at, status,"
    " rows_affected, error, note"
)


def _to_run(row: dict) -> TaskRun:
    return TaskRun(
        id=str(row["id"]),
        job_name=str(row["job_name"]),
        market=row["market"],
        period_key=row["period_key"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=str(row["status"]),
        rows_affected=row["rows_affected"],
        error=row["error"],
        note=row["note"],
    )


class TaskRunRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def start(
        self, job_name: str, *, market: str | None = None, period_key: str | None = None
    ) -> str:
        if not job_name:
            raise ValidationError("job_name 不能为空：一行没有名字的执行记录不是证据")
        run_id = new_id()
        self.db.execute(
            "INSERT INTO task_runs (id, job_name, market, period_key, started_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, job_name, market, period_key, self.clock.now_utc(), RUNNING],
        )
        return run_id

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        rows_affected: int = 0,
        error: str | None = None,
        note: str | None = None,
    ) -> TaskRun:
        if status not in _FINISHED:
            raise ValidationError(
                f"未知 status={status!r}：收尾只能是 {list(_FINISHED)}；{RUNNING} 是起点"
            )
        self.db.execute(
            "UPDATE task_runs SET finished_at = ?, status = ?, rows_affected = ?,"
            " error = ?, note = ? WHERE id = ?",
            [self.clock.now_utc(), status, int(rows_affected), error, note, run_id],
        )
        row = self.get(run_id)
        assert row is not None
        return row

    def get(self, run_id: str) -> TaskRun | None:
        rows = self.db.query(f"SELECT {_COLUMNS} FROM task_runs WHERE id = ?", [run_id])
        return _to_run(rows.to_dicts()[0]) if rows.height else None

    def latest_for_period(
        self, job_name: str, market: str | None, period_key: str
    ) -> TaskRun | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM task_runs"
            " WHERE job_name = ? AND market IS NOT DISTINCT FROM ? AND period_key = ?"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1",
            [job_name, market, period_key],
        )
        return _to_run(rows.to_dicts()[0]) if rows.height else None

    def recent(self, job_name: str, market: str | None, *, limit: int = 3) -> list[TaskRun]:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM task_runs"
            " WHERE job_name = ? AND market IS NOT DISTINCT FROM ?"
            " ORDER BY started_at DESC, rowid DESC LIMIT ?",
            [job_name, market, int(limit)],
        )
        return [_to_run(row) for row in rows.to_dicts()]

    def latest_per_period(
        self, job_name: str, market: str | None, *, limit: int
    ) -> list[TaskRun]:
        """每个周期只报它最近的那一行，新到旧，最多 `limit` 个**周期**。

        `recent` 数的是行，而一个一直失败的周期每 `RETRY_GAP_MIN` 分钟就多写一行：
        按行数窗口，多大的数都会被同一个周期自己的重试占满。
        一个周期的结论就是它的最后一行：成功之后这个周期不再被重跑，
        所以最后一行不会被更早的行盖住。
        """
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM ("
            " SELECT *, rowid,"
            " row_number() OVER ("
            "   PARTITION BY period_key ORDER BY started_at DESC, rowid DESC"
            " ) AS rnk"
            " FROM task_runs WHERE job_name = ? AND market IS NOT DISTINCT FROM ?)"
            " WHERE rnk = 1 ORDER BY started_at DESC, rowid DESC LIMIT ?",
            [job_name, market, int(limit)],
        )
        return [_to_run(row) for row in rows.to_dicts()]

    def count(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM task_runs") or 0)


class HeartbeatRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def start(self, *, note: str | None = None) -> str:
        beat_id = new_id()
        now = self.clock.now_utc()
        self.db.execute(
            "INSERT INTO scheduler_heartbeats (id, started_at, beat_at, note)"
            " VALUES (?, ?, ?, ?)",
            [beat_id, now, now, note],
        )
        return beat_id

    def beat(self, beat_id: str) -> None:
        self.db.execute(
            "UPDATE scheduler_heartbeats SET beat_at = ? WHERE id = ?",
            [self.clock.now_utc(), beat_id],
        )

    def latest(self) -> Heartbeat | None:
        rows = self.db.query(
            "SELECT id, started_at, beat_at, note FROM scheduler_heartbeats"
            " ORDER BY beat_at DESC, rowid DESC LIMIT 1"
        )
        if rows.is_empty():
            return None
        row = rows.to_dicts()[0]
        return Heartbeat(
            id=str(row["id"]),
            started_at=row["started_at"],
            beat_at=row["beat_at"],
            note=row["note"],
        )
