"""存量乱码修复：只碰白名单里的用户文本列，干跑清单就是回填清单（R3-26/D360）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.text import repair_mojibake
from sams.services.text_repair import TextRepairService


def broken(text: str) -> str:
    return text.encode("utf-8").decode("latin-1")


def seed(db):
    db.execute(
        "INSERT INTO watchlists (user_id, market, code, note) VALUES (?, ?, ?, ?)",
        ["u1", "A", "600519", broken("验收-混排")],
    )
    db.execute(
        "INSERT INTO watchlists (user_id, market, code, note) VALUES (?, ?, ?, ?)",
        ["u1", "A", "000001", "本来就对"],
    )
    db.execute(
        "INSERT INTO thesis_events (id, thesis_id, to_status, actor_id, note)"
        " VALUES ('ev1', 't1', 'open', 'u1', ?)",
        [broken("现场验证")],
    )
    # 源数据的同形字节不在白名单里：它不该被这条命令改写
    now = datetime.now(UTC)
    db.execute(
        "INSERT INTO news_items"
        " (market, code, source, item_id, published_at, title, url, fetched_at)"
        " VALUES ('A', '600519', 'sina', 'n1', ?, ?, 'http://example.test/n1', ?)",
        [now, broken("源标题"), now],
    )


def test_scan_lists_only_the_whitelisted_rows_that_fold_back(db):
    seed(db)
    found = TextRepairService(db).scan()
    assert {(item.table, item.column, item.key) for item in found} == {
        ("watchlists", "note", "u1/A/600519"),
        ("thesis_events", "note", "ev1"),
    }
    assert {item.after for item in found} == {"验收-混排", "现场验证"}


def test_apply_writes_the_folded_text_and_is_idempotent(db):
    seed(db)
    service = TextRepairService(db)
    assert len(service.apply()) == 2

    note = db.scalar("SELECT note FROM watchlists WHERE code = '600519'")
    assert note == "验收-混排"
    assert db.scalar("SELECT note FROM watchlists WHERE code = '000001'") == "本来就对"
    assert db.scalar("SELECT note FROM thesis_events WHERE id = 'ev1'") == "现场验证"
    # 源数据不动
    assert db.scalar("SELECT title FROM news_items WHERE item_id = 'n1'") == broken("源标题")

    assert service.scan() == ()
    assert service.apply() == ()


def test_the_shared_predicate_is_the_same_one_the_form_uses(db):
    broken_note = broken("验收-混排")
    assert repair_mojibake(broken_note) == "验收-混排"
    db.execute(
        "INSERT INTO watchlists (user_id, market, code, note) VALUES ('u1', 'A', 'X', ?)",
        [repair_mojibake(broken_note)],
    )
    assert TextRepairService(db).scan() == ()
