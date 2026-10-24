"""登录、CSRF、闲置超时与退出。cookie 自己带时间戳，所以用真钟就能测。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.testclient import TestClient

from sams.web.auth import sign


def _csrf(client) -> str:
    raw = client.cookies.get("sams_csrf")
    assert raw, "登录之后应该有 CSRF cookie"
    return raw.rpartition(".")[0]


def test_login_success_redirects_home(web_app):
    response = web_app.client.post(
        "/login",
        data={"username": "alice", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert web_app.client.cookies.get("sams_session")


def test_wrong_password_stays_on_the_page_with_the_message(web_app):
    response = web_app.client.post(
        "/login", data={"username": "alice", "password": "nope-nope-nope"}
    )
    assert response.status_code == 200
    assert "用户名或密码不对" in response.text


def test_too_many_failures_lock_the_pair(web_app):
    client = web_app.client
    for _ in range(web_app.core.settings.server.login_max_failures):
        client.post("/login", data={"username": "alice", "password": "wrong-wrong"})
    response = client.post(
        "/login",
        data={"username": "alice", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_post_without_csrf_is_rejected(logged):
    response = logged.client.post("/logout", data={}, follow_redirects=False)
    assert response.status_code == 403


def test_logout_revokes_the_session(logged):
    client = logged.client
    response = client.post("/logout", data={"csrf": _csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    after = client.get("/", follow_redirects=False)
    assert after.status_code == 303
    assert after.headers["location"].startswith("/login")


def test_anonymous_pages_redirect_to_login(web_app):
    response = web_app.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_idle_timeout_revokes_the_session(logged):
    client = logged.client
    stale_at = datetime.now(UTC) - timedelta(
        minutes=logged.core.settings.server.idle_timeout_min + 1
    )
    client.cookies.set(
        "sams_active",
        sign(logged.core.settings.secret_key, f"{stale_at.isoformat()}:x"),
    )
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")


def test_unexpected_errors_render_a_500_page(make_web_app):
    web = make_web_app()

    @web.app.get("/boom")
    def boom(request: Request):
        raise RuntimeError("炸了")

    client = TestClient(web.app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    assert "内部错误" in response.text


def test_missing_pages_render_the_error_page(web_app):
    response = web_app.client.get("/definitely-not-here")
    assert response.status_code == 404
    assert "打不开这一页" in response.text
