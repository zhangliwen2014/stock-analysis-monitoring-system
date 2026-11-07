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


def test_two_kinds_for_the_same_symbol_are_two_rows(db):
    """一条待补日线不该把「公司行为覆盖也没确认」一起吞掉（spec 2026-11-03 §3.3）。

    去重键不带 kind 时，先排上的那条会占住这只票：后一种活永远进不了队列，
    而先完成的那件又会把两件一起标成 done。
    """
    repo = SymbolBackfillRepo(db)
    assert repo.enqueue("A", "600519", kind="bars") is True
    assert repo.enqueue("A", "600519", kind="actions") is True
    assert repo.enqueue("A", "600519", kind="actions") is False, "同 kind 重复排要吸收掉"
    assert {row.kind for row in repo.take_pending()} == {"bars", "actions"}


def test_kind_defaults_to_bars(db):
    """不传 kind 的旧调用方（自选、看板、成交）语义一字不改。"""
    repo = SymbolBackfillRepo(db)
    assert repo.enqueue("A", "600519") is True
    assert repo.open_for("A", "600519").kind == "bars"


def test_the_bars_wrap_up_does_not_close_the_actions_row(db):
    """盘后日线任务结的是日线的账：把同一票的「公司行为待确认」顺手标成 done，
    那条覆盖就永远补不上了，而界面上那句「后台已排队」变成假话。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind="bars")
    repo.enqueue("A", "600519", kind="actions")
    repo.mark_done_by_code("A", "600519")
    assert [row.kind for row in repo.take_pending()] == ["actions"]


def test_defer_parks_a_row_without_pretending_the_consumer_tried(db):
    """孤源止损顺延的行直接进 deferred（D330）。

    先 `enqueue` 再记三次失败会把 `tries` 写成 3，读起来像「消费者真试过三次」——
    实际一次都没有；顺延是 job 在被撞之前就替它们做的决定。
    """
    repo = SymbolBackfillRepo(db)
    assert repo.defer("A", "600519", "valuation", "A 孤源 yfinance 连败 10 次") is True
    row = repo.open_for("A", "600519", kind="valuation")
    assert (row.status, row.tries) == ("deferred", 0)
    assert row.last_error == "A 孤源 yfinance 连败 10 次"
    assert repo.take_pending() == [], "deferred 不占一拍的三条额度"
    assert repo.defer("A", "600519", "valuation", "again") is False, "已有开放行就吸收掉"


def test_list_filtered_combines_status_market_and_pagination(db):
    """管理页三块筛选共用一个页码：总数与行必须是同一组条件筛出来的。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind="bars")
    repo.enqueue("A", "000001", kind="bars")
    repo.defer("HK", "00700", "valuation", "孤源顺延")

    rows, total = repo.list_filtered()
    assert total == 3 and len(rows) == 3
    rows, total = repo.list_filtered(status="pending")
    assert total == 2 and {row.code for row in rows} == {"600519", "000001"}
    rows, total = repo.list_filtered(market="HK")
    assert total == 1 and (rows[0].status, rows[0].kind) == ("deferred", "valuation")
    rows, total = repo.list_filtered(page=2, per_page=2)
    assert total == 3 and len(rows) == 1


def test_retry_deferred_requeues_only_that_row_and_resets_its_budget(db):
    repo = SymbolBackfillRepo(db)
    repo.defer("A", "600519", "valuation", "孤源顺延")
    row = repo.open_for("A", "600519", kind="valuation")

    assert repo.retry_deferred(row.id) is True
    reopened = repo.open_for("A", "600519", kind="valuation")
    assert (reopened.status, reopened.tries, reopened.last_error) == ("pending", 0, None)
    assert repo.retry_deferred(row.id) is False, "already pending 不再动它"

    repo.enqueue("A", "000001", kind="bars")
    done = next(row for row in repo.take_pending() if row.code == "000001")
    repo.mark_done(done.id)
    assert repo.retry_deferred(done.id) is False, "完成的行不许借重试回到待办"
    assert repo.retry_deferred("missing") is False


def test_rearm_deferred_gives_the_next_day_a_new_budget(db):
    """deferred 的原文承诺是「今天预算用光，明天再来」：跨天顺延要真的把它排回去。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind="actions")
    row = repo.take_pending()[0]
    for _ in range(3):
        repo.note_failure(row.id, "源挂了")
    assert repo.open_for("A", "600519", kind="actions").status == "deferred"
    assert repo.rearm_deferred("actions") == 1
    rearmed = repo.open_for("A", "600519", kind="actions")
    assert (rearmed.status, rearmed.tries) == ("pending", 0)
    # 另一种 kind 的 deferred 不归这次顺延管：日线队列自有盘后任务收尾
    repo.enqueue("A", "000001", kind="bars")
    # 按 kind 取那一行：刚顺延回来的 actions 行创建得更早，`take_pending()[0]` 是它
    bars_row = next(row for row in repo.take_pending() if row.kind == "bars")
    for _ in range(3):
        repo.note_failure(bars_row.id, "源挂了")
    assert repo.rearm_deferred("actions") == 0
