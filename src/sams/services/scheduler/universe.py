"""一条盘后链该覆盖哪些对象、该换哪些币种对。

并集而不是「按用户分开跑」：采集是公共资源，同一只票被两个人自选，没有理由拉两次。
对象来源有四种：自选股、持仓、启用的规则目标、在跟的（opened/tracking）观点。
停用的规则与观察中的观点不拉数据：它们不会产生任何读写，拉回来的数没人用。

**用户状态不过滤**：这是数据采集，不是投递。今天停用的用户明天启用，库里该已经有数。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sams.core.codes import canonical_code, canonical_market
from sams.core.config import Settings
from sams.core.errors import ValidationError
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.theses import OPENED, TRACKING, ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

if TYPE_CHECKING:
    from sams.services.rules.snapshot import RuleData

logger = logging.getLogger("sams.services.scheduler.universe")


@dataclass(frozen=True)
class Universe:
    #: market → 去重并排好序的代码；配置里的每个市场都有一格（可能是空元组）
    by_market: Mapping[str, tuple[str, ...]]
    #: (原币, 账户报告币)，只留两者不同的对
    fx_pairs: tuple[tuple[str, str], ...]

    def codes(self, market: str) -> tuple[str, ...]:
        return self.by_market.get(canonical_market(market), ())


class UniverseBuilder:
    def __init__(
        self,
        *,
        users: UserRepo,
        watchlists: WatchlistRepo,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        rules: RuleRepo,
        theses: ThesisRepo,
        data: RuleData,
        settings: Settings,
    ) -> None:
        self.users = users
        self.watchlists = watchlists
        self.accounts = accounts
        self.orders = orders
        self.rules = rules
        self.theses = theses
        self.data = data
        self.settings = settings

    def build(self) -> Universe:
        found: set[tuple[str, str]] = set()
        pairs: set[tuple[str, str]] = set()
        for item in self.watchlists.list_all():
            found.add(_key(item.market, item.code))

        for user in self.users.list_users():
            for account in self.accounts.list_for(user.id):
                held = [row for row in self.orders.positions(account.id) if row.qty > 0]
                found.update(_key(row.market, row.code) for row in held)
                currencies = {row.currency for row in held}
                currencies |= {
                    currency
                    for currency, amount in self.accounts.cash_balances(account.id).items()
                    if amount
                }
                for currency in currencies:
                    if currency and currency != account.report_currency:
                        pairs.add((currency, account.report_currency))

            for rule in self.rules.list_for(user.id, enabled_only=True):
                try:
                    targets = self.data.expand(rule)
                except ValidationError as exc:
                    # 跳过这一条，继续收其余对象：`RuleData.expand` 只在规则自身的形状
                    # 不成时抛 ValidationError（持仓类没绑账户、target_type 不认、
                    # target_ref 的市场段或代码段为空），坏一条不该让整晚的采集停摆
                    logger.warning("规则 %s 展开失败，跳过：%s", rule.id, exc)
                    continue
                found.update(_key(target.market, target.code) for target in targets)

            for status in (OPENED, TRACKING):
                for thesis in self.theses.list(user.id, status=status):
                    found.add(_key(thesis.market, thesis.code))

        markets = set(self.settings.markets) | {market for market, _ in found}
        by_market = {
            market: tuple(sorted(code for found_market, code in found if found_market == market))
            for market in sorted(markets)
        }
        return Universe(by_market=by_market, fx_pairs=tuple(sorted(pairs)))


def _key(market: str, code: str) -> tuple[str, str]:
    normalized = canonical_market(market)
    return normalized, canonical_code(normalized, code)
