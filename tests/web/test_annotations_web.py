"""批注面板与写入路：fragment、可见性、Markdown 安全渲染、目标校验、附件与并发。"""

from __future__ import annotations

import io

import polars as pl
from PIL import Image


def _alice(ctx):
    return ctx.users.get_by_username("alice")


def _seed_annotation(ctx, user, *, body="**渠道**库存见底", tags=("逻辑",)):
    return ctx.annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md=body, tags=list(tags)
    )


def test_panel_renders_visible_annotations_as_fragment(logged):
    ctx = logged.context
    _seed_annotation(ctx, _alice(ctx))
    resp = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    assert resp.status_code == 200
    # Markdown 源码渲染成 HTML：粗体活着（正文里源码星号已被 render_markdown 折掉，
    # 但编辑态 textarea 原样回填 body_md，所以整块 fragment 仍见得到 `**渠道**`——不断言它缺席）
    assert "<strong>渠道</strong>" in resp.text
    # fragment 是裸的：不套 base，否则 swap 进容器就成了画中画
    assert "<nav" not in resp.text
    assert "逻辑" in resp.text  # 标签
    assert "我" in resp.text  # 自己的批注作者位显示「我」，与推送同一口径


def test_panel_requires_a_known_target(logged):
    resp = logged.client.get("/api/partials/annotations", params={"target_type": "symbol"})
    assert resp.status_code == 400
    resp = logged.client.get(
        "/api/partials/annotations", params={"target_type": "planet", "target_ref": "x"}
    )
    assert resp.status_code == 400


def test_panel_hides_raw_html_and_images(logged):
    """D122 的 web 侧证：库里存的源码在 fragment 里也只能长成转义后的样子。"""
    ctx = logged.context
    user = _alice(ctx)
    ctx.annotations.create(
        user.id,
        target_type="symbol",
        target_ref="A:600519",
        body_md='<img src=x onerror=alert(1)> <script>bad()</script> ![pic](http://x/y.png)',
    )
    resp = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    assert "<script>bad()</script>" not in resp.text
    assert "onerror=alert(1)" in resp.text  # 转义后作为文本还在——源码不丢
    assert 'onerror="alert' not in resp.text


def test_panel_hides_other_users_private_notes(logged):
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    ctx.annotations.create(
        bob.id, target_type="symbol", target_ref="A:600519", body_md="bob 的私有观点"
    )
    resp = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    assert "bob 的私有观点" not in resp.text


def test_symbol_page_embeds_the_panel_container(logged):
    """嵌入是一行 htmx 容器：fragment 没加载也不白屏。"""
    ctx = logged.context
    # 个股页认主档：没有这只票就是 404，先按 `SymbolRepo.upsert` 的 DataFrame 契约装进去
    ctx.symbols.upsert(
        pl.DataFrame(
            {"market": ["A"], "code": ["600519"], "name": ["贵州茅台"], "currency": ["CNY"]}
        )
    )
    page = logged.client.get("/symbol/A/600519")
    assert page.status_code == 200
    assert "target_type=symbol" in page.text and "target_ref=A:600519" in page.text


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    return buf.getvalue()


def _post_annotation(logged, *, body="今天放量，逻辑没变", files=None):
    """multipart 建批注的公共入口：`files=None` 也发一枚空文件名分片来强制 multipart，
    端点按「文件名为空」丢掉它——与浏览器不选文件直接提交的形状一致。"""
    data = {
        "csrf": logged.csrf,
        "target_type": "symbol",
        "target_ref": "A:600519",
        "body_md": body,
        "tags": "逻辑, 放量",
    }
    if files is None:
        files = [("files", ("", b""))]
    return logged.client.post("/annotations", data=data, files=files)


def test_create_writes_annotation_and_rerenders_panel(logged):
    resp = _post_annotation(logged, body="渠道**库存见底**")
    assert resp.status_code == 200
    assert "<strong>库存见底</strong>" in resp.text
    ctx = logged.context
    alice = _alice(ctx)
    rows = ctx.annotations.list_target(alice.id, "symbol", "A:600519")
    assert len(rows) == 1
    assert rows[0].tags == ["逻辑", "放量"]


def test_create_rejects_oversized_file_before_writing_the_annotation(logged):
    """§10 的次序闸：尺寸在**建批注之前**判——人都传完才说「批注存了、图没了」
    等于让草稿断在一半。"""
    big = b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024 + 1)
    resp = _post_annotation(logged, files=[("files", ("big.png", big, "image/png"))])
    assert resp.status_code == 400
    assert "5 MB" in resp.text
    ctx = logged.context
    rows = ctx.annotations.list_target(_alice(ctx).id, "symbol", "A:600519")
    assert rows == []  # 一条没写：被拒的那次不留半份草稿
    assert "今天放量" in resp.text  # 原文回填：刚敲的正文还在 textarea 里


