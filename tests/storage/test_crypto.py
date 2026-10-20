"""通道凭据的加密与掩码。

整份 config 一个 token（不是逐字段）：字段名单是通道的实现细节，会随通道改版变，
而「库里存的字节是密文」这个承诺不该因为某个通道多了两个可选字段就漏一个。
"""

from __future__ import annotations

import pytest

from sams.core.errors import ValidationError
from sams.storage.crypto import cipher_for, mask_config, mask_secret


class SettingsStub:
    def __init__(self, *, secret_key: str) -> None:
        self.secret_key = secret_key


def test_round_trip_keeps_types():
    cipher = cipher_for(SettingsStub(secret_key="a" * 32))
    token = cipher.encrypt({"url": "https://x", "attempts": 3})
    assert isinstance(token, str) and token != "https://x"
    assert cipher.decrypt(token) == {"url": "https://x", "attempts": 3}


def test_two_settings_with_the_same_key_interop():
    a = cipher_for(SettingsStub(secret_key="b" * 32))
    b = cipher_for(SettingsStub(secret_key="b" * 32))
    assert b.decrypt(a.encrypt({"k": 1})) == {"k": 1}


def test_a_different_key_cannot_read_it(settings):
    other = SettingsStub(secret_key="c" * 32)
    token = cipher_for(settings).encrypt({"k": 1})
    with pytest.raises(ValidationError, match="密钥"):
        cipher_for(other).decrypt(token)


def test_empty_secret_key_is_refused():
    with pytest.raises(ValidationError, match="secret_key"):
        cipher_for(SettingsStub(secret_key=""))


def test_mask_never_reveals_more_than_half_the_tail():
    assert mask_secret("https://evil.example/very/long/path") == "***path"
    assert mask_secret("short") == "***"  # 5 字符：露 2 个就等于露了四成，不露
    assert mask_secret("abcdefghij") == "***ghij"  # 10 字符：上限 4，且不超过一半
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


def test_mask_marks_which_keys_are_hidden():
    shown = mask_config({"url": "https://a.example/bot", "token": "abcdefghijklmnop"})
    assert shown["url"].startswith("***") and shown["url"].endswith("bot")
    assert set(shown) == {"url", "token"}
