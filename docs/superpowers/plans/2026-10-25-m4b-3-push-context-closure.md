# M4-B 计划 3：推送上下文与闭环动作 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 推送在发送时带上「接收人可见的该标的最近 3 条批注」（spec §7），并把告警闭环的用例层补齐（spec §8：act/dismiss/ack/defer 与 trade 的同事务成交），CLI 五动作接线（§8.1）。

**Architecture:** `core/notify` 只加词汇（`AnnotationContext`/`PushContext`/格式化），不碰 I/O；`services/annotation_context` 是发送时取批注的 provider（monitor 通过注入认识它，不 import services——§12.1 分层红线）；`monitor/router` 在 `_deliver` 里做**逐通道**字节预算与 `notify_log.context_json` 审计；`services/review` 是闭环用例层（trade 与成交同事务，仓库层的状态机闸一个都不重复）；CLI 把 `handle/dismiss` 从旧的 `set_status` 迁到新动词并补 `ack/defer`。

**Tech Stack:** Python 3.13、DuckDB（单写连接-每线程，D113）、pytest、ruff（E,F,I,UP,B,SIM，行宽 100）。依赖无新增。

**Spec:** `docs/superpowers/specs/2026-10-23-m4b-annotations-review-design.md` §7、§8.1–§8.3、§9（`public_base_url`/`display_tz` 已在计划 1 落地）。

**全局约束（每个任务都适用）：**
- 只用 `uv`：测试 `uv run pytest <文件> -q`，检查 `uv run ruff check --no-cache src tests`。**绝不 `ruff format`、绝不 pip/venv。**
- git 一律在仓库根用 plain bash（`git add …`、`git commit …`），不 `cd`。
- `qodercli_resume.sh` 与 `tmp/` 保持未跟踪，不许 `git add -A`。
- 分层守卫在 `tests/test_layering.py`：`core` 不 import 任何其它层；`monitor` 不 import `services`；`services` 不 import `monitor`/`web`。
- 注释写「为什么」而不是「做了什么」；本计划里给出的注释是最终文本，照抄。

---

## 文件结构（本计划创建/修改的全部文件）

| 文件 | 责任 |
|---|---|
| 修改 `src/sams/core/notify.py` | 推送上下文的词汇：`AnnotationContext`/`PushContext`/`format_annotation_context`/`PUSH_CONTEXT_KINDS` |
| 新建 `tests/core/test_push_context.py` | 选择、排序、去重、单行渲染、字节预算 |
| 修改 `src/sams/storage/repos/alerts.py` | `record()` 收 `context`，写 `notify_log.context_json` |
| 新建 `tests/storage/test_alerts_context.py` | 那一列的读写 |
| 新建 `src/sams/storage/repos/app_settings.py` | `app_settings` 运行时策略读（带 5 秒缓存，§4.2） |
| 新建 `src/sams/services/annotation_context.py` | 发送时取「接收人可见的批注」的 provider（失败降级为 None） |
| 新建 `tests/storage/test_app_settings.py`、`tests/services/test_annotation_context.py` | 上面两个的测试 |
| 修改 `src/sams/monitor/router.py` | `NotifyService` 注入 provider、逐通道追加与预算、逐通道 `context_json` |
| 修改 `tests/monitor/test_router.py` | 重写旧的字符串接缝用例 + 追加上下文用例 |
| 新建 `src/sams/services/review.py` | 闭环用例：`act`/`act_with_fill`（trade 同事务）+ 可选同步批注 |
| 新建 `tests/services/test_review_service.py` | 校验矩阵与回滚 |
| 修改 `src/sams/cli/alert.py` | handle/dismiss 迁到新动词，新增 ack/defer，JSON 补闭环列 |
| 修改 `tests/cli/test_alert_cli.py` | 三枚旧用例按新状态机重写 + 追加五动作用例 |
| 修改 `src/sams/bootstrap.py` | 装配三个新件并把 provider 注入 notify |
| 修改 `tests/test_bootstrap.py` | 钉住装配接缝 |

任务顺序即依赖顺序：T1 → T2 → T3 → T4 → T5 → T6。T4 需要 T1 的类型与 T2 的 `record(context=)`；T6 需要 T5 的 `ReviewService`。

---

## Task 1: core/notify —— 推送上下文的词汇与格式化

**Files:**
- Modify: `src/sams/core/notify.py`（追加在 `AlertMessage` 之后、`NotifyResult` 之前的位置随意，建议放文件末尾 `AlertSender` 之前）
- Test: `tests/core/test_push_context.py`（新建）

- [ ] **Step 1: 写失败的测试**

新建 `tests/core/test_push_context.py`：

```python
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
    assert len(line.split(" ", 2)[2]) == 120
    assert line.endswith("…")


def test_empty_items_render_no_header():
    text, ids, dropped = format_annotation_context(())
    assert text == "" and ids == () and dropped == 0


def test_dedupe_by_id_keeps_the_first_occurrence():
    text, ids, _ = format_annotation_context((item("a"), item("a", body="后写的"), item("b")))
    assert ids == ("a", "b")
    assert "渠道库存见底" in text


def test_pinned_first_then_newest():
    old_pinned = item("pinned-old", pinned=True, when="10-01 09:00")
    fresh = item("fresh", when="10-22 09:00")
    old = item("plain-old", when="10-02 09:00")
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
    items = (item("a", body="第一" + "条" * 20), item("b", body="第二条内容"), item("c", body="第三条内容"))
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/core/test_push_context.py -q`
Expected: FAIL，`ImportError: cannot import name 'CONTEXT_HEADER'`（或同名符号缺失）。

- [ ] **Step 3: 实现**

在 `src/sams/core/notify.py` 末尾（`AlertSender` 之前）追加：

```python
# ---------- 推送上下文（spec §7.2，D126）----------

#: 上下文块的题头那一行：屏幕上与 `notify_log` 里都按这一句逐字断言，所以它是常量。
CONTEXT_HEADER = "—— 最近批注 ——"
#: 一条推送最多带几条批注（§7.1）。固定 3 条的代价写在 spec §15：重要的旧批注要置顶。
CONTEXT_MAX_ITEMS = 3
#: 单行正文的硬上限。`plain_text` 默认也是这个数，这里再挡一次：
#: provider 递来的若是未压过的原文，一行也不许炸开。
CONTEXT_LINE_CHARS = 120
#: 只有文本通道带上下文：语音/桌面的 `LIMITS` 是 140/200 字节，放不下且价值低（D126）。
PUSH_CONTEXT_KINDS = ("wecom", "dingtalk", "pushplus", "email")


@dataclass(frozen=True)
class AnnotationContext:
    """一条要进推送的候选批注：只有「这行怎么读」与「谁先谁后」的那几样。

    `moment_label` 是**已经折好**的 `MM-DD HH:MM`：时区换算读 `server.display_tz`，
    那是 provider 的事——这一层不许 import 任何配置，也不碰 zoneinfo。
    """

    annotation_id: str
    created_at: datetime
    moment_label: str
    author_label: str
    body_text: str
    tags: tuple[str, ...] = ()
    pinned: bool = False


@dataclass(frozen=True)
class PushContext:
    """provider 给 router 的那一包：候选批注加深链。

    `items` 为空与 provider 返回 `None` 是两个事实：前者是「这只票没人写过批注」，
    后者是「这条告警不该有上下文」（系统告警没有标的）。`notify_log.context_json`
    要能分出这两样，审计才回答得了「为什么那条推送里没有批注」。
    """

    items: tuple[AnnotationContext, ...] = ()
    link: str = ""


def _select_annotation_context(
    items: Sequence[AnnotationContext],
) -> tuple[AnnotationContext, ...]:
    """按 id 去重（保留第一次出现），置顶优先、其余按时间倒序，最多 3 条（§7.1）。"""
    seen: set[str] = set()
    unique: list[AnnotationContext] = []
    for entry in items:
        if entry.annotation_id in seen:
            continue
        seen.add(entry.annotation_id)
        unique.append(entry)
    unique.sort(key=lambda entry: (entry.pinned, entry.created_at), reverse=True)
    return tuple(unique[:CONTEXT_MAX_ITEMS])


def annotation_context_line(item: AnnotationContext) -> str:
    """一条批注一行：`· MM-DD HH:MM 作者 [标签] 正文`。"""
    tags = f" [{' '.join(item.tags)}]" if item.tags else ""
    body = " ".join((item.body_text or "").split())
    if len(body) > CONTEXT_LINE_CHARS:
        body = body[: CONTEXT_LINE_CHARS - 1] + "…"
    return f"· {item.moment_label} {item.author_label}{tags} {body}".rstrip()


def format_annotation_context(
    items: Sequence[AnnotationContext],
    *,
    budget_bytes: int | None = None,
) -> tuple[str, tuple[str, ...], int]:
    """渲染上下文块，返回（文本、实际带入的 id、被预算剪掉的条数）。

    `budget_bytes` 是「这个通道的正文还剩多少字节」：超预算从**尾部**剪（尾部是
    最新但非置顶的那几条）；连第一条都放不下就整块不发而不是截半行（§7.2）。
    预算为 `None`（`budget_bytes is None`）表示不剪——Web 详情页那种「与推送同源」
    的展示要的是完整 3 条。
    """
    chosen = _select_annotation_context(items)
    if not chosen:
        return "", (), 0
    lines = [CONTEXT_HEADER] + [annotation_context_line(entry) for entry in chosen]
    while (
        len(lines) > 2
        and budget_bytes is not None
        and len("\n".join(lines).encode()) > budget_bytes
    ):
        lines.pop()
    kept = len(lines) - 1
    if budget_bytes is not None and len("\n".join(lines).encode()) > budget_bytes:
        # 剪到只剩「题头 + 第一条」仍放不下：题头自己也不发了
        return "", (), len(chosen)
    text = "\n".join(lines)
    ids = tuple(entry.annotation_id for entry in chosen[:kept])
    return text, ids, len(chosen) - kept
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/core/test_push_context.py -q`
Expected: 全部 PASS。若行宽超 100，把长行折行（不改语义）。

- [ ] **Step 5: lint + 提交**

```bash
uv run ruff check --no-cache src tests
git add src/sams/core/notify.py tests/core/test_push_context.py
git commit -m "feat(core): 推送上下文的词汇与格式化——AnnotationContext/PushContext/字节预算（§7.2）"
```

---

## Task 2: AlertRepo.record 写 notify_log.context_json

**Files:**
- Modify: `src/sams/storage/repos/alerts.py:343-385`（`record`）
- Test: `tests/storage/test_alerts_context.py`（新建）

- [ ] **Step 1: 写失败的测试**

新建 `tests/storage/test_alerts_context.py`：

