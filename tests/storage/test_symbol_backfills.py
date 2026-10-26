"""补数队列：open_key 去重、结案后可重开、三次失败转 deferred（D144）。"""

import pytest

from sams.storage.repos.symbol_backfills import SymbolBackfillRepo


def test_enqueue_dedupe_and_reopen(db):
    repo = SymbolBackfillRepo(db)
    assert repo.enqueue("A", "600519") is True
    assert repo.enqueue("A", "600519") is False
    row = repo.open_for("A", "600519")
    repo.mark_done(row.id)
    assert repo.open_for("A", "600519") is None
    # 结案的老行 open_key 已置 NULL，不再占唯一键：同一只票可以再排
    assert repo.enqueue("A", "600519") is True


def test_note_failure_defers(db):
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519")
    row = repo.take_pending()[0]
    assert repo.note_failure(row.id, "boom\nstack") == "pending"
    # 多行错误只留第一行：这一句才是 splitlines 的证明（三次调用各用可区分的字符串）
    assert repo.open_for("A", "600519").last_error == "boom"
    assert repo.note_failure(row.id, "second line only") == "pending"
    assert repo.note_failure(row.id, "kapow") == "deferred"
    row = repo.open_for("A", "600519")
    # deferred 仍占着开放键；错误只存第一行
    assert (row.status, row.tries, row.last_error) == ("deferred", 3, "kapow")


def test_note_failure_unknown_id_raises(db):
    """失踪的 id 不返回假字符串："None" 会被调用方当成一个未知状态继续排下去。"""
    repo = SymbolBackfillRepo(db)
    with pytest.raises(ValueError, match="不存在"):
        repo.note_failure("deadbeef", "boom")


def test_take_pending_respects_limit(db):
    repo = SymbolBackfillRepo(db)
    for i in range(5):
        repo.enqueue("A", f"60000{i}")
    assert len(repo.take_pending(limit=3)) == 3


def test_mark_done_by_code(db):
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519")
    repo.mark_done_by_code("A", "600519")
    assert repo.open_for("A", "600519") is None
    assert repo.enqueue("A", "600519") is True
