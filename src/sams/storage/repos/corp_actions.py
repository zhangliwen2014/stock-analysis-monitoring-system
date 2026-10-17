"""公司行为仓库：除权除息、拆合股、送转配。

后复权因子与持仓成本换算从这里推导，而不是从 `bars_daily` 的某一列读——
0003 迁移删掉了那个恒等于 1 的 `adj_factor` 占位列：没有生产者的列会被下游当成可信口径。
M1 只提供读写与幂等保证，推导因子是 M2 的事，但表结构与口径必须现在定。

同一 (market, code, ex_date, kind) 重复入库视为同一条记录的修正，不产生第二行：
数据源回填改数是常态，留下两行等于让因子算两次。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import polars as pl

from sams.core.errors import ValidationError
from sams.storage.db import Database

#: 已知的公司行为类型。裸 VARCHAR 会让 M2 各写各的拼写
KINDS = frozenset({"dividend", "split", "bonus", "transfer", "rights"})


@dataclass(frozen=True, kw_only=True)
class CorpAction:
    market: str
    code: str
    ex_date: date
    kind: str
    src: str
    cash_per_share: float | None = None
    ratio: float | None = None
    note: str | None = None


class CorpActionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, action: CorpAction) -> None:
        if action.kind not in KINDS:
            raise ValidationError(f"未知公司行为类型 {action.kind!r}，可选 {sorted(KINDS)}")
        if action.cash_per_share is None and action.ratio is None:
            raise ValidationError("公司行为必须给出现金或比例，否则复权因子无从计算")
        # NaN 也算「给了」吗？不算——0 与负的派息/比例同样无从推导因子，却能一路乘进后复权序列
        for name, value in (
            ("cash_per_share", action.cash_per_share),
            ("ratio", action.ratio),
        ):
            if value is None:
                continue
            if not math.isfinite(value) or value < 0 or (name == "ratio" and value == 0):
                raise ValidationError(f"{name}={value} 非法：必须是非负的有限数（ratio 不能为 0）")
        self.db.execute(
            """
            INSERT OR REPLACE INTO corp_actions
                (market, code, ex_date, kind, cash_per_share, ratio, src, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())
            """,
            [
                action.market,
                action.code,
                action.ex_date,
                action.kind,
                action.cash_per_share,
                action.ratio,
                action.src,
                action.note,
            ],
        )

    def list_for(
        self, market: str, code: str, *, until: date | None = None
    ) -> pl.DataFrame:
        """按除权生效日升序返回。`until` 给出 PIT 读法：只取到某一天为止已发生的行为。"""
        sql = ["SELECT * FROM corp_actions WHERE market = ? AND code = ?"]
        params: list = [market, code]
        if until is not None:
            sql.append("AND ex_date <= ?")
            params.append(until)
        return self.db.query(" ".join(sql) + " ORDER BY ex_date, kind", params)

    def count(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM corp_actions") or 0)