```python
"""`notify_log.context_json`：这一次发送**实际**带入了哪些批注（D126）。

列是 0020 加的，可空。这里钉的是仓库层的三件事：给了就写、没给是 NULL、
`ensure_ascii=False` 让审计行在 DuckDB Studio 里读得出来——排查「是不是上下文太长被剪」
的人看的就是这一列。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.users import UserRepo

T0 = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)
PASSWORD = "correct-horse-battery"


@pytest.fixture
def repo(db) -> AlertRepo:
    return AlertRepo(db)


@pytest.fixture
def user_id(db) -> str:
    users = UserRepo(db)
    users.create("trader", PASSWORD)
    return users.get_by_username("trader").id


def _alert(repo: AlertRepo, user_id: str, **over):
    kw: dict = {
        "rule_id": "rule-1",
        "rule_name": "PR 低",
        "account_id": None,
        "account_name": "主账户",
        "market": "A",
        "code": "601088",
        "symbol_name": "中国神华",
        "severity": "mid",
        "stage": "入池",
        "value_text": "PR=0.31",
        "threshold_text": "PR≤0.4",
        "action_note": "",
        "triggered_at": T0,
    }
    kw.update(over)
    return repo.create(user_id, **kw)


def _context_column(db, alert_id: str) -> list[object]:
    rows = db.query(
        "SELECT context_json FROM notify_log WHERE alert_id = ? ORDER BY ts, id", [alert_id]
    )
    return [row["context_json"] for row in rows.to_dicts()]


def test_a_context_lands_as_json_on_the_delivery_row(repo, user_id, db) -> None:
    alert = _alert(repo, user_id)
    channel_id = "chan-1"
    repo.record(
        alert.id,
        channel_id,
        "sent",
        context={"ids": ["ann-1", "ann-2"], "dropped": 0, "rendered_chars": 42},
    )
    raw = _context_column(db, alert.id)[0]
    assert json.loads(raw) == {"ids": ["ann-1", "ann-2"], "dropped": 0, "rendered_chars": 42}


def test_no_context_stays_null_not_an_empty_string(repo, user_id, db) -> None:
    """NULL 与 `''` 必须分得开：前者是「这一行没记上下文这件事」，后者是坏了的行。"""
    alert = _alert(repo, user_id)
    repo.record(alert.id, None, "no_channel", error="没有启用的通道")
    assert _context_column(db, alert.id) == [None]


def test_cjk_in_the_context_is_written_not_escaped(repo, user_id, db) -> None:
    alert = _alert(repo, user_id)
    repo.record(alert.id, "chan-1", "sent", context={"note": "中文原因"})
    raw = _context_column(db, alert.id)[0]
    assert "中文原因" in raw


def test_each_channel_row_carries_its_own_context(repo, user_id, db) -> None:
    """每个通道的投递行各记一份（§7.2）：同一批批注在不同通道可能被剪成不同样子。"""
    alert = _alert(repo, user_id)
    repo.record(
        alert.id, "chan-1", "sent", context={"ids": ["a"], "dropped": 0, "rendered_chars": 5}
    )
    repo.record(alert.id, "chan-2", "sent", context={"ids": [], "dropped": 1, "rendered_chars": 0})
    first, second = _context_column(db, alert.id)
    assert json.loads(first)["ids"] == ["a"]
    assert json.loads(second)["dropped"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_alerts_context.py -q`
Expected: FAIL——`TypeError: record() got an unexpected keyword argument 'context'`。

- [ ] **Step 3: 实现**

把 `src/sams/storage/repos/alerts.py` 的 `record` 整个替换为（docstring 追加的那一段是新增文本，其余保持原样）：

```python
    def record(
        self,
        alert_id: str,
        channel_id: str | None,
        outcome: str,
        *,
        error: str = "",
        attempts: int = 1,
        context: Mapping[str, object] | None = None,
    ) -> None:
        """一次投递尝试的结果。

        `channel_id=None` 只允许配 `suppressed`/`no_channel`。这条约束写在 Python 而不是
        DDL：DuckDB 的 CHECK 报错到屏幕上只剩一个约束名，而这一句要能说清
        「为什么压制没有通道」——压制的意思是「还没有选到通道就停了」。

        `context` 是这一次发送实际带入的推送上下文（D126）：原样存成 JSON，
        仓库层不校验它的形状——形状是 `router` 的装配事实，校验两份就有两个口径。
        `None` 写 NULL：那与「上下文是空列表」是两个答案（见 `PushContext` 的分工）。
        """
        if outcome not in OUTCOMES:
            raise ValidationError(f"未知投递结果 outcome={outcome!r}：只有 {list(OUTCOMES)}")
        if self.get(alert_id) is None:
            raise StorageError(f"告警 {alert_id} 不存在（投递记录必须挂在一条告警上）")
        if outcome == NO_CHANNEL and channel_id is not None:
            raise ValidationError(
                "no_channel 不能带 channel_id：那一句话的意思是「一个通道都没选到」，"
                "配一个 id 就自相矛盾了"
            )
        if outcome in (SENT, FAILED) and not channel_id:
            raise ValidationError(f"{outcome} 必须带 channel_id：一次真发过的事件总得发到哪")
        # `suppressed` 两种形状都合法：免打扰压制发生在选路之前（channel_id 为空），
        # 而「这台机器没有桌面」是关于某一条具体通道的事实（id 要留着才认得出是哪条）
        if attempts < 0:
            raise ValidationError(f"attempts 不能为负（收到 {attempts}）")
        self.db.execute(
            "INSERT INTO notify_log"
            " (id, alert_id, channel_id, ts, attempt, outcome, error, context_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                new_id(),
                alert_id,
                channel_id,
                self.clock.now_utc(),
                int(attempts),
                outcome,
                (error or "").strip()[:_ERROR_MAX_CHARS],
                None if context is None else json.dumps(context, ensure_ascii=False),
            ],
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_alerts_context.py tests/storage/test_alerts_repo.py -q`
Expected: 全部 PASS（旧调用方没传 `context`，默认 None，行为不变）。

- [ ] **Step 5: lint + 提交**

```bash
uv run ruff check --no-cache src tests
git add src/sams/storage/repos/alerts.py tests/storage/test_alerts_context.py
git commit -m "feat(storage): record 收推送上下文并写 notify_log.context_json（D126）"
```

---

## Task 3: AppSettingsRepo + 推送上下文的 provider

**Files:**
- Create: `src/sams/storage/repos/app_settings.py`
- Create: `src/sams/services/annotation_context.py`
- Test: `tests/storage/test_app_settings.py`、`tests/services/test_annotation_context.py`（新建）

- [ ] **Step 1: 写 AppSettingsRepo 的失败测试**

新建 `tests/storage/test_app_settings.py`：

```python
"""`app_settings`：常驻期间改完即生效的那几个策略（D127、§4.2）。

白名单在仓库层——spec 把「未知键读写都拒绝」写在 §9，而「读」的第一个调用方
就是推送 provider：闸放在调用方等于每个调用方自己记得放。
5 秒缓存的坏消息要说清：「改完即生效」在跨进程时最长滞后 5 秒，那是缓存的代价，
写路径会立刻废掉本进程的缓存条目。
"""

from __future__ import annotations

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.app_settings import TEAM_SHARING, AppSettingsRepo


def test_the_migration_row_says_team_sharing_is_on(db):
    repo = AppSettingsRepo(db)
    assert repo.get(TEAM_SHARING, default="on") == "on"
    assert repo.team_sharing() is True


def test_an_unknown_key_is_refused_on_both_sides(db):
    repo = AppSettingsRepo(db)
    with pytest.raises(ValidationError, match="白名单"):
        repo.get("alerts.everything", default="")
    with pytest.raises(ValidationError, match="白名单"):
        repo.set("alerts.everything", "on")


def test_a_bad_value_for_the_known_key_is_refused(db):
    repo = AppSettingsRepo(db)
    with pytest.raises(ValidationError, match="on/off"):
        repo.set(TEAM_SHARING, "maybe")


def test_set_flips_the_value_and_records_the_operator(db):
    repo = AppSettingsRepo(db)
    repo.set(TEAM_SHARING, "off", updated_by="admin-1")
    assert repo.team_sharing() is False
    row = db.query("SELECT updated_by FROM app_settings WHERE key = ?", [TEAM_SHARING]).to_dicts()
    assert row[0]["updated_by"] == "admin-1"


def test_a_write_from_another_repo_instance_is_seen_after_the_cache_expires(db, monkeypatch):
    """缓存必须存在（否则每次批注读取都问一遍库）也必须过期（否则「改完即生效」是谎）。

    把 `time.monotonic` 拨快 6 秒而不是真等：钉的是「按单调钟过期」这个实现选择。
    """
    first = AppSettingsRepo(db)
    assert first.team_sharing() is True
    AppSettingsRepo(db).set(TEAM_SHARING, "off")
    assert first.team_sharing() is True  # 5 秒内的旧答案
    real_monotonic = __import__("time").monotonic
    monkeypatch.setattr(
        "sams.storage.repos.app_settings.time.monotonic", lambda: real_monotonic() + 6.0
    )
    assert first.team_sharing() is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_app_settings.py -q`
Expected: FAIL——模块不存在（`ModuleNotFoundError`）。

- [ ] **Step 3: 实现 AppSettingsRepo**

新建 `src/sams/storage/repos/app_settings.py`：

```python
"""`app_settings` 的读写：运行时策略，改完即生效，不重启（D127、§4.2）。

白名单只有 `annotations.team_sharing`（spec §9）：一个开放键值的表会让「随便塞个键」
变成默认习惯，半年后没人记得哪个键还有人读。未知键读写都拒在这里而不是调用方：
第一个不记得校验的调用方不会报错，只会把一个没人读的键当成策略。

读带 5 秒进程内缓存：每次批注读取与每一次推送都要问一遍「共享开着吗」，
而开关是一个礼拜按一次的东西。跨进程的可见延迟最多 5 秒——写路径废掉的是
**本进程**的条目，别的进程靠过期。
"""

from __future__ import annotations

import time

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database

#: 唯一的策略键（§9）。名字住在这里：SQL 里的那个字符串与 Python 里的引用必须是同一份。
TEAM_SHARING = "annotations.team_sharing"
ALLOWED_KEYS = (TEAM_SHARING,)
TEAM_SHARING_VALUES = ("on", "off")
CACHE_S = 5.0


class AppSettingsRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock
        self._cache: dict[str, tuple[float, str]] = {}

    def get(self, key: str, *, default: str) -> str:
        if key not in ALLOWED_KEYS:
            raise ValidationError(f"未知策略键 {key!r}：白名单只有 {list(ALLOWED_KEYS)}")
        moment = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and moment - hit[0] < CACHE_S:
            return hit[1]
        row = self.db.scalar("SELECT value FROM app_settings WHERE key = ?", [key])
        # 行由 0020 的 `INSERT OR IGNORE` 铺：读不到只可能是被删过——按默认值回答，不抛
        value = str(row) if row is not None else default
        self._cache[key] = (moment, value)
        return value

    def set(self, key: str, value: str, *, updated_by: str | None = None) -> str:
        """写一行并废掉本进程的缓存条目。审计（谁改的）落在同一行的 `updated_by`。"""
        if key not in ALLOWED_KEYS:
            raise ValidationError(f"未知策略键 {key!r}：白名单只有 {list(ALLOWED_KEYS)}")
        if key == TEAM_SHARING and value not in TEAM_SHARING_VALUES:
            raise ValidationError(f"{key} 只收 {'/'.join(TEAM_SHARING_VALUES)}（收到 {value!r}）")
        self._cache.pop(key, None)
        self.db.execute(
            "INSERT INTO app_settings (key, value, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            [key, value, self.clock.now_utc(), updated_by],
        )
        return value

    def team_sharing(self) -> bool:
        """团队共享开关：除 `'off'` 外都算开。默认行是迁移铺的，缺行不是错误。"""
        return self.get(TEAM_SHARING, default="on") != "off"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_app_settings.py -q`
