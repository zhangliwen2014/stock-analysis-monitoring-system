"""规则的维护面：写之前问一遍「这条规则现在响得起来吗」，改参数时把论上那一列一起改。

`RuleRepo` 校验的是**形状**（`stop.fixed` 要 `price`、`target_type` 得在 core.rules 那五种里，
而 `portfolio` 还另有一拒）。
这一层补**上下文**：用户存不存在、账户是不是他的、通道还在不在、展开出来是不是零个对象。
这四件事仓库层都看不见（`rules` 表上没有一条外键，`_validate` 只查 user_id 非空），而后果是
同一种：一条看起来启用的规则，永远不产出任何一行告警。

第四条不当场拒绝——清单今天空、明天就能加——所以它进 `view` 的 `notes`，屏幕那一行说得出
「它现在为什么没动静」。前两条与通道那条都在写入时拒：那三个是敲错了，重打一遍就对了。

`edit` 是这一层唯一的跨表写入：派生规则的 `price` 与论上那一列必须一起变（§9.2），分开提交
会留下一条盯着「不在论点上的止损线」的规则。所以它开一个事务块（同一个理由见
`Database.transaction` 的注释：现金已扣、持仓未减不是待修的脏数据）。块内先改规则再改论：
`rules.update` 会把合并后的参数重走一遍创建时的校验（它末尾那句 `self._validate(**merged)`），
那样 `write_back` 拿到的是校验过的数：`float("八")` 那一下已经被 `_as_number` 折成了
`ValidationError`（它 catch 的正是那个 `ValueError`），而不是穿过校验、在回写那一跳才抛出来。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import StorageError, ValidationError
from sams.services.rules.derive import write_back
from sams.services.rules.snapshot import RuleData
from sams.storage.db import Database
from sams.storage.repos.channels import ChannelRepo
from sams.storage.repos.ledger import SimAccountRepo
from sams.storage.repos.rules import Rule, RuleRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo


@dataclass(frozen=True)
class RuleView:
    """一条规则 + 它现在能不能响。CLI 打的是这一份，不是裸的 `Rule`。"""

    rule: Rule
    #: `expand()` 出来的对象数。0 是「这一条现在不会响」，不是「坏了」
    targets: int
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditReport:
    rule: Rule
    #: 回写到论上的那几列（空 = 这一改与任何论点无关）
    written_back: dict[str, float]
    thesis_id: str | None


class RuleService:
    """增删改查与启停。求值不在这里（`engine`），派生也不在这里（`derive`）。"""

    def __init__(
        self,
        *,
        rules: RuleRepo,
        theses: ThesisRepo,
        accounts: SimAccountRepo,
        channels: ChannelRepo,
        users: UserRepo,
        data: RuleData,
        db: Database,
        settings: Settings,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.rules = rules
        self.theses = theses
        self.accounts = accounts
        self.channels = channels
        self.users = users
        self.data = data
        self.db = db
        self.settings = settings
        self.clock = clock

    # --- 写 -----------------------------------------------------------------------------

    def add(
        self,
        user_id: str,
        *,
        kind: str,
        name: str,
        target_type: str,
        target_ref: str,
        params: Mapping[str, object] | None = None,
        filters: Mapping[str, object] | None = None,
        account_id: str | None = None,
        thesis_id: str | None = None,
        severity: str | None = None,
        channel_ids: Sequence[str] = (),
        cooldown_min: int | None = None,
        enabled: bool = True,
    ) -> RuleView:
        self._require_user(user_id)
        self._require_account(user_id, account_id)
        self._require_channels(user_id, channel_ids)
        # `thesis_id` 的归属不在这里查：`rules._require_thesis` 已经在做同一件事，
        # 两处各查一份的话，第二处只会把同一句话说两遍
        rule = self.rules.create(
            user_id,
            kind=kind,
            name=name,
            target_type=target_type,
            target_ref=target_ref,
            params=params,
            filters=filters,
            account_id=account_id,
            thesis_id=thesis_id,
            severity=severity,
            channel_ids=channel_ids,
            cooldown_min=(
                self.settings.cooldown_min if cooldown_min is None else int(cooldown_min)
            ),
            # 人在命令行填的价属于此刻的价空间；派生那条路盖的是论上的读数日（derive.py）
            params_on=self.clock.today(),
            enabled=enabled,
        )
        return self.view(rule)

    def edit(
        self,
        user_id: str,
        rule_id: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        params: Mapping[str, object] | None = None,
        filters: Mapping[str, object] | None = None,
        severity: str | None = None,
        channel_ids: Sequence[str] | None = None,
        cooldown_min: int | None = None,
        target_type: str | None = None,
        target_ref: str | None = None,
    ) -> EditReport:
        before = self._require(user_id, rule_id)
        if channel_ids is not None:
            self._require_channels(user_id, channel_ids)
        with self.db.transaction():
            moved = self.rules.update(
                rule_id,
                changed_by=user_id,
                name=name,
                kind=kind,
                params=params,
                filters=filters,
                severity=severity,
                channel_ids=channel_ids,
                cooldown_min=cooldown_min,
                target_type=target_type,
                target_ref=target_ref,
            )
            columns = write_back(before, before.params, moved.params)
            if columns and before.thesis_id:
                # `levels_on` 留空：这是手填的一个数，不是「今天重新量过一遍」
                self.theses.set_levels(
                    user_id,
                    before.thesis_id,
                    actor_id=user_id,
                    now=self.clock.now_utc(),
                    **columns,
                )
        return EditReport(rule=moved, written_back=columns, thesis_id=before.thesis_id)

    def set_enabled(self, user_id: str, rule_id: str, enabled: bool) -> RuleView:
        row = self._require(user_id, rule_id)
        return self.view(self.rules.set_enabled(row.id, bool(enabled), user_id=user_id))

    def remove(self, user_id: str, rule_id: str) -> bool:
        row = self._require(user_id, rule_id)
        return self.rules.remove(row.id, user_id=user_id)

    # --- 读 -----------------------------------------------------------------------------

    def show(self, user_id: str, rule_id: str) -> RuleView:
        return self.view(self._require(user_id, rule_id))

    def list(self, user_id: str, *, enabled_only: bool = False) -> tuple[RuleView, ...]:
        return tuple(
            self.view(row)
            for row in self.rules.list_for(user_id, enabled_only=enabled_only)
        )

    def view(self, rule: Rule) -> RuleView:
        targets = self.data.expand(rule)
        return RuleView(
            rule=rule, targets=len(targets), notes=self._notes(rule, len(targets))
        )

    # --- 那四件事 -------------------------------------------------------------------------

    def _require_user(self, user_id: str) -> None:
        if self.users.get(user_id) is None:
            raise ValidationError(
                f"没有用户 {user_id}：规则必须挂在一个收得到告警的人身上，"
                "否则当场建成功、命中那天在投递那里抛回来"
            )

    def _require_account(self, user_id: str, account_id: str | None) -> None:
        if account_id is None:
            return
        if self.accounts.get(user_id, account_id) is None:
            raise ValidationError(
                f"{user_id} 名下没有账户 {account_id}：持仓类读的是这个人的成本与浮盈，"
                "挂到别人的账户上，报出来的数与他无关"
            )

    def _unusable_channels(self, user_id: str, channel_ids: Sequence[str]) -> list[str]:
        bad: list[str] = []
        for channel_id in channel_ids:
            # 只读掩码视图（`show`）：这一层要的是「还在不在、启不启用」，
            # 而 `credentials()` 是凭据的明文出口，路由那一头才需要它
            node = self.channels.show(channel_id, user_id=user_id)
            if node is None:
                bad.append(f"{channel_id}（不存在，或不属于这个用户）")
            elif not node.enabled:
                bad.append(f"{node.name}（已停用）")
        return bad

    def _require_channels(self, user_id: str, channel_ids: Sequence[str]) -> None:
        bad = self._unusable_channels(user_id, channel_ids)
        if not bad:
            return
        raise ValidationError(
            f"这些通道收不到这条规则的告警：{'、'.join(bad)}。"
            "现在写进去不报错，但每一次命中都只落一条 `no_channel` 记录；而指定过的通道"
            "全部不可用时不会降级到默认路由——那是把 A 群的告警发到 B 群去"
        )

    def _notes(self, rule: Rule, targets: int) -> tuple[str, ...]:
        out: list[str] = []
        if not rule.enabled:
            out.append("已停用：任何一趟求值都不会取用它")
        if targets == 0:
            out.append(self._why_zero(rule))
        elif rule.target_type == "symbol":
            market, _, code = rule.target_ref.partition("/")
            if self.data.symbols.get(market, code) is None:
                # 下一步指日线那条链而不是一条「补主档」的命令：库里没有别的入口写
                # `symbols`，而 `SyncService.sync_daily` 在落 K 线时顺带 upsert 它
                out.append(
                    f"库里没有 {rule.target_ref} 这条标的：读不到现价就不判任何价格类条件，"
                    f"先 `sams sync daily --market {market} --code {code}"
                    " --start 起 --end 止`（那条链会顺带把主档这一行补上）"
                )
        bad = self._unusable_channels(rule.user_id, rule.channel_ids)
        if bad:
            out.append(f"通道不可用：{'、'.join(bad)} ⇒ 命中只落一条 `no_channel` 记录")
        return tuple(out)

    def _why_zero(self, rule: Rule) -> str:
        head = f"展开成零个对象（{rule.target_type}:{rule.target_ref}），这一条不会响："
        if rule.target_type == "watchlist":
            return (
                head
                + f"{rule.user_id} 的自选股清单是空的：等清单里有标的，这一条自己就响"
                "（M3-C 没有 `sams watch` 命令，清单的写入口在 M4 那一头）"
            )
        if rule.target_type == "position":
            return head + f"账户 {rule.account_id} 现在没有持仓（清仓之后静默，不是坏了）"
        if rule.target_type == "thesis":
            # 只说查得到的一句话：`theses` 这一层的写入方从不删行（归档那条 `get` 也返回），
            # 所以「被删了」不是这一句该给的猜测
            return head + "那条论在这一用户名下读不到：`theses` 里没有 (user_id, id) 这一行"
        return head + "这一种的展开方式没有给出任何目标"

    def _require(self, user_id: str, rule_id: str) -> Rule:
        row = self.rules.get(rule_id, user_id=user_id)
        if row is None:
            raise StorageError(f"规则 {rule_id} 不存在（或不属于 {user_id}）")
        return row
