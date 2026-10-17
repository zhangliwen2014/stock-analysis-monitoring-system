"""共用夹具：内存库 + 已迁移 + 一份不联网的 Settings。

测试一律不碰真实网络与磁盘库：外部调用通过构造参数注入假实现
（适配器喂录制 payload、registry 塞假 adapter、CLI 传 stub adapters）。
样本数据与替身在 support.py，这里只做 fixture 绑定。
"""

from __future__ import annotations

import duckdb
import polars as pl
import pytest
import support

from sams.core.config import MarketCfg, Settings
from sams.storage.db import Database
from sams.storage.migrate import apply_migrations


@pytest.fixture
def db() -> Database:
    """每个用例一个独立的内存库，建表后交给用例，用完即弃。"""
    database = Database(duckdb.connect(":memory:"))
    apply_migrations(database)
    yield database
    database.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "sams.duckdb"),
        secret_key="test-secret",
        report_currency="CNY",
        rate_limit_per_s=1000,
        polling_interval_s=10,
        cooldown_min=30,
        markets={
            "A": MarketCfg("A", "Asia/Shanghai", "CNY", 30, ("akshare", "eastmoney")),
            "HK": MarketCfg("HK", "Asia/Hong_Kong", "HKD", 30, ("yfinance",)),
            "US": MarketCfg(
                "US",
                "America/New_York",
                "USD",
                60,
                ("yfinance",),
                (("pre", "04:00", "09:30"), ("post", "16:00", "20:00")),
            ),
        },
    )


@pytest.fixture
def bars_frame() -> pl.DataFrame:
    """三天标准列集（数据本体见 support.bars_frame）：够验幂等与区间读取。"""
    return support.bars_frame()