Expected: PASS。

- [ ] **Step 5: 写 provider 的失败测试**

新建 `tests/services/test_annotation_context.py`：

```python
"""推送上下文的 provider：发送那一刻取「接收人可见」的批注（spec §7.1）。

隐私边界按接收人算：别人的私有批注一个字都不许进推送；团队共享关闭时，
既有的 team 批注对非作者隐藏（D127）——这些不是「尽量」，是这里逐条断言的东西。

还钉一件事：**坏掉的查询不许挡告警**（D126）。provider 吞掉一切异常返回 None，
让 router 按无上下文发送；「批注表被锁了」的代价是推送少了三行字，
而不是手机上收不到一条止损。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sams.core.config import ServerCfg
from sams.services.annotation_context import AnnotationContextProvider
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.app_settings import TEAM_SHARING, AppSettingsRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

PASSWORD = "correct-horse-battery"
T0 = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)
CREATED = datetime(2026, 10, 22, 6, 3, tzinfo=UTC)  # 折到 Asia/Shanghai 是 14:03


class World:
    def __init__(self, db, settings) -> None:
        self.db = db
        self.users = UserRepo(db)
        self.me = self.users.create("me", PASSWORD)
        self.other = self.users.create("other", PASSWORD)
        self.annotations = AnnotationRepo(db)
        self.theses = ThesisRepo(db)
        self.settings_store = AppSettingsRepo(db)
        self.alerts = AlertRepo(db)
        self.settings = settings
        self.provider = AnnotationContextProvider(
            self.annotations, self.users, self.theses, self.settings_store, settings
        )

    def uid(self, name: str) -> str:
        return self.users.get_by_username(name).id

    def annotate(self, author: str, *, ref="A:601088", target="symbol", **over):
        created_at = over.pop("created_at", None)
        kw: dict = {
            "user_id": self.uid(author),
            "target_type": target,
            "target_ref": ref,
            "target_label": "中国神华",
            "body_md": "渠道库存见底，Q4 提价",
        }
        kw.update(over)
        made = self.annotations.create(**kw)
        if created_at is not None:
            # 仓库层的 create 按自己的钟写 created_at，排序用例要的是指定的那一刻
            self.db.execute(
                "UPDATE annotations SET created_at = ? WHERE id = ?",
                [created_at, made.id],
            )
        return made

    def alert(self, *, user: str = "me", **over):
        kw: dict = {
            "rule_id": "rule-1",
            "rule_name": "PR 低",
            "account_id": None,
            "account_name": "主账户",
            "market": "A",
            "code": "601088",
            "symbol_name": "中国神华",
            "severity": "mid",
            "stage": "入池",
            "value_text": "PR=0.31",
            "threshold_text": "PR≤0.4",
            "action_note": "",
            "triggered_at": T0,
        }
        kw.update(over)
        return self.alerts.create(self.uid(user), **kw)


@pytest.fixture
def world(db, settings) -> World:
    return World(db, settings)


def test_own_private_annotation_shows_up_with_me_as_author(world):
    made = world.provider(world.alert())
    assert made is not None
    assert [item.author_label for item in made.items] == ["我"]
    assert made.items[0].body_text == "渠道库存见底，Q4 提价"


def test_the_moment_label_is_folded_into_display_tz(world):
    world.annotate("me", created_at=CREATED)
    made = world.provider(world.alert())
    assert made.items[0].moment_label == "10-22 14:03"
    # 排序键也在场：UTC 那一刻原样带回来，跨源合并靠它，不是只留一个给屏幕看的串
    assert made.items[0].created_at.astimezone(UTC) == CREATED


def test_team_annotation_by_another_user_shows_with_their_name(world):
    world.annotate("other", visibility="team")
    made = world.provider(world.alert())
    assert [item.author_label for item in made.items] == ["other"]


def test_display_name_wins_over_username(world):
    world.users.set_display_name(world.uid("other"), "张三")
    world.annotate("other", visibility="team")
    made = world.provider(world.alert())
    assert made.items[0].author_label == "张三"


def test_another_users_private_annotation_never_shows_up(world):
    world.annotate("other")  # 默认 private
    made = world.provider(world.alert())
    assert made is not None and made.items == ()


def test_turning_off_team_sharing_hides_team_from_others(world):
    world.annotate("other", visibility="team")
    world.settings_store.set(TEAM_SHARING, "off")
    fresh = AnnotationContextProvider(
        world.annotations, world.users, world.theses, world.settings_store, world.settings
    )
    made = fresh(world.alert())
    assert made is not None and made.items == ()


def test_the_author_still_sees_their_own_team_annotation_when_sharing_is_off(world):
    world.annotate("me", visibility="team")
    world.settings_store.set(TEAM_SHARING, "off")
    fresh = AnnotationContextProvider(
        world.annotations, world.users, world.theses, world.settings_store, world.settings
    )
    made = fresh(world.alert())
    assert [item.author_label for item in made.items] == ["我"]


def test_a_system_alert_has_no_context_at_all(world):
    assert world.provider(world.alert(market="SYS", code="scheduler")) is None


def test_an_alert_without_a_code_has_no_context(world):
    assert world.provider(world.alert(code="")) is None


def test_thesis_annotations_join_when_the_alert_names_a_readable_thesis(world):
    thesis_id = world.theses.create(
        user_id=world.uid("me"), market="A", code="601088",
        style="PR", actor_id=world.uid("me"), now=T0,
    ).id
    world.annotate("me", target="thesis", ref=thesis_id, body_md="等半年线企稳")
    made = world.provider(world.alert(thesis_id=thesis_id))
    bodies = {item.body_text for item in made.items}
    assert "等半年线企稳" in bodies and "渠道库存见底，Q4 提价" in bodies


def test_a_foreign_or_vanished_thesis_falls_back_to_symbol_only(world):
    thesis_id = world.theses.create(
        user_id=world.uid("other"), market="A", code="601088",
        style="PR", actor_id=world.uid("other"), now=T0,
    ).id
    world.annotate("other", target="thesis", ref=thesis_id, body_md="别人的想法")
    world.annotate("me")
    made = world.provider(world.alert(thesis_id=thesis_id))
    assert [item.body_text for item in made.items] == ["渠道库存见底，Q4 提价"]


def test_markdown_gets_flattened_into_one_line(world):
    world.annotate("me", body_md="**渠道库存** 见底\n\n- Q4 提价\n- 已核实")
    made = world.provider(world.alert())
    assert made.items[0].body_text == "渠道库存 见底 Q4 提价 已核实"


def test_the_deep_link_lands_on_the_push_context(world, settings):
    over = replace(settings.server, public_base_url="http://lan:8765")
    with_url = replace(settings, server=over)
    fresh = AnnotationContextProvider(
        world.annotations, world.users, world.theses, world.settings_store, with_url
    )
    alert = world.alert()
    made = fresh(alert)
    assert made.link == f"http://lan:8765/alerts/{alert.id}"


def test_no_base_url_means_no_link(world):
    assert world.provider(world.alert()).link == ""


def test_a_broken_query_degrades_to_none_without_raising(world, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("库坏了")

    monkeypatch.setattr(world.annotations, "list_visible", boom)
    assert world.provider(world.alert()) is None


def test_pinned_annotation_is_first_among_candidates(world):
    older = world.annotate("me", body_md="后来的")
    world.annotations.set_pinned(older.id, user_id=world.uid("me"), pinned=True)
    made = world.provider(world.alert())
    assert made.items[0].pinned is True
```

- [ ] **Step 6: 跑测试确认失败**

Run: `uv run pytest tests/services/test_annotation_context.py -q`
Expected: FAIL——`ModuleNotFoundError: No module named 'sams.services.annotation_context'`。

- [ ] **Step 7: 实现 provider**

新建 `src/sams/services/annotation_context.py`：

```python
"""推送上下文的 provider：在**发送那一刻**取「接收人可见」的批注（spec §7）。

monitor 不认识 services：它拿到的是装配根注入的一个可调用对象（§12.1 的分层红线）。
取的是接收人（alert.user_id）可见的两路合并：该标的的 symbol 批注（本人私有 +
策略开启时的 team）与告警 `thesis_id` 的 thesis 批注（§7.1、D137）——排序、去重、
取前三住在 `core.notify` 的格式化里，这里只管「把候选摆全」。

一切异常吞成 `None`（D126）：批注查询坏了的代价是推送少三行字，不是手机上
收不到一条止损。系统告警（`SYS`）与没有代码的行返回 `None`——那是
「这条告警不该有上下文」，与「这只票没人写过批注」（`PushContext()`）分得开。
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from sams.core.config import Settings
from sams.core.notify import SYSTEM_MARKET, AnnotationContext, PushContext
from sams.services.markdown import plain_text
from sams.storage.repos.alerts import Alert
from sams.storage.repos.annotations import SYMBOL, THESIS, AnnotationRepo
from sams.storage.repos.app_settings import AppSettingsRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

logger = logging.getLogger("sams.services.annotation_context")

#: 每一路取几条候选：最终的截断在格式化那一层（3 条），这里保证
#: 「每一路的置顶 + 最新」都进得了候选池
CANDIDATES_PER_SOURCE = 3


class AnnotationContextProvider:
    def __init__(
        self,
        annotations: AnnotationRepo,
        users: UserRepo,
        theses: ThesisRepo,
        settings_store: AppSettingsRepo,
        settings: Settings,
    ) -> None:
        self.annotations = annotations
        self.users = users
        self.theses = theses
        self.settings_store = settings_store
        self.settings = settings

    def __call__(self, alert: Alert) -> PushContext | None:
        try:
            return self._build(alert)
        except Exception as exc:  # noqa: BLE001 — 降级是这个函数的契约，异常类型不在名单里也一样
            logger.warning("告警 %s 取批注上下文失败，按无上下文发送：%s", alert.id, exc)
            return None

    def _build(self, alert: Alert) -> PushContext | None:
        if alert.market == SYSTEM_MARKET or not alert.code:
            return None
        team = self.settings_store.team_sharing()
        candidates = list(
            self.annotations.list_visible(
                alert.user_id,
                team_sharing=team,
                target_type=SYMBOL,
                target_ref=f"{alert.market}:{alert.code}",
                limit=CANDIDATES_PER_SOURCE,
            )
        )
        # thesis 只有「接收人读得到这条 thesis」才展开：别人的 thesis 连存在都不说（D121）
        readable_thesis = (
            alert.thesis_id is not None
            and self.theses.get(alert.user_id, alert.thesis_id) is not None
        )
        if readable_thesis:
            candidates += list(
                self.annotations.list_visible(
                    alert.user_id,
                    team_sharing=team,
                    target_type=THESIS,
                    target_ref=alert.thesis_id,
                    limit=CANDIDATES_PER_SOURCE,
                )
            )
        base = self.settings.server.public_base_url
        return PushContext(
            items=tuple(self._to_context(alert.user_id, made) for made in candidates),
            link=f"{base}/alerts/{alert.id}" if base else "",
        )

    def _to_context(self, viewer_id: str, made) -> AnnotationContext:
        if made.user_id == viewer_id:
            label = "我"
        else:
            author = self.users.get(made.user_id)
            label = "未知作者" if author is None else (author.display_name or author.username)
        zone = ZoneInfo(self.settings.server.display_tz)
        return AnnotationContext(
            annotation_id=made.id,
            created_at=made.created_at,
            moment_label=made.created_at.astimezone(zone).strftime("%m-%d %H:%M"),
            author_label=label,
            body_text=plain_text(made.body_md),
            tags=tuple(made.tags),
            pinned=made.pinned,
        )
```

