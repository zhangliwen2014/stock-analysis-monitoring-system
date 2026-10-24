"""盘中一趟共用的日线读数缓存（spec §5.4）。

缓存里只有日线的部分，从来不是报价：报价每一轮都变，把两种东西存进同一张表，
「改一条规则要不要清缓存」就没法回答了。失效只有两种：任何一版变更
（rules/theses/ledger/bars 之一被 bump）→ 清**全部**；日子变了或 TTL 到了 → 单键当没有。

不加锁：读方只有盘中那一个线程；收盘那一趟从不经过它。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sams.services.rules.snapshot import Snapshot

if TYPE_CHECKING:
    from sams.core.version import VersionGate

#: 盘中一层兜底：版本闸没碰上（比如有人绕过服务层直接写库）时，一份读数最多信五分钟
DEFAULT_TTL_S = 300.0


@dataclass(frozen=True)
class CacheKey:
    """一份日线读数属于谁（§5.4）：去重键那六元组减掉账户无关的两维。

    `account_id`/`thesis_id` 取自展开后的对象（`Target`），不是规则上的那一列——
    快照里带着的是前者的值。
    """

    user_id: str
    rule_id: str
    market: str
    code: str
    account_id: str | None = None
    thesis_id: str | None = None


@dataclass(frozen=True)
class _Entry:
    snapshot: Snapshot
    day: date
    at: float


class SnapshotCache:
    def __init__(
        self,
        versions: VersionGate,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._versions = versions
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._stamp = versions.snapshot()
        self._entries: dict[CacheKey, _Entry] = {}

    def get(self, key: CacheKey, *, day: date) -> Snapshot | None:
        self._settle()
        entry = self._entries.get(key)
        if entry is None or entry.day != day:
            return None
        if self._monotonic() - entry.at > self._ttl_s:
            return None
        return entry.snapshot

    def put(self, key: CacheKey, snapshot: Snapshot, *, day: date) -> None:
        self._settle()
        self._entries[key] = _Entry(snapshot, day, self._monotonic())

    def _settle(self) -> None:
        """版本闸动过就整表清空：什么时候动过的不必知道，字典一换就是新的。"""
        stamp = self._versions.snapshot()
        if stamp != self._stamp:
            self._entries.clear()
            self._stamp = stamp
