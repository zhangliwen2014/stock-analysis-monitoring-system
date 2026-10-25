"""批注的 Markdown 是用户输入：渲染必须先去能力，而不是先渲染再清洗（D122）。

这里钉的是「执行面为零」：原始 HTML、图片语法、危险协议都不能产生可点击/可执行的
元素；表格与代码块要能用；推送给手机的那一句是同一份源码压出来的纯文本。
"""

from __future__ import annotations

from sams.services.markdown import plain_text, render_markdown


def test_raw_html_is_escaped():
    html = render_markdown("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_event_handler_in_html_is_escaped():
    html = render_markdown('<img src=x onerror="alert(1)">')
    assert "<img" not in html
    assert "onerror" in html  # 作为被转义的文本出现，而不是属性
    assert "&lt;img" in html


def test_a_javascript_link_is_plain_text():
    html = render_markdown("[点我](javascript:alert(1))")
    assert "javascript:" not in html
    assert "href" not in html
    assert "点我" in html


def test_a_data_link_is_plain_text():
    html = render_markdown("[证据](data:text/html;base64,PHNjcmlwdD4=)")
    assert "data:" not in html
    assert "href" not in html


def test_a_safe_link_opens_in_a_new_tab_without_a_referrer():
    html = render_markdown("[公告](https://example.com/a)")
    assert 'href="https://example.com/a"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_mailto_is_allowed():
    html = render_markdown("[写信](mailto:a@example.com)")
    assert 'href="mailto:a@example.com"' in html


def test_markdown_images_are_disabled():
    """截图走附件：Markdown 图片会让一张外链图在别人的浏览器里发请求。"""
    html = render_markdown("![日线](https://example.com/x.png)")
    assert "<img" not in html


def test_tables_and_code_blocks_render():
    html = render_markdown("| 项 | 值 |\n| - | - |\n| PE | 8 |\n\n```\nx = 1\n```")
    assert "<table>" in html
    assert "<pre>" in html and "<code>" in html


def test_plain_text_flattens_markup_and_links():
    text = plain_text("## 结论\n\n**渠道**库存见底，[公告](https://example.com)\n\n- 第二行")
    assert text == "结论 渠道库存见底，公告 第二行"


def test_plain_text_truncates_at_one_hundred_twenty_chars():
    text = plain_text("字" * 200)
    assert len(text) == 120
    assert text.endswith("…")


def test_plain_text_is_one_line():
    assert plain_text("第一行\n第二行") == "第一行 第二行"