- [ ] **Step 8: 跑测试确认通过**

Run: `uv run pytest tests/services/test_annotation_context.py tests/test_layering.py -q`
Expected: 全部 PASS（provider 住在 services，monitor 不 import 它——分层守卫不动）。

- [ ] **Step 9: lint + 提交**

```bash
uv run ruff check --no-cache src tests
git add src/sams/storage/repos/app_settings.py src/sams/services/annotation_context.py \
  tests/storage/test_app_settings.py tests/services/test_annotation_context.py
git commit -m "feat(services): 推送上下文 provider 与 app_settings 读取（§7.1、D126/D127）"
```

---

## Task 4: NotifyService 逐通道装配与 context_json 审计

**Files:**
- Modify: `src/sams/monitor/router.py:109-205`（`__init__`、`deliver_existing`）、`router.py:408-424`（`_deliver`）+ 新私有方法 `_attach_context`
- Test: `tests/monitor/test_router.py`（重写一枚旧用例 + 追加一组）

- [ ] **Step 1: 写失败的测试**

先重写旧接缝（`tests/monitor/test_router.py:501-508` 的 `test_a_context_provider_extends_the_body` **整个替换**为）：

```python
def test_a_context_provider_appends_the_block(world):
    """接缝从「返回一个字符串」改成「返回 PushContext」：题头、行、深链逐字钉住。

    上下文不进 `alerts.payload_json`（§7.1）：告警创建时还不知道发送时刻的批注，
    这里断言的是**发出去那一刻**的消息体。
    """
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    alert = world.notify.record(msg(), user_id=user.id)
    world.notify.deliver_existing(
        alert.id,
        context_provider=lambda a: PushContext(items=(_ctx(a.id),), link=f"http://lan/{a.id}"),
    )
    sent = world.made["wecom"].sent[-1]
    assert "—— 最近批注 ——" in sent.body
    assert "· 10-22 14:03 我 [逻辑] 渠道库存见底" in sent.body
    assert sent.body.endswith(f"详情：http://lan/{alert.id}")
```

并在文件 import 区补两处：文件顶部加 `import json`（`_context_rows` 要解析 `context_json`），
既有的 `from sams.core.notify import (...)` 括号里按字母序加 `AnnotationContext`、`PushContext`：

```python
from sams.core.notify import (
    FAILED,
    NO_CHANNEL,
    SENT,
    SUPPRESSED,
    AlertMessage,
    AnnotationContext,
    NotifyResult,
    PushContext,
)
```

然后在文件末尾追加（`test_a_vanished_recipient_leaves_a_row_before_raising` 的 `build_context` 返回类型注成 `-> PushContext | None` 并把 body 改成 `return None`——它断言 `built == []`，语义不变）：

```python
# ---------- 推送上下文（计划 3，§7.2）----------


def _ctx(alert_id: str, n: str = "ann-1", body: str = "渠道库存见底") -> AnnotationContext:
    return AnnotationContext(
        annotation_id=n,
        created_at=datetime(2026, 10, 22, 6, 3, tzinfo=UTC),
        moment_label="10-22 14:03",
        author_label="我",
        body_text=body,
        tags=("逻辑",),
    )


def _context_rows(world, db, alert_id: str) -> dict[str, object]:
    """`kind -> notify_log.context_json`（解析过的），给下面几枚用例共用。"""
    rows = db.query(
        "SELECT c.kind AS kind, n.context_json AS context_json"
        " FROM notify_log n JOIN channels c ON c.id = n.channel_id"
        " WHERE n.alert_id = ?",
        [alert_id],
    ).to_dicts()
    return {
        str(row["kind"]): (None if row["context_json"] is None else json.loads(row["context_json"]))
        for row in rows
    }


def test_the_instance_provider_is_used_without_call_arguments(world):
    """队列与 Web 重发只传 alert_id：实例那一格是唯一接得住 provider 的地方。"""
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    alert = world.notify.record(msg(), user_id=user.id)
    world.notify.context_provider = lambda a: PushContext(items=(_ctx(a.id),))
    world.notify.deliver_existing(alert.id)
    assert "—— 最近批注 ——" in world.made["wecom"].sent[-1].body


def test_each_text_channel_row_carries_its_own_context_json(world, db):
    """`context_json` 逐通道各记一份（§7.2）：三个通道剪成三个样子，审计要分得开。"""
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    world.add(user.id, "dingtalk", "钉 B")
    alert = world.notify.record(msg(), user_id=user.id)
    world.notify.deliver_existing(
        alert.id, context_provider=lambda a: PushContext(items=(_ctx(a.id),))
    )
    logged = _context_rows(world, db, alert.id)
    expected = "—— 最近批注 ——\n· 10-22 14:03 我 [逻辑] 渠道库存见底"
    assert logged["wecom"] == {"ids": ["ann-1"], "dropped": 0, "rendered_chars": len(expected)}
    assert logged["dingtalk"]["ids"] == ["ann-1"]


def test_voice_and_desktop_never_carry_the_block(world, db):
    """语音/桌面不追加（D126）：LIMITS 140/200 字节放不下，而它们的价值在「出声」。"""
    user = world.user()
    voice = world.add(user.id, "voice", "本机声音")
    desktop = world.add(user.id, "desktop", "桌面通知")
    alert = world.notify.record(
        msg(severity="strong"), user_id=user.id, channel_ids=[voice.id, desktop.id]
    )
    world.notify.deliver_existing(
        alert.id, context_provider=lambda a: PushContext(items=(_ctx(a.id),))
    )
    for kind in ("voice", "desktop"):
        assert "最近批注" not in world.made[kind].sent[-1].body
    logged = _context_rows(world, db, alert.id)
    assert logged["voice"] is None and logged["desktop"] is None


def test_a_full_body_drops_the_whole_block_and_says_so(world, db):
    """正文本身已经顶到通道上限：连一条都放不下 ⇒ 整块不发，而不是截半行（§7.2）。

    context_json 仍要落：`dropped` 记的就是「有几条被预算剪掉」——排查
    「是不是上下文太长被剪」的人看的是这一列。
    """
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    alert = world.notify.record(msg(body="字" * 1400), user_id=user.id)
    original_body = alert.payload["extra"]["body"]
    world.notify.deliver_existing(
        alert.id,
        context_provider=lambda a: PushContext(
            items=(_ctx(a.id), _ctx(a.id, n="ann-2"))
        ),
    )
    sent = world.made["wecom"].sent[-1]
    assert sent.body == original_body
    logged = _context_rows(world, db, alert.id)
    assert logged["wecom"] == {"ids": [], "dropped": 2, "rendered_chars": 0}


def test_the_link_survives_when_the_annotations_do_not_fit(world, db):
    """预算只够题头加一条：剪批注，深链若还放得下就留着——它不占批注的名额。"""
    user = world.user()
    world.add(user.id, "dingtalk", "钉 B")
    alert = world.notify.record(msg(), user_id=user.id)
    long_a = _ctx(alert.id, n="a1", body="甲" * 100)
    long_b = _ctx(alert.id, n="b2", body="乙" * 100)
    world.notify.deliver_existing(
        alert.id,
        context_provider=lambda a: PushContext(items=(long_a, long_b), link="http://lan/x"),
    )
    logged = _context_rows(world, db, alert.id)
    # 两条各 100 字加题头仍在钉钉的 18000 字节以内：这一枚钉的是「剪剩的 id 都要落列」，
    # dropped 那一路由 `test_a_full_body_drops_the_whole_block_and_says_so` 负责
    assert logged["dingtalk"]["dropped"] == 0
    assert logged["dingtalk"]["ids"] == ["a1", "b2"]
    assert world.made["dingtalk"].sent[-1].body.endswith("详情：http://lan/x")
```

文件顶部 import 需补 `json`（`from __future__ import annotations` 之后）。

注意 `LIMITS` 里 dingtalk 是 18000 字节，最后那枚用例**不会**真的触发预算——它的真实作用是钉「批注与深链在同一块里、ids 与实际带入一致」。保持断言弱（如上面的 `or`）即可，别为它调预算常数。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/monitor/test_router.py -q`
Expected: 新用例 FAIL（`_deliver` 不接受 push / `context_provider` 构造参数不存在 / 旧字符串拼接把 `PushContext` 拼进 body 报 TypeError）。

- [ ] **Step 3: 实现 router**

`src/sams/monitor/router.py` 改动四处。

（a）import：`sams.core.notify` 的括号里加 `LIMITS`、`PUSH_CONTEXT_KINDS`、`PushContext`、`format_annotation_context`、`format_headline`。

（b）`__init__` 加参数与属性（放在 `env` 之后）：

```python
        context_provider: Callable[[Alert], PushContext | None] | None = None,
```

```python
        self.env = env
        #: 推送上下文的 provider（§7.2）。装配顺序决定它多半是**事后赋值**接进来的
        #: （`bootstrap` 先造 notify、后造批注服务），所以它是一格公开属性而不是
        #: 只在构造时读一次的闭包：队列与 Web 重发都只调 `deliver_existing(alert_id)`，
        #: 实例这一格是唯一接得住它的地方
        self.context_provider = context_provider
```

类 docstring 里那段「参数表里没有 cipher…也没有 clock」之后追加一句：

```python
    `context_provider` 是唯一一个「可能为 None 的取数入口」：取批注这件事坏了不能挡
    告警，降级住在 provider 自己那一头（`services/annotation_context`），这里只负责
    在发送时问一次、按通道预算装配。
