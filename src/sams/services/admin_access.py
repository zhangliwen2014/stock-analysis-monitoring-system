"""break-glass 用例（D139）：单条授权、限时限次、理由 4–200 字。

时长与次数取自 settings.annotations（治理参数：放宽靠改配置、每次改动留痕，
数字不写死在这一层）；「至少 4 个字」则是本层固定底线——spec 钉它，不给调。
规则全在这一层，仓库只存行：审计的「能不能批」得能被没有 DB 的单测问出来。
不做批量：一屏一条的授权，才是「确需查看」四个字的形状。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import Conflict, NotFound, ValidationError
from sams.storage.repos.admin_access import AdminAccess, AdminAccessRepo
from sams.storage.repos.annotations import AnnotationRepo

MIN_REASON_CHARS = 4
#: 理由的下限与上限都来自 spec §6.3/D139（M4-B spec:341）的「4–200 字」：
#: 上限不是排版偏好——审计里那一格要答的是「为什么看」，贴日志全文进来既读不出
#: 意图，也会把 admin 页的最近授权表撑坏。
MAX_REASON_CHARS = 200


class BreakGlassService:
    def __init__(
        self,
        logs: AdminAccessRepo,
        annotations: AnnotationRepo,
        settings: Settings,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.logs = logs
        self.annotations = annotations
        # 必填而不是默认：漏接线在启动就炸；时长/次数只有一个出处（settings），别再抄一份
        self.settings = settings
        self.clock = clock

    def request_access(
        self, admin_user_id: str, annotation_id: str, reason: str
    ) -> AdminAccess:
        text = (reason or "").strip()
        if len(text) < MIN_REASON_CHARS:
            raise ValidationError(
                f"理由至少 {MIN_REASON_CHARS} 个字：写下为什么要看这条私有批注——它会进审计"
            )
        if len(text) > MAX_REASON_CHARS:
            # 消息点名上限：被截了字数的人要知道该删到多少，而不是「再短点」
            raise ValidationError(
                f"理由最多 {MAX_REASON_CHARS} 个字（收到 {len(text)} 字）："
                "审计要的是为什么要看这一条，不是日志全文"
            )
        # 墓碑也要能问得到：默认 get 把 purged/deleted 行折成 None，那条「只剩墓碑」的
        # Conflict 就永远走不到——include_deleted 是这道判断的前提
        row = self.annotations.get(annotation_id, include_deleted=True)
        if row is None:
            raise NotFound("批注不存在：break-glass 只对真实存在的那一条生效")
        if row.purged_at is not None:
            raise Conflict("正文已被清除：这条批注只剩墓碑，没有可读的东西")
        max_per_hour = self.settings.annotations.break_glass_max_per_hour
        now = self.clock.now_utc()
        used = self.logs.count_since(admin_user_id, since=now - timedelta(hours=1))
        if used >= max_per_hour:
            raise Conflict(
                f"本小时已授权 {used} 次：每人每小时最多 {max_per_hour} 条——"
                "批量排查请把问题写成一条批注让作者自己贴出来"
            )
        return self.logs.grant(
            admin_user_id=admin_user_id,
            annotation_id=annotation_id,
            owner_user_id=row.user_id,
            reason=text,
            granted_until=now
            + timedelta(minutes=self.settings.annotations.break_glass_minutes),
        )

    def can_read(
        self, admin_user_id: str, annotation_id: str, *, now: datetime | None = None
    ) -> bool:
        moment = now or self.clock.now_utc()
        return self.logs.active(admin_user_id, annotation_id, now=moment) is not None
