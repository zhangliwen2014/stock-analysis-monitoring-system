"""手动补采快照：输出第一句是范围与日，源没答的票要说数。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime

import polars as pl
from support import FrozenClock, StubAdapter

import sams.cli as cli
from sams.bootstrap import build_container
from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.symbol_list import SymbolListRepo


class SnapStub:
    name = "tencent"
    markets = frozenset({"A"})
    market_snapshot_markets = frozenset({"A"})

    def __init__(self):
        self.codes: list[str] = []

    def market_snapshot(self, market, codes):
        self.codes = list(codes)
        keep = [c for c in codes if c != "000001"]
        return pl.DataFrame(
            {
                "market": ["A"] * len(keep),
                "code": keep,
                "name": ["票"] * len(keep),
                "quoted_on": [date(2026, 10, 28)] * len(keep),
                "price": [10.0] * len(keep),
                "pct_chg": [1.0] * len(keep),
                "amount": [1e8] * len(keep),
                "turnover_rate": [1.0] * len(keep),
                "volume_ratio": [1.0] * len(keep),
                "amplitude": [1.0] * len(keep),
                "pe_ttm": [10.0] * len(keep),
                "pe_dyn": [9.0] * len(keep),
                "pb": [1.2] * len(keep),
                "market_cap": [100.0] * len(keep),
                "float_cap": [90.0] * len(keep),
                "is_st": [False] * len(keep),
                "source": ["tencent"] * len(keep),
            },
            schema=SPOT_SCHEMA,
        )


def container_for(db, settings):
    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in ("600519", "000001", "300750")
        ],
    )
    # 夹具的 A 链默认是 akshare/eastmoney；装配期会校验「配置了就必须注册」，所以换成本桩
    markets = dict(settings.markets)
    markets["A"] = replace(markets["A"], sources=("tencent",))
    built = build_container(
        replace(settings, markets=markets),
        db=db,
        clock=FrozenClock(datetime(2026, 10, 28, 8, 41, tzinfo=UTC)),
        adapters={
            "tencent": SnapStub(),
            "yfinance": StubAdapter("yfinance", ["HK", "US"], None),
        },
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv):
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def test_sync_spot_reports_range_and_day(db, settings, capsys):
    container = container_for(db, settings)
    capsys.readouterr()
    assert run(container, ["sync", "spot", "--market", "A"]) == 0
    out = capsys.readouterr().out
    assert "快照 2026-10-28" in out
    assert "2 行" in out and "请求 3 只" in out
    assert "000001" in out, "源没答的票要点名，不能让「少了 1 只」混进「这个市场就这些票」"


def test_sync_spot_json_carries_the_same_numbers(db, settings, capsys):
    container = container_for(db, settings)
    capsys.readouterr()
    run(container, ["sync", "spot", "--market", "A", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 2 and payload["universe_size"] == 3
    assert payload["skipped"] == ["000001"]
    assert payload["day"] == "2026-10-28"
