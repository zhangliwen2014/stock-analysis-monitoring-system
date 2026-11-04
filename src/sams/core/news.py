"""公告与新闻的统一读模型（S3-A，D243/D244）。

`InfoItem` 只回答「哪一天、哪只票、什么标题、来源与原文链接」；公告与新闻底层
各读各的表，合并发生在这一层，不搬表、不去重、不做情绪判断。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

KINDS = ("announcement", "news")
_KIND_ORDER = {kind: rank for rank, kind in enumerate(KINDS)}


@dataclass(frozen=True)
class InfoItem:
    market: str
    code: str
    kind: str
    title: str
    summary: str | None
    published_at: datetime
    source: str
    url: str
    #: 我们是什么时候知道这条资讯的：资讯卡与「本列表读于何时」那句都问它（D288）。
    #: 屏幕上的时间线不用它——那一列说的是 `published_at`。
    as_of: datetime | None = None
    #: 新闻在自己源里的 id（公告没有这一格）：关键词命中按 `(source, item_id)`
    #: 去重，缺了它「同一篇只提醒一次」就落不了地。
    item_id: str | None = None
    #: 这个用户命中的关键词（公告恒为空）：页面据此高亮，摘要据此排序。
    matched: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _KIND_ORDER:
            raise ValueError(f"不认识的资讯类型 {self.kind!r}：只认 {', '.join(KINDS)}")


def merge_info_items(items: Iterable[InfoItem], *, limit: int | None = None) -> list[InfoItem]:
    """按发布时刻倒序合并；同一时刻公告在前——它比转载的新闻更接近原文。"""
    ordered = sorted(items, key=lambda item: (_KIND_ORDER[item.kind], item.source, item.title))
    ordered.sort(key=lambda item: item.published_at, reverse=True)
    return ordered if limit is None else ordered[:limit]
