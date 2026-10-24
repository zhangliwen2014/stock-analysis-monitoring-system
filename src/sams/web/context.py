"""线程级上下文的登记处。

`build_context` 每造一台就开一条 DuckDB 连接（D113），而 HTTP 线程池的线程不固定，
不能预造几条塞给「某条线程」。所以按「第一次在这条线程上用到」装配；threading.local
的值无法从别的线程枚举，登记只能靠一把锁加一份列表，thread-local 只是快路径。
"""

from __future__ import annotations

import threading

from sams.bootstrap import AppCore, ServiceContext, build_context


class ThreadContexts:
    def __init__(self, core: AppCore, *, providers: dict | None = None) -> None:
        self._core = core
        self._providers = providers
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: list[ServiceContext] = []

    def current(self) -> ServiceContext:
        ctx = getattr(self._local, "ctx", None)
        if ctx is None:
            ctx = build_context(self._core, providers=self._providers)
            self._local.ctx = ctx
            with self._lock:
                self._all.append(ctx)
        return ctx

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._all)

    def close_all(self) -> None:
        with self._lock:
            contexts, self._all = self._all, []
        for ctx in contexts:
            ctx.close()
