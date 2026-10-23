"""M4-A 的 HTTP 入口。

这一版只有 `/healthz`：它回答「这个进程还活着吗、三段线程各自在做什么」。
页面、登录与超媒体在计划 3 接入。`create_app` 只收一个 Runtime、不 import bootstrap：
应用不该知道依赖是怎么装的，否则每加一个页面测试都要先造一个完整容器。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from sams.services.runtime import Runtime


def create_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(title="SAMS", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(runtime.healthz(), status_code=200)

    return app
