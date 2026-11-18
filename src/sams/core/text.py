"""字符串编码的判据：一份写入口用，一份存量修复用（R3-26 / D360）。

`starlette` 对 `application/x-www-form-urlencoded` 的字段值写死 `decode("latin-1")`
（HTTP 的历史默认），Content-Type 里的 charset 它根本不看。裸 UTF-8 客户端因此把
中文交成一串「latin-1 看 UTF-8 字节」的字符；浏览器一律 percent-encode，不会经过这里。
"""

from __future__ import annotations


def repair_mojibake(value: str) -> str:
    """把 latin-1 读出来的裸 UTF-8 字节折回正确字符；折不动就原样返回。

    整串能编回 latin-1（每字符 < U+0100）且那些字节是**合法** UTF-8 才折：
    真中文（> U+00FF）会撞 `UnicodeEncodeError` 原样返回；真 latin-1 的裸字节
    （如 lone 0xE9）过不了 UTF-8 校验，也原样返回。不猜、不兜底。
    """
    if value.isascii():
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
