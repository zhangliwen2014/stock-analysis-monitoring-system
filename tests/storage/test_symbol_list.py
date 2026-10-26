"""代码表仓库：入库幂等、退市关停、五档排序一次 SQL 定序（D142）。"""

import pytest

from sams.storage.repos.symbol_list import SymbolListRepo

ROWS = [
    {
        "code": "000001",
        "name": "平安银行",
        "py_initials": "payh",
        "py_full": "pinganyinhang",
        "currency": "CNY",
    },
    {
        "code": "600519",
        "name": "贵州茅台",
        "py_initials": "gzym",
        "py_full": "guizhoumaotai",
        "currency": "CNY",
    },
    {
        "code": "00700",
        "name": "腾讯控股",
        "py_initials": "txkg",
        "py_full": "tengxunkonggu",
        "currency": "HKD",
    },
]


@pytest.fixture
def repo(db):
    r = SymbolListRepo(db)
    r.upsert_batch("A", ROWS[:2])
    r.upsert_batch("HK", ROWS[2:])
    return r


def test_get_and_replace(db, repo):
    assert repo.get("A", "000001")["name"] == "平安银行"
    assert repo.get("A", "000001")["currency"] == "CNY"
    repo.upsert_batch("A", [{**ROWS[0], "name": "平安银行A"}])
    assert repo.get("A", "000001")["name"] == "平安银行A"
    assert repo.get("A", "不存在") is None


def test_search_tiers(repo):
    # 第 1 档：精确代码
    assert repo.search("000001")[0]["code"] == "000001"
    # 第 2 档：首字母精确，输入大小写无关
    assert [row["code"] for row in repo.search("PAYH")] == ["000001"]
    # 第 3 档：全拼精确
    assert [row["code"] for row in repo.search("guizhoumaotai")] == ["600519"]
    # 第 4 档：首字母前缀
    assert [row["code"] for row in repo.search("gzy")] == ["600519"]
    # 第 5 档：名称片段（中文原样进 lower 不受伤）
    assert [row["code"] for row in repo.search("银行")] == ["000001"]
    # 跨市同码不相撞：007 前缀命中 HK/00700
    assert repo.search("007")[0]["market"] == "HK"


def test_search_skips_inactive_and_caps(repo):
    repo.deactivate_missing("A", ["000001"])
    assert [row["code"] for row in repo.search("gzy")] == []
    assert repo.search("000001")[0]["market"] == "A"  # 关停不删行，精确代码仍在
    assert len(repo.search("0", limit=10)) <= 10


def test_deactivate_missing_counts(repo):
    assert repo.deactivate_missing("A", ["000001"]) == 1
    assert repo.count("A") == 1
    assert repo.count() == 2
    assert repo.deactivate_missing("A", ["000001", "600519"]) == 0
