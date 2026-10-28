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
    # 跨档钉：这一票的 py_full 恰是 000001 的 py_initials，一个检索词同时命中两档，
    # 才测得出「每档各查一遍再拼」是不是真的把全局序钉住（档位互换就会挂）
    {
        "code": "600001",
        "name": "兴祥药业",
        "py_initials": "xxyy",
        "py_full": "payh",
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
    r.upsert_batch("A", ROWS[:3])
    r.upsert_batch("HK", ROWS[3:])
    return r


def test_get_and_replace(db, repo):
    assert repo.get("A", "000001")["name"] == "平安银行"
    assert repo.get("A", "000001")["currency"] == "CNY"
    repo.upsert_batch("A", [{**ROWS[0], "name": "平安银行A"}])
    assert repo.get("A", "000001")["name"] == "平安银行A"
    assert repo.get("A", "不存在") is None


def test_upsert_batch_is_one_set_write(db, repo):
    """集合写的两个边角自己也得有证明：空批不动表，同批同码留最后一条（不靠 DuckDB 的次序）。"""
    before = repo.count("A")
    repo.upsert_batch("A", [])
    assert repo.count("A") == before
    repo.upsert_batch(
        "A",
        [
            {**ROWS[1], "name": "贵州茅台一号"},
            {**ROWS[1], "name": "贵州茅台二号"},
        ],
    )
    assert repo.get("A", "600519")["name"] == "贵州茅台二号"
    assert repo.count("A") == before


def test_search_tiers(repo):
    # 第 1 档：精确代码
    assert repo.search("000001")[0]["code"] == "000001"
    # 第 2 档：首字母精确，输入大小写无关（600001 只有全拼相等，排在后面）
    assert [row["code"] for row in repo.search("PAYH")] == ["000001", "600001"]
    # 第 3 档：全拼精确
    assert [row["code"] for row in repo.search("guizhoumaotai")] == ["600519"]
    # 第 4 档：首字母前缀
    assert [row["code"] for row in repo.search("gzy")] == ["600519"]
    # 第 5 档：名称片段（中文原样进 lower 不受伤）
    assert [row["code"] for row in repo.search("银行")] == ["000001"]
    # 跨市同码不相撞：007 前缀命中 HK/00700
    assert repo.search("007")[0]["market"] == "HK"


def test_search_tiers_ordered_across_rows(repo):
    """跨档的完整有序列表——单档各查一遍的写法在这里挂，不在上面那一段挂。"""
    # "payh" 同时是 000001 的首字母精确（第 2 档）和 600001 的全拼精确（第 3 档）；
    # 按 market/code 排或按命中先后拼，都会给出 ["600001", "000001"] 或别的次序
    assert [row["code"] for row in repo.search("payh")] == ["000001", "600001"]
    # "pay" 只有前缀与片段两种命中：000001 首字母以 pay 开头（第 4 档），
    # 600001 只是 py_full 里含 pay（第 5 档）——前缀档必须排在片段档之前
    assert [row["code"] for row in repo.search("pay")] == ["000001", "600001"]
    # 这一例才是「ORDER BY 掉了 tier」的钉子：HK/00700 的首字母前缀（第 4 档）必须排在
    # A/600519 的全拼片段（第 5 档，guizhouMAOTai 含 "t"）之前，而 market/code 兜底序
    # 正好相反（A 在 HK 前）——上面两档同向的用例对这种错法是哑的
    assert [row["code"] for row in repo.search("T")] == ["00700", "600519"]
    # 精确代码档（第 1 档）不会把别票捎进来：600001 与 "000001" 无涉
    assert [row["code"] for row in repo.search("000001")] == ["000001"]


def test_search_skips_inactive_and_caps(repo):
    # 这份清单只保 000001：A 市的 600519 与 600001 被关停，前缀/片段档查不到它们
    assert repo.deactivate_missing("A", ["000001"]) == 2
    assert [row["code"] for row in repo.search("gzy")] == []
    assert [row["code"] for row in repo.search("payh")] == ["000001"]
    # 000001 在这份清单里，压根没被关停，所以精确代码照常命中（不是「关停行仍可读」）
    assert repo.search("000001")[0]["market"] == "A"
    assert len(repo.search("0", limit=10)) <= 10
    # 真把 000001 关停之后，精确代码也查不到：is_active 在 WHERE 里，五档无一例外
    assert repo.deactivate_missing("A", ["600519"]) == 1
    assert repo.search("000001") == []


def test_search_rejects_blank_query(repo):
    """空检索词是仓库层的自守：`contains(x, "")` 全表真，无名票（空 py 列）会整个落进第 2 档。
    现有调用方 `normalize_query` 已保证非空，但仓库不该依赖调用方的自觉。"""
    assert repo.search("") == []
    assert repo.search("   ") == []


def test_search_fullpinyin_prefix_is_tier4_not_fragment(repo):
    """D142 第 4 档含全拼前缀：`pingan` 是 000001 的全拼开头（第 4 档），
    而对照票只是全拼里含 `pingan`（第 5 档）——旧写法把前者也落下第 5 档，
    对照票码更小、market/code 兜底序反而把它排前面，档位就露馅。"""
    repo.upsert_batch(
        "A",
        [
            {
                "code": "000000",
                "name": "华平金融",
                "py_initials": "hpjr",
                "py_full": "huapinganjirong",
                "currency": "CNY",
            }
        ],
    )
    assert [row["code"] for row in repo.search("pingan")] == ["000001", "000000"]


def test_search_code_fragment_finds_inner_digits(repo):
    """敲「0028」要能找到 000028：`contains(code)` 只在 WHERE 放行，档位落第 5 段。"""
    repo.upsert_batch(
        "A",
        [
            {
                "code": "000028",
                "name": "深圳市情",
                "py_initials": "szsq",
                "py_full": "shenzhenshiqing",
                "currency": "CNY",
            }
        ],
    )
    hits = {(row["market"], row["code"]) for row in repo.search("0028")}
    assert ("A", "000028") in hits


def test_deactivate_missing_counts(repo):
    assert repo.deactivate_missing("A", ["000001"]) == 2
    assert repo.count("A") == 1
    assert repo.count() == 2
    assert repo.deactivate_missing("A", ["000001", "600519", "600001"]) == 0


def test_active_codes_returns_the_ordered_live_list(repo):
    assert repo.active_codes("A") == ["000001", "600001", "600519"]
    # 传的是「这一轮仍在市」的清单：不在清单里的 600519 被关停
    repo.deactivate_missing("A", ["000001", "600001"])
    assert repo.active_codes("A") == ["000001", "600001"]
    assert repo.active_codes("HK") == ["00700"]
