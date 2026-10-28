"""设置页的「图表偏好」卡片：两张输入框，改完这一页与个股图都跟着变。"""

from sams.web.chart_prefs import CHART_PERIODS

KEYS = tuple(name for name, _label in CHART_PERIODS)


def _alice(web):
    return web.context.users.get_by_username("alice")


def _save(client, csrf, periods, ma):
    return client.post(
        "/settings/chart",
        data={"csrf": csrf, "periods": periods, "ma": ma},
        follow_redirects=False,
    )


def test_the_card_starts_empty_and_lists_every_period(logged):
    page = logged.client.get("/settings")
    assert page.status_code == 200
    assert "图表偏好" in page.text
    assert 'name="periods"' in page.text
    assert 'name="ma"' in page.text
    assert "1分" in page.text and "10日" in page.text


def test_saving_keeps_the_typed_order_and_windows(logged):
    response = _save(logged.client, logged.csrf, ",".join(reversed(KEYS)), "5,20")
    assert response.status_code == 303
    page = logged.client.get("/settings")
    assert 'value="' + ",".join(reversed(KEYS)) + '"' in page.text
    assert 'value="5,20"' in page.text
    pref = logged.context.chart_prefs.get(_alice(logged).id)
    assert pref.periods == tuple(reversed(KEYS))
    assert pref.windows == (5, 20)


def test_a_bad_order_is_refused_and_the_typed_text_stays(logged):
    response = _save(logged.client, logged.csrf, ",".join(KEYS[:-1]), "5,20")
    assert response.status_code == 400
    assert "每档正好一次" in response.text
    assert 'value="' + ",".join(KEYS[:-1]) + '"' in response.text
    assert logged.context.chart_prefs.get(_alice(logged).id) is None


def test_a_bad_window_is_refused(logged):
    response = _save(logged.client, logged.csrf, ",".join(KEYS), "0")
    assert response.status_code == 400
    assert "1..500" in response.text


def test_clearing_both_fields_goes_back_to_the_defaults(logged):
    _save(logged.client, logged.csrf, ",".join(reversed(KEYS)), "5,20")
    response = _save(logged.client, logged.csrf, "", "")
    assert response.status_code == 303
    assert logged.context.chart_prefs.get(_alice(logged).id) is None


def _period_bar(html: str) -> str:
    """周期条那一段：视图链接里也有 `period=`，只能按 aria-label 切。"""
    return html.split('aria-label="周期"', 1)[1].split("</span>", 1)[0]


def test_the_period_bar_follows_the_saved_order(logged):
    _save(logged.client, logged.csrf, ",".join(reversed(KEYS)), "")
    bar = _period_bar(logged.client.get("/api/partials/chart?market=A&code=600519").text)
    assert bar.index(">10日<") < bar.index(">日<") < bar.index(">1分<")


def test_the_chart_defaults_to_the_saved_ma_windows(logged):
    _save(logged.client, logged.csrf, ",".join(KEYS), "5,20")
    html = logged.client.get("/api/partials/chart?market=A&code=600519").text
    assert "ma=5,20" in html


def test_an_explicit_ma_wins_over_the_preference(logged):
    _save(logged.client, logged.csrf, ",".join(KEYS), "5,20")
    html = logged.client.get("/api/partials/chart?market=A&code=600519&ma=3,7").text
    assert "ma=3,7" in html
    assert "ma=5,20" not in html
