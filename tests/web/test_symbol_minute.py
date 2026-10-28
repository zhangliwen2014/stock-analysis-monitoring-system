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


def test_bars_answers_a_minute_period_with_the_daily_key_set(logged_minute):
    """分钟档走出 `bars` 必须是**同一套键**：前端在画图之前就按键取数，缺键先崩。"""
    _seed(logged_minute)
    payload = logged_minute.client.get(
        "/api/symbol/A/600519/bars?period=m5&main=ma&sub=vol"
    ).json()
    assert payload["period"] == "m5"
    assert payload["period_label"] == "5分"
    # 分钟档的标签要带上时刻：同一天的三根只写「10-28」在屏幕上分不出谁是谁
    assert payload["dates"][0].endswith("10:55")
    assert payload["close"] == payload["close_hfq"]
    assert set(payload["main"]["ma"]) and set(payload["sub"]["vol"]) == {"volume"}


def test_minute_bars_reuse_the_empty_shape_when_the_chain_refuses(logged_minute):
    """HK 的分钟 K 不答：200 + 与日线空态同形的 16 个键 + 那句 note。"""
    _seed(logged_minute, market="HK", code="00700")
    payload = logged_minute.client.get("/api/symbol/HK/00700/bars?period=m5").json()
    assert payload["dates"] == [] and payload["main"] == {} and payload["sub"] == {}
    assert payload["note"] and "分钟" in payload["note"]
    assert "warmup_floor" in payload and "anchored_on" in payload


def test_the_minute_periods_are_offered_on_the_chart_bar(logged_minute):
    """周期条上那六档必须存在：置灰的判据是「档在但点不动」，不是「档没了」。"""
    _seed(logged_minute)
    html = logged_minute.client.get("/api/partials/chart?market=A&code=600519").text
    for label in ("1分", "5分", "15分", "30分", "60分", "120分"):
        assert label in html


def test_an_unsupported_minute_period_still_400s(logged_minute):
    """`m7` 不在六档里：与拼错指标名同一姿势，400 并说清可选的是哪些。"""
    _seed(logged_minute)
    response = logged_minute.client.get("/api/symbol/A/600519/bars?period=m7")
    assert response.status_code == 400
