"""推送上下文的 provider：在**发送那一刻**取「接收人可见」的批注（spec §7）。

monitor 不认识 services：它拿到的是装配根注入的一个可调用对象（§12.1 的分层红线）。
取的是接收人（alert.user_id）可见的两路合并：该标的的 symbol 批注（本人私有 +
策略开启时的 team）与告警 `thesis_id` 的 thesis 批注（§7.1、D137）——排序、去重、
取前三住在 `core.notify` 的格式化里，这里只管「把候选摆全」。

一切异常吞成 `None`（D126）：批注查询坏了的代价是推送少三行字，不是手机上
收不到一条止损。系统告警（`SYS`）与没有代码的行返回 `None`——那是
「这条告警不该有上下文」，与「这只票没人写过批注」（`PushContext()`）分得开。
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from sams.core.config import Settings
from sams.core.notify import SYSTEM_MARKET, AnnotationContext, PushContext
from sams.services.markdown import plain_text
from sams.storage.repos.alerts import Alert
from sams.storage.repos.annotations import SYMBOL, THESIS, AnnotationRepo
from sams.storage.repos.app_settings import AppSettingsRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

logger = logging.getLogger("sams.services.annotation_context")

#: 每一路取几条候选：最终的截断在格式化那一层（3 条），这里保证
#: 「每一路的置顶 + 最新」都进得了候选池
CANDIDATES_PER_SOURCE = 3


class AnnotationContextProvider:
    def __init__(
        self,
        annotations: AnnotationRepo,
        users: UserRepo,
        theses: ThesisRepo,
        settings_store: AppSettingsRepo,
        settings: Settings,
    ) -> None:
        self.annotations = annotations
        self.users = users
        self.theses = theses
        self.settings_store = settings_store
        self.settings = settings

    def __call__(self, alert: Alert) -> PushContext | None:
        try:
            return self._build(alert)
        except Exception as exc:  # noqa: BLE001 — 降级是这个函数的契约，异常类型不在名单里也一样
            logger.warning("告警 %s 取批注上下文失败，按无上下文发送：%s", alert.id, exc)
            return None

    def _build(self, alert: Alert) -> PushContext | None:
        if alert.market == SYSTEM_MARKET or not alert.code:
            return None
        team = self.settings_store.team_sharing()
        candidates = list(
            self.annotations.list_visible(
                alert.user_id,
                team_sharing=team,
                target_type=SYMBOL,
                target_ref=f"{alert.market}:{alert.code}",
                limit=CANDIDATES_PER_SOURCE,
            )
        )
        # thesis 只有「接收人读得到这条 thesis」才展开：别人的 thesis 连存在都不说（D121）
        readable_thesis = (
            alert.thesis_id is not None
            and self.theses.get(alert.user_id, alert.thesis_id) is not None
        )
        if readable_thesis:
            candidates += list(
                self.annotations.list_visible(
                    alert.user_id,
                    team_sharing=team,
                    target_type=THESIS,
                    target_ref=alert.thesis_id,
                    limit=CANDIDATES_PER_SOURCE,
                )
            )
        base = self.settings.server.public_base_url
        return PushContext(
            items=tuple(self._to_context(alert.user_id, made) for made in candidates),
            link=f"{base}/alerts/{alert.id}" if base else "",
        )

    def _to_context(self, viewer_id: str, made) -> AnnotationContext:
        if made.user_id == viewer_id:
            label = "我"
        else:
            author = self.users.get(made.user_id)
            label = "未知作者" if author is None else (author.display_name or author.username)
        zone = ZoneInfo(self.settings.server.display_tz)
        return AnnotationContext(
            annotation_id=made.id,
            created_at=made.created_at,
            moment_label=made.created_at.astimezone(zone).strftime("%m-%d %H:%M"),
            author_label=label,
            body_text=plain_text(made.body_md),
            tags=tuple(made.tags),
            pinned=made.pinned,
        )
