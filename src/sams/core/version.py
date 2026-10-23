"""进程内的版本计数：谁缓存了「规则/观点/账本/日线」，谁就要知道自己过期了没有。

盘中线程不该每 10 秒把规则与日线重读一遍；但用户在网页上改了规则、录了成交、推进了
观点，或盘后 job 刚补了日线——这些都不经过盘中线程。缓存若没有失效判据，最坏的样子是
「用户刚把止损改到 9 元，页面显示已生效，盘中仍按 10 元在判」，而且没有任何报错。
计数只在这个进程里有意义：它答的是「这份缓存」与「当前库」的差，不是数据版本。

用法协议：先 `snapshot()` 再读库，缓存建立后每轮用 `changed_since(那份快照)`
判断要不要重读；为真时重新 `snapshot()` 再重读。反过来「读完才记快照」会把
读取与记快照之间落进来的 bump 吞掉，而且没有任何报错。
"""

from __future__ import annotations

import threading

DEFAULT_KINDS = ("rules", "theses", "ledger", "bars")


class VersionGate:
    def __init__(self, kinds: tuple[str, ...] = DEFAULT_KINDS) -> None:
        self._versions = {kind: 0 for kind in kinds}
        self._lock = threading.Lock()

    @property
    def kinds(self) -> tuple[str, ...]:
        # 键集在 __init__ 后不再变化，读它不需要锁（值会变，键不会增删）
        return tuple(self._versions)

    def _check(self, kind: str) -> None:
        if kind not in self._versions:
            raise KeyError(f"未知的缓存种类 {kind!r}：已登记 {list(self._versions)}")

    def bump(self, kind: str) -> int:
        with self._lock:
            self._check(kind)
            self._versions[kind] += 1
            return self._versions[kind]

    def current(self, kind: str) -> int:
        with self._lock:
            self._check(kind)
            return self._versions[kind]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._versions)

    def changed_since(self, snapshot: dict[str, int]) -> bool:
        with self._lock:
            for kind in snapshot:
                self._check(kind)
            return any(self._versions[kind] != value for kind, value in snapshot.items())
