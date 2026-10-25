"""推送上下文那一块的字面文本（spec §7.2）：选择、排序、去重、单行、字节预算。

core 不做 I/O 是这一层的定义性约束：这里只有「这三行该长什么样」，
取哪几条批注住在 `services/annotation_context`，什么时候取住在 `monitor/router`。
`budget_bytes` 按 UTF-8 字节算，因为通道的限制与 `trim` 都是字节——同一条链上
两处剪文字，单位必须一致，否则一处按字、一处按字节，边界永远差着中文那两三倍。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.notify import (
    CONTEXT_HEADER,
    CONTEXT_MAX_ITEMS,
    AnnotationContext,
    PushContext,
    annotation_context_line,
    format_annotation_context,
)


def item(
    n: str,
    *,
    created: datetime | None = None,
    when: str = "10-22 14:03",
    author: str = "我",
    body: str = "渠道库存见底",
    tags: tuple[str, ...] = (),
    pinned: bool = False,
) -> AnnotationContext:
    return AnnotationContext(
        annotation_id=n,
        created_at=created or datetime(2026, 10, 22, 6, 3, tzinfo=UTC),
        moment_label=when,
        author_label=author,
        body_text=body,
        tags=tags,
        pinned=pinned,
    )


def test_push_context_defaults_are_empty():
    """「没找到批注」与「provider 说这条不该有上下文」是两个事实，默认值不许混。"""
    push = PushContext()
    assert push.items == () and push.link == ""


def test_a_line_is_the_literal_shape_from_the_spec():
    line = annotation_context_line(item("a", tags=("逻辑", "风险")))
    assert line == "· 10-22 14:03 我 [逻辑 风险] 渠道库存见底"


def test_a_line_without_tags_has_no_brackets_and_collapses_whitespace():
    line = annotation_context_line(item("a", body="两行\n\n压成  一行"))
    assert line == "· 10-22 14:03 我 两行 压成 一行"


def test_the_body_is_hard_capped_at_one_twenty_chars():
    """`plain_text` 已经把批注压到 120，格式化这一层再挡一次：provider 给的是
    `body_md` 原文也不能让一行炸开——推送里一行就是一行。"""
    line = annotation_context_line(item("a", body="字" * 200))
    assert len(line.split(" ", 4)[4]) == 120
    assert line.endswith("…")


def test_empty_items_render_no_header():
    text, ids, dropped = format_annotation_context(())
    assert text == "" and ids == () and dropped == 0


def test_dedupe_by_id_keeps_the_first_occurrence():
    text, ids, _ = format_annotation_context((item("a"), item("a", body="后写的"), item("b")))
    assert ids == ("a", "b")
    assert "渠道库存见底" in text


def test_pinned_first_then_newest():
    old_pinned = item(
        "pinned-old", pinned=True, when="10-01 09:00", created=datetime(2026, 10, 1, tzinfo=UTC)
    )
    fresh = item("fresh", when="10-22 09:00", created=datetime(2026, 10, 22, tzinfo=UTC))
    old = item("plain-old", when="10-02 09:00", created=datetime(2026, 10, 2, tzinfo=UTC))
    _, ids, _ = format_annotation_context((old, fresh, old_pinned))
    assert ids == ("pinned-old", "fresh", "plain-old")


def test_at_most_three_items_survive():
    items = tuple(item(str(n), when=f"10-2{n} 09:00") for n in range(1, 6))
    text, ids, dropped = format_annotation_context(items)
    assert len(ids) == CONTEXT_MAX_ITEMS
    assert dropped == len(items) - CONTEXT_MAX_ITEMS
    assert text.count("·") == CONTEXT_MAX_ITEMS


def test_budget_none_is_no_budget():
    items = (item("a"), item("b"))
    text, ids, dropped = format_annotation_context(items)
    assert ids == ("a", "b") and dropped == 0 and CONTEXT_HEADER in text


def test_a_tight_budget_drops_from_the_back():
    """尾部是「最新但非置顶」的那几条：先剪它们，置顶的优先级才不是空话。"""
    items = (
        item("a", body="第一" + "条" * 20),
        item("b", body="第二条内容"),
        item("c", body="第三条内容"),
    )
    full, _, _ = format_annotation_context(items)
    budget = len(full.encode()) - len("· …第三条内容".encode()) + 1
    text, ids, dropped = format_annotation_context(items, budget_bytes=budget)
    assert "第三条" not in text
    assert ids[-1] != "c" if len(ids) > 1 else "c" not in ids
    assert dropped >= 1


def test_a_budget_that_cannot_fit_the_first_item_drops_the_whole_block():
    """「剩余空间连一条都放不下」时整块不发（§7.2）：半行截成乱码不如不发。"""
    items = (item("a"), item("b"))
    budget = len(CONTEXT_HEADER.encode()) + 10  # 放得下题头、放不下第一条
    text, ids, dropped = format_annotation_context(items, budget_bytes=budget)
    assert text == "" and ids == () and dropped == 2


def test_a_negative_budget_drops_everything_too():
    """正文本身已经超限时 room 是负的：不许出现「只剩一行孤零零的题头」。"""
    text, ids, dropped = format_annotation_context((item("a"),), budget_bytes=-100)
    assert text == "" and ids == () and dropped == 1
