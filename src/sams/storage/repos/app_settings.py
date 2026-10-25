"""`app_settings` 的读写：运行时策略，改完即生效，不重启（D127、§4.2）。

白名单只有 `annotations.team_sharing`（spec §9）：一个开放键值的表会让「随便塞个键」
变成默认习惯，半年后没人记得哪个键还有人读。未知键读写都拒在这里而不是调用方：
第一个不记得校验的调用方不会报错，只会把一个没人读的键当成策略。

读带 5 秒进程内缓存：每次批注读取与每一次推送都要问一遍「共享开着吗」，
而开关是一个礼拜按一次的东西。跨进程的可见延迟最多 5 秒——写路径废掉的是
**本进程**的条目，别的进程靠过期。
"""

from __future__ import annotations

import time

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database

#: 唯一的策略键（§9）。名字住在这里：SQL 里的那个字符串与 Python 里的引用必须是同一份。
TEAM_SHARING = "annotations.team_sharing"
ALLOWED_KEYS = (TEAM_SHARING,)
TEAM_SHARING_VALUES = ("on", "off")
CACHE_S = 5.0


class AppSettingsRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock
        self._cache: dict[str, tuple[float, str]] = {}

    def get(self, key: str, *, default: str) -> str:
        if key not in ALLOWED_KEYS:
            raise ValidationError(f"未知策略键 {key!r}：白名单只有 {list(ALLOWED_KEYS)}")
        moment = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and moment - hit[0] < CACHE_S:
            return hit[1]
        row = self.db.scalar("SELECT value FROM app_settings WHERE key = ?", [key])
        # 行由 0020 的 `INSERT OR IGNORE` 铺：读不到只可能是被删过——按默认值回答，不抛
        value = str(row) if row is not None else default
        self._cache[key] = (moment, value)
        return value

    def set(self, key: str, value: str, *, updated_by: str | None = None) -> str:
        """写一行并废掉本进程的缓存条目。审计（谁改的）落在同一行的 `updated_by`。"""
        if key not in ALLOWED_KEYS:
            raise ValidationError(f"未知策略键 {key!r}：白名单只有 {list(ALLOWED_KEYS)}")
        if key == TEAM_SHARING and value not in TEAM_SHARING_VALUES:
            raise ValidationError(f"{key} 只收 {'/'.join(TEAM_SHARING_VALUES)}（收到 {value!r}）")
        self._cache.pop(key, None)
        self.db.execute(
            "INSERT INTO app_settings (key, value, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            [key, value, self.clock.now_utc(), updated_by],
        )
        return value

    def team_sharing(self) -> bool:
        """团队共享开关：除 `'off'` 外都算开。默认行是迁移铺的，缺行不是错误。"""
        return self.get(TEAM_SHARING, default="on") != "off"
