"""存量乱码的一次性修复（R3-26 / D360）。

写入侧的折返在 `web/forms.form_data`（判据住在 `core.text.repair_mojibake`）；这里
补的是先把库里已经坏掉的行列出来、再看一眼后写回。两件事共用同一份判据与同一条
扫描路径：干跑说 2 处、实修就该是 2 处。

白名单只收**用户填写的文本列**。新闻、公告、行情、F10 是源数据：同样的字节序列
在那边是源的问题，猜着改会把证据链上的原文换掉。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.core.text import repair_mojibake
from sams.storage.db import Database

#: (表, 主键列, 用户填写的文本列)。只加「人打的字」；系统生成的 note 不进来。
TARGETS = (
    ("watchlists", ("user_id", "market", "code"), ("note",)),
    ("thesis_events", ("id",), ("note",)),
    ("thesis_assumptions", ("id",), ("note",)),
    ("annotations", ("id",), ("body_md",)),
    ("annotation_revisions", ("id",), ("before_md", "after_md")),
    ("rules", ("id",), ("name",)),
    ("screener_presets", ("id",), ("name", "description")),
    ("channels", ("id",), ("name",)),
    ("alert_events", ("id",), ("note",)),
    ("portfolio_limits", ("id",), ("note",)),
    ("users", ("id",), ("display_name",)),
)

_PKS = {table: pks for table, pks, _ in TARGETS}


@dataclass(frozen=True)
class Repair:
    table: str
    column: str
    #: 主键值（回填的 WHERE 用它，不把拼好的字符串再拆开）
    ids: tuple[object, ...]
    before: str
    after: str

    @property
    def key(self) -> str:
        return "/".join(str(value) for value in self.ids)


class TextRepairService:
    """`sams db repair-encoding` 的出口：扫描只读，apply 在一个事务里写回。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def scan(self) -> tuple[Repair, ...]:
        found: list[Repair] = []
        for table, pks, columns in TARGETS:
            selected = ", ".join((*pks, *columns))
            rows = self.db.query(f"SELECT {selected} FROM {table}").to_dicts()
            for row in rows:
                ids = tuple(row[pk] for pk in pks)
                for column in columns:
                    value = row[column]
                    if value is None:
                        continue
                    fixed = repair_mojibake(value)
                    if fixed != value:
                        found.append(Repair(table, column, ids, value, fixed))
        return tuple(found)

    def apply(self) -> tuple[Repair, ...]:
        found = self.scan()
        if not found:
            return ()
        with self.db.transaction():
            for item in found:
                where = " AND ".join(f"{pk} = ?" for pk in _PKS[item.table])
                self.db.execute(
                    f"UPDATE {item.table} SET {item.column} = ? WHERE {where}",
                    [item.after, *item.ids],
                )
        return found
