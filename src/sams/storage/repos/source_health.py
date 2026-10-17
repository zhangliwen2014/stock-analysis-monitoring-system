"""数据源健康仓库：实现 core.sources.SourceHealthStore。

写入是覆盖式的（每个源一行），因此不需要历史序列；
历史留给 task_runs，本表只回答「这个源现在到底好不好」。
"""

from __future__ import annotations

from sams.core.sources import SourceRecord
from sams.storage.db import Database


class SourceHealthRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, record: SourceRecord) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO source_health
                (name, markets, ok, last_error, last_ok_at, consecutive_failures, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, now())
            """,
            [
                record.name,
                ",".join(record.markets),
                record.ok,
                record.last_error,
                record.last_ok_at,
                record.consecutive_failures,
            ],
        )

    def load(self) -> dict[str, SourceRecord]:
        if "source_health" not in self.db.table_names():
            return {}
        rows = self.db.query("SELECT * FROM source_health").to_dicts()
        return {row["name"]: _to_record(row) for row in rows}


def _to_record(row: dict) -> SourceRecord:
    markets = row["markets"] or ""
    return SourceRecord(
        name=row["name"],
        markets=tuple(markets.split(",")) if markets else (),
        ok=bool(row["ok"]),
        last_error=row["last_error"],
        last_ok_at=row["last_ok_at"],
        consecutive_failures=int(row["consecutive_failures"] or 0),
    )
