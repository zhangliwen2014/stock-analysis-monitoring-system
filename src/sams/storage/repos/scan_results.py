"""选股扫描结果的写入：一张 0001 就建好、直到 M2-C 才有仓库的表。

命中的票以 JSON 存，不为几个字段建表：一次扫描的结果属于「这一天这条规则的产物」，
它的形状跟着规则走（PR 的 hit 带 basis，均线突破的 hit 带周期），把它拆成列等于每加
一条规则改一次 schema。要比较两次扫描结果时，读 `hits_json` 就够了。

同 `(日期, 市场, 规则)` 重跑覆盖：一天里改门槛跑三遍是常态，留三行等于问「今天扫出
什么」时给出三个答案。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import polars as pl

from sams.core.codes import canonical_market
from sams.storage.db import Database


class ScanResultRepo:
    TABLE = "scan_results"

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        *,
        on: date,
        market: str,
        strategy_id: str,
        universe_size: int,
        hits: list[dict],
    ) -> None:
        self.db.execute(
            f"""
            INSERT OR REPLACE INTO {self.TABLE}
                (date, market, strategy_id, universe_size, hits_json, created_at)
            VALUES (?, ?, ?, ?, ?, now())
            """,
            [
                on,
                canonical_market(market),
                strategy_id,
                universe_size,
                # ensure_ascii=False：命中里全是中文票名，转义成一串 \uXXXX 的 JSON
                # 既没法直接看，也没比省空间——这张表存在 DuckDB 里，不是发给浏览器的
                json.dumps(hits, ensure_ascii=False, default=_jsonable),
            ],
        )

    def get(self, *, on: date, market: str, strategy_id: str) -> dict | None:
        row = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE date = ? AND market = ? AND strategy_id = ?",
            [on, canonical_market(market), strategy_id],
        )
        if row.is_empty():
            return None
        return _expand(row.to_dicts()[0])

    def recent(self, *, market: str, strategy_id: str, limit: int = 5) -> list[dict]:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE market = ? AND strategy_id = ?"
            " ORDER BY date DESC LIMIT ?",
            [canonical_market(market), strategy_id, int(limit)],
        )
        return [_expand(r) for r in rows.to_dicts()]

    def count(self) -> int:
        frame = self.db.query(f"SELECT count(*) AS n FROM {self.TABLE}")
        return int(frame["n"].item() or 0)


def _expand(row: dict) -> dict:
    row["hits"] = json.loads(row.pop("hits_json") or "[]")
    return row


def _jsonable(value: object) -> object:
    """date/datetime 之外的东西一律拒绝，而不是让 json 抛一个看不懂的 TypeError。"""
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"扫描结果里有不能序列化的值: {type(value).__name__}")


def hits_frame(record: dict) -> pl.DataFrame:
    """命中列表转帧，方便打印与断言；空命中给出空帧而不是 None。"""
    hits = record.get("hits") if record else None
    if not hits:
        return pl.DataFrame()
    return pl.DataFrame(hits)
