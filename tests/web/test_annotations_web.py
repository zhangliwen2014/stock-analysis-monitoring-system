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


def _rgba_png_bytes() -> bytes:
    """RGBA 模式的 PNG：重编码只给「有 alpha 的图」保留 PNG（services/attachments.py
    的 has_alpha 支路），RGB 会被统一转成 JPEG——下载用例要钉的是 image/png 那条路，
    所以喂带 alpha 的那一种。"""
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(buf, format="PNG")
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


def test_attachment_uploads_are_limited_to_10_per_minute(logged):
    """§10 的第二张卡：带附件的第 11 次要被 10 次/分钟挡下，
    而随后一条不带附件的照过——证明挡住它的是上传卡，不是批注卡。"""
    png = ("files", ("shot.png", _png_bytes(), "image/png"))
    for i in range(10):
        resp = _post_annotation(logged, body=f"带图 {i}", files=[png])
        assert resp.status_code == 200, i
    resp = _post_annotation(logged, body="第 11 张", files=[png])
    assert resp.status_code == 429
    assert "annotations-panel" in resp.text
    resp = _post_annotation(logged, body="不带图")
    assert resp.status_code == 200


def _make_annotation_with_png(logged, filename="截图-首页.png", count=1):
    """走真实写路建一条带图批注，返回 (Annotation, [Attachment])。

    文件名避开空格与 `>`：断言里要拿它比 percent-encoding，少一个变量少一处错。
    """
    resp = _post_annotation(
        logged,
        files=[("files", (filename, _rgba_png_bytes(), "image/png"))] * count,
    )
    assert resp.status_code == 200 and "被拒" not in resp.text
    ctx = logged.context
    alice = _alice(ctx)
    # list_visible 的 ORDER BY created_at DESC：新条目在 [0]
    made = ctx.annotations.list_target(alice.id, "symbol", "A:600519")[0]
    return made, ctx.attachments.list_for(alice.id, made.id)


