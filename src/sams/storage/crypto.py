"""通道凭据的加密与掩码：全项目唯一知道「密文长什么样」的地方。

为什么整份 JSON 一个 token：逐字段加密看起来更「精细」，代价是字段名单变成存储层的知识
——加一个 `attempts` 字段就要记得把它加进「哪些字段要加密」，漏掉的那一个会以明文躺在库里。
一个 token 覆盖整份 config，就不存在「这个字段忘了」这件事。

`cipher_for` 是自由函数而不是 `Container` 上的方法：bootstrap 只需要
`cipher_for(settings)`，不该为了拿一个加密器去 import `services` 的某个部件——
组装根认识实现没错，但「认识多少」决定了下一个人能在哪里顺手接线。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from sams.core.errors import ValidationError

#: 掩码时最多露出的尾部字符数。露尾巴是为了认得出「是哪一条」（两个 webhook 往往只差
#: 末尾几位），但短值露出来就可能直接可用，所以下面还有一条最低长度。
MASK_TAIL = 4
#: 低于这个长度的值**一个字符都不露**：一个 5 位的 token 露 4 位就是几乎全露。
MASK_MIN_CHARS = 8
MASK_PREFIX = "***"


def key_from_secret(secret_key: str) -> bytes:
    """`secret_key` → Fernet 要的 32 字节 urlsafe-base64 密钥。

    不直接 `secret_key.encode()`：用户的 key 可能是 8 个字符（短）或含非 base64 字符，
    Fernet 会在构造时抛 `ValueError`，那句话读起来像「库坏了」。sha256 折一次，
    长度与字符集就都稳定，而「换 secret_key ⇒ 换通道密钥」这条因果关系是直接的。
    """
    if not secret_key:
        raise ValidationError(
            "缺少 secret_key：通道凭据要加密，没有密钥就不能读也不能写"
            "（请设置环境变量 SAMS_SECRET_KEY）"
        )
    return base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())


class Cipher:
    """一份配置 ↔ 一个 token。`encrypt` 收 Mapping、`decrypt` 回 dict：调用方不需要知道 JSON。"""

    def __init__(self, secret_key: str) -> None:
        self._fernet = Fernet(key_from_secret(secret_key))

    def encrypt(self, config: Mapping[str, Any]) -> str:
        try:
            payload = json.dumps(dict(config), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"通道配置不能序列化：{exc}") from exc
        return self._fernet.encrypt(payload.encode()).decode()

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            raw = self._fernet.decrypt(token.encode())
        except InvalidToken as exc:
            # 这一句要说「密钥不对」而不是「数据坏了」：唯一常见的原因是 secret_key 换过，
            # 而换 key 之后旧凭据在数学上就读不回来——这不是可以用重试救的错
            raise ValidationError(
                "通道凭据解密失败：读它用的密钥与写入时不是同一把"
                "（换过 secret_key 就要重新 `sams channel add` 一次）"
            ) from exc
        loaded = json.loads(raw.decode())
        if not isinstance(loaded, dict):
            raise ValidationError("通道凭据解出来不是一个对象：库里那一行不是本模块写的形状")
        return loaded


def cipher_for(settings) -> Cipher:
    """从 `Settings` 建加密器。只读 `settings.secret_key`，不认任何其它字段。"""
    return Cipher(settings.secret_key)


def mask_secret(value: object) -> str:
    """掩码：短值完全不露，长值最多露 `MASK_TAIL` 个字符。

    露尾巴是为了认得出「这是哪个 webhook」——两家的地址常常只差末尾几位；
    而一个 5 位的 token 露 4 位就等于把凭据抄在屏幕上。所以短值一律不露。
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if len(text) < MASK_MIN_CHARS:
        return MASK_PREFIX
    return MASK_PREFIX + text[-MASK_TAIL:]


def mask_config(config: Mapping[str, Any]) -> dict[str, str]:
    """每个值都掩掉，键名原样保留：屏幕上要能看出「这条通道配了 url 和 token」，
    但不能看出任何一个值（§4.2：admin 只见启用状态，不见明文）。"""
    return {str(key): mask_secret(item) for key, item in config.items()}
