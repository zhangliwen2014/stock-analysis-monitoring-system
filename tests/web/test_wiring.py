"""Web 与运行时的接线：线程各得一条上下文，关停时全部收掉。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from sams.bootstrap import build_core
from sams.core.errors import StorageError
from sams.web.app import create_app


class BareRuntime:
    def healthz(self):
        return {"status": "ok"}


def test_each_thread_gets_its_own_context(make_web_app):
    web = make_web_app()
    seen: list = []

    def work():
        seen.append(web.contexts.current())

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert web.contexts.count == 2


def test_lifespan_closes_every_registered_context_on_shutdown(make_web_app):
    web = make_web_app()
    web.contexts.current()
    with TestClient(web.app):
        pass
    assert web.contexts.count == 0


def test_close_all_leaves_connections_unusable(make_web_app):
    web = make_web_app()
    ctx = web.contexts.current()
    web.contexts.close_all()
    assert web.contexts.count == 0
    # 精确断言 StorageError：关闭后的连接报错要穿过 db 的转换点，而不是漏出原始 duckdb 异常
    with pytest.raises(StorageError):
        ctx.db.query("SELECT 1")


def test_app_state_wiring_and_static_mount(make_web_app):
    web = make_web_app()
    client = TestClient(web.app)
    assert web.app.state.web.core is web.core
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_core_without_contexts_is_rejected(migrated, stub_sources):
    core = build_core(migrated, adapters=stub_sources)
    try:
        with pytest.raises(ValueError):
            create_app(BareRuntime(), core=core)
    finally:
        core.close()


def test_create_app_without_core_keeps_the_healthz_only_shape():
    response = TestClient(create_app(BareRuntime())).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
