"""`sams serve` —— 常驻进程：盘后调度、盘中轮询与 Web 在同一个进程里。

这个命令与其余命令的形状不同：它拿的是 `Settings` 而不是 `Container`。
三条线程各要一条 DuckDB 连接（D113），而 CLI 的容器只有一条；
所以 `run()` 在装配容器之前就把它分派出去（见 `cli/__init__.py`）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

import uvicorn

from sams.bootstrap import build_serve
from sams.cli.common import EXIT_LOCKED, advertised_url
from sams.core.config import ServerCfg, Settings
from sams.core.errors import DatabaseLocked, SamsError, ValidationError
from sams.web.app import create_app


def add_to(subparsers) -> None:
    # 不设 handler：`run()` 在造容器之前就分派到 `start(settings, args)`，
    # 它的第一个参数不是 Container，放进通用的 `args.handler(container, args)` 会错位
    parser = subparsers.add_parser("serve", help="常驻运行：盘后调度 + 盘中监控 + Web")
    parser.add_argument("--host", help="监听地址，默认取 [server].host")
    parser.add_argument("--port", type=int, help="监听端口，默认取 [server].port")


def _server_for(settings: Settings, args: argparse.Namespace) -> ServerCfg:
    """命令行覆盖只合并 host/port，然后整份重新校验。

    只检查这两项而不跑 `validate()`，`--host 0.0.0.0` 就绕开了 `allow_lan`：
    一条命令把库暴露到局域网，正是 spec §7 要防的事。
    """
    server = settings.server
    merged = replace(
        server,
        host=args.host if args.host is not None else server.host,
        port=args.port if args.port is not None else server.port,
    )
    merged.validate()
    return merged


def _configure_logging() -> None:
    """常驻进程的日志出口。

    库里的服务用 `logger.info` 报启动与每日清理，而根记录器默认只放行 WARNING：
    不在这里配置一次，`serve.log` 里就只剩 uvicorn 那几行，运维 grep 不到
    「运行时已启动」「每日清理」（验收要它们）。已被别的 handler 接管时
    `basicConfig` 自己不动手——嵌进别人的进程时不该再插一手。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def start(settings: Settings, args: argparse.Namespace) -> int:
    _configure_logging()
    try:
        server = _server_for(settings, args)
    except ValidationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1

    parts = None
    runtime = None
    try:
        parts = build_serve(settings)
        runtime = parts.runtime
        restored = parts.prepare()
        runtime.start()
        print(f"SAMS 已启动：{advertised_url(server)}（Ctrl-C 停止，单 worker）")
        if restored:
            print(f"  行情板已恢复 {restored} 只票的最近报价")
        uvicorn.run(
            create_app(runtime),
            host=server.host,
            port=server.port,
            workers=1,
            reload=False,
            ssl_certfile=server.tls_certfile,
            ssl_keyfile=server.tls_keyfile,
        )
        return 0
    except KeyboardInterrupt:
        # Ctrl-C 是正常的停机方式：屏幕不该多一段 traceback
        return 0
    except DatabaseLocked as exc:
        # 另一个 `sams serve` 正握着库：与其它命令同一个码（D111）
        print(f"数据库被占用: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except ValidationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    except SamsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            runtime.stop()
        if parts is not None:
            parts.close()