```

（c）`deliver_existing` 整段替换（docstring 更新为新契约）：

```python
    def deliver_existing(
        self,
        alert_id: str,
        *,
        context_provider: Callable[[Alert], PushContext | None] | None = None,
    ) -> DispatchResult:
        """把库里已有的一条告警走完路由与投递。正文从库里重建，不信任内存里的对象。

        上下文在**发送时**取（§7.3）：排队两分钟后再发，带的是当时的批注而不是
        入队时的。显式传入的 provider 赢过实例那一格；两边都没有就是不带上下文。
        """
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise StorageError(f"告警 {alert_id} 不存在：发送必须落在一条真实告警上")
        # 收件人先查：人已不在就不该再为这条告警拼正文、跑一次可能很贵的上下文构建
        user = self.users.get(alert.user_id)
        if user is None:
            reason = f"没有用户 {alert.user_id}：告警没有接收人就不能发"
            # 这一条拒绝也要先落一行再抛（模块开头那条不变式对每条拒绝成立）；
            # raise 保住是给队列用的：它要能区分「没有收件人」，
            # 并且会对自己那一侧的失败另调 `abandon`
            self.alerts.record(alert.id, None, NO_CHANNEL, error=reason, attempts=0)
            raise ValidationError(reason)
        pick = context_provider if context_provider is not None else self.context_provider
        push = pick(alert) if pick is not None else None
        message = _message_from(alert)
        dnd = self._dnd_note(user, message)
        if dnd:
            self.alerts.record(alert.id, None, SUPPRESSED, error=dnd, attempts=0)
            return DispatchResult(alert.id, (DeliveryReport("", None, SUPPRESSED, dnd, 0),))
        targets, refusal = self._route(user, message.severity, list(alert.channel_ids))
        if not targets:
            self.alerts.record(alert.id, None, NO_CHANNEL, error=refusal, attempts=0)
            return DispatchResult(alert.id, (DeliveryReport("", None, NO_CHANNEL, refusal, 0),))
        return DispatchResult(
            alert.id, tuple(self._deliver(alert.id, message, node, push) for node in targets)
        )
```

（d）`_deliver` 改签名并新增 `_attach_context`（放在 `_deliver` 之前）：

```python
    def _attach_context(
        self, message: AlertMessage, kind: str, push: PushContext | None
    ) -> tuple[AlertMessage, dict | None]:
        """把批注块与深链拼到正文末尾，返回（这条通道该发的消息、context_json 的字典）。

        预算与通道实现同一个口径：企微/钉钉/PushPlus 发的是「标题行 + 正文」，
        邮件发的是正文（标题走 subject），所以基准文字按 kind 折出来再问剩多少字节。
        剪不掉就整块不发而不是截半行——那条判据住在 `format_annotation_context`。
        `push is None` 与「批注为空但要有深链」必须可分：前者不落 `context_json`。
        """
        if push is None or kind not in PUSH_CONTEXT_KINDS:
            return message, None
        headline = format_headline(message)
        if kind == "email":
            base = message.body
        else:
            base = f"{headline}\n\n{message.body}" if message.body else headline
        room = LIMITS[kind] - len(base.encode())
        text, ids, dropped = format_annotation_context(push.items, budget_bytes=room)
        if text:
            room -= len(text.encode())
        parts = [part for part in (message.body, text) if part]
        link = f"详情：{push.link}" if push.link else ""
        if link and len(f"\n{link}".encode()) <= max(room, 0):
            parts.append(link)
        body = "\n".join(parts)
        if body != message.body:
            message = replace(message, body=body)
        return message, {"ids": list(ids), "dropped": dropped, "rendered_chars": len(text)}

    def _deliver(
        self, alert_id: str, message: AlertMessage, node: Channel, push: PushContext | None
    ) -> DeliveryReport:
        sent, context = self._attach_context(message, node.kind, push)
        report = self._attempt(node, sent)
        self.alerts.record(
            alert_id,
            node.id,
            report.outcome,
            error=report.error,
            attempts=report.attempts,
            context=context,
        )
        if report.outcome == FAILED:
            # §12 的「连续失败降 SystemAlarm」那个计数属于调度器（M3-D），不属于这里：
            # 一个 CLI 命令一个进程，进程里没有「连续」可言
            self.bus.publish(
                NotifyFailed(
                    alert_id=alert_id,
                    channel_id=node.id,
                    attempt=report.attempts,
                    error=report.error,
                )
            )
        return report
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/monitor/test_router.py tests/monitor/test_queue.py tests/web/test_alerts_web.py -q`
Expected: 全部 PASS（queue 与 web 的假 `deliver_existing` 都有 `context_provider=None` 关键字，签名兼容；真 NotifyService 的实例 provider 默认 None，行为不变）。

- [ ] **Step 5: lint + 提交**

```bash
uv run ruff check --no-cache src tests
git add src/sams/monitor/router.py tests/monitor/test_router.py
git commit -m "feat(monitor): 推送上下文逐通道装配与 notify_log 审计（§7.2/§7.3）"
```

---

## Task 5: ReviewService —— 闭环用例与 trade 的同事务成交

**Files:**
- Create: `src/sams/services/review.py`
- Test: `tests/services/test_review_service.py`（新建）

- [ ] **Step 1: 写失败的测试**

新建 `tests/services/test_review_service.py`：

```python
"""闭环用例层（spec §8.2/§8.3）：仓库闸之外的三样——账户归属、引用存在、标的相符。

状态机本身（终态不可回退、note 长度、引用必填、`alert_events` 逐动作一行）
住在 `AlertRepo`，这里一枚都不重复断言；这一文件只钉仓库看不见的那半：
`thesis` 的引用必须存在且同标的、`watch` 要与闭环同事务、`trade` 的成交与闭环
在同一个事务里要么都成要么都不留。

回滚那两枚是 §11「成交闭环失败：整体回滚，告警保持原状态」的机器版本——
它们防的不是异常，是「库里多了一笔没发生的成交」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from sams.core.errors import Conflict, StorageError, ValidationError
from sams.services.annotations import AnnotationService
from sams.services.ledger import LedgerService
from sams.services.review import ReviewService
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

USER = "me"
OTHER = "other"
PASSWORD = "correct-horse-battery"
NOW = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)
#: 规则的 params_on 是日粒度，成交时刻用整分的 UTC
RULE_DAY = NOW.date()


@dataclass
class World:
    review: ReviewService
    users: UserRepo
    alerts: AlertRepo
    ledger: LedgerService
    accounts: SimAccountRepo
    orders: SimOrderRepo
    theses: ThesisRepo
    annotations: AnnotationService
    watchlists: WatchlistRepo

    def uid(self, name: str = USER) -> str:
        return self.users.get_by_username(name).id

    def alert(self, *, user: str = USER, **over):
        kw: dict = {
            "rule_id": "rule-1",
            "rule_name": "跌破止损",
            "account_id": None,
            "account_name": "模拟盘",
            "market": "A",
            "code": "601088",
            "symbol_name": "中国神华",
            "severity": "strong",
            "stage": "破位",
            "value_text": "34.20 CNY",
            "threshold_text": "34.00 CNY",
            "action_note": "按纪律减仓",
            "triggered_at": NOW,
        }
        kw.update(over)
        return self.alerts.create(self.uid(user), **kw)

    def funded_account(self, name: str = "主账户") -> str:
        account = self.ledger.open_account(USER, name)
        self.ledger.deposit(
            account.id, "CNY", 100_000.0, username=USER, reason="初始", actor_id=self.uid()
        )
        return account.id

    def thesis(self, code: str = "601088", *, user: str = USER) -> str:
        repo = self.theses
        made = repo.create(
            user_id=self.uid(user), market="A", code=code, style="PR",
            actor_id=self.uid(user), now=NOW,
        )
        repo.advance(self.uid(user), made.id, "proposed", actor_id=self.uid(user), now=NOW)
        repo.set_levels(
            self.uid(user), made.id, actor_id=self.uid(user), now=NOW,
            entry_low=38.0, entry_high=40.0, stop_price=34.0, target_price=50.0, reward_risk=1.8,
        )
        repo.advance(self.uid(user), made.id, "opened", actor_id=self.uid(user), now=NOW)
        return made.id

    def rule(self, target_ref: str = "A/601088", *, user: str = USER) -> str:
        return RuleRepo(self.theses.db).create(
            self.uid(user), kind="stop.fixed", name="止损", target_type="symbol",
            target_ref=target_ref, params={"price": 34.0}, params_on=RULE_DAY,
        ).id


@pytest.fixture
def world(db, settings) -> World:
    users = UserRepo(db)
    users.create(USER, PASSWORD)
    users.create(OTHER, PASSWORD)
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    theses = ThesisRepo(db)
    alerts = AlertRepo(db)
    annotations = AnnotationService(
        AnnotationRepo(db), users, SymbolRepo(db), theses, RuleRepo(db),
        alerts, accounts, orders, settings,
    )
    review = ReviewService(
        db,
        alerts=alerts,
        ledger=LedgerService(accounts, orders, theses, users, settings),
        users=users,
        theses=theses,
        rules=RuleRepo(db),
        watchlists=WatchlistRepo(db),
        annotations=annotations,
    )
    return World(
        review=review, users=users, alerts=alerts, ledger=review.ledger,
        accounts=accounts, orders=orders, theses=theses,
        annotations=annotations, watchlists=review.watchlists,
    )


def _orders_count(db) -> int:
    return int(db.scalar("SELECT count(*) FROM sim_orders") or 0)


# ---------- other / trade 的入口分工 ----------


def test_other_without_a_note_is_refused(world):
    alert = world.alert()
    with pytest.raises(ValidationError, match="必须写一句说明"):
        world.review.act(world.uid(), alert.id, action_kind="other")
    assert world.alerts.get(alert.id).status == "new"


def test_other_with_a_note_closes_it(world):
    alert = world.alert()
    moved = world.review.act(world.uid(), alert.id, action_kind="other", note="已挂条件单")
    assert moved.status == "handled" and moved.action_kind == "other"
    assert moved.handle_note == "已挂条件单"


def test_plain_act_refuses_trade(world):
    """trade 只能走 `act_with_fill`（D125）：这一格不接「口头说成交」。"""
    alert = world.alert()
    with pytest.raises(ValidationError, match="act_with_fill"):
        world.review.act(world.uid(), alert.id, action_kind="trade", action_ref="order-1")


# ---------- thesis / rule 的引用 ----------


def test_thesis_ref_must_be_mine_and_same_symbol(world):
    alert = world.alert()
    other_thesis = world.thesis(user=OTHER)
    with pytest.raises(ValidationError, match="不存在或不属于"):
        world.review.act(
            world.uid(), alert.id, action_kind="thesis", action_ref=other_thesis, note="推进"
        )
    wrong_symbol = world.thesis(code="000001")
    with pytest.raises(ValidationError, match="不符"):
        world.review.act(
            world.uid(), alert.id, action_kind="thesis", action_ref=wrong_symbol, note="推进"
        )


def test_thesis_ref_closes_the_alert(world):
    alert = world.alert()
    made = world.thesis()
    moved = world.review.act(
        world.uid(), alert.id, action_kind="thesis", action_ref=made, note="推进到止盈"
    )
    assert moved.status == "handled" and moved.action_ref == made


def test_rule_ref_must_be_mine_but_may_guard_another_symbol(world):
    """规则可以跨标的（watchlist 展开）：§8.2 只要求「本人的」，不要求同标的。"""
    alert = world.alert()
    mine = world.rule(target_ref="A/000001")
    moved = world.review.act(
        world.uid(), alert.id, action_kind="rule", action_ref=mine, note="收紧阈值"
    )
    assert moved.action_ref == mine
    theirs = world.rule(user=OTHER)
    with pytest.raises(ValidationError, match="不存在或不属于"):
        world.review.act(
            world.uid(), world.alert().id, action_kind="rule", action_ref=theirs, note="改它"
        )


def test_a_foreign_alert_is_refused(world):
    theirs = world.alert(user=OTHER)
    with pytest.raises(StorageError, match="不属于"):
        world.review.act(world.uid(), theirs.id, action_kind="other", note="顺手关掉别人的")
    assert world.alerts.get(theirs.id).status == "new"


def test_a_missing_alert_is_refused(world):
    with pytest.raises(StorageError, match="不存在"):
        world.review.act(world.uid(), "no-such-alert", action_kind="other", note="查无此条")


# ---------- watch ----------


def test_watch_lands_in_the_watchlist_and_closes_together(world, db):
    alert = world.alert()
    moved = world.review.act(
        world.uid(), alert.id, action_kind="watch", note="进自选盯着"
    )
    assert moved.status == "handled" and moved.action_kind == "watch"
    assert world.watchlists.contains(world.uid(), "A", "601088") is True


# ---------- act_with_fill：同事务的成功与回滚 ----------


def test_fill_and_closure_commit_together(world):
    account_id = world.funded_account()
    alert = world.alert(account_id=account_id)
    moved = world.review.act_with_fill(
        world.uid(), alert.id, account_id=account_id, side="sell", qty=100, price=34.5,
        note="按纪律减仓",
    )
    assert moved.status == "handled" and moved.action_kind == "trade"
    order = world.orders.order(account_id, moved.action_ref)
    assert order is not None and order.side == "sell" and order.qty == 100
    # 标的与账户来自告警那一行：构造上就不可能出现「告警是神华、成交是别的票」
    assert (order.market, order.code) == ("A", "601088")


def test_a_terminal_alert_rolls_the_fill_back(world, db):
    """闭环闸在事务里抛 Conflict ⇒ 成交也不留（§11）。

    这条钉的不是异常类型，是库里的行数：一半都没了才算回滚。
    """
    account_id = world.funded_account()
    alert = world.alert()
    world.alerts.dismiss(alert.id, user_id=world.uid(), code="expected")
    before = _orders_count(db)
    with pytest.raises(Conflict):
        world.review.act_with_fill(
            world.uid(), alert.id, account_id=account_id, side="buy", qty=100, price=35.0
        )
    assert _orders_count(db) == before
    assert world.alerts.get(alert.id).status == "dismissed"


def test_a_foreign_account_rolls_the_closure_back(world, db):
    """账户归属由 ledger 拒；拒在事务里，告警就不许已经被关上。"""
    alert = world.alert()
    # open_account 按用户名解析归属：OTHER 名下这笔，我的 act_with_fill 用不了
    foreign = world.ledger.open_account(OTHER, "别人的账户")
    before = _orders_count(db)
    with pytest.raises(StorageError, match="不属于|不存在"):
        world.review.act_with_fill(
            world.uid(), alert.id, account_id=foreign.id, side="buy", qty=100, price=35.0
        )
    assert _orders_count(db) == before
    assert world.alerts.get(alert.id).status == "new"


def test_non_positive_qty_or_price_is_refused_before_anything_is_written(world, db):
    account_id = world.funded_account()
    alert = world.alert()
    with pytest.raises(ValidationError):
        world.review.act_with_fill(
            world.uid(), alert.id, account_id=account_id, side="buy", qty=0, price=35.0
        )
    with pytest.raises(ValidationError):
        world.review.act_with_fill(
            world.uid(), alert.id, account_id=account_id, side="hold", qty=100, price=35.0
        )
    assert _orders_count(db) == 0
    assert world.alerts.get(alert.id).status == "new"


# ---------- 闭环后「顺手写一条标的批注」（D137）----------


def test_sync_annotation_lands_on_the_symbol(world):
    alert = world.alert()
    world.review.act(
        world.uid(), alert.id, action_kind="other", note="已挂条件单",
        sync_annotation="跌破 34 减半，等企稳回补",
    )
    rows = world.annotations.list_target(world.uid(), "symbol", "A:601088")
    assert [made.body_md for made in rows] == ["跌破 34 减半，等企稳回补"]


def test_a_broken_sync_annotation_keeps_the_closure(world):
    """同步批注失败只吞掉批注本身：闭环已经提交，不能被一句话说崩（§12.1）。"""
    alert = world.alert()
    moved = world.review.act(
        world.uid(), alert.id, action_kind="other", note="已挂条件单",
        sync_annotation="超" * 20_001,  # 超过 max_body_chars=20000，AnnotationService 会拒
    )
    assert moved.status == "handled"
    assert world.annotations.list_target(world.uid(), "symbol", "A:601088") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_review_service.py -q`
Expected: FAIL——`ModuleNotFoundError: No module named 'sams.services.review'`。

- [ ] **Step 3: 实现 ReviewService**

新建 `src/sams/services/review.py`：

```python
"""闭环用例层：把「人对这条告警做了什么」落成带引用的结论（spec §8.2/§8.3）。

