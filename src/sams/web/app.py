"""M4-A 的 HTTP 入口。

`create_app(runtime)`（不传 core）保留计划 1 的形状：只有 /healthz，应用不认识
依赖装配——看门狗与最简部署用。传 core 与 contexts 才有页面。

线程不变量（D113）：同步端点经 run_in_threadpool 执行，依赖与端点可能落在不同线程。
所以依赖只许碰 request；一切仓库与服务调用都写在端点函数体内，经
`request.app.state.web.contexts.current()` 拿本线程上下文。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from sams.bootstrap import AppCore
from sams.services.runtime import Runtime
from sams.web.context import ThreadContexts

_STATIC = Path(__file__).parent / "static"
_TEMPLATES = Path(__file__).parent / "templates"


@dataclass
class WebState:
    """一张页面要用的进程级东西。仓库不在这里：它们属于线程，走 `contexts.current()`。"""

    runtime: Runtime
    core: AppCore
    contexts: ThreadContexts
    templates: Jinja2Templates


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
        templates=Jinja2Templates(directory=str(_TEMPLATES)),
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    return app
