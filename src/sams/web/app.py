"""M4-A 的 HTTP 入口。

`create_app(runtime)`（不传 core）保留计划 1 的形状：只有 /healthz，应用不认识
依赖装配——看门狗与最简部署用。传 core 与 contexts 才有页面。

线程不变量（D113）：同步端点经 run_in_threadpool 执行，依赖与端点可能落在不同线程。
所以依赖只许碰 request；一切仓库与服务调用都写在端点函数体内，经
`request.app.state.web.contexts.current()` 拿本线程上下文。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.templating import Jinja2Templates

from sams.bootstrap import AppCore
from sams.core.errors import DatabaseLocked, SamsError
from sams.services.runtime import Runtime
from sams.web.auth import (
    ACTIVE_COOKIE,
    CSRF_COOKIE,
    SESSION_COOKIE,
    LoginLimiter,
    LoginRequired,
    RateCard,
    _stamp_active,
)
from sams.web.auth import router as auth_router
from sams.web.context import ThreadContexts
from sams.web.display import alert_status, hms, money, pct, state_label
from sams.web.pages import admin, alerts, dashboard, portfolio, rules, settings, symbol, theses

_STATIC = Path(__file__).parent / "static"
_TEMPLATES = Path(__file__).parent / "templates"


def _templates() -> Jinja2Templates:
    """显示函数注册成 Jinja 全局：数字怎么排、状态叫什么，模板里不该各写一份。"""
    templates = Jinja2Templates(directory=str(_TEMPLATES))
    templates.env.globals.update(
        money=money, pct=pct, hms=hms, state_label=state_label, alert_status=alert_status
    )
    return templates


@dataclass
class WebState:
    """一张页面要用的进程级东西。仓库不在这里：它们属于线程，走 `contexts.current()`。"""

    runtime: Runtime
    core: AppCore
    contexts: ThreadContexts
    templates: Jinja2Templates
    limiter: LoginLimiter
    #: /settings/test-alert 的每用户 5 分钟一张卡（spec §6.3）：进程内的计数，
    #: 重启就重置——这一条挡的是连点，不是攻击
    test_alerts: RateCard
    #: /settings/channels/{id}/probe 的每用户 30 秒一张卡：探针也会真发到手机上，
    #: 而且一次跑三趟带退避、就压在这条线程池线程上。窗口比测试告警短，因为排障时
    #: 人要连着试几条（改完 url 立刻探一下），而连点照旧要挡
    probes: RateCard
    #: /admin/sources/probe 的每用户 60 秒一张卡：这个探针打的是真采集（一个源、一整段
    #: 日线），而且同步跑在页面那条池线程上。少了这张卡，连点就是每次都真发一趟请求，
    #: 而那条页面请求要等它跑完才返回——三个标签页就能把池线程按死在别人的排障上
    admin_source_probes: RateCard

    def render(self, request: Request, name: str, *, status_code: int = 200, **ctx):
        from sams.web.auth import csrf_value

        ctx.setdefault("csrf", csrf_value(request))
        ctx.setdefault("user", getattr(request.state, "sams_user", None))
        return self.templates.TemplateResponse(
            request=request, name=name, context=ctx, status_code=status_code
        )


def create_app(
    runtime: Runtime,
    core: AppCore | None = None,
    *,
    contexts: ThreadContexts | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if contexts is not None:
            contexts.close_all()

    app = FastAPI(
        title="SAMS",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(runtime.healthz(), status_code=200)

    if core is None:
        return app
    if contexts is None:
        raise ValueError("给了 core 就必须给 contexts：页面要按线程装配上下文")

    app.state.web = WebState(
        runtime=runtime,
        core=core,
        contexts=contexts,
        templates=_templates(),
        limiter=LoginLimiter(
            max_failures=core.settings.server.login_max_failures,
            window_s=core.settings.server.login_window_min * 60,
        ),
        test_alerts=RateCard(300),
        probes=RateCard(30),
        admin_source_probes=RateCard(60),
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    app.include_router(auth_router)
    app.include_router(admin.router)
    app.include_router(alerts.router)
    app.include_router(dashboard.router)
    app.include_router(portfolio.router)
    app.include_router(rules.router)
    app.include_router(settings.router)
    app.include_router(symbol.router)
    app.include_router(theses.router)

    def _login_redirect(_request: Request, _exc: LoginRequired) -> RedirectResponse:
        """会话失效要连 CSRF 与心跳一起抹掉：留任何一枚都是可用的半成品凭据。"""
        response = RedirectResponse("/login", status_code=303)
        for name in (SESSION_COOKIE, CSRF_COOKIE, ACTIVE_COOKIE):
            response.delete_cookie(name, path="/")
        return response

    def _error_response(request: Request, status: int, title: str, error: str):
        web = getattr(request.app.state, "web", None)
        if web is None:
            return PlainTextResponse(error, status_code=status)
        return web.render(request, "error.html", status_code=status, title=title, error=error)

    def _on_sams_error(request: Request, exc: SamsError):
        status = 503 if isinstance(exc, DatabaseLocked) else 400
        return _error_response(request, status, "出错了", str(exc))

    def _on_http_error(request: Request, exc: StarletteHTTPException):
        response = _error_response(request, exc.status_code, "打不开这一页", str(exc.detail))
        if exc.headers:
            for key, value in exc.headers.items():
                response.headers[key] = value
        return response

    def _on_boom(request: Request, exc: Exception):
        logger = logging.getLogger("sams.web")
        logger.exception("页面 500：%s %s", request.method, request.url.path)
        return _error_response(request, 500, "内部错误", "服务器内部错误，已记入日志")

    app.add_exception_handler(LoginRequired, _login_redirect)
    app.add_exception_handler(DatabaseLocked, _on_sams_error)
    app.add_exception_handler(SamsError, _on_sams_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_error)
    app.add_exception_handler(Exception, _on_boom)

    @app.middleware("http")
    async def security_and_active(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';"
            " script-src 'self'",
        )
        if getattr(request.state, "sams_user", None) is not None:
            _stamp_active(response, request)
        return response

    return app
