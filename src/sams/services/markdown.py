"""服务端 Markdown：去能力之后再渲染（D122）。

`html=False` 让原始 HTML 一律转义；`image` 规则被禁用（截图走附件）；链接只放行
http/https/mailto，其余协议把 `link_open/link_close` 换成空文本——**文本保留、链接消失**，
不会出现半截 `</a>`。推送用的纯文本从同一棵 token 树抽取，渲染与摘要因此不会两套口径。
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

#: 只有这三种协议能变成可点击的东西。相对链接也不放行：批注里没有「当前页面」，
#: 一个 `/admin` 会让读者以为这是应用自己的入口。
_SAFE_LINK = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)

PINNED_LIMIT = 120

def _accept_any_scheme(url: str) -> bool:
    """让所有协议都先解析成链接，再由 `_sanitize_links` 统一裁决。

    markdown-it 默认会把 `javascript:`/`data:` 直接留成字面量源码，危险协议因此
    逃过 token 层面的清洗；先放行再中和，才能保证「文本保留、链接消失」。
    """
    return True


_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_MD.enable("table")
_MD.enable("strikethrough")
_MD.disable("image")
_MD.validateLink = _accept_any_scheme


def _neutralize(token) -> None:
    """把一个链接标记改成空文本：嵌套的文字照常渲染，标签与属性都不留。"""
    token.type = "text"
    token.tag = ""
    token.nesting = 0
    token.attrs = {}
    token.content = ""
    token.markup = ""


def _sanitize_links(tokens) -> None:
    for token in tokens:
        children = token.children or ()
        stack: list[bool] = []
        for child in children:
            if child.type == "link_open":
                href = child.attrGet("href") or ""
                unsafe = not _SAFE_LINK.match(href)
                stack.append(unsafe)
                if unsafe:
                    _neutralize(child)
                else:
                    child.attrSet("target", "_blank")
                    child.attrSet("rel", "noopener noreferrer")
            elif child.type == "link_close":
                unsafe = stack.pop() if stack else False
                if unsafe:
                    _neutralize(child)


def render_markdown(source: str) -> str:
    """渲染成 HTML 片段。任何输入都不该抛：读不懂的部分最差也只是文本。"""
    tokens = _MD.parse(source or "")
    _sanitize_links(tokens)
    return _MD.renderer.render(tokens, _MD.options, {})


def plain_text(source: str, *, limit: int = PINNED_LIMIT) -> str:
    """推送与摘要用的一行纯文本：标题/段落/列表项/代码块取文字，链接取链接文字。"""
    parts: list[str] = []
    for token in _MD.parse(source or ""):
        if token.type == "inline":
            segment: list[str] = []
            for child in token.children or ():
                if child.type in ("text", "code_inline"):
                    segment.append(child.content)
                elif child.type in ("softbreak", "hardbreak"):
                    segment.append(" ")
            parts.append("".join(segment))
        elif token.type in ("fence", "code_block"):
            parts.append(token.content)
    text = " ".join(" ".join(parts).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