def test_create_keeps_annotation_but_names_each_rejected_file(logged):
    """批注成立之后才拦下的附件（内容不是图）不回滚批注：说清「哪张、为什么」。"""
    resp = _post_annotation(
        logged, files=[("files", ("note.gif", b"GIF89a-fake", "image/gif"))]
    )
    assert resp.status_code == 200
    assert "批注已保存，但这些附件被拒" in resp.text
    assert "note.gif" in resp.text
    ctx = logged.context
    rows = ctx.annotations.list_target(_alice(ctx).id, "symbol", "A:600519")
    assert len(rows) == 1


def test_create_stores_a_real_png_attachment(logged):
    resp = _post_annotation(
        logged, files=[("files", ("shot.png", _png_bytes(), "image/png"))]
    )
    assert resp.status_code == 200
    assert "被拒" not in resp.text
    ctx = logged.context
    made = ctx.annotations.list_target(_alice(ctx).id, "symbol", "A:600519")[0]
    atts = ctx.attachments.list_for(_alice(ctx).id, made.id)
    assert len(atts) == 1
    assert atts[0].filename == "shot.png"


def test_edit_with_fresh_stamp_saves(logged):
    ctx = logged.context
    alice = _alice(ctx)
    made = _seed_annotation(ctx, alice, body="旧正文")
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={
            "csrf": logged.csrf,
            "body_md": "新正文**带强调**",
            "tags": "更正",
            "updated_at": made.updated_at.isoformat(),
        },
    )
    assert resp.status_code == 200
    assert "新正文" in resp.text and "<strong>带强调</strong>" in resp.text


def test_edit_with_stale_stamp_rerenders_panel_with_409(logged):
    ctx = logged.context
    alice = _alice(ctx)
    made = _seed_annotation(ctx, alice, body="v1")
    stale = made.updated_at.isoformat()
    ctx.annotations.edit(alice.id, made.id, body_md="v2 由另一台设备改过")
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={"csrf": logged.csrf, "body_md": "v3", "tags": "", "updated_at": stale},
    )
    assert resp.status_code == 409
    assert "在你编辑期间被改过" in resp.text
    assert "v2 由另一台设备改过" in resp.text  # 面板重渲染：屏幕上看到的是最新的


def test_pin_and_delete_roundtrip(logged):
    ctx = logged.context
    alice = _alice(ctx)
    made = _seed_annotation(ctx, alice)
    resp = logged.client.post(
        f"/annotations/{made.id}/pin", data={"csrf": logged.csrf, "pinned": "1"}
    )
    assert resp.status_code == 200 and "置顶" in resp.text
    resp = logged.client.post(f"/annotations/{made.id}/delete", data={"csrf": logged.csrf})
    assert resp.status_code == 200
    assert "还没有批注" in resp.text  # empty_note（§6.2：删除是软删，面板立刻隐身）
    assert ctx.annotations.list_target(alice.id, "symbol", "A:600519") == []


def test_edit_foreign_annotation_is_404(logged):
    """别人的批注没有第二种回答：404，与「不存在」同一句（服务层的折法搬上来）。"""
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    made = ctx.annotations.create(
        bob.id, target_type="symbol", target_ref="A:600519", body_md="bob 的私有"
    )
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={
            "csrf": logged.csrf,
            "body_md": "改",
            "tags": "",
            "updated_at": made.updated_at.isoformat(),
        },
    )
    assert resp.status_code == 404


def test_preview_returns_rendered_html(logged):
    resp = logged.client.post(
        "/annotations/preview",
        data={"csrf": logged.csrf, "body_md": "**粗** 与 <script>bad()</script>"},
    )
    assert resp.status_code == 200
    assert "<strong>粗</strong>" in resp.text
    assert "<script>bad()</script>" not in resp.text


def test_create_is_limited_to_30_per_minute(logged):
    for i in range(30):
        resp = _post_annotation(logged, body=f"第 {i} 条")
        assert resp.status_code == 200, i
    resp = _post_annotation(logged, body="第 31 条")
    assert resp.status_code == 429
    assert "30 秒" in resp.text or "秒后" in resp.text
    # 429 也回面板本体（htmx swap 的是面板容器），不是 error.html
    assert "annotations-panel" in resp.text
