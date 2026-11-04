"""「回到刚才那一屏」的两块料：这一屏的 URL，和一条只肯留在本站的返回地址。

整站只有一处守门（`local_back`）。分叉成两份的后果不是报错，是某天的「返回」
做成一条能跳出站外的链接——一处放宽（比如允许带 query），另一处没跟上。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.requests import Request


def local_back(raw: str | None) -> str:
    """`back` 只收本站路径：它会原样写进 href，协议、`//host` 与反斜杠都是把人送出站外的写法。

    不收就空着——少一条返回链接，好过把「返回」做成一扇跳去别人站点的门。
    """
    value = (raw or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return ""
    if any(ord(ch) < 32 for ch in value):
        return ""
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return ""
    return value


def page_url(request: Request) -> str:
    """这一屏的 `path?query`，原样（未编码）——列表出链拿它 urlencode 进 `back=`。

    带 query 是必需的：`/alerts?status=all&page=2` 退回来得是同一张筛过的表，
    裸 `/alerts` 会把人送回默认「未读」那一屏，等于「返回」按了一半。
    """
    url = request.url
    return f"{url.path}?{url.query}" if url.query else url.path
