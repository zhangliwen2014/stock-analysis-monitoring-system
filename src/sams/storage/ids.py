"""ID 生成：uuid4 hex。用户域实体一律应用侧生成主键，避免依赖库的自增语义。"""

from __future__ import annotations

from uuid import uuid4


def new_id() -> str:
    return uuid4().hex