def test_attachment_download_serves_png_inline_with_rfc5987_name(logged):
    made, atts = _make_annotation_with_png(logged)
    resp = logged.client.get(f"/annotations/{made.id}/attachments/{atts[0].id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    # inline + RFC 5987：中文名原样带出，浏览器里直接看图而不是先落盘
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("inline;")
    assert "filename*=UTF-8''" in disposition
    assert "%E6%88%AA%E5%9B%BE" in disposition  # 「截图」的 percent-encoding
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "private" in resp.headers["cache-control"]


def test_attachment_download_folds_foreign_to_404(logged):
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    made = ctx.annotations.create(
        bob.id, target_type="symbol", target_ref="A:600519", body_md="bob 的图"
    )
    att = ctx.attachments_store.create(
        made.id,
        bob.id,
        filename="b.png",
        media_type="image/png",
        size_bytes=1,
        sha256="0" * 64,
        storage_path="bob/x.png",
        position=0,
    )
    resp = logged.client.get(f"/annotations/{made.id}/attachments/{att.id}")
    assert resp.status_code == 404  # 不可见就是不可见：不回答「文件在不在」


def test_download_of_missing_file_says_data_status_not_permission(logged):
    made, atts = _make_annotation_with_png(logged)
    path = logged.context.attachments.path_of(atts[0])
    hidden = path.with_suffix(".hidden")
    path.rename(hidden)
    try:
        resp = logged.client.get(f"/annotations/{made.id}/attachments/{atts[0].id}")
        assert resp.status_code == 404
        assert "缺失" in resp.text
    finally:
        hidden.rename(path)


def test_panel_renders_thumbnails_and_degrades_missing_file(logged):
    made, atts = _make_annotation_with_png(logged)
    url = f"/annotations/{made.id}/attachments/{atts[0].id}"
    panel = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    assert url in panel.text  # 缩略图与下载是同一个 URL：CSP img-src 'self' 放行
    path = logged.context.attachments.path_of(atts[0])
    hidden = path.with_suffix(".hidden2")
    path.rename(hidden)
    try:
        panel = logged.client.get(
            "/api/partials/annotations",
            params={"target_type": "symbol", "target_ref": "A:600519"},
        )
        assert "附件缺失" in panel.text
        assert url not in panel.text  # 坏图不留 <img>：碎图标比那句话更没信息量
    finally:
        hidden.rename(path)


def test_panel_shows_all_thumbs_within_the_configured_cap(logged):
    """缩略图上限改读配置（max_attachments，默认 5）：编辑器要能勾选到每一张，
    4 张就全是 thumbs——旧「3 张封顶 + 还有 N 张」的语义随之作废。"""
    made, atts = _make_annotation_with_png(logged, count=4)
    panel = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    # 每张附件的 id 出现三次（<a href> 与 <img src> 用 URL，编辑表单的勾选框用裸 id）
    for att in atts:
        assert panel.text.count(att.id) == 3
    assert "还有" not in panel.text  # extra == 0：没有溢出提示
    assert "附件缺失" not in panel.text  # 四张都在磁盘上：missing 不许多报


# --- 编辑批注时增删附件（§4.3/§5.4）-----------------------------------------------------------


def test_edit_can_add_an_attachment(logged):
    """编辑不带新文件、也不勾删除时要照常成功（`remove_attachments: list[str] = Form(default=[])`
    在多分部表单下收 0 个值）——这条钉的是「空提交不被 FastAPI 判 422」。"""
    ctx = logged.context
    uid = _alice(ctx).id
    made = ctx.annotations.create(
        uid, target_type="symbol", target_ref="A:600519", body_md="先没图", tags=()
    )
    panel = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={
            "csrf": logged.csrf,
            "body_md": "先没图",
            "tags": "",
            "updated_at": made.updated_at.isoformat(),
        },
        files={"files": ("shot.png", _rgba_png_bytes(), "image/png")},
    )
    assert panel.status_code == 200
    assert "ann-imgs" in panel.text
    assert len(ctx.attachments_store.list_for(made.id)) == 1


def test_edit_can_remove_a_checked_attachment(logged):
    ctx = logged.context
    made, atts = _make_annotation_with_png(logged)
    att = atts[0]
    path = ctx.attachments.path_of(att)
    assert path.is_file()
    panel = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={
            "csrf": logged.csrf,
            "body_md": made.body_md,
            "tags": "",
            "updated_at": made.updated_at.isoformat(),
            "remove_attachments": att.id,
        },
        # 浏览器「没选文件」直接提交发的就是空文件名分片：这里与它同形
        files=[("files", ("", b""))],
    )
    assert panel.status_code == 200
    assert att.id not in panel.text
    assert "ann-imgs" not in panel.text  # 图区整个没了
    assert ctx.attachments_store.list_for(made.id) == []
    assert not path.exists()
    # 最新 revision 记的是「这次编辑之后」的附件集合：删干净就是空列表
    assert ctx.annotations_store.revisions(made.id)[-1].meta["attachments"] == []


def test_edit_oversize_keeps_everything(logged):
    """尺寸闸在改动之前：图超了正文一个字都不动——和 create 同一条纪律（§10）。"""
    ctx = logged.context
    made = _seed_annotation(ctx, _alice(ctx), body="原样")
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (6 * 1024 * 1024)
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={
            "csrf": logged.csrf,
            "body_md": "改了也不该存",
            "tags": "",
            "updated_at": made.updated_at.isoformat(),
        },
        files=[("files", ("b.png", big, "image/png"))],
    )
    assert resp.status_code == 400 and "超了" in resp.text
    assert ctx.annotations_store.get(made.id).body_md == "原样"
    assert ctx.attachments_store.list_for(made.id) == []


