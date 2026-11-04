"""中文别名（S4-C）：只补检索，不改主档；去重、同档、大小写不敏感。"""

from sams.storage import migrate
from sams.storage.repos.symbol_list import SymbolListRepo

AAPL = {
    "code": "AAPL",
    "name": "Apple Inc.",
    "py_initials": "aapl",
    "py_full": "appleinc",
    "currency": "USD",
}
TENCENT = {
    "code": "00700",
    "name": "TENCENT",
    "py_initials": "txkg",
    "py_full": "tengxunkonggu",
    "currency": "HKD",
}


def _alias(db, market: str, code: str, alias: str) -> None:
    db.execute(
        "INSERT INTO symbol_aliases (market, code, alias, source) VALUES (?, ?, ?, 'user')",
        [market, code, alias],
    )


def test_seed_alias_finds_a_symbol_the_master_name_does_not_contain(db):
    """搜「苹果」到 AAPL：中文名只在别名表里，主档名称是英文——这正是要补的那一格。"""
    repo = SymbolListRepo(db)
    repo.upsert_batch("US", [AAPL])
    repo.upsert_batch("HK", [TENCENT])
    assert [row["code"] for row in repo.search("苹果")] == ["AAPL"]
    assert [row["code"] for row in repo.search("腾讯")] == ["00700"]
    # 代码那条老路照旧：别名不是唯一入口
    assert [row["code"] for row in repo.search("AAPL")] == ["AAPL"]


def test_seeded_aliases_are_applied_twice_without_growing(db):
    """迁移的种子是 `INSERT OR IGNORE`：重放同一份文件不该多出第二份别名。"""
    path = migrate.MIGRATIONS_DIR / "0041_symbol_aliases.sql"
    before = int(db.scalar("SELECT count(*) FROM symbol_aliases", []))
    assert before >= 30, "种子至少覆盖 spec 里那 30 个常见中文名"
    for statement in migrate._split_statements(path.read_text(encoding="utf-8")):
        db.execute(statement)
    assert int(db.scalar("SELECT count(*) FROM symbol_aliases", [])) == before


def test_alias_is_case_insensitive_but_the_master_name_is_untouched(db):
    """别名大小写不敏感；命中后返回的仍是主档名称，别名只补充、不覆盖。"""
    repo = SymbolListRepo(db)
    repo.upsert_batch(
        "US",
        [
            {
                "code": "AAPL",
                "name": "Cupertino Co.",
                "py_initials": "cc",
                "py_full": "cupertinoco",
                "currency": "USD",
            }
        ],
    )
    _alias(db, "US", "AAPL", "Apple")
    rows = repo.search("apple")
    assert [row["code"] for row in rows] == ["AAPL"]
    assert rows[0]["name"] == "Cupertino Co."


def test_alias_hit_is_deduped_and_stays_behind_the_name_match(db):
    """一只票同时被名称与别名命中只出现一次；名称命中的那票排在纯别名命中之前。"""
    repo = SymbolListRepo(db)
    repo.upsert_batch(
        "A",
        [
            {
                "code": "000001",
                "name": "苹果园",
                "py_initials": "pgy",
                "py_full": "pingguoyuan",
                "currency": "CNY",
            }
        ],
    )
    repo.upsert_batch("US", [AAPL])
    _alias(db, "A", "000001", "苹果")
    rows = repo.search("苹果")
    assert [row["code"] for row in rows] == ["000001", "AAPL"]
    # 名称片段（第 5 档）领先纯别名（第 6 档）：market/code 兜底序会给出相反的一对
    assert [row["code"] for row in rows].count("000001") == 1


def test_inactive_rows_stay_out_of_alias_hits(db):
    repo = SymbolListRepo(db)
    repo.upsert_batch("US", [AAPL])
    assert repo.deactivate_missing("US", []) == 0  # 空清单不动表，别名也照常命中
    assert repo.search("苹果")[0]["code"] == "AAPL"
    assert repo.deactivate_missing("US", ["MSFT"]) == 1
    assert repo.search("苹果") == []


def test_blank_query_stays_empty_with_aliases_in_play(db):
    repo = SymbolListRepo(db)
    repo.upsert_batch("US", [AAPL])
    assert repo.search("") == []
    assert repo.search("   ") == []
