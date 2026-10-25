"""批注面板（读取路）：fragment、可见性、Markdown 安全渲染、目标校验。"""

from __future__ import annotations

import polars as pl


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
