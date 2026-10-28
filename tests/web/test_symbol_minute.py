"""分时接口：spec §5.2 那七个键，与 §6 钉 7/8 的姿势（拒绝也答 200 + note）。"""

from __future__ import annotations

import polars as pl

_NAME = "贵州茅台"


def _seed(logged, market="A", code="600519"):
    logged.context.symbols.upsert(
        pl.DataFrame({"market": [market], "code": [code], "name": [_NAME], "currency": ["CNY"]})
    )


def test_the_minute_endpoint_answers_exactly_the_seven_spec_keys(logged_minute):
    _seed(logged_minute)
    payload = logged_minute.client.get("/api/symbol/A/600519/minute").json()
    assert set(payload) == {"date", "times", "price", "avg", "volume", "prev_close", "note"}
    assert payload["times"] == ["09:30", "09:31", "09:32"]
    # 每分钟量是差分出来的：第三条累计 400000 股 → 这一分钟 81400
    assert payload["volume"] == [153700.0, 164900.0, 81400.0]
    assert payload["prev_close"] == 1355.9
    assert payload["note"] is None


def test_minute_refusal_is_200_with_a_note_and_empty_arrays(logged_minute):
    """§6 钉 8：HK 的分钟 K 两个源都不答 → 200 + note，不许画成「这只票没数据」。"""
    _seed(logged_minute, market="HK", code="00700")
    response = logged_minute.client.get("/api/symbol/HK/00700/minute")
    assert response.status_code == 200
    payload = response.json()
    assert payload["times"] == [] and payload["price"] == []
    assert "分时" in payload["note"]


def test_minute_requires_a_session(minute_app):
    """未登录：与 `test_chart_partial_requires_a_login` 同一姿势——本站的兜底是 303 去登录页。

    计划原文写的「已登录再把 cookie 置空」行不通：per-request `cookies=` 遮不住
    client  jar 里那枚有效 `sams_session`（httpx 合并两边，空值不覆盖），打出去仍是 200。
    """
    response = minute_app.client.get(
        "/api/symbol/A/600519/minute", follow_redirects=False
    )
    assert response.status_code in (303, 401)
