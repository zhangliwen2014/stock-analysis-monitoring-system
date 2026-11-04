"""表单原文的字符集：starlette 把 urlencoded 体按 latin-1 解码，`forms.form_data` 折回去。

`formparsers.FormParser.parse` 对字段值是写死的 `decode("latin-1")`（HTTP 的历史默认），
浏览器一律 percent-encode 所以不受影响；`curl -d "note=腾讯"` 那种**裸 UTF-8 字节**
才会以乱码上屏。修复只认「urlencoded 且没声明 charset」这一种请求，且要求
latin-1 → UTF-8 的往返**严格**成立——折不动的原样保留，真 latin-1 的客户端不受影响。
"""

from __future__ import annotations


def _post_profile(logged, raw_body: bytes):
    return logged.client.post(
        "/settings/profile",
        content=raw_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def test_raw_utf8_urlencoded_body_survives(logged):
    body = b"csrf=" + logged.csrf.encode("ascii") + b"&display_name=" + "腾讯".encode()
    resp = _post_profile(logged, body)
    assert resp.status_code == 303, resp.text
    alice = logged.context.users.get_by_username("alice")
    assert alice.display_name == "腾讯"


def test_percent_encoded_utf8_is_left_alone(logged):
    """浏览器那一侧的形状（percent-encoded，starlette 已按 UTF-8 折好）：
    修复不能把已经正确的值再折一次。"""
    from urllib.parse import quote

    body = f"csrf={logged.csrf}&display_name={quote('腾讯')}".encode("ascii")
    resp = _post_profile(logged, body)
    assert resp.status_code == 303
    alice = logged.context.users.get_by_username("alice")
    assert alice.display_name == "腾讯"


def test_genuine_latin1_bytes_are_not_mangled(logged):
    """é 的裸字节 0xE9 不是合法 UTF-8 序列：严格折不动 → 保持 latin-1 的读法。"""
    body = b"csrf=" + logged.csrf.encode("ascii") + b"&display_name=caf\xe9"
    resp = _post_profile(logged, body)
    assert resp.status_code == 303
    alice = logged.context.users.get_by_username("alice")
    assert alice.display_name == "café"


def _post_raw(logged, path: str, **fields: str):
    """把裸 UTF-8 字节写进 urlencoded 体：`curl -d 'note=腾讯'` 的形状。

    键与 csrf 是 ASCII，值按字段自己的编码走——这正是 starlette 会按 latin-1
    读错、而 `form_data` 要折回来的那种请求。
    """
    parts = [b"csrf=" + logged.csrf.encode("ascii")]
    parts += [key.encode("ascii") + b"=" + value.encode("utf-8") for key, value in fields.items()]
    return logged.client.post(
        path,
        content=b"&".join(parts),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def test_watchlist_note_from_raw_utf8_body_survives(logged):
    resp = _post_raw(logged, "/watchlist/add", market="A", code="600519", note="高端白酒")
    assert resp.status_code == 303, resp.text
    alice = logged.context.users.get_by_username("alice")
    rows = {item.code: item for item in logged.context.watchlists.list(alice.id)}
    assert rows["600519"].note == "高端白酒"


def test_thesis_note_from_raw_utf8_body_survives(logged):
    resp = _post_raw(
        logged, "/theses/create", market="A", code="600036", style="value", note="低估值银行"
    )
    assert resp.status_code == 303, resp.text
    thesis = next(item for item in logged.context.theses.list("alice") if item.code == "600036")
    alice = logged.context.users.get_by_username("alice")
    events = logged.context.theses.theses.events(alice.id, thesis.id)
    assert events[0].note == "低估值银行"


def test_annotation_body_from_raw_utf8_body_survives(logged):
    resp = _post_raw(
        logged,
        "/annotations",
        target_type="symbol",
        target_ref="A:600519",
        body_md="渠道**库存见底**",
        tags="逻辑,放量",
    )
    assert resp.status_code == 200, resp.text
    alice = logged.context.users.get_by_username("alice")
    rows = logged.context.annotations.list_target(alice.id, "symbol", "A:600519")
    assert rows[0].body_md == "渠道**库存见底**"
    assert rows[0].tags == ["逻辑", "放量"]


def test_rule_name_from_raw_utf8_body_survives(logged):
    resp = _post_raw(
        logged,
        "/rules/new",
        kind="stop.fixed",
        name="茅台止损",
        target_type="symbol",
        target_ref="A/600519",
        severity="strong",
        cooldown_min="30",
        param_price="1400",
    )
    assert resp.status_code == 303, resp.text
    alice = logged.context.users.get_by_username("alice")
    names = {rule.name for rule in logged.context.rules_store.list_for(alice.id)}
    assert "茅台止损" in names
