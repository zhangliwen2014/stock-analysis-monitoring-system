"""`task_runs` 与 `scheduler_heartbeats`（spec §12）。

两个表放在一个模块里，因为它们是同一个问题的两半：
「上一轮跑成了什么」（task_runs）与「现在还有没有人在跑」（heartbeats）。
分开放的话，单实例守卫与健康页要同时认识两个模块，而它们读的是同一次时钟。

状态只有三种：`running` 是「这一跑还没收尾」；同一行超过六小时仍未收尾才是崩溃留下的
证据（D105），读取侧不能把每个 `running` 都当成死进程。`ok` 与 `failed` 是两种结论。
新进程认领心跳时会把上一进程留下的 `running` 一次结掉（`close_interrupted`），所以
「六小时」这条判据管的是一轮常驻循环**之内**的并发读，不是允许孤儿行留三天。
没有 `skipped`：「这一轮不该跑」由到期计算决定，不会先写一行再跳过——那会让「没跑过」
与「跑过但没做事」在健康页上长得一样。
"""

from __future__ import annotations

from collections.abc import Sequence
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
    stopped_at: datetime | None = None


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


#: 结孤儿时写进 `error` 的那句话。`sams health` 打印 `error` 的前 60 个字符，
#: 所以这一句要自己站得住，不是一个标签。
INTERRUPTED = "上一进程未收尾：本进程启动时结为失败"


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

    def close_interrupted(self) -> int:
        """把所有还挂着 `running` 的行结为 `failed`，返回结掉几行。

        只在**新进程认领心跳之后**调用：DuckDB 单写者，能走到这里就说明没有别的进程
        在写，那么库里任何一行 `running` 都不可能是「正在进行」，只能是上一个进程被杀
        时留下的。六小时那条判据（D105）管的是读取侧怎么看待一行旧数据，它不删也不写；
        不结的话 `sams health` 会把三天前死掉的那次读成「正在跑」，而那一行的周期也不会
        被算进摘要那句「这一晚哪些没跑成」。
        """
        before = int(
            self.db.scalar("SELECT count(*) FROM task_runs WHERE status = ?", [RUNNING]) or 0
        )
        if not before:
            return 0
        self.db.execute(
            "UPDATE task_runs SET finished_at = ?, status = ?, error = ? WHERE status = ?",
            [self.clock.now_utc(), FAILED, INTERRUPTED, RUNNING],
        )
        return before

    def get(self, run_id: str) -> TaskRun | None:
        rows = self.db.query(f"SELECT {_COLUMNS} FROM task_runs WHERE id = ?", [run_id])
        return _to_run(rows.to_dicts()[0]) if rows.height else None

    def running(self) -> list[TaskRun]:
        """还挂着 `running` 的行，新到旧——「现在有没有人在做活」的那一眼。

        这一句的可信度来自单写者加 `close_interrupted`：常驻循环一认领心跳就把上一进程
        的孤儿结掉，此后读到的 `running` 只能是当前这个进程写的。它因此是「心跳过期」的
        反证：那一拍没翻，可能只是因为某一趟 job 长得超过了阈值。
        """
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM task_runs WHERE status = ?"
            " ORDER BY started_at DESC, rowid DESC",
            [RUNNING],
        )
        return [_to_run(row) for row in rows.to_dicts()]

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

    def history(
        self,
        *,
        market: str | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[TaskRun], int]:
        """管理页的固定分页台账；筛选值由调用方先校验，这里只拼参数。"""
        clauses: list[str] = []
        params: list = []
        if market:
            clauses.append("market = ?")
            params.append(market)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(self.db.scalar(f"SELECT count(*) FROM task_runs{where}", params) or 0)
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM task_runs{where}"
            " ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        )
        return [_to_run(row) for row in rows.to_dicts()], total

    def failed_names_for(
        self, market: str, period_key: str, *, exclude: Sequence[str] = ()
    ) -> tuple[str, ...]:
        """这一晚哪些 job 没跑成：一个 job 一行，按名字排序。

        摘要只需要一句「哪些没更新」，不需要每个重试各一行——同一个周期失败三次
        在 `task_runs` 里是三行，在摘要正文里写三遍是同一件事说三次。
        """
        rows = self.db.query(
            "SELECT DISTINCT job_name FROM task_runs"
            " WHERE market = ? AND period_key = ? AND status = ?"
            " ORDER BY job_name",
            [market, period_key, FAILED],
        )
        blocked = set(exclude)
        return tuple(
            str(row["job_name"])
            for row in rows.to_dicts()
            if str(row["job_name"]) not in blocked
        )

    def count_before(self, cutoff: datetime) -> int:
        return int(
            self.db.scalar("SELECT count(*) FROM task_runs WHERE started_at < ?", [cutoff]) or 0
        )

    def delete_before(self, cutoff: datetime) -> None:
        """超过保留期的执行台账一律删，包括一直没收尾的 `running`。

        九十天还停在 `running` 的唯一解释是崩溃留下的：D105 的六小时判据早就把它
        标成「上一跑没收尾」，再留着只会把「最近一次」永远占住。
        """
        self.db.execute("DELETE FROM task_runs WHERE started_at < ?", [cutoff])

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

    def stop(self, beat_id: str) -> None:
        """正常退出的那一行打上停止标记；未知 id 什么也不做。

        守卫据此区分「还在跳」与「跳完收工了」。不删行：历史仍要可查（清理另有 prune）。
        """
        self.db.execute(
            "UPDATE scheduler_heartbeats SET stopped_at = ?"
            " WHERE id = ? AND stopped_at IS NULL",
            [self.clock.now_utc(), beat_id],
        )

    def latest(self) -> Heartbeat | None:
        rows = self.db.query(
            "SELECT id, started_at, beat_at, note, stopped_at FROM scheduler_heartbeats"
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
            stopped_at=row["stopped_at"],
        )

    def count_beyond(self, keep: int) -> int:
        if keep < 1:
            raise ValidationError("keep 至少是 1：0 会把全部心跳删掉")
        return int(
            self.db.scalar(
                "SELECT count(*) FROM scheduler_heartbeats WHERE id NOT IN ("
                "  SELECT id FROM scheduler_heartbeats"
                "  ORDER BY beat_at DESC, rowid DESC LIMIT ?)",
                [keep],
            )
            or 0
        )

    def delete_beyond(self, keep: int) -> None:
        if keep < 1:
            raise ValidationError("keep 至少是 1：0 会把全部心跳删掉")
        self.db.execute(
            "DELETE FROM scheduler_heartbeats WHERE id NOT IN ("
            "  SELECT id FROM scheduler_heartbeats"
            "  ORDER BY beat_at DESC, rowid DESC LIMIT ?)",
            [keep],
        )
