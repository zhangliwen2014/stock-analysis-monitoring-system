"""批注用例（M4-B §4）：目标归属、标签、正文上限与读取可见性。

写入口已放宽到 team（B2）：闸在 `_gate_team`——策略关时**新写** team 被拒，
保持既有 team 不变不拦（§4.2 说既有行只是对非作者隐藏，不是不能碰）；
非 symbol 的 team 由仓库那条 `_reject_team_on_non_symbol` 拒掉（D121），这里不重复造。

目标归属在这里问各自的仓库（thesis/rule/alert/order），因为仓库只认「这一行是不是
你的」。目标不存在与不属于当前用户统一 `NotFound`：两种回答都告诉探测者同一件事。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sams.core.codes import canonical_code, canonical_market
from sams.core.config import Settings
from sams.core.errors import NotFound, ValidationError
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import (
    ALERT,
    ORDER,
    PRIVATE,
    RULE,
    SYMBOL,
    TARGET_TYPES,
    TEAM,
    THESIS,
    Annotation,
    AnnotationRepo,
)
from sams.storage.repos.app_settings import AppSettingsRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

MAX_TAGS = 10
MAX_TAG_CHARS = 20


def normalize_tags(raw: str | Sequence[str] | None) -> list[str]:
    """逗号分隔或序列 → 去空白、去前导 `#`、大小写不敏感去重（保留第一次的写法）。"""
    if raw is None:
        return []
    parts = raw.split(",") if isinstance(raw, str) else [str(item) for item in raw]
    seen: set[str] = set()
    tags: list[str] = []
    for part in parts:
        text = part.strip()
        if text.startswith("#"):
            text = text[1:].strip()
        text = " ".join(text.split())
        if not text:
            continue
        if len(text) > MAX_TAG_CHARS:
            raise ValidationError(f"单个标签最多 {MAX_TAG_CHARS} 字：{text!r}")
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(text)
    if len(tags) > MAX_TAGS:
        raise ValidationError(f"一条批注最多 {MAX_TAGS} 个标签（收到 {len(tags)} 个）")
    return tags


class AnnotationService:
    def __init__(
        self,
        annotations: AnnotationRepo,
        users: UserRepo,
        symbols: SymbolRepo,
        theses: ThesisRepo,
        rules: RuleRepo,
        alerts: AlertRepo,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        settings: Settings,
        app_settings: AppSettingsRepo,
    ) -> None:
        self.annotations = annotations
        self.users = users
        self.symbols = symbols
        self.theses = theses
        self.rules = rules
        self.alerts = alerts
        self.accounts = accounts
        self.orders = orders
        self.settings = settings
        # 必填而不是默认 None：漏接线要在启动就炸，而不是第一次写 team 时才炸
        self.app_settings = app_settings

    # --- 写 -----------------------------------------------------------------------------

    def create(
        self,
        user_id: str,
        *,
        target_type: str,
        target_ref: str,
        body_md: str,
        tags: str | Sequence[str] | None = (),
        visibility: str = PRIVATE,
    ) -> Annotation:
        if self.users.get(user_id) is None:
            raise ValidationError(f"用户 {user_id} 不存在：批注必须有一个作者")
        ref = self._normalize_ref(target_type, target_ref)
        label = self._target_label(user_id, target_type, ref)
        self._gate_team(visibility, target_type)
        return self.annotations.create(
            user_id,
            target_type=target_type,
            target_ref=ref,
            target_label=label,
            body_md=self._body(body_md),
            tags=normalize_tags(tags),
            visibility=visibility,
        )

    def edit(
        self,
        user_id: str,
        annotation_id: str,
        *,
        body_md: str,
        tags: str | Sequence[str] | None = (),
        expected_updated_at=None,
        visibility: str | None = None,
    ) -> Annotation:
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        wanted = made.visibility if visibility is None else (visibility or made.visibility)
        # 闸只管「从私有翻成团队」这一跳：关策略后保持原样的 team 行还能编辑，
        # 否则人不改一个字都保存不了——§4.2 说的「不删、只是隐藏」就落空了
        if wanted == TEAM and made.visibility != TEAM:
            self._gate_team(wanted, made.target_type)
        return self.annotations.update(
            annotation_id,
            user_id=user_id,
            body_md=self._body(body_md),
            tags=normalize_tags(tags),
            visibility=wanted,
            expected_updated_at=expected_updated_at,
        )

    def pin(self, user_id: str, annotation_id: str, *, pinned: bool) -> Annotation:
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        return self.annotations.set_pinned(annotation_id, user_id=user_id, pinned=pinned)

    def delete(self, user_id: str, annotation_id: str) -> None:
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        self.annotations.soft_delete(annotation_id, user_id=user_id)

    # --- 读 -----------------------------------------------------------------------------

    def get(
        self, viewer_id: str, annotation_id: str, *, team_sharing: bool = False
    ) -> Annotation:
        made = self.annotations.get(annotation_id)
        if made is None or not self._readable(made, viewer_id, team_sharing):
            raise NotFound(f"批注 {annotation_id} 不存在或不可见")
        return made

    def list_target(
        self,
        viewer_id: str,
        target_type: str,
        target_ref: str,
        *,
        team_sharing: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Annotation]:
        ref = self._normalize_ref(target_type, target_ref)
        return self.annotations.list_visible(
            viewer_id,
            team_sharing=team_sharing,
            target_type=target_type,
            target_ref=ref,
            limit=limit,
            offset=offset,
        )

    def list_recent(
        self,
        viewer_id: str,
        *,
        since: datetime,
        team_sharing: bool = False,
        limit: int = 20,
    ) -> list[Annotation]:
        """`/review` 的「本周批注」：可见性闸与面板同一条（仓库的 `list_visible`），
        只是按时间切、不按目标切。复盘要的是「这周写了什么」，不问写在哪个对象上。"""
        return self.annotations.list_visible(
            viewer_id, since=since, team_sharing=team_sharing, limit=limit
        )

    def list_mine(self, viewer_id: str, *, limit: int = 20, offset: int = 0) -> list[Annotation]:
        return self.annotations.list_visible(
            viewer_id, mine_only=True, limit=limit, offset=offset
        )

    def revisions(self, user_id: str, annotation_id: str):
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        return self.annotations.revisions(annotation_id)

    # --- 内部 ---------------------------------------------------------------------------

    def _gate_team(self, visibility: str, target_type: str) -> None:
        """策略关着就不许**新写** team（D127）；非标的 team 由仓库那同一条闸拒（D121）。"""
        if visibility == TEAM and target_type == SYMBOL and not self.app_settings.team_sharing():
            raise ValidationError(
                "团队共享当前已关闭：这条只能先写成私有，开关在管理后台随时能再打开"
            )

    def _readable(self, made: Annotation, viewer_id: str, team_sharing: bool) -> bool:
        if made.user_id == viewer_id:
            return True
        # D121：team 只对 symbol 生效；别的目标上的 team 行一律按私有处理
        return bool(
            team_sharing
            and made.visibility == "team"
            and made.target_type == SYMBOL
        )

    def _body(self, body_md: str) -> str:
        text = (body_md or "").strip()
        if not text:
            raise ValidationError("批注正文不能为空")
        limit = self.settings.annotations.max_body_chars
        if len(text) > limit:
            raise ValidationError(f"批注正文最多 {limit} 字（收到 {len(text)} 字）")
        return text

    def _normalize_ref(self, target_type: str, target_ref: str) -> str:
        if target_type not in TARGET_TYPES:
            raise ValidationError(
                f"未知批注目标 target_type={target_type!r}：只有 {list(TARGET_TYPES)}"
            )
        ref = (target_ref or "").strip()
        if not ref:
            raise ValidationError("target_ref 不能为空：一条批注必须说清它是关于什么的")
        if target_type != SYMBOL:
            return ref
        market, _, code = ref.replace("/", ":").partition(":")
        try:
            market = canonical_market(market)
            code = canonical_code(market, code)
        except ValidationError as exc:
            raise ValidationError(f"标的键要写成 市场:代码（例如 A:600519）：{exc}") from exc
        return f"{market}:{code}"

    def _target_label(self, user_id: str, target_type: str, ref: str) -> str:
        if target_type == SYMBOL:
            market, _, code = ref.partition(":")
            row = self.symbols.get(market, code)
            return str((row or {}).get("name") or code)
        if target_type == THESIS:
            made = self.theses.get(user_id, ref)
            if made is None:
                raise NotFound("观点不存在或不属于当前用户")
            return f"{made.market}:{made.code} 观点"
        if target_type == RULE:
            made = self.rules.get(ref, user_id=user_id)
            if made is None:
                raise NotFound("规则不存在或不属于当前用户")
            return made.name
        if target_type == ALERT:
            made = self.alerts.get(ref, user_id=user_id)
            if made is None:
                raise NotFound("告警不存在或不属于当前用户")
            return made.rule_name or made.symbol_name or f"{made.market}:{made.code} 告警"
        if target_type == ORDER:
            for account in self.accounts.list_for(user_id):
                order = self.orders.order(account.id, ref)
                if order is not None:
                    side = "买入" if order.side == "buy" else "卖出"
                    return f"{side} {order.market}:{order.code} {order.qty:g}@{order.price:g}"
            raise NotFound("成交不存在或不属于当前用户")
        raise ValidationError(f"未知批注目标 target_type={target_type!r}")
