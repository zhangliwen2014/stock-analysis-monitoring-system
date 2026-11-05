"""代码表周同步：拼音两列写时算、全链空答复不动表（D141/D147）。"""

import polars as pl
import pytest

from sams.datasource.registry import SymbolListResult
from sams.services.symbol_master import SymbolMasterSyncService, pinyin_columns
from sams.storage.repos.symbol_list import SymbolListRepo


def test_pinyin_columns():
    assert pinyin_columns("平安银行") == ("payh", "pinganyinhang")  # 行在银行里读 háng
    assert pinyin_columns(" 贵州 茅台 ") == ("gzmt", "guizhoumaotai")  # 空白先去掉
    assert pinyin_columns("AB Corp") == ("abcorp", "abcorp")  # 非中文原样小写


class StubRegistry:
    def __init__(self, result):
        self._result = result

    def fetch_symbol_list(self, market):
        return self._result


def _result(rows):
    frame = (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(schema={"code": pl.Utf8, "name": pl.Utf8})
    )
    return SymbolListResult(market="A", source="akshare", frame=frame, empty_everywhere=not rows)


@pytest.fixture
def repo(db):
    return SymbolListRepo(db)


def test_sync_writes_py_and_currency(db, repo, settings):
    svc = SymbolMasterSyncService(
        StubRegistry(_result([{"code": "000001", "name": "平安银行"}])), repo, settings.markets
    )
    summary = svc.sync("A")
    assert (summary.rows, summary.deactivated, summary.source) == (1, 0, "akshare")
    row = repo.get("A", "000001")
    assert row["py_initials"] == "payh" and row["py_full"] == "pinganyinhang"
    assert row["currency"] == "CNY"


def test_sync_deactivates_gone_codes(db, repo, settings):
    repo.upsert_batch(
        "A",
        [
            {
                "code": "600000",
                "name": "老票",
                "py_initials": "lp",
                "py_full": "lao",
                "currency": "CNY",
            }
        ],
    )
    svc = SymbolMasterSyncService(
        StubRegistry(_result([{"code": "000001", "name": "平安银行"}])), repo, settings.markets
    )
    summary = svc.sync("A")
    assert summary.deactivated == 1
    assert repo.get("A", "600000")["is_active"] is False


def test_empty_everywhere_keeps_old_table(db, repo, settings):
    repo.upsert_batch(
        "A",
        [
            {
                "code": "000001",
                "name": "平安银行",
                "py_initials": "payh",
                "py_full": "pinganyinhang",
                "currency": "CNY",
            }
        ],
    )
    svc = SymbolMasterSyncService(StubRegistry(_result([])), repo, settings.markets)
    summary = svc.sync("A")
    assert (summary.rows, summary.deactivated, summary.kept_old) == (0, 0, True)
    assert repo.get("A", "000001")["is_active"] is True  # 网络抖动一次不许把全市场标成退市


def test_a_halved_roster_deactivates_nothing(db, repo, settings):
    """新名单比旧表少一半 = 源读少了，不是全市场同时退市（D315）。

    准入闸（D314）把「在册」变成采集的前置条件之后，一次读小的名单不再只是搜索少几只：
    没进名单的票当晚起从每一条按只循环的链里掉出去，整片停更。真实退市是每周几十只的量级
    （实机三张快照一共关停 6 行），一半这个地板拦不到它。
    """
    repo.upsert_batch(
        "A",
        [
            {
                "code": f"60{suffix:04d}",
                "name": "老票",
                "py_initials": "lp",
                "py_full": "lao",
                "currency": "CNY",
            }
            for suffix in range(4)
        ],
    )
    svc = SymbolMasterSyncService(
        StubRegistry(_result([{"code": "000001", "name": "平安银行"}])), repo, settings.markets
    )
    summary = svc.sync("A")
    assert (summary.deactivated, summary.kept_floor) == (0, True)
    for suffix in range(4):
        assert repo.get("A", f"60{suffix:04d}")["is_active"] is True
    # 地板只拦「关停」，不拦「写入」：新读到的那一行照常在册
    assert repo.get("A", "000001")["is_active"] is True