def test_edit_conflict_does_not_touch_attachments(logged):
    ctx = logged.context
    made, atts = _make_annotation_with_png(logged)
    stale = made.updated_at.isoformat()
    ctx.annotations.edit(_alice(ctx).id, made.id, body_md="另一台设备改过")
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={
            "csrf": logged.csrf,
            "body_md": "冲突正文",
            "tags": "",
            "updated_at": stale,
        },
        files=[("files", ("c.png", _rgba_png_bytes(), "image/png"))],
    )
    assert resp.status_code == 409
    assert "在你编辑期间被改过" in resp.text
    # 409 时文件一个都不动：既没加进来，勾选的也没被删
    assert len(ctx.attachments_store.list_for(made.id)) == len(atts)


def test_revision_meta_records_final_attachments(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    made = ctx.annotations.create(
        uid, target_type="symbol", target_ref="A:600519", body_md="先没图", tags=()
    )
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={
            "csrf": logged.csrf,
            "body_md": "补一张",
            "tags": "",
            "updated_at": made.updated_at.isoformat(),
        },
        files={"files": ("shot.png", _rgba_png_bytes(), "image/png")},
    )
    assert resp.status_code == 200
    atts = ctx.attachments_store.list_for(made.id)
    assert len(atts) == 1
    revisions = ctx.annotations_store.revisions(made.id)
    assert revisions[-1].meta["attachments"] == [atts[0].id]


# --- 团队可见写路径（B2：§4.2/D121/D127）---------------------------------------------------


def test_symbol_panel_offers_visibility(logged):
    page = logged.client.get(
        "/api/partials/annotations",
        params={"target_type": "symbol", "target_ref": "A:600519"},
    )
    assert 'name="visibility"' in page.text
    assert "团队可见" in page.text


def test_thesis_panel_hides_visibility(logged):
    """非 symbol 目标：D121——不给选择器，那句「仅自己」的说明替代它。"""
    ctx = logged.context
    alice = _alice(ctx)
    rule = ctx.rules_store.create(
        alice.id,
        kind="price.cross_below",
        name="跌破 10 元",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
        account_id=None,
    )
    page = logged.client.get(
        "/api/partials/annotations",
        params={"target_type": "rule", "target_ref": rule.id},
    )
    assert page.status_code == 200
    # 只认 select：非 symbol 的行里有 `<input type="hidden" name="visibility">`，
    # 断言整个 name 会把「没有选择器」和「有控件」混为一谈
    assert '<select name="visibility">' not in page.text
    assert "仅自己" in page.text


def test_rejected_draft_keeps_team_visibility_selected(logged):
    """被拒草稿重渲染不能悄悄改可见范围：选了「团队可见」、因超大附件被 400 拒掉，
    选择器还得停在 team——否则用户照着原样重提，笔记就存成私有了。"""
    big = b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024 + 1)
    resp = logged.client.post(
        "/annotations",
        data={
            "csrf": logged.csrf,
            "target_type": "symbol",
            "target_ref": "A:600519",
            "body_md": "给团队",
            "visibility": "team",
        },
        files=[("files", ("big.png", big, "image/png"))],
    )
    assert resp.status_code == 400
    assert 'value="team" selected' in resp.text


def test_create_team_flips_badge(logged):
    resp = logged.client.post(
        "/annotations",
        data={
            "csrf": logged.csrf,
            "target_type": "symbol",
            "target_ref": "A:600519",
            "body_md": "给团队",
            "visibility": "team",
        },
    )
    assert resp.status_code == 200
    assert '<span class="badge">团队</span>' in resp.text


def test_create_team_refused_when_policy_off(logged):
    logged.context.app_settings.set("annotations.team_sharing", "off")
    resp = logged.client.post(
        "/annotations",
        data={
            "csrf": logged.csrf,
            "target_type": "symbol",
            "target_ref": "A:600519",
            "body_md": "给团队",
            "visibility": "team",
        },
    )
    assert resp.status_code == 400
    assert "团队共享当前已关闭" in resp.text


