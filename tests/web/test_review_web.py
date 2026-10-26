"""/review 那一屏（M4-B §6.4、D135）：窗口算术、清单顺序、比率与「—」的措辞。

口径不在这测——`tests/services/test_review_stats.py` 测的是函数；这里测的是
「窗口在 display_tz 里怎么切」「数是不是从对的那条查询来的」「旧版那两个字出没出现」。
数一律用 `id="…-total"` 的锚点读页面上的那个 `<span>`，不读测试自己写的常量。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from support import FrozenClock

from sams.storage.repos.alerts import ACKNOWLEDGED, HANDLED


def _seed(ctx, *, user_id, market="A", code="600519", **overrides):
    """与 `test_alerts_web.py` 同款（不建 tests/web/support.py，那会遮住根目录的 `support`）。"""
    kwargs = dict(
        rule_id=None,
        rule_name="止损线",
        account_id=None,
        account_name="",
        market=market,
        code=code,
        symbol_name="贵州茅台",
        severity="mid",
        stage="close",
        value_text="1500.00",
        threshold_text="1550.00",
        action_note="考虑减仓",
        triggered_at=datetime.now(UTC),
    )
    kwargs.update(overrides)
    return ctx.alerts_store.create(user_id, **kwargs)


def _alice(ctx):
    return ctx.users.get_by_username("alice")


def _number(page, anchor):
    found = re.search(rf'id="{anchor}"[^>]*>(\d+)<', page.text)
    assert found is not None, page.text
    return int(found.group(1))


def test_review_defaults_to_the_seven_day_window(logged):
    page = logged.client.get("/review")
    assert page.status_code == 200
    assert 'value="7d" selected' in page.text
    assert "超 24 小时未处理" in page.text


def test_unknown_window_is_answered_with_the_allowed_list(logged):
    page = logged.client.get("/review?window=14d")
    assert page.status_code == 400
    assert "未知窗口" in page.text


def test_week_window_starts_this_monday_by_display_tz(make_app):
    """冻结时钟装在「北京周日 09:00」：本周一 00:00 起算，上周日那条不该在。

    冻结点取「真实当下之后的下一个周日 01:00 UTC」而不是写死 2026-10-25：
    空闲心跳 `_active_ok` 拿真实墙钟比对（auth.py），写死的过去时刻让这条测试
    只在写计划那一周跑得绿——窗口算术只认星期几，跟着墙钟走一样证得动。
    """
    now = datetime.now(UTC)
    frozen = (now + timedelta(days=(6 - now.weekday()) % 7)).replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    if frozen <= now:
        frozen += timedelta(days=7)
    web = make_app(clock=FrozenClock(frozen))
    ctx = web.contexts.current()
    ctx.users.create("alice", "correct-horse-battery", is_admin=True)
    uid = ctx.users.get_by_username("alice").id
    _seed(ctx, user_id=uid, triggered_at=frozen - timedelta(days=7))  # 上周日
    _seed(ctx, user_id=uid, triggered_at=frozen - timedelta(days=6))  # 本周一 09:00
    with TestClient(web.app) as client:
        resp = client.post(
            "/login",
            data={"username": "alice", "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = client.get("/review?window=week")
        assert _number(page, "window-total") == 1


def test_seven_day_window_includes_the_sixth_midnight(make_app):
    """「近 7 天」含今天：起点是 6 天前的零点，那一秒那一条要在；再早一分钟不在。

    冻结时钟（同 week 测试）：页面与种子各读一次时钟的话，跨本地零点时两个
    「6 天前的零点」会漂移，断言撞运气。冻结点取真实墙钟之后的 09:00 UTC——
    换成北京时间是傍晚，离本地午夜最远。
    """
    now = datetime.now(UTC)
    frozen = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    if frozen <= now:
        # 兜底：空闲心跳拿真实墙钟比对会话（auth.py），冻结点必须严格在未来，
        # 否则登录后的会话校验会先于窗口断言失败——再推一周保证它总在将来。
        frozen += timedelta(days=7)
    web = make_app(clock=FrozenClock(frozen))
    ctx = web.contexts.current()
    ctx.users.create("alice", "correct-horse-battery", is_admin=True)
    uid = ctx.users.get_by_username("alice").id
    tz = ZoneInfo(ctx.settings.server.display_tz)
    sixth = frozen.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=6
    )
    _seed(ctx, user_id=uid, triggered_at=sixth + timedelta(minutes=1))
    _seed(ctx, user_id=uid, triggered_at=sixth - timedelta(minutes=1))
    with TestClient(web.app) as client:
        resp = client.post(
            "/login",
            data={"username": "alice", "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = client.get("/review")
        assert _number(page, "window-total") == 1


def test_overdue_lists_old_not_fresh_and_the_top_number_is_full(logged):
    ctx = logged.context
    old = _seed(ctx, user_id=_alice(ctx).id, triggered_at=datetime.now(UTC) - timedelta(hours=30))
    fresh = _seed(ctx, user_id=_alice(ctx).id, triggered_at=datetime.now(UTC) - timedelta(hours=2))
    page = logged.client.get("/review")
    assert f"/alerts/{old.id}" in page.text
    assert fresh.id not in page.text  # 不到 24 小时的新告警不属于这份清单
    assert _number(page, "overdue-total") == 1


def test_deferred_not_yet_due_sorts_last(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    older = _seed(ctx, user_id=uid, triggered_at=datetime.now(UTC) - timedelta(hours=40))
    old = _seed(ctx, user_id=uid, triggered_at=datetime.now(UTC) - timedelta(hours=30))
    deferred = _seed(ctx, user_id=uid, triggered_at=datetime.now(UTC) - timedelta(hours=50))
    ctx.alerts_store.defer(
        deferred.id, user_id=uid, until=datetime.now(UTC) + timedelta(hours=3)
    )
    page = logged.client.get("/review")
    order = [page.text.index(f"/alerts/{row.id}") for row in (older, old, deferred)]
    assert order == sorted(order)  # 到点的最老在前，未到点的垫底


def test_quick_ack_button_closes_in_place(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id, triggered_at=datetime.now(UTC) - timedelta(hours=30))
    page = logged.client.get("/review")
    assert f'action="/alerts/{alert.id}/ack"' in page.text
    resp = logged.client.post(
        f"/alerts/{alert.id}/ack", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert ctx.alerts_store.get(alert.id).status == ACKNOWLEDGED  # 行为直接从库里断言
    # 已读仍算未处理，仍在清单里（D124）
    assert f"/alerts/{alert.id}" in logged.client.get("/review").text


def test_rule_ratio_top_rows_and_thin_single_column(logged):
    ctx = logged.context
    uid = _alice(ctx).id

    def _rule(rule_id, fired, missed):
        rows = []
        for _index in range(fired):
            rows.append(
                _seed(
                    ctx, user_id=uid,
                    rule_id=rule_id, rule_name=f"规则{rule_id}",
                    triggered_at=datetime.now(UTC) - timedelta(hours=2),
                )
            )
        for row in rows[:missed]:
            ctx.alerts_store.dismiss(row.id, user_id=uid, code="expected")
        return rows

    _rule("r1", 5, 4)
    _rule("r2", 3, 2)
    _rule("r5", 2, 2)  # 样本不足：不排名
    page = logged.client.get("/review")
    assert "/rules/r1/edit" in page.text and "/rules/r2/edit" in page.text
    assert "样本不足" in page.text and "规则r5" in page.text


def test_handled_section_links_to_the_objects(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    watch = _seed(ctx, user_id=uid)
    ctx.alerts_store.act(watch.id, user_id=uid, action_kind="watch")
    legacy = _seed(ctx, user_id=uid, triggered_at=datetime.now(UTC) - timedelta(hours=2))
    # 旧版关法：不经过 `act`（没有 action_kind、也没有事件），只把状态列改了
    ctx.db.execute(
        "UPDATE alerts SET status = ?, handled_at = ? WHERE id = ?",
        [HANDLED, datetime.now(UTC), legacy.id],
    )
    page = logged.client.get("/review")
    assert "加入自选" in page.text and "/watchlist" in page.text
    assert "旧版" in page.text


def test_empty_denominators_are_dashes_not_zero(logged):
    """有一条 new 而无闭环：处置率/忽略率的分母是 0——「—」，不是 0%。"""
    ctx = logged.context
    _seed(ctx, user_id=_alice(ctx).id)
    page = logged.client.get("/review")
    assert page.text.count("<td>—</td>") >= 2


def test_empty_window_says_the_do_something_sentence(logged):
    page = logged.client.get("/review")
    assert "本周没有告警。可以去看看自选与持仓，或写一条复盘批注。" in page.text


def test_the_page_says_test_alerts_are_excluded(logged):
    """D131 把 `is_test` 挡在复盘之外，而屏幕从不解释：收件箱里躺着两条测试告警、
    窗口说「本周没有告警」时，读的人只会以为统计坏了——排除规则要写在同一屏上。"""
    page = logged.client.get("/review")
    assert "测试告警不计入复盘统计" in page.text


def test_annotations_section_lists_this_week_only(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    ctx.annotations.create(uid, target_type="symbol", target_ref="A/600519", body_md="本周写的逻辑")
    old = ctx.annotations.create(
        uid, target_type="symbol", target_ref="A/600519", body_md="上个月写的"
    )
    ctx.db.execute(
        "UPDATE annotations SET created_at = ? WHERE id = ?",
        [datetime.now(UTC) - timedelta(days=30), old.id],
    )
    page = logged.client.get("/review")
    assert "本周写的逻辑" in page.text
    assert "上个月写的" not in page.text


def test_open_count_partial_is_the_badge_number(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    _seed(ctx, user_id=uid)
    _seed(ctx, user_id=uid, is_test=True)  # 徽标不该把测试告警数进去（D131）
    resp = logged.client.get("/api/partials/open-count")
    assert resp.status_code == 200
    assert resp.text == "1"


def test_nav_carries_the_review_link_and_the_badge_hook(logged):
    page = logged.client.get("/review")
    assert '<a href="/review">复盘' in page.text
    assert 'hx-get="/api/partials/open-count"' in page.text
