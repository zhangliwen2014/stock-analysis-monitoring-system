"""闭环用例层：把「人对这条告警做了什么」落成带引用的结论（spec §8.2/§8.3）。

状态机的闸（终态不可回退、引用必填、note 长度、`alert_events` 逐动作一行）全部住在
`AlertRepo`，这里一枚都不重复——闸出现两处，先失效的那处会让 JSON 与屏幕上不是同一句
（`cli/alert.py` 模块开头是同一条原则）。这一层补的是仓库看不见的那三样：
引用**存在**且属于本人、`thesis` 与告警标的相符、`trade` 的成交与闭环同事务。

`trade` 是唯一在这里写业务数据的动作（§8.2）：`Database.transaction` 是深度计数的，
`ledger` 与 `alerts.act` 内部的各自事务并进外层这一只——一半成一半败的那种
「库里多了一笔没发生的成交」不允许存在。

「顺手写一条标的批注」（D137）在提交**之后**、best-effort：批注写崩了不 rollback
一次已经成立的闭环，spec §12.1 钉的就是这个方向。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sams.core.errors import StorageError, ValidationError
from sams.services.annotations import AnnotationService
from sams.services.ledger import LedgerService
from sams.storage.db import Database
from sams.storage.repos.alerts import Alert, AlertRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

logger = logging.getLogger("sams.services.review")

#: 成交方向只有这两个（与 `sim_orders.side` 一致）；`hold` 之类是敲错
SIDES = ("buy", "sell")


class ReviewService:
    def __init__(
        self,
        db: Database,
        *,
        alerts: AlertRepo,
        ledger: LedgerService,
        users: UserRepo,
        theses: ThesisRepo,
        rules: RuleRepo,
        watchlists: WatchlistRepo,
        annotations: AnnotationService,
    ) -> None:
        self.db = db
        self.alerts = alerts
        self.ledger = ledger
        self.users = users
        self.theses = theses
        self.rules = rules
        self.watchlists = watchlists
        self.annotations = annotations

    def act(
        self,
        user_id: str,
        alert_id: str,
        *,
        action_kind: str,
        action_ref: str | None = None,
        note: str = "",
        sync_annotation: str | None = None,
    ) -> Alert:
        """闭环一次处置，把「引用存在」的闸补在仓库之外（§8.2 那一列「校验」）。

        `trade` 在这里直接拒：它的引用是**这一次新造的**成交，只有
        `act_with_fill` 能给得出——接住一个手写 order id 就等于承认「口头成交」。
        """
        if action_kind == "trade":
            raise ValidationError(
                "trade 走 act_with_fill：成交与闭环必须同事务写（D125），"
                "这个入口不接「已经成交了」的一句话"
            )
        alert = self._owned(user_id, alert_id)
        ref = (action_ref or "").strip()
        if action_kind == "thesis":
            thesis = self.theses.get(user_id, ref)
            if thesis is None:
                raise ValidationError(
                    f"观点 {ref!r} 不存在或不属于当前用户：关联必须指到你自己那一条"
                )
            if (thesis.market, thesis.code) != (alert.market, alert.code):
                raise ValidationError(
                    f"观点的标的是 {thesis.market}:{thesis.code}，与告警"
                    f" {alert.market}:{alert.code} 不符：那条不是这只票的「当时怎么想的」"
                )
        # 规则可以跨标的（自选展开），§8.2 只要求「本人的」
        if action_kind == "rule" and self.rules.get(ref, user_id=user_id) is None:
            raise ValidationError(
                f"规则 {ref!r} 不存在或不属于当前用户：调整必须指到你自己那一条"
            )
        if action_kind == "watch":
            # 加自选与闭环同事务：败一边就两边都别留，否则屏幕上「已处置」而自选里没有
            with self.db.transaction():
                self.watchlists.add(user_id, alert.market, alert.code)
                moved = self.alerts.act(
                    alert_id, user_id=user_id, action_kind="watch", action_ref=ref, note=note
                )
        else:
            moved = self.alerts.act(
                alert_id, user_id=user_id, action_kind=action_kind, action_ref=ref, note=note
            )
        self._sync_annotation(user_id, moved, sync_annotation)
        return moved

    def act_with_fill(
        self,
        user_id: str,
        alert_id: str,
        *,
        account_id: str,
        side: str,
        qty: float,
        price: float,
        fee: float = 0.0,
        ts: datetime | None = None,
        note: str = "",
        sync_annotation: str | None = None,
    ) -> Alert:
        """成交 + 闭环在同一个事务里落（§8.3）。

        标的与账户来自告警那一行（market/code 直接取列），「一致」这条校验是构造出来的
        而不是查出来的；能挡住的只有非正数量/价格、账户不是你的、告警已是终态。
        A 股 T+1、整手、涨跌停这些**不挡**：模拟账本常在补记真实成交，§6.3 说它们
        只该在表单上警告。
        """
        if side not in SIDES:
            raise ValidationError(f"未知方向 side={side!r}：只有 {list(SIDES)}")
        if qty <= 0 or price <= 0:
            raise ValidationError(
                "数量与价格必须为正：一条 0 股的成交在复盘里回答不了任何事"
            )
        alert = self._owned(user_id, alert_id)
        user = self.users.get(user_id)
        if user is None:
            raise StorageError(f"用户 {user_id} 不存在：成交必须有一个记账的人")
        with self.db.transaction():
            fill = self.ledger.buy if side == "buy" else self.ledger.sell
            order = fill(
                account_id, user.username, alert.market, alert.code,
                qty=qty, price=price, fee=fee, ts=ts,
            )
            moved = self.alerts.act(
                alert_id, user_id=user_id, action_kind="trade", action_ref=order.id, note=note
            )
        self._sync_annotation(user_id, moved, sync_annotation)
        return moved

    # —— 内部 ——

    def _owned(self, user_id: str, alert_id: str) -> Alert:
        """「不存在」与「不是你的」折成一句：id 探测不该有第二种回答。"""
        row = self.alerts.get(alert_id, user_id=user_id)
        if row is None:
            raise StorageError(
                f"告警 {alert_id} 不存在（或不属于 {user_id}）：动作要落在本人那条告警上"
            )
        return row

    def _sync_annotation(self, user_id: str, alert: Alert, text: str | None) -> None:
        """闭环后顺手写一条标的批注（D137）：提交之后、best-effort。

        它不许把一次已经成立的闭环拖回去：批注写崩只记一行 warning。
        正文的预填（告警摘要 + 处置结论）是入口那一层的事，这里只收成品。
        """
        if not (text or "").strip():
            return
        try:
            self.annotations.create(
                user_id,
                target_type="symbol",
                target_ref=f"{alert.market}:{alert.code}",
                body_md=text,
            )
        except Exception as exc:  # noqa: BLE001 — 降级是这条链的契约，见模块开头
            logger.warning(
                "告警 %s 闭环已成立，但同步批注失败（不影响闭环）：%s", alert.id, exc
            )
