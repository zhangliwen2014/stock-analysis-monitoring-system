"""最小 HTTP 入口：`/healthz` 永远 200，降级写在 JSON 的 status 里。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from services.test_runtime import make_runtime

from sams.web.app import create_app


class DegradedRuntime:
    def healthz(self):
        return {
            "status": "degraded",
            "db": {"ok": False, "error": "库坏了"},
            "scheduler": {"beat_age_s": None, "stale": True},
            "intraday": {"A": {"last_poll_at": None, "age_s": None, "source": None, "ok": False}},
            "version": "0.1.0",
            "uptime_s": 3,
        }


def test_healthz_is_200_even_when_degraded():
    """看门狗只认 HTTP 与 status：500 会让它把「进程活着但库坏了」当成进程死了。"""
    response = TestClient(create_app(DegradedRuntime())).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["db"]["error"] == "库坏了"


def test_openapi_and_docs_are_off():
    client = TestClient(create_app(DegradedRuntime()))
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_the_real_healthz_payload_serializes(settings):
    """JSONResponse 用的是严格 json.dumps：Runtime 真实返回的字典里混进一个
    datetime/NaN，这里就该当场炸，而不是等看门狗第一次拉 /healthz 才发现。
    """
    response = TestClient(create_app(make_runtime(settings))).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
