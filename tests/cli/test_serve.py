"""`sams serve`：启动前把三件事做完（迁移、灌板、起线程），退出时收干净。

uvicorn 在这一层换成记录参数的替身：真起一个 HTTP 服务属于验收（spec §10），
单测钉的是「传给它的 host/port/worker 是哪几个」与「崩了以后有没有收尾」。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sams.cli import serve
from sams.cli.common import EXIT_LOCKED
from sams.core.config import ServerCfg
from sams.core.errors import DatabaseLocked, ValidationError


def args(host=None, port=None):
    return SimpleNamespace(host=host, port=port)


class FakeRuntime:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeParts:
    def __init__(self, *, loaded=2):
        self.runtime = FakeRuntime()
        self.loaded = loaded
        self.prepared = False
        self.closed = False

    def prepare(self):
        self.prepared = True
        return self.loaded

    def close(self):
        self.closed = True


@pytest.fixture
def started(monkeypatch):
    parts = FakeParts()
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(serve, "build_serve", lambda settings: parts)
    monkeypatch.setattr(serve.uvicorn, "run", fake_run)
    return parts, seen


def test_a_command_line_port_override_keeps_the_configured_host(settings):
    server = serve._server_for(settings, args(port=9001))
    assert server.host == "127.0.0.1" and server.port == 9001
    assert serve._server_for(settings, args()).port == 8765


def test_a_wildcard_host_from_the_command_line_still_needs_allow_lan(settings):
    """覆盖只合并两项，然后整份重新校验：漏掉 `validate()` 就是一条命令开一扇门。"""
    with pytest.raises(ValidationError, match="allow_lan"):
        serve._server_for(settings, args(host="0.0.0.0"))


def test_start_prepares_then_starts_and_passes_single_worker_flags(
    settings, started, capsys
):
    parts, seen = started

    code = serve.start(settings, args())

    assert code == 0
    assert parts.prepared and parts.runtime.started
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    assert seen["workers"] == 1 and seen["reload"] is False
    assert seen["ssl_certfile"] is None and seen["ssl_keyfile"] is None
    assert parts.runtime.stopped and parts.closed
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765" in out
    assert "行情板已恢复 2 只票" in out


def test_start_opens_the_info_channel_for_the_runtime_log(settings, started, monkeypatch):
    """不配日志，`logger.info("运行时已启动")` 会被根记录器的 WARNING 门槛吞掉：
    `serve.log` 里只剩 uvicorn 几行，验收要 grep 的启动与清理一行都没有。"""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)

    assert serve.start(settings, args()) == 0

    assert root.level == logging.INFO
    assert root.handlers, "至少要有往 stderr 写的一只手"
    assert logging.getLogger("sams.services.runtime").isEnabledFor(logging.INFO)


def test_tls_files_go_to_uvicorn_and_the_advertised_url_is_https(settings, started, capsys):
    _, seen = started
    tls = replace(
        settings,
        server=ServerCfg(
            tls_certfile="cert.pem",
            tls_keyfile="key.pem",
        ),
    )

    assert serve.start(tls, args()) == 0

    assert seen["ssl_certfile"] == "cert.pem"
    assert seen["ssl_keyfile"] == "key.pem"
    assert "https://127.0.0.1:8765" in capsys.readouterr().out


def test_a_crashed_server_still_stops_the_threads_and_closes(settings, monkeypatch):
    parts = FakeParts()

    def boom(app, **kwargs):
        raise RuntimeError("端口被占")

    monkeypatch.setattr(serve, "build_serve", lambda settings: parts)
    monkeypatch.setattr(serve.uvicorn, "run", boom)

    with pytest.raises(RuntimeError, match="端口被占"):
        serve.start(settings, args())

    assert parts.runtime.stopped and parts.closed


def test_a_refused_address_exits_1_without_opening_the_database(
    settings, monkeypatch, capsys
):
    monkeypatch.setattr(
        serve, "build_serve", lambda settings: pytest.fail("配置没过就不该开库")
    )

    assert serve.start(settings, args(host="0.0.0.0")) == 1

    assert "allow_lan" in capsys.readouterr().err


def test_a_locked_database_exits_3(settings, monkeypatch, capsys):
    def locked(settings):
        raise DatabaseLocked(settings.db_path, detail="Conflicting lock (PID 42)")

    monkeypatch.setattr(serve, "build_serve", locked)

    assert serve.start(settings, args()) == EXIT_LOCKED
    assert "PID 42" in capsys.readouterr().err


def test_run_dispatches_serve_before_assembling_a_container(settings, monkeypatch):
    """serve 要三条连接：先造 CLI 那条容器会白开一条，还会把它握在手里（D113）。"""
    from sams import cli

    monkeypatch.setattr(
        cli.Settings, "load", classmethod(lambda cls, path=None: settings)
    )
    monkeypatch.setattr(
        cli, "build_container", lambda *a, **k: pytest.fail("serve 不该先造容器")
    )
    seen: dict = {}

    def fake_start(loaded, parsed):
        seen["settings"] = loaded
        seen["args"] = parsed
        return 7

    monkeypatch.setattr(cli.serve, "start", fake_start)

    assert cli.run(["serve", "--port", "9001"]) == 7
    assert seen["settings"] is settings
    assert seen["args"].port == 9001
