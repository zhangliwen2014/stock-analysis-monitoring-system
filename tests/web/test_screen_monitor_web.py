"""方案卡上的自动监控开关（S4-B / D254）：渠道、上次运行与当前名单都在同一行。

开关不是一个偏好，而是一条会发消息的接线：「开」之后写下的渠道就是新命中会走的
通道；没有渠道也不是没开，只是只进告警中心。
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_screen_web import DAY, _alice, _save, default_seed


def _monitor(logged_screen, preset_id, **data):
    payload = {"csrf": logged_screen.csrf}
    payload.update(data)
    return logged_screen.client.post(
        f"/screen/presets/{preset_id}/monitor",
        data=payload,
        follow_redirects=False,
    )


def test_turning_monitoring_on_starts_with_a_baseline(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]

    response = _monitor(logged_screen, preset.id, enabled="1")

    assert response.status_code == 303
    fresh = logged_screen.context.screener_presets.get(_alice(logged_screen), preset.id)
    assert fresh is not None and fresh.enabled is True
    assert fresh.last_run_at is None
    page = logged_screen.client.get("/screen")
    section = page.text.split("筛选方案")[1]
    assert "自动监控" in page.text and "名单" in section
    assert "开" in section


def test_the_card_remembers_the_channels_and_the_membership_count(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    user = _alice(logged_screen)
    channel = logged_screen.context.channels_store.create(
        user, "desktop", "桌面", {}
    ).id
    logged_screen.context.screener_hits.sync(user, preset.id, "A", ["600519"], DAY)

    _monitor(logged_screen, preset.id, enabled="1", channel_ids=[channel])

    fresh = logged_screen.context.screener_presets.get(user, preset.id)
    assert fresh is not None and fresh.channel_ids == (channel,)
    page = logged_screen.client.get("/screen")
    assert "桌面" in page.text
    assert "名单 1 只" in page.text


def test_turning_monitoring_off_stops_the_daily_run(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    _monitor(logged_screen, preset.id, enabled="1")

    response = _monitor(logged_screen, preset.id)

    assert response.status_code == 303
    fresh = logged_screen.context.screener_presets.get(_alice(logged_screen), preset.id)
    assert fresh is not None and fresh.enabled is False
    assert "自动监控已关闭" in logged_screen.client.get("/screen").text


def test_overwriting_the_conditions_resets_the_baseline(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    user = _alice(logged_screen)
    logged_screen.context.screener_presets.mark_run(
        user, preset.id, datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    )

    response = logged_screen.client.post(
        f"/screen/presets/{preset.id}",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "f": "pr_now",
            "o": "<=",
            "v": "0.3",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    fresh = logged_screen.context.screener_presets.get(user, preset.id)
    assert fresh is not None and fresh.last_run_at is None
    page = logged_screen.client.get(response.headers["location"])
    assert "下次自动运行只记基线，不推送" in page.text