def test_team_row_sharing_closed_says_so(logged):
    made = logged.context.annotations.create(
        _alice(logged.context).id,
        target_type="symbol",
        target_ref="A:600519",
        body_md="共享着",
        visibility="team",
    )
    logged.context.app_settings.set("annotations.team_sharing", "off")
    page = logged.client.get(
        "/api/partials/annotations",
        params={"target_type": "symbol", "target_ref": "A:600519"},
    )
    assert "团队共享当前已关闭" in page.text
    assert made.id in page.text  # §4.2：作者仍可见自己那条


# --- /annotations 全局搜索页（§4.5）----------------------------------------------------------


def _create_sym(ctx, user, body, **kw):
    return ctx.annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md=body, **kw
    )


def test_search_requires_login(web_app):
    resp = web_app.client.get("/annotations", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_search_default_scope_is_mine_and_hides_teammates(logged):
    ctx = logged.context
    alice = _alice(ctx)
    mine = _create_sym(ctx, alice, "我的私有笔记")
    bob = ctx.users.create("bob", "correct-horse-battery")
    theirs = _create_sym(ctx, bob, "bob 的团队逻辑", visibility="team")
    page = logged.client.get("/annotations")
    assert page.status_code == 200
    assert mine.id in page.text and "我的私有笔记" in page.text
    assert theirs.id not in page.text and "bob 的团队逻辑" not in page.text
    # 导航入口：批注页从顶栏可达（base.html 插在复盘与设置之间）
    assert '<a href="/annotations">批注</a>' in page.text


def test_search_team_scope_shows_teammates_when_shared(logged):
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    theirs = _create_sym(ctx, bob, "bob 的团队逻辑", visibility="team")
    page = logged.client.get("/annotations", params={"scope": "team"})
    assert theirs.id in page.text and "bob 的团队逻辑" in page.text


def test_search_team_scope_hides_teammate_when_policy_off(logged):
    ctx = logged.context
    alice = _alice(ctx)
    bob = ctx.users.create("bob", "correct-horse-battery")
    mine_team = _create_sym(ctx, alice, "我的团队条", visibility="team")
    theirs = _create_sym(ctx, bob, "bob 的团队条", visibility="team")
    ctx.app_settings.set("annotations.team_sharing", "off")
    page = logged.client.get("/annotations", params={"scope": "team"})
    assert mine_team.id in page.text
    assert theirs.id not in page.text and "bob 的团队条" not in page.text


def test_search_query_does_not_leak_private(logged):
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    _create_sym(ctx, bob, "茅台提价")
    page = logged.client.get("/annotations", params={"q": "提价"})
    assert "茅台提价" not in page.text
    assert "共 0 条" in page.text


def test_search_type_bad_value_says_so(logged):
    """下拉之外的值说明有人在改 URL：悄悄吞掉会教人以为筛到了，要报错但不崩。"""
    page = logged.client.get("/annotations", params={"type": "planet"})
    assert page.status_code == 200
    assert "未知批注目标" in page.text


def test_search_pagination_second_page(logged):
    ctx = logged.context
    alice = _alice(ctx)
    for n in range(25):
        _create_sym(ctx, alice, f"第 {n} 条")
    page = logged.client.get("/annotations", params={"page": "2"})
    assert page.text.count('class="ann-row"') == 5
    assert "共 25 条" in page.text
    # 地址栏手滑不判 400：page=abc 回第 1 页、scope 野值回 mine
    bad = logged.client.get("/annotations", params={"page": "abc", "scope": "galaxy"})
    assert bad.status_code == 200
    assert bad.text.count('class="ann-row"') == 20


def test_search_tag_filter_is_case_insensitive(logged):
    ctx = logged.context
    alice = _alice(ctx)
    made = _create_sym(ctx, alice, "逻辑在线", tags=["logic"])
    page = logged.client.get("/annotations", params={"tag": "LOGIC"})
    assert made.id in page.text and "逻辑在线" in page.text
    miss = logged.client.get("/annotations", params={"tag": "other"})
    assert made.id not in miss.text
