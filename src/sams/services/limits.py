"""组合限额（S4-E §7）：单票/市场/行业权重与当前回撤，四条线各算各的。

三条口径决定这一层会不会骗人：

* **只判用户自己设的线**。没有自定义线时给出同类的参考线（单票 20%、行业 40%、
  回撤 20%），标成「参考」不算违规——默认值替用户做决定，和没有默认值一样糟；
* **算不出来就说算不出来**。缺价、缺汇率、缺行业、净值有缺口时该条是「无法判定」
  且 `actual=None`，不拿一个偏小的分母算出一个看起来正常的小数；
* **当前回撤不是区间最大回撤**。早先那段更深的坑若已部分修复，拿它报超限就是
  让用户为一个已经过去的状态动作。

权限半径与别处一致：找账户走 `accounts.get(user.id, …)`，红线按 `user_id` 过滤，
别人的账户与别人的线在这里既读不到也删不掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_market
from sams.core.config import Settings
from sams.core.errors import StorageError, ValidationError
from sams.core.industry import normalize_industry
from sams.services.actors import resolve_actor
from sams.services.exposure import HoldingValue, exposure
from sams.services.industry import IndustryService
from sams.services.nav import NAV_WINDOW_DAYS, NavSeries, NavService
from sams.services.portfolio import PortfolioService
from sams.storage.repos.ledger import SimAccountRepo
from sams.storage.repos.limits import LimitRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.users import UserRepo

SINGLE = "single"
MARKET = "market"
INDUSTRY = "industry"
DRAWDOWN = "drawdown"
KINDS = (SINGLE, MARKET, INDUSTRY, DRAWDOWN)
KIND_LABELS = {SINGLE: "单票", MARKET: "市场", INDUSTRY: "行业", DRAWDOWN: "回撤"}

#: 没有自定义线时的参考值：灰色，不算违规（§7）
DEFAULT_SINGLE = 0.20
DEFAULT_INDUSTRY = 0.40
DEFAULT_DRAWDOWN = 0.20

BREACH = "breach"
OK = "ok"
UNKNOWN = "unknown"
REFERENCE = "reference"

DRAWDOWN_LABEL = "净值回撤"


@dataclass(frozen=True)
class LimitRow:
    kind: str
    scope: str
    label: str
    threshold: float
    #: None = 算不出来；0.0 是一个真实的权重，两者不能混
    actual: float | None
    status: str
    custom: bool
    limit_id: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class LimitReport:
    account_id: str
    rows: tuple[LimitRow, ...]

    @property
    def breaches(self) -> tuple[LimitRow, ...]:
        return tuple(row for row in self.rows if row.status == BREACH)

    @property
    def undetermined(self) -> tuple[LimitRow, ...]:
        return tuple(row for row in self.rows if row.status == UNKNOWN)


class LimitService:
    def __init__(
        self,
        limits: LimitRepo,
        users: UserRepo,
        accounts: SimAccountRepo,
        portfolio: PortfolioService,
        industry: IndustryService,
        nav: NavService,
        symbols: SymbolRepo,
        settings: Settings,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.limits = limits
        self.users = users
        self.accounts = accounts
        self.portfolio = portfolio
        self.industry = industry
        self.nav = nav
        self.symbols = symbols
        self.settings = settings
        self.clock = clock

    # --- 读侧 ---------------------------------------------------------------------------

    def evaluate(self, username: str, account_id: str, *, prices=None) -> LimitReport:
        user = resolve_actor(self.users, username)
        account = self.accounts.get(user.id, account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在或不属于用户 {user.username}")

        positions = self.portfolio.positions(username, account_id, prices=prices)
        summary = self.portfolio.summary(username, account_id, prices=prices)
        holdings = [
            HoldingValue(
                market=row.market,
                code=row.code,
                name=self._name(row.market, row.code),
                currency=row.currency,
                value=row.pnl.market_report,
                industry=self.industry.industry_of(row.market, row.code),
            )
            for row in positions
            if row.pnl is not None
        ]
        cash = summary.total_report - sum(item.value for item in holdings)
        book = exposure(holdings, cash=cash)
        partial = any(row.pnl is None for row in positions) or bool(summary.notes)
        weight_gap = self._weight_gap(book, partial)
        industry_gap = self._industry_gap(book)
        drawdown_actual, drawdown_gap = self._drawdown(account_id)

        custom = self.limits.list_for(user.id, account_id=account.id)
        rows: list[LimitRow] = []
        for kind in KINDS:
            mine = [limit for limit in custom if limit.kind == kind]
            if mine:
                for limit in mine:
                    rows.append(
                        self._custom_row(
                            limit,
                            book=book,
                            weight_gap=weight_gap,
                            industry_gap=industry_gap,
                            drawdown=(drawdown_actual, drawdown_gap),
                        )
                    )
            else:
                reference = self._reference_row(
                    kind,
                    book=book,
                    weight_gap=weight_gap,
                    industry_gap=industry_gap,
                    drawdown=(drawdown_actual, drawdown_gap),
                )
                if reference is not None:
                    rows.append(reference)
        return LimitReport(account_id=account.id, rows=tuple(rows))

    def breach_lines(self, username: str, market: str) -> tuple[str, ...]:
        """摘要尾行的违规条目：一份摘要只说本市场账户的事，别的市场不在这里重复。

        筛的是账户属于哪个市场，不是持仓标的属于哪个市场：A 股账户里的港股仓位
        是这个账户的风险，把它归给 HK 摘要等于对只收 A 摘要的人永久隐瞒这条线。
        """
        user = resolve_actor(self.users, username)
        lines: list[str] = []
        for account in self.accounts.list_for(user.id):
            if account.home_market != market:
                continue
            report = self.evaluate(username, account.id)
            for row in report.breaches:
                lines.append(
                    f"{account.name} {row.label} {_pct(row.actual)} > {_pct(row.threshold)}"
                )
        return tuple(lines)

    # --- 写侧 ---------------------------------------------------------------------------

    def save(
        self,
        username: str,
        *,
        kind: str,
        scope: str,
        threshold: float,
        account_id: str | None = None,
        note: str | None = None,
    ):
        user = resolve_actor(self.users, username)
        kind = (kind or "").strip().lower()
        if kind not in KINDS:
            labels = "、".join(KIND_LABELS[item] for item in KINDS)
            raise ValidationError(f"限额种类只能是{labels}，收到 {kind!r}")
        value = _ratio(threshold)
        normalized = self._scope(kind, scope)
        account = None if account_id is None or not account_id.strip() else account_id.strip()
        if account is not None and self.accounts.get(user.id, account) is None:
            raise ValidationError(f"账户 {account} 不存在或不属于用户 {user.username}")
        return self.limits.save(
            user.id,
            account_id=account,
            kind=kind,
            scope=normalized,
            threshold=value,
            note=(note or "").strip() or None,
        )

    def remove(self, username: str, limit_id: str) -> bool:
        user = resolve_actor(self.users, username)
        return self.limits.delete(user.id, limit_id)

    # --- 小件 ---------------------------------------------------------------------------

    def _scope(self, kind: str, raw: str) -> str:
        scope = (raw or "").strip()
        if kind == DRAWDOWN:
            if scope:
                raise ValidationError("回撤线不要填对象：它按整个账户的净值算")
            return ""
        if not scope:
            raise ValidationError(
                "这条线要填对象：单票填代码，市场填 A/HK/US，行业填行业名"
            )
        if kind == SINGLE:
            return scope.upper()
        if kind == MARKET:
            market = canonical_market(scope)
            if market not in self.settings.markets:
                raise ValidationError(f"不认识的市场 {scope!r}：可选的是 A/HK/US")
            return market
        industry = normalize_industry(scope)
        if not industry:
            raise ValidationError(f"行业名 {scope!r} 归一后为空")
        return industry

    def _name(self, market: str, code: str) -> str:
        found = self.symbols.get(market, code)
        return str((found or {}).get("name") or code)

    def _series(self, account_id: str) -> NavSeries:
        end = self.clock.today()
        return self.nav.series(account_id, end - timedelta(days=NAV_WINDOW_DAYS), end)

    def _drawdown(self, account_id: str) -> tuple[float | None, str | None]:
        series = self._series(account_id)
        if series.latest is None:
            # 与归因卡、净值卡同一句话（D281）：先说盘后 job 每晚自己重建，再把命令带上
            # 真的账户 id 交出去——`<账户>` 占位符等于让每个人自己回去查那个 id
            return None, (
                f"还没有算得出来的净值：盘后 job 每晚重建最近 {NAV_WINDOW_DAYS} 天，"
                f"也可以跑 `sams nav rebuild --account {account_id}`"
            )
        if series.gap_days > 0:
            return None, (
                f"区间有 {series.gap_days} 天净值算不出（缺价或缺汇率），回撤不跨缺口"
            )
        if series.current_drawdown is None:
            return None, "净值为 0 或为负，回撤算不出"
        return series.current_drawdown, None

    @staticmethod
    def _weight_gap(book, partial: bool) -> str | None:
        if book.total <= 0:
            return "账户总资产不为正，权重算不出"
        if partial:
            return "有持仓缺价或缺汇率：权重分母不完整，这一条无法判定"
        return None

    @staticmethod
    def _industry_gap(book) -> str | None:
        if book.unclassified_weight <= 0:
            return None
        return (
            f"有 {_pct(book.unclassified_weight)} 的持仓缺行业（未分类）："
            "行业权重不完整，这一条无法判定"
        )

    def _custom_row(
        self,
        limit,
        *,
        book,
        weight_gap: str | None,
        industry_gap: str | None,
        drawdown: tuple[float | None, str | None],
    ) -> LimitRow:
        kind, scope, threshold = limit.kind, limit.scope, limit.threshold
        if kind == DRAWDOWN:
            actual, note = drawdown
            status = UNKNOWN if actual is None else (BREACH if actual > threshold else OK)
            return LimitRow(
                kind=kind,
                scope="",
                label=DRAWDOWN_LABEL,
                threshold=threshold,
                actual=actual,
                status=status,
                custom=True,
                limit_id=limit.id,
                note=note,
            )
        note = industry_gap if kind == INDUSTRY else weight_gap
        gap = weight_gap is not None or (kind == INDUSTRY and industry_gap is not None)
        actual = None if gap else _weight(book, kind, scope)
        status = UNKNOWN if gap else (BREACH if actual > threshold else OK)
        return LimitRow(
            kind=kind,
            scope=scope,
            label=self._label(kind, scope, book),
            threshold=threshold,
            actual=actual,
            status=status,
            custom=True,
            limit_id=limit.id,
            note=note if gap else None,
        )

    def _reference_row(
        self,
        kind: str,
        *,
        book,
        weight_gap: str | None,
        industry_gap: str | None,
        drawdown: tuple[float | None, str | None],
    ) -> LimitRow | None:
        if kind == DRAWDOWN:
            actual, note = drawdown
            return LimitRow(
                kind=kind,
                scope="",
                label=DRAWDOWN_LABEL,
                threshold=DEFAULT_DRAWDOWN,
                actual=actual,
                status=REFERENCE if actual is not None else UNKNOWN,
                custom=False,
                note=note,
            )
        if not book.holdings:
            return None
        gap = weight_gap or (industry_gap if kind == INDUSTRY else None)
        if kind == SINGLE:
            biggest = book.holdings[0]
            scope = biggest.code
            threshold = DEFAULT_SINGLE
        elif kind == INDUSTRY:
            biggest = book.by_industry[0]
            scope = biggest.label
            threshold = DEFAULT_INDUSTRY
        else:  # pragma: no cover - KINDS 只有上面这些
            return None
        return LimitRow(
            kind=kind,
            scope=scope,
            label=self._label(kind, scope, book),
            threshold=threshold,
            actual=None if gap else biggest.weight,
            status=UNKNOWN if gap else REFERENCE,
            custom=False,
            note=gap,
        )

    def _label(self, kind: str, scope: str, book) -> str:
        if kind == SINGLE:
            held = next((item for item in book.holdings if item.code == scope), None)
            return f"单票 {scope}" + (f" {held.name}" if held is not None else "")
        if kind == MARKET:
            return f"市场 {scope}"
        if kind == INDUSTRY:
            return f"行业「{scope}」"
        return DRAWDOWN_LABEL


def _ratio(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"阈值要是数字，收到 {raw!r}") from exc
    if not 0 < value <= 1:
        raise ValidationError(f"阈值要在 0 与 1 之间（0.2 = 20%），收到 {value:g}")
    return value


def _weight(book, kind: str, scope: str) -> float:
    if kind == SINGLE:
        return sum(item.weight for item in book.holdings if item.code.upper() == scope)
    if kind == MARKET:
        return sum(row.weight for row in book.by_market if row.label == scope)
    if kind == INDUSTRY:
        return sum(row.weight for row in book.by_industry if row.label == scope)
    raise AssertionError(f"权重线不存在这种种类：{kind}")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"
