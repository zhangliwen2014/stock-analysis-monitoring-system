"""批注用例（M4-B §4）：目标归属、标签、正文上限与读取可见性。

B1 只开私有批注：`team` 的两列已经在 schema 里，但写入口不给它开口子——UI 没开的
开关比一个能写进去却没人看得见的值诚实。B2 接入策略后由同一个服务放宽写入口。

目标归属在这里问各自的仓库（thesis/rule/alert/order），因为仓库只认「这一行是不是
你的」。目标不存在与不属于当前用户统一 `NotFound`：两种回答都告诉探测者同一件事。
"""

from __future__ import annotations

from collections.abc import Sequence

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
    THESIS,
    Annotation,
    AnnotationRepo,
)
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

    # --- 写 -----------------------------------------------------------------------------

    def create(
        self,
        user_id: str,
        *,
        target_type: str,
        target_ref: str,
        body_md: str,
        tags: str | Sequence[str] | None = (),
    ) -> Annotation:
        if self.users.get(user_id) is None:
            raise ValidationError(f"用户 {user_id} 不存在：批注必须有一个作者")
        ref = self._normalize_ref(target_type, target_ref)
        label = self._target_label(user_id, target_type, ref)
        return self.annotations.create(
            user_id,
            target_type=target_type,
            target_ref=ref,
            target_label=label,
            body_md=self._body(body_md),
            tags=normalize_tags(tags),
            visibility=PRIVATE,
        )

    def edit(
        self,
        user_id: str,
        annotation_id: str,
        *,
        body_md: str,
        tags: str | Sequence[str] | None = (),
        expected_updated_at=None,
    ) -> Annotation:
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        return self.annotations.update(
            annotation_id,
            user_id=user_id,
            body_md=self._body(body_md),
            tags=normalize_tags(tags),
            visibility=made.visibility,
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
