"""数据源管理的用例层：把「加一个源、停用一周、换个顺序」变成带业务校验的操作。

放在 services 而不是仓库层，因为这里的每条校验都要同时认识三样东西：
`Settings`（哪些市场被配置过）、`datasource.sources`（哪些实现真的存在）、
仓库里的路由表。storage 只该管结构，不该知道 Yahoo 支持哪些市场。

两条不可绕过的规则：
  * `kind` 必须是注册表里已有的实现名——配置里不接受模块路径或 import 字符串，
    否则管理后台就成了远程代码执行入口。
  * 停用/删除不能把某个市场弄成**没有能取日线的源**：那样下次启动 registry 直接抛错，
    而现场看起来「刚才是好的」。要真撤干净，得先把备源加上。
    （只报公司行为的源不算数——它兜不住价格，见 D38。）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from time import monotonic

from sams.core.config import Settings
from sams.core.errors import DatasourceError, SamsError, StorageError, ValidationError
from sams.datasource.sources import SourceEntry, build, catalog, known
from sams.storage.repos.data_sources import DataSourceRepo, SourceRow

logger = logging.getLogger("sams.services.sources")


@dataclass(frozen=True)
class ProbeResult:
    """单独试一个源：管理后台「测试这个源」按钮背后的东西。"""

    name: str
    market: str
    code: str
    ok: bool
    rows: int = 0
    first_date: date | None = None
    last_date: date | None = None
    error: str | None = None
    seconds: float = 0.0

    @property
    def verdict(self) -> str:
        return f"取到 {self.rows} 根" if self.ok else (self.error or "失败")


class SourceAdminService:
    def __init__(self, repo: DataSourceRepo, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings

    # ---------- 只读视图 ----------

    def implementations(self) -> dict[str, SourceEntry]:
        return known()

    def catalog(self) -> list[SourceEntry]:
        return list(catalog().entries)

    def list(self) -> list[SourceRow]:
        return self.repo.list()

    def chains(self) -> dict[str, tuple[str, ...]]:
        return self.repo.chains()

    def routes(self) -> list[tuple[str, str, int]]:
        return self.repo.routes()

    def supports(self, market: str) -> tuple[str, ...]:
        """哪些已注册实现能服务这个市场——加源界面的候选列表。"""
        return catalog().supporting(market)

    # ---------- 写操作 ----------

    def add(
        self,
        name: str,
        kind: str,
        markets: list[str],
        *,
        options: dict | None = None,
        note: str | None = None,
        enabled: bool = True,
    ) -> SourceRow:
        entry = self._require_kind(kind)
        targets = self._require_markets(kind, markets, entry)
        self._require_options(kind, options or {})
        row = SourceRow(
            name=name, kind=kind, enabled=enabled, options=options or {}, note=note or entry.note
        )
        saved = self.repo.upsert(row)
        for market in targets:
            # 新源默认排到最后：插到第 1 位等于「我比主源熟」，那是要显式 move 的决定
            self.repo.set_route(market, name, self._next_priority(market))
        return saved

    def update(
        self,
        name: str,
        *,
        options: dict | None = None,
        note: str | None = None,
        enabled: bool | None = None,
    ) -> SourceRow:
        current = self.repo.get(name)
        if current is None:
            raise ValidationError(f"数据源 {name} 不存在")
        if options is not None:
            self._require_options(current.kind, options)
        return self.repo.upsert(
            SourceRow(
                name=name,
                kind=current.kind,
                enabled=current.enabled if enabled is None else enabled,
                options=current.options if options is None else options,
                note=current.note if note is None else note,
            )
        )

    def remove(self, name: str) -> int:
        """删除源；市场不会因为删它而失明时才允许删（同 set_enabled 的规则）。"""
        if self.repo.get(name) is None:
            raise ValidationError(f"数据源 {name} 不存在")
        self._require_markets_keep_sources(name, keeping=True)
        return self.repo.remove(name)

    def set_enabled(self, name: str, enabled: bool) -> None:
        if self.repo.get(name) is None:
            raise ValidationError(f"数据源 {name} 不存在")
        if not enabled:
            self._require_markets_keep_sources(name, keeping=False)
        self.repo.set_enabled(name, enabled)

    def route(self, market: str, name: str, priority: int) -> None:
        self._require_routable(market, name)
        self.repo.set_route(market, name, priority)

    def unroute(self, market: str, name: str) -> bool:
        dark = self._would_go_dark(market, dropping={name})
        if dark:
            raise ValidationError(
                f"市场 {market} 摘掉 {name} 就没有能取日线的源了；先加备源"
            )
        return self.repo.remove_route(market, name)

    def reorder(self, market: str, names: list[str]) -> None:
        if not names:
            raise ValidationError(f"市场 {market} 的重排列表为空：要清空请用 unroute")
        for name in names:
            self._require_routable(market, name)
        self.repo.reorder(market, names)

    # ---------- 试跑 ----------

    def probe(
        self, name: str, market: str, code: str, start: date, end: date
    ) -> ProbeResult:
        """绕开降级链，只打这一个源。

        存在的意义：全链跑通不代表每个源都好——被备源救回来的那次失败，
        在 `sams health` 里只留下备源的成功。排障要能单独问某一个源。
        """
        row = self.repo.get(name)
        if row is None:
            raise ValidationError(f"数据源 {name} 不存在")
        self._require_routable(market, name)
        began = monotonic()
        try:
            adapter = build(row.kind, row.options)
            frame = adapter.daily_bars(market, code, start, end)
        except (SamsError, DatasourceError, StorageError) as exc:
            return ProbeResult(
                name, market, code, ok=False, error=f"{type(exc).__name__}: {exc}",
                seconds=round(monotonic() - began, 3),
            )
        dates = frame["date"] if "date" in frame.columns and not frame.is_empty() else None
        return ProbeResult(
            name=name,
            market=market,
            code=code,
            ok=True,
            rows=frame.height,
            first_date=None if dates is None else dates[0],
            last_date=None if dates is None else dates[-1],
            seconds=round(monotonic() - began, 3),
        )

    # ---------- 校验 ----------

    def _require_kind(self, kind: str) -> SourceEntry:
        entry = known().get(kind)
        if entry is None:
            raise ValidationError(
                f"数据源实现 {kind!r} 未注册。可用实现: "
                f"{ {e.kind: sorted(e.markets) for e in catalog().entries} }"
            )
        return entry

    def _require_options(self, kind: str, options: dict) -> None:
        entry = self._require_kind(kind)
        unknown = [k for k in options if k not in entry.options]
        if unknown:
            raise ValidationError(
                f"数据源 {kind} 不接受参数 {sorted(unknown)}；可配置的是 {list(entry.options)}"
            )

    def _require_markets(self, kind: str, markets: list[str], entry: SourceEntry) -> list[str]:
        if not markets:
            raise ValidationError(
                f"至少要给一个市场：{kind} 能服务 {sorted(entry.markets)}，"
                f"系统已配置 {sorted(self.settings.markets)}"
            )
        unsupported = [m for m in markets if m not in entry.markets]
        if unsupported:
            raise ValidationError(
                f"实现 {kind} 不支持市场 {unsupported}；它只覆盖 {sorted(entry.markets)}"
            )
        unconfigured = [m for m in markets if m not in self.settings.markets]
        if unconfigured:
            raise ValidationError(
                f"市场 {unconfigured} 没在配置文件的 [markets.*] 里定义"
                f"（时区与币种在那里），已配置: {sorted(self.settings.markets)}"
            )
        return list(dict.fromkeys(markets))

    def _require_routable(self, market: str, name: str) -> None:
        row = self.repo.get(name)
        if row is None:
            raise ValidationError(f"数据源 {name} 不存在，先 add")
        entry = self._require_kind(row.kind)
        if market not in entry.markets:
            raise ValidationError(
                f"数据源 {name}（实现 {row.kind}）不支持市场 {market}；"
                f"它只覆盖 {sorted(entry.markets)}"
            )
        if market not in self.settings.markets:
            raise ValidationError(f"市场 {market} 没在配置里定义，不能建路由")

    def _require_markets_keep_sources(self, name: str, *, keeping: bool) -> None:
        """停用/删除前确认不会把某个市场弄成「没有能取日线的源」。"""
        for market in self._markets_of(name):
            if not self._bars_sources_in(market, dropping={name}):
                verb = "删除" if keeping else "停用"
                raise ValidationError(
                    f"市场 {market} 只有 {name} 一个可用源，{verb}它会让该市场完全取不到数；"
                    f"请先给 {market} 加上备源（`sams source add` 支持的实现: "
                    f"{catalog().supporting(market)}）"
                )

    def _would_go_dark(self, market: str, *, dropping: set[str]) -> bool:
        return not self._bars_sources_in(market, dropping)

    def _markets_of(self, name: str) -> set[str]:
        return {m for m, n, _ in self.repo.routes() if n == name}

    def _bars_sources_in(self, market: str, dropping: set[str]) -> set[str]:
        """市场上还剩几个**能取日线**的启用源。

        「还有源」不等于「还能取到价格」：D38 之后链上可以只有一个只报公司行为的源
        （akshare_hk），把它当备源，`sams source` 会允许把最后一个拉价格的源摘掉，
        而 registry 只在采集时才发现链上没人能提供日线。
        """
        left = {
            n
            for m, n, _ in self.repo.routes()
            if m == market and n not in dropping and self._is_enabled(n)
        }
        return {n for n in left if self._has_bars(n)}

    def _has_bars(self, name: str) -> bool:
        row = self.repo.get(name)
        entry = known().get(row.kind) if row else None
        return bool(entry and "bars" in entry.capabilities)

    def _is_enabled(self, name: str) -> bool:
        row = self.repo.get(name)
        return bool(row and row.enabled)

    def _next_priority(self, market: str) -> int:
        used = [p for m, _, p in self.repo.routes() if m == market]
        return max(used, default=0) + 1