状态机的闸（终态不可回退、引用必填、note 长度、`alert_events` 逐动作一行）全部住在
`AlertRepo`，这里一枚都不重复——闸出现两处，先失效的那处会让 JSON 与屏幕上不是同一句
（`cli/alert.py` 模块开头是同一条原则）。这一层补的是仓库看不见的那三样：
引用**存在**且属于本人、`thesis` 与告警标的相符、`trade` 的成交与闭环同事务。

`trade` 是唯一在这里写业务数据的动作（§8.2）：`Database.transaction` 是深度计数的，
`ledger` 与 `alerts.act` 内部的各自事务并进外层这一只——一半成一半败的那种
「库里多了一笔没发生的成交」不允许存在。

「顺手写一条标的批注」（D137）在提交**之后**、best-effort：批注写崩了不 rollback
一次已经成立的闭环，spec §12.1 钉的就是这个方向。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sams.core.errors import StorageError, ValidationError
from sams.services.annotations import AnnotationService
from sams.services.ledger import LedgerService
from sams.storage.db import Database
from sams.storage.repos.alerts import Alert, AlertRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

logger = logging.getLogger("sams.services.review")

#: 成交方向只有这两个（与 `sim_orders.side` 一致）；`hold` 之类是敲错
SIDES = ("buy", "sell")


