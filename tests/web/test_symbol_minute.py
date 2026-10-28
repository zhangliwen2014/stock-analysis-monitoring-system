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


def _group(html: str, name: str) -> str:
    """某一组里的那几个字。

    `aria-current` 的判据是「**一组**只许一个」，整段计数会跟着视图组一起变：
    加一条视图切换就把那条老用例变成恒假，而它想钉的东西一条都没变。
    """
    start = html.index(f'aria-label="{name}"')
    return html[start : html.index("</span>", start)]


def test_the_chart_partial_offers_a_view_switch_with_kline_current(logged_minute):
    """默认看 K 线：视图那一组存在、`K线` 带 `aria-current`，周期条照旧带自己那一个。"""
    _seed(logged_minute)
    html = logged_minute.client.get("/api/partials/chart?market=A&code=600519").text
    assert 'aria-label="视图"' in html
    assert _group(html, "视图").count('aria-current="true"') == 1
    assert "K线" in _group(html, "视图")
    assert _group(html, "周期").count('aria-current="true"') == 1


def test_the_time_view_points_the_chart_at_the_minute_endpoint(logged_minute):
    """切到分时：div 上的 kind 与 url 一起换，图注换成没有复权那回事的那一句（D164/D158）。

    钉 `data-chart-kind` 是因为 `app.js` 就靠它分两条画法（§5.3）——
    这个属性写了却没人读，等于画分时的那条路根本没接上。
    """
    _seed(logged_minute)
    html = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&view=time"
    ).text
    assert 'data-chart-kind="time"' in html
    assert 'data-chart-url="/api/symbol/A/600519/minute"' in html
    assert "当日分时" in html
    # 分时那一屏没有可切的指标：留着开关会让人以为点下去会换东西
    assert "data-indicator" not in html


def test_an_unknown_view_is_a_400_not_a_silent_kline(logged_minute):
    """与拼错指标名同一姿势：写错的视图名当场说清可选的是哪些，不安静按 K 线画。"""
    _seed(logged_minute)
    response = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&view=ren"
    )
    assert response.status_code == 400


def test_a_minute_period_opens_the_partial_without_400(logged_minute):
    """周期条上写着 `5分`，那一档的局部就必须点得开：`bars` 认了而局部 400 是最坏的一种不一致。"""
    _seed(logged_minute)
    response = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&period=m5"
    )
    assert response.status_code == 200
    assert 'data-chart-url="/api/symbol/A/600519/bars?period=m5' in response.text


def test_the_time_view_falls_back_to_kline_when_a_period_is_clicked(logged_minute):
    """从分时点日线 = 回到 K 线：周期那一组的链接自己带 `view=kline`。

    否则「在分时视图里点周期」会得到一块照旧画分时的图，选中的档说了假话。
    """
    _seed(logged_minute)
    html = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&view=time"
    ).text
    assert "view=kline&period=day" in _group(html, "周期")


def test_the_unanswerable_periods_are_greyed_and_still_clickable(logged_minute):
    """D165：US 的分钟 K 链上没有源 → 那一档置灰、说出为什么，但**还在**、还点得动。

    灰的是 `class="dim"` 而不是 `aria-disabled="true"`：后者在那儿说的是「点不动」
    （副图满了那一格才是那个意思），而这一档点下去会回一句 note——
    读屏软件念成「不可用」就等于把那句为什么藏起来了。
    """
    _seed(logged_minute, market="US", code="AAPL")
    html = logged_minute.client.get("/api/partials/chart?market=US&code=AAPL").text
    periods = _group(html, "周期")
    assert "5分" in periods  # 不是消失
    assert 'class="dim"' in periods
    assert "分钟 K" in periods  # title 里那句为什么
    assert "hx-get" in periods


def test_the_time_tab_is_greyed_when_no_source_answers_series(logged_minute):
    """分时那一档同理：腾讯答 A/HK、不答 US，US 这一屏上它是灰的但仍指得到接口。"""
    _seed(logged_minute, market="US", code="AAPL")
    html = logged_minute.client.get("/api/partials/chart?market=US&code=AAPL").text
    views = _group(html, "视图")
    assert 'class="dim"' in views
    assert "分时" in views
