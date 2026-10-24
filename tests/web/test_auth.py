"""登录、CSRF、闲置超时与退出。cookie 自己带时间戳，所以用真钟就能测。"""

from __future__ import annotations

import itertools
import threading
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.testclient import TestClient

from sams.web.auth import RateCard, sign


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


def test_rate_card_holds_one_caller_at_a_time():
    """读—判—写必须在同一把锁里：端点跑在线程池上，两个标签页可能同时按。

    不加锁时这张卡是「大概每 5 分钟一条」：两次并发都读到空，于是各放行一次，
    手机上就真多出一条告警。所以让第一个调用**停在卡里**（只有第一次读钟会卡住），
    第二个必须等到第一个写完才拿得到钟——没上锁它就直接放行，测试当场就红。
    """
    inside = threading.Event()
    release = threading.Event()
    calls = itertools.count()

    def stuck_once():
        # 只有第一次读钟会停：停在卡里的那一个占着锁，别人进不来
        if next(calls) == 0:
            inside.set()
            assert release.wait(5), "测试没有放行第一个调用"
        return 1000.0

    card = RateCard(300, now=stuck_once)
    results: list[int] = []
    second_done = threading.Event()

    def take():
        results.append(card.try_take("alice"))
        second_done.set()

    first = threading.Thread(target=take)
    second = threading.Thread(target=take)
    first.start()
    assert inside.wait(5)
    second.start()
    assert not second_done.wait(0.2), "第二个调用没等第一个做完：try_take 不是原子的"
    release.set()
    first.join(5)
    second.join(5)
    # 第一个拿到令牌；第二个进来时看到的正是它刚落下的那个时间戳，只能等
    assert results == [0, 301]