class ReviewService:
    def __init__(
        self,
        db: Database,
        *,
        alerts: AlertRepo,
        ledger: LedgerService,
        users: UserRepo,
        theses: ThesisRepo,
        rules: RuleRepo,
        watchlists: WatchlistRepo,
        annotations: AnnotationService,
    ) -> None:
        self.db = db
        self.alerts = alerts
        self.ledger = ledger
        self.users = users
        self.theses = theses
        self.rules = rules
        self.watchlists = watchlists
        self.annotations = annotations

    def act(
        self,
        user_id: str,
        alert_id: str,
        *,
        action_kind: str,
        action_ref: str | None = None,
        note: str = "",
        sync_annotation: str | None = None,
    ) -> Alert:
        """闭环一次处置，把「引用存在」的闸补在仓库之外（§8.2 那一列「校验」）。

        `trade` 在这里直接拒：它的引用是**这一次新造的**成交，只有
        `act_with_fill` 能给得出——接住一个手写 order id 就等于承认「口头成交」。
        """
        if action_kind == "trade":
            raise ValidationError(
                "trade 走 act_with_fill：成交与闭环必须同事务写（D125），"
                "这个入口不接「已经成交了」的一句话"
            )
        alert = self._owned(user_id, alert_id)
        ref = (action_ref or "").strip()
        if action_kind == "thesis":
            thesis = self.theses.get(user_id, ref)
            if thesis is None:
                raise ValidationError(
                    f"观点 {ref!r} 不存在或不属于当前用户：关联必须指到你自己那一条"
                )
            if (thesis.market, thesis.code) != (alert.market, alert.code):
                raise ValidationError(
                    f"观点的标的是 {thesis.market}:{thesis.code}，与告警"
                    f" {alert.market}:{alert.code} 不符：那条不是这只票的「当时怎么想的」"
                )
        if action_kind == "rule":
            # 规则可以跨标的（自选展开），§8.2 只要求「本人的」
            if self.rules.get(ref, user_id=user_id) is None:
                raise ValidationError(
                    f"规则 {ref!r} 不存在或不属于当前用户：调整必须指到你自己那一条"
                )
        if action_kind == "watch":
            # 加自选与闭环同事务：败一边就两边都别留，否则屏幕上「已处置」而自选里没有
            with self.db.transaction():
                self.watchlists.add(user_id, alert.market, alert.code)
                moved = self.alerts.act(
                    alert_id, user_id=user_id, action_kind="watch", action_ref=ref, note=note
                )
        else:
            moved = self.alerts.act(
                alert_id, user_id=user_id, action_kind=action_kind, action_ref=ref, note=note
            )
        self._sync_annotation(user_id, moved, sync_annotation)
        return moved

    def act_with_fill(
        self,
        user_id: str,
        alert_id: str,
        *,
        account_id: str,
        side: str,
        qty: float,
        price: float,
        fee: float = 0.0,
        ts: datetime | None = None,
        note: str = "",
        sync_annotation: str | None = None,
    ) -> Alert:
        """成交 + 闭环在同一个事务里落（§8.3）。

        标的与账户来自告警那一行（market/code 直接取列），「一致」这条校验是构造出来的
        而不是查出来的；能挡住的只有非正数量/价格、账户不是你的、告警已是终态。
        A 股 T+1、整手、涨跌停这些**不挡**：模拟账本常在补记真实成交，§6.3 说它们
        只该在表单上警告。
        """
        if side not in SIDES:
            raise ValidationError(f"未知方向 side={side!r}：只有 {list(SIDES)}")
        if qty <= 0 or price <= 0:
            raise ValidationError(
                "数量与价格必须为正：一条 0 股的成交在复盘里回答不了任何事"
            )
        alert = self._owned(user_id, alert_id)
        user = self.users.get(user_id)
        if user is None:
            raise StorageError(f"用户 {user_id} 不存在：成交必须有一个记账的人")
        with self.db.transaction():
            fill = self.ledger.buy if side == "buy" else self.ledger.sell
            order = fill(
                account_id, user.username, alert.market, alert.code,
                qty=qty, price=price, fee=fee, ts=ts,
            )
            moved = self.alerts.act(
                alert_id, user_id=user_id, action_kind="trade", action_ref=order.id, note=note
            )
        self._sync_annotation(user_id, moved, sync_annotation)
        return moved

    # —— 内部 ——

    def _owned(self, user_id: str, alert_id: str) -> Alert:
        """「不存在」与「不是你的」折成一句：id 探测不该有第二种回答。"""
        row = self.alerts.get(alert_id, user_id=user_id)
        if row is None:
            raise StorageError(
                f"告警 {alert_id} 不存在（或不属于 {user_id}）：动作要落在本人那条告警上"
            )
        return row

    def _sync_annotation(self, user_id: str, alert: Alert, text: str | None) -> None:
        """闭环后顺手写一条标的批注（D137）：提交之后、best-effort。

        它不许把一次已经成立的闭环拖回去：批注写崩只记一行 warning。
        正文的预填（告警摘要 + 处置结论）是入口那一层的事，这里只收成品。
        """
        if not (text or "").strip():
            return
        try:
            self.annotations.create(
                user_id,
                target_type="symbol",
                target_ref=f"{alert.market}:{alert.code}",
                body_md=text,
            )
        except Exception as exc:  # noqa: BLE001 — 降级是这条链的契约，见模块开头
            logger.warning(
                "告警 %s 闭环已成立，但同步批注失败（不影响闭环）：%s", alert.id, exc
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/services/test_review_service.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: lint + 提交**

```bash
uv run ruff check --no-cache src tests
git add src/sams/services/review.py tests/services/test_review_service.py
git commit -m "feat(services): ReviewService 闭环用例——act 校验矩阵与 trade 同事务成交（§8.2/§8.3）"
```

---

## Task 6: CLI 五动作 + 装配根接线

**Files:**
- Modify: `src/sams/cli/alert.py`
- Modify: `src/sams/bootstrap.py`（`ServiceContext` 字段 + `_context_on` 装配 + provider 注入）
- Test: `tests/cli/test_alert_cli.py`（重写 3 枚 + 追加一组）、`tests/test_bootstrap.py`（追加 1 枚）

- [ ] **Step 1: 写失败的测试（CLI）**

在 `tests/cli/test_alert_cli.py` 中：

（a）**重写** `test_switching_your_mind_replaces_the_previous_sentence`（旧语义「handled→dismissed 允许」被 D124 的终态闸推翻）：

```python
def test_a_terminal_state_refuses_the_second_opinion(container, capsys) -> None:
    """终态不可回退（D124）：先 handle 再 dismiss 是 exit 1，不是静默覆盖。

    旧版允许「改主意就换一句」，代价是复盘里第一次的结论再也找不回来——
    要更正走 5 分钟内的 undo（Web）或再写一条批注，CLI 只把闸亮出来。
    """
    alert = make_alert(container)
    invoke(container, "handle", alert.id, "--user", USER, "--note", "已挂条件单")
    code, text = out(
        container, capsys, "dismiss", alert.id, "--user", USER, "--reason", "其实是误报"
    )
    assert code == 1
    assert "终态不可回退" in text
    got = container.alerts_store.get(alert.id)
    assert got.status == "handled" and got.handle_note == "已挂条件单"
```

（b）**改** `test_dismiss_without_reason_is_refused_with_two` 的断言文字（闸移到了仓库的 dismiss，措辞变了）：

```python
    assert "必须写一句说明" in text
```

（c）**改** `test_handle_json_shape`：补 `--note`（现在 handle 必须带说明）：

```python
def test_handle_json_shape(container, capsys) -> None:
    alert = make_alert(container)
    code, body = payload(
        container, capsys, "handle", alert.id, "--user", USER, "--note", "已复核"
    )
    assert code == 0 and body["exit"] == 0
    assert body["alert"]["status"] == "handled"
    assert body["alert"]["action_kind"] == "other"
    # JSON 里的时刻必须解析得开（屏幕上那种短横是给机器读不了的写法），
    # 而且按下的就是这一趟的那一枚：夹具的钟冻在哪个时刻，那一列就是哪个时刻
    stamped = datetime.fromisoformat(body["alert"]["handled_at"])
    assert stamped == container.alerts_store.clock.now_utc()


def test_handle_without_a_note_is_refused_with_two(container, capsys) -> None:
    """`handle` 现在是 acted(other) 的入口：说明必填（D125），拒在仓库层、也有 JSON。"""
    alert = make_alert(container)
    code, text = out(container, capsys, "handle", alert.id, "--user", USER)
    assert code == 2
    assert "必须写一句说明" in text
    assert container.alerts_store.get(alert.id).status == "new"
```

（d）文件末尾**追加**一组：

```python
# ---------- ack / defer（spec §8.1：CLI 的闭环动词补全）----------


def test_ack_moves_to_acknowledged_and_is_idempotent(container, capsys) -> None:
    """已读只是中间态：重复点不是新结论（仓库层幂等），第二趟照样 exit 0。"""
    alert = make_alert(container)
    code, body = payload(container, capsys, "ack", alert.id, "--user", USER)
    assert code == 0
    assert body["alert"]["status"] == "acknowledged"
    assert body["alert"]["acknowledged_at"] is not None
    code2, body2 = payload(container, capsys, "ack", alert.id, "--user", USER)
    assert code2 == 0 and body2["alert"]["acknowledged_at"] == body["alert"]["acknowledged_at"]


def test_ack_after_a_terminal_state_is_refused_with_one(container, capsys) -> None:
    alert = make_alert(container)
    invoke(container, "handle", alert.id, "--user", USER, "--note", "已挂条件单")
    code, text = out(container, capsys, "ack", alert.id, "--user", USER)
    assert code == 1
    assert "不能再标已读" in text


def test_defer_relative_until_lands_on_the_row(container, capsys) -> None:
    """稍后不改状态，只写一个到点时刻（D138）。

    `_parse_until` 的相对时间按真实钟表折（1h/2d 是「人话的时长」），所以这里的
    边界比对用 `datetime.now`——夹具冻的是仓库的钟，不是解析器的。
    """
    alert = make_alert(container)
    code, body = payload(container, capsys, "defer", alert.id, "--user", USER, "--until", "2h")
    assert code == 0
    assert body["alert"]["status"] == "new"
    until = datetime.fromisoformat(body["alert"]["deferred_until"])
    assert until.tzinfo is not None
    delta = (until - datetime.now(UTC)).total_seconds()
    assert 1.5 * 3600 < delta < 2.5 * 3600


def test_defer_absolute_until_is_accepted(container, capsys) -> None:
    alert = make_alert(container)
    code, body = payload(
        container, capsys, "defer", alert.id, "--user", USER,
        "--until", "2027-01-01T09:00+08:00",
    )
    assert code == 0
    assert datetime.fromisoformat(body["alert"]["deferred_until"]) == datetime.fromisoformat(
        "2027-01-01T01:00+00:00"
    )


def test_defer_with_an_unreadable_until_is_refused_with_two(container, capsys) -> None:
    alert = make_alert(container)
    code, text = out(container, capsys, "defer", alert.id, "--user", USER, "--until", "很快")
    assert code == 2
    assert "读不懂" in text


def test_defer_on_a_terminal_state_is_refused_with_one(container, capsys) -> None:
    alert = make_alert(container)
    invoke(container, "handle", alert.id, "--user", USER, "--note", "已挂条件单")
    code, text = out(container, capsys, "defer", alert.id, "--user", USER, "--until", "1h")
    assert code == 1
    assert "不能再稍后" in text


def test_dismiss_lands_the_other_code(container, capsys) -> None:
    """CLI 只给 `--reason` ⇒ 原因码落 `other`（§8.5）；code 那一列仍要写得出来。"""
    alert = make_alert(container)
    code, body = payload(
        container, capsys, "dismiss", alert.id, "--user", USER, "--reason", "除权造成的假跌破"
    )
    assert code == 0
    assert body["alert"]["dismiss_code"] == "other"
    assert body["alert"]["dismiss_reason"] == "除权造成的假跌破"


def test_alert_json_carries_the_closure_columns(container, capsys) -> None:
    """复盘统计读的就是这几列：JSON 里缺一个键，下游就把它读成「没有这件事」。"""
    alert = make_alert(container)
    invoke(container, "ack", alert.id, "--user", USER)
    code, body = payload(container, capsys, "show", alert.id, "--user", USER)
    assert code == 0
    got = body["alert"]
    for key in (
        "acknowledged_at", "action_kind", "action_ref", "dismiss_code",
        "deferred_until", "thesis_id",
    ):
        assert key in got
    assert got["acknowledged_at"] is not None and got["action_kind"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/cli/test_alert_cli.py -q`
Expected: FAIL——`ack`/`defer` 子命令不存在（SystemExit 2）、`container.review` 不存在、旧断言不符。

- [ ] **Step 3: 实现 CLI**

`src/sams/cli/alert.py` 改动如下（未列出的部分不动）。

（a）模块 docstring 第一段之后加一句：

```python
闭环的四个动词按 spec §8.1 映射：`handle` 是 acted 的 `other`（说明必填）、
`dismiss` 落 `dismiss_code=other`、`ack` 与 `defer` 是新增的两格。状态机的闸全在
仓库层（`AlertRepo`）与 `services/review`，这里只把它们翻成中文和退出码。
```

（b）import 区：`import argparse` 之后加 `import re`；`from sams.cli.common import ...` 不动；新增

```python
from datetime import UTC, datetime, timedelta
```

（c）`add_to`：group 帮助改为 `"告警：看、已读、处置（说明必填）、忽略（原因必填）、稍后"`。`handle`/`dismiss` 的叶子与新增 `ack`/`defer` 叶子最终形为：

```python
    handle = leaf("handle", handle_alert, "标成已处置：acted 的 other——说明必填（≥2 字）")
    handle.add_argument("alert_id")
    handle.add_argument("--note", help="做了什么（这一条现在必须有：「已处置」三个字回答不了复盘）")

    dismiss = leaf("dismiss", dismiss_alert, "忽略一条：必须带原因，那一列是忽略率唯一的输入")
    dismiss.add_argument("alert_id")
    # 不用 argparse 的 required=True：拒在仓库层才能同时给 JSON（D57），
    # argparse 的失败是 SystemExit 加一段 usage，`--json` 的那一侧只读到空 stdin
    dismiss.add_argument(
        "--reason",
        help="为什么不处理；不给就拒（拒在仓库层，那里也给 JSON）。"
        "首期 CLI 只落 other 码：misreport/expected/data_issue/not_applicable/duplicate/other",
    )

    ack = leaf("ack", ack_alert, "标成已读：从此算「看到了还没做」，仍算未处理")
    ack.add_argument("alert_id")

    defer = leaf("defer", defer_alert, "稍后提醒：状态不变，写一个到点时刻（D138）")
    defer.add_argument("alert_id")
    defer.add_argument(
        "--until",
        required=True,
        help="1h/3h/2d 这样的相对时长，或带时区的绝对时间（2026-10-26T09:30+08:00）",
    )
```

（d）`_alert_payload` 的 dict 里追加（放在 `dismiss_reason` 之后）：

```python
        "acknowledged_at": _at(alert.acknowledged_at),
        "action_kind": alert.action_kind,
        "action_ref": alert.action_ref,
        "dismiss_code": alert.dismiss_code,
        "deferred_until": _at(alert.deferred_until),
        "thesis_id": alert.thesis_id,
```

（e）`show_alert` 的文本输出，在 `if alert.handled_at is not None:` 之前插入：

```python
    if alert.acknowledged_at is not None:
        print(f"  已读    {stamp(alert.acknowledged_at)}")
    if alert.action_kind:
        print(f"  动作    {alert.action_kind} {alert.action_ref or ''}".rstrip())
    if alert.deferred_until is not None:
        print(f"  稍后    {stamp(alert.deferred_until)}（到点前回收件箱，不计忽略率）")
```

（f）`handle_alert`/`dismiss_alert` 与 `_move` 整段替换为：

```python
# ---------- ack / defer / handle / dismiss ----------


def _emit_move(args, alert, lines: list[str]) -> int:
    if args.json:
        emit({"status": "ok", "exit": 0, "alert": _alert_payload(alert)})
        return 0
    print(_one_line(alert).removeprefix("  "))
    for line in lines:
        print(line)
    return 0


def ack_alert(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        alert = container.alerts_store.acknowledge(args.alert_id, user_id=user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    return _emit_move(
        args,
        alert,
        [
            f"  已标成 acknowledged：{stamp(alert.acknowledged_at)}"
            "（看到了、还没做——同票其余告警照旧挡着）"
        ],
    )


def defer_alert(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        until = _parse_until(args.until)
        alert = container.alerts_store.defer(args.alert_id, user_id=user.id, until=until)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    return _emit_move(
        args,
        alert,
        [
            f"  已稍后：{stamp(alert.deferred_until)} 之后回收件箱顶部；"
            f"在那之前状态仍是 {alert.status}"
        ],
    )


def handle_alert(container: Container, args) -> int:
    """`handle` 是 acted 的 CLI 入口（§8.1）：动作类型 `other`，说明必填。

    说明的闸在仓库层（`act` 对 `other` 要 ≥2 字），这里不重复——两处闸就会有两处失效。
    """
    try:
        user = resolve_actor(container.users, args.user)
        alert = container.review.act(
            user.id, args.alert_id, action_kind="other", note=args.note or ""
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    return _emit_move(
        args,
        alert,
        [
            f"  已标成 handled：{alert.handle_note}",
            "  这一条不再算未处理（`no_open_alert` 数的是 status=new/acknowledged）；"
            "同票上若还有别的新告警，那些照样挡着后面的推送——更响的那一档除外（D94）",
        ],
    )


def dismiss_alert(container: Container, args) -> int:
    """CLI 只收 `--reason`，原因码落 `other`（§8.5）：码的枚举入口在 Web 表单。"""
    try:
        user = resolve_actor(container.users, args.user)
        alert = container.alerts_store.dismiss(
            args.alert_id, user_id=user.id, code="other", reason=args.reason or ""
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    return _emit_move(
        args,
        alert,
        [
            f"  已忽略：{alert.dismiss_reason}",
            "  那一列进忽略率统计：「这条规则是不是在瞎报」只靠它回答",
        ],
    )


_RELATIVE_UNTIL = re.compile(r"^(\d+)([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _parse_until(value: str) -> datetime:
    """`1h`/`3h`/`2d` 折成「真实钟表的未来」，绝对时间按 ISO 读。

    相对时长按 `datetime.now` 而不是注入的 `clock` 算吗？——是：稍后提醒是给**人**的
    时长，仓库那头的闸（`defer` 要求未来时刻）读自己的钟，两边都在真实时间轴上。
    裸 ISO 时刻按本机时区补（`astimezone()`）：一个不带偏移的「09:30」说的是
    人那里的 09:30，不是 UTC 的 09:30。
    """
    text = (value or "").strip().lower()
    match = _RELATIVE_UNTIL.match(text)
    if match:
        amount = int(match.group(1))
        if amount < 1:
            raise ValidationError(f"--until 的相对时长要为正（收到 {value!r}）")
        return datetime.now(UTC) + timedelta(seconds=amount * _UNIT_SECONDS[match.group(2)])
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(
            f"--until 读不懂：{value!r}。用 1h/3h/2d，或带时区的绝对时间"
            "（例如 2026-10-26T09:30+08:00）"
        ) from exc
    return moment if moment.tzinfo is not None else moment.astimezone()
```

注意：`ValidationError` 已在文件 import 区（原样保留的 import 里有 `SamsError, StorageError, ValidationError`）。旧的 `_move` 函数删掉（不再有调用方）。

- [ ] **Step 4: 跑测试确认失败→分半通过**

Run: `uv run pytest tests/cli/test_alert_cli.py -q`
Expected：CLI 测试仍会因 `container.review` 不存在而部分 FAIL——下一步补装配。

- [ ] **Step 5: bootstrap 接线**

`src/sams/bootstrap.py`：

（a）import 区按现有分组补：

```python
from sams.services.annotation_context import AnnotationContextProvider
from sams.services.review import ReviewService
from sams.storage.repos.app_settings import AppSettingsRepo
```

（b）`ServiceContext` 在 `annotation_pruner: AnnotationPruner` 之后插入三个字段：

```python
    #: 运行时策略（D127）：首期只有团队共享开关，推送 provider 每次都问它
    app_settings: AppSettingsRepo
    #: 推送上下文的 provider：注入给 `notify`（构造顺序在它之后，见 `_context_on`）
    annotation_context: AnnotationContextProvider
    #: 闭环用例：`sams alert handle` 走的就是它（spec §8.2/§8.3）
    review: ReviewService
```

（c）`_context_on` 里，`annotation_pruner = AnnotationPruner(...)` 那一段之后、`sync = SyncService(...)` 之前插入：

```python
    app_settings = AppSettingsRepo(database, clock=clock)
    annotation_context = AnnotationContextProvider(
        annotations_store, users, theses_store, app_settings, cfg
    )
    #: notify 造在批注之前（装配顺序），provider 只能事后接：队列与 Web 重发都只调
    #: `deliver_existing(alert_id)`，实例这一格是唯一接得住它的地方（§7.3）
    notify.context_provider = annotation_context
    review = ReviewService(
        database,
        alerts=alerts_store,
        ledger=ledger,
        users=users,
        theses=theses_store,
        rules=rules_store,
        watchlists=watchlists,
        annotations=annotations,
    )
```

（d）文件末尾 `return ServiceContext(...)` 的参数里，`annotation_pruner=annotation_pruner,` 之后加：

```python
        app_settings=app_settings,
        annotation_context=annotation_context,
        review=review,
```

（`build_container` 用 `fields(context)` 反射搬运，`Container` 不需要动。）

- [ ] **Step 6: 钉住装配的容器测试**

在 `tests/test_bootstrap.py` 里，仿照既有的 `test_container_wires_attachments_and_the_pruner` 的容器构造方式（同一个 fixture、同一个 `build_*` 调用），追加：

```python
def test_container_wires_the_push_context_and_the_review(<照抄相邻用例的 fixture 形参>):
    """provider 只有一格接得住队列与重发：`notify.context_provider`（§7.3）。

    这枚钉的是装配本身的接缝：`AnnotationContextProvider` 改了构造签名而这里没跟上，
    红的是这一条，而不是某个交易日里通知线程的栈追踪。
    """
    built = <照抄相邻用例构造容器的那一行>
    assert built.annotation_context is not None
    assert built.notify.context_provider is built.annotation_context
    assert built.review is not None
    assert built.app_settings.team_sharing() is True
```

- [ ] **Step 7: 全量跑通（本任务触及的 + 邻居）**

Run: `uv run pytest tests/cli/test_alert_cli.py tests/test_bootstrap.py tests/monitor tests/web/test_alerts_web.py -q`
Expected: 全部 PASS。

- [ ] **Step 8: lint + 提交**

```bash
uv run ruff check --no-cache src tests
git add src/sams/cli/alert.py src/sams/bootstrap.py tests/cli/test_alert_cli.py tests/test_bootstrap.py
git commit -m "feat(cli): alert 补 ack/defer 并迁到闭环动词；装配根接 provider 与 ReviewService（§8.1）"
```

---

## 收尾（控制器执行，不属于任何实现任务）

- [ ] 全量：`uv run pytest -q`（预期在上一里程碑 3726 枚基础上净新增约 45 枚，零失败）
- [ ] `uv run ruff check --no-cache src tests` 干净
- [ ] 确认 `qodercli_resume.sh` 与 `tmp/` 仍未被跟踪
- [ ] 推送由控制器执行；子代理不许 push

## 与 spec 的对账单（自查用）

| spec 条目 | 落点 |
|---|---|
| §7.1 SYS/无 code 排除、两路合并、接收人可见、置顶优先、≤3、去重、作者「我」/display_name/用户名、单行 ≤120 | T3 provider + T1 格式化 |
| §7.1 不写 payload_json、只写 notify_log.context_json | T4（payload 不动）+ T2（列） |
| §7.2 AnnotationContext/PushContext/format_annotation_context 住 core | T1 |
| §7.2 发送时装配、逐通道预算、放不下整块不发、语音/桌面不带、深链行 | T4 |
| §7.2 组装失败不阻断告警 | T3 provider 吞异常返 None |
| §7.2 context_json 形状 `{"ids","dropped","rendered_chars"}` 逐通道 | T4 + T2 |
| §7.3 队列/重发只传 alert_id ⇒ 实例 provider | T4 属性 + T6 装配 |
| §8.1 CLI：handle→acted(other 必填)、dismiss→other 码、新增 ack/defer | T6 |
| §8.2 引用存在/归属校验、thesis 同标的、rule 可跨标的、watch 同事务 | T5 |
| §8.3 trade 同事务 + 失败整体回滚 + ledger bump（经 LedgerService 内部） | T5 |
| D137 闭环可选同步批注、失败不改闭环 | T5 `_sync_annotation` |
| §4.2 策略每次读取带 5 秒缓存；§9 白名单键 | T3 AppSettingsRepo |
| §12.1 分层：monitor 通过注入拿批注 | T3/T4/T6（无跨层 import，layering 测试不动即绿） |

**不做（明确排除，防越界）**：`/review` 页、Web 告警闭环表单、附件下载路由、admin/break-glass、undo 的 CLI 命令——分属计划 4 与计划 5。`AlertRepo.set_status` 仍保留（Web M3 旧页还在调用它，计划 4 迁移后处理）。
