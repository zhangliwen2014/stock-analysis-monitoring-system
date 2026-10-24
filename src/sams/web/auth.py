"""登录、会话与 CSRF。

三枚 cookie（spec §6.2）：`sams_session` 是会话令牌（库里存哈希）；`sams_csrf`
是签过名的随机串，表单里回填一份，POST 时比对；`sams_active` 是活跃心跳——
require_user 每次盖章，中间件在响应上写回，过期就会话被吊销。
签名用 HMAC 只防伪造不防偷看：cookie 里没有值得读的东西。
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.storage.repos.users import ACTIVE

logger = logging.getLogger("sams.web")

SESSION_COOKIE = "sams_session"
CSRF_COOKIE = "sams_csrf"
ACTIVE_COOKIE = "sams_active"


def sign(secret: str, value: str) -> str:
    digest = hmac.new(secret.encode(), value.encode(), "sha256").hexdigest()
    return f"{value}.{digest}"


def unsigned_ok(secret: str, signed: str) -> str | None:
    """验签并取回原文；对不上返回 None，绝不返回验不过的那半截。"""
    value, _, digest = signed.rpartition(".")
    if not value or not digest:
        return None
    expected = hmac.new(secret.encode(), value.encode(), "sha256").hexdigest()
    # compare_digest 而不是 ==：长度与逐字节的比较时机都不该泄漏签名信息
    return value if hmac.compare_digest(digest, expected) else None


class LoginRequired(Exception):
    """require_user 没认出人：交给 app.py 的 handler 送去登录页。"""


class LoginLimiter:
    """按 (来源 IP, 用户名) 数失败；达到上限锁一阵子，把「慢慢试」的通道关掉。"""

    def __init__(self, *, max_failures: int, window_s: int, now=time.monotonic) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._now = now
        self._fails: dict[tuple[str, str], list[float]] = {}

    def blocked_for(self, key: tuple[str, str]) -> int:
        cutoff = self._now() - self.window_s
        tries = [t for t in self._fails.get(key, []) if t > cutoff]
        if len(tries) < self.max_failures:
            return 0
        return max(1, int(min(tries) + self.window_s - self._now()))

    def record_failure(self, key: tuple[str, str]) -> None:
        cutoff = self._now() - self.window_s
        tries = [t for t in self._fails.get(key, []) if t > cutoff]
        tries.append(self._now())
        self._fails[key] = tries

    def reset(self, key: tuple[str, str]) -> None:
        self._fails.pop(key, None)


class RateCard:
    """固定间隔的令牌：try_take 返回 0 表示放行，否则返回要等的秒数。"""

    def __init__(self, interval_s: int, *, now=time.monotonic) -> None:
        self.interval_s = interval_s
        self._now = now
        self._last: dict[str, float] = {}

    def try_take(self, key: str) -> int:
        now = self._now()
        last = self._last.get(key)
        if last is not None and now - last < self.interval_s:
            return int(self.interval_s - (now - last)) + 1
        self._last[key] = now
        return 0


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def csrf_value(request: Request) -> str:
    """模板要回填的那一段原文；签名不对就当没有（宁可让人刷新页面也不发出坏表单）。"""
    raw = request.cookies.get(CSRF_COOKIE)
    if not raw:
        return ""
    return unsigned_ok(request.app.state.web.core.settings.secret_key, raw) or ""


async def csrf_protect(request: Request, csrf: str = Form("")) -> None:
    """CSRF 依赖。登录 POST 豁免（那时还没有 cookie 可比对）。"""
    expected = csrf_value(request)
    if not expected or not hmac.compare_digest(csrf, expected):
        raise HTTPException(status_code=403, detail="CSRF 校验失败：请刷新页面重试")


def audit(request: Request, *, action: str, target: str, ok: bool, detail: str = "") -> None:
    """审计写应用日志，不建审计表（spec §6.4）：个人系统里表本身是一条要备份的敏感数据。"""
    user = getattr(request.state, "sams_user", None)
    who = user.username if user is not None else "-"
    logger.info(
        "%s %s user=%s target=%s %s%s",
        "ok" if ok else "deny",
        action,
        who,
        target,
        client_ip(request),
        f" {detail}" if detail else "",
    )


def require_user(request: Request):
    """端点体内调用（不是依赖）：依赖与端点可能落在不同线程（D113）。"""
    web = request.app.state.web
    token = request.cookies.get(SESSION_COOKIE)
    ctx = web.contexts.current()
    user_id = ctx.sessions.verify(token) if token else None
    if user_id is None:
        raise LoginRequired()
    user = ctx.users.get(user_id)
    if user is None or user.status != ACTIVE:
        raise LoginRequired()
    if not _active_ok(request):
        ctx.sessions.revoke(token)
        raise LoginRequired()
    request.state.sams_user = user
    request.state.sams_token = token
    return user


def _active_ok(request: Request) -> bool:
    """心跳缺失、签名不对、时间戳太旧都算过期：三种情况都只能重新登录，别无出路。"""
    raw = request.cookies.get(ACTIVE_COOKIE)
    if not raw:
        return False
    value = unsigned_ok(request.app.state.web.core.settings.secret_key, raw)
    if value is None:
        return False
    stamped, _, _nonce = value.rpartition(":")
    try:
        moment = datetime.fromisoformat(stamped)
    except ValueError:
        return False
    idle = timedelta(minutes=request.app.state.web.core.settings.server.idle_timeout_min)
    return datetime.now(UTC) - moment <= idle


def require_admin(request: Request):
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="这一页只有管理员能开")
    return user


def _secure(request: Request) -> bool:
    return request.app.state.web.core.settings.server.tls_certfile is not None


def _active_value(request: Request) -> str:
    now = request.app.state.web.core.clock.now_utc()
    return f"{now.isoformat()}:{secrets.token_urlsafe(8)}"


def _stamp_active(response, request: Request) -> None:
    settings = request.app.state.web.core.settings.server
    response.set_cookie(
        ACTIVE_COOKIE,
        sign(request.app.state.web.core.settings.secret_key, _active_value(request)),
        max_age=settings.idle_timeout_min * 60,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
        path="/",
    )


def _set_session_cookies(response, request: Request, token: str) -> None:
    settings = request.app.state.web.core.settings.server
    secure = _secure(request)
    ttl = settings.session_ttl_days * 86400
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    # CSRF 每次登录换新：留着旧的等于让攻击者在登录前就拿到一份能用的配对值
    fresh = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE,
        sign(request.app.state.web.core.settings.secret_key, fresh),
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    _stamp_active(response, request)


router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return request.app.state.web.render(request, "login.html", title="登录")


@router.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    web = request.app.state.web
    settings = web.core.settings.server
    key = (client_ip(request), username.strip().lower())
    wait = web.limiter.blocked_for(key)
    if wait:
        response = web.render(
            request,
            "login.html",
            status_code=429,
            title="登录",
            error=f"失败次数太多，{wait} 秒后再试",
        )
        response.headers["Retry-After"] = str(wait)
        return response
    user = web.contexts.current().users.authenticate(username, password)
    if user is None:
        web.limiter.record_failure(key)
        audit(request, action="login", target=username, ok=False)
        return web.render(
            request, "login.html", status_code=200, title="登录", error="用户名或密码不对"
        )
    web.limiter.reset(key)
    token = web.contexts.current().sessions.issue(user.id, ttl_days=settings.session_ttl_days)
    audit(request, action="login", target=user.username, ok=True)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookies(response, request, token)
    return response


@router.post("/logout")
def logout(request: Request, _csrf: None = Depends(csrf_protect)):
    web = request.app.state.web
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        web.contexts.current().sessions.revoke(token)
    audit(request, action="logout", target="-", ok=True)
    response = RedirectResponse("/login", status_code=303)
    for name in (SESSION_COOKIE, CSRF_COOKIE, ACTIVE_COOKIE):
        response.delete_cookie(name, path="/")
    return response
