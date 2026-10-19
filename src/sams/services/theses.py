"""thesis 的用例层（M2-E，spec §9.1）：把状态机、当前估值与那条时间线接成一屏。

这里不算任何东西（式子在 `analysis/valuation`，合法跳转在 `storage/repos/theses`），
它只管三件事，而每一件都有一个静默出错的口子：

* **立论时那几列价位从哪来**。当场跑一次估值，只落**它给得出的**那几列。给不出时补一个数
  （哪怕是 0）就等于库里多了一条没有式子 backing 的止损价，而 `advance --to opened`
  那一步查的是「有没有」，不是「对不对」。
* **`show` 上那两栏是两次测量**。库里那次写于立论那一天，当前这次按今天的库与今天的口径
  重算。两者不等不代表观点变了——「漂移」的判定要拿建仓时那份快照对比（§9.3），而快照
  还没有写入方（D76）。所以这里只并排列数，不下结论。
* **谁在读写这一条**。用户名解析成 `user_id` 才交给仓库；不存在与已禁用是两种拒绝（后者
  若放行，「禁用即停止一切动作」就成了纸面规则）。别人的 thesis 连时间线都读不到，那条
  不变式在仓库层，这里只验接线没漏。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date

from sams.analysis.valuation.compose import Plan
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import NotAdjustable, ValidationError
from sams.core.fundamentals import ROE_BASIS_ANNUAL
from sams.services.valuation import STYLE_NAMES, ValuationRead, ValuationService
from sams.storage.repos.theses import (
    ARCHIVED,
    OBSERVING,
    OPENED_REQUIRED,
    STATUSES,
    TRANSITIONS,
    Thesis,
    ThesisEvent,
    ThesisRepo,
)
from sams.storage.repos.users import ACTIVE, User, UserRepo

#: 命令行要念的状态词表与 `--style` 的候选名，都是下层那张表的转述（抄第二份就会分叉）
STATUS_NAMES = STATUSES

#: 对照表里的四栏：`key` 是给 `--json` 用的稳定标识，`label` 是屏幕上那个词，
#: 后面那对是库里的列（单值的两列同名，区间那栏才是两列）
_ROWS = (
    ("entry", "买入区", ("entry_low", "entry_high")),
    ("stop", "止损", ("stop_price", "stop_price")),
    ("target", "目标", ("target_price", "target_price")),
    ("odds", "赔率", ("reward_risk", "reward_risk")),
)


def levels_from_plan(plan: Plan) -> dict:
    """从一条结论里取出能落库的几列：**算不出来的那一列根本不出现**。

    出现即写入，所以这里不能返回 `{"stop_price": None}` 来表示「没数」——`set_levels`
    把 None 读成「把它抹成空」，于是「按现在的库重算一次」会顺手删掉用户手填的那档止损。
    """
    out: dict[str, float] = {}
    if plan.entry.ok:
        out["entry_low"], out["entry_high"] = plan.entry.low, plan.entry.high
    if plan.stop.ok:
        out["stop_price"] = plan.stop.low
    if plan.target.ok:
        out["target_price"] = plan.target.low
    if plan.odds.ok:
        out["reward_risk"] = plan.odds.value
    return out


@dataclass(frozen=True)
class LevelRow:
    """对照表里的一栏：库里那次、当前那次，以及两者差多少。"""

    key: str
    label: str
    stored: tuple[float | None, float | None]
    current: tuple[float | None, float | None]
    note: str
    #: 这一栏在库里的列名（单值的两列同名）：那道「建仓前要先给齐」的闸要按列名来找这一栏
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThesisReport:
    """一屏 thesis：记录本身、那条时间线、当前那份估值，以及它们怎么对上。"""

    username: str
    thesis: Thesis
    events: tuple[ThesisEvent, ...]
    rows: tuple[LevelRow, ...]
    placement: str
    notes: tuple[str, ...] = ()
    written: tuple[str, ...] = ()
    valuation: ValuationRead | None = None
    unavailable: str | None = None

    @property
    def price(self) -> float | None:
        return None if self.valuation is None else self.valuation.price

    @property
    def complete(self) -> bool:
        """这一屏是否读出一条**可用**的结论：当前估值可得，且建仓那一步要的三列都在库里。

        齐不齐不是拿 `plan.ok` 说的——`plan.ok` 还要求买入区，而买入区依赖基本面：
        一只没有基本面的票，止损/目标/赔率照样算得出也照样能建仓，把它判成「没给出结论」
        等于让退出码替数据缺失背锅。三列的名单与仓库那道闸共用同一个来源。
        """
        if self.valuation is None:
            return False
        values: dict[str, float | None] = {}
        for row in self.rows:
            for column, value in zip(row.columns, row.stored, strict=True):
                values[column] = value
        return all(values.get(column) is not None for column, _ in OPENED_REQUIRED)


class ThesisService:
    def __init__(
        self,
        theses: ThesisRepo,
        users: UserRepo,
        valuation: ValuationService,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.theses = theses
        self.users = users
        self.valuation = valuation
        self.clock = clock

    # --- 写 --------------------------------------------------------------------------------

    def add(
        self,
        username: str,
        market: str,
        code: str,
        *,
        style: str = "value",
        status: str = OBSERVING,
        note: str | None = None,
        as_of: date | None = None,
        roe_estimate: float | None = None,
        roe_basis: str = ROE_BASIS_ANNUAL,
    ) -> ThesisReport:
        """立一条论：先当场算一次，把算得出的价位落成那几列，再落这条记录。

        顺序是故意的。反过来写（先落记录再算）的话，一次算崩会在库里留下一条「有 id、
        有状态、没有任何价位」的半条结论，而屏幕上它和「这只票还没到买入区」长得一样。
        """
        user = self._actor(username)
        if style not in STYLE_NAMES:
            raise ValidationError(
                f"没有叫 {style!r} 的打分模板：能用的是 {'、'.join(STYLE_NAMES)}。"
                "这一列决定以后每次 `show` 按哪套权重读，存一个读不出来的名字等于没存"
            )
        self._slot_is_free(user, market, code, style)
        read, unavailable = self._read(
            market,
            code,
            as_of=as_of,
            style=style,
            roe_estimate=roe_estimate,
            roe_basis=roe_basis,
        )
        levels = {} if read is None else levels_from_plan(read.plan)
        thesis = self.theses.create(
            user_id=user.id,
            market=market,
            code=code,
            style=style,
            status=status,
            actor_id=user.id,
            now=self.clock.now_utc(),
            note=note,
            **levels,
        )
        return self._report(
            user.username,
            thesis,
            read=read,
            unavailable=unavailable,
            written=_written(levels),
            written_from=None if read is None else f"当场算出的那条结论（截至 {read.as_of}）",
        )

    def advance(
        self, username: str, thesis_id: str, to: str, *, note: str | None = None
    ) -> Thesis:
        """走一跳。跳转表与「建仓要先有价位」那条都在仓库层，这里只补上谁在走、何时走。"""
        user = self._actor(username)
        return self.theses.advance(
            user.id, thesis_id, to, actor_id=user.id, now=self.clock.now_utc(), note=note
        )

    def revise(
        self,
        username: str,
        thesis_id: str,
        *,
        from_plan: bool = False,
        as_of: date | None = None,
        roe_estimate: float | None = None,
        roe_basis: str = ROE_BASIS_ANNUAL,
        **levels: float | None,
    ) -> ThesisReport:
        """改那几列价量，不改状态（因此不留事件：时间线记的是跳转，不是字段修订史）。

        `from_plan` 是「按现在的库重算一遍再落」：它只覆盖这条结论**给得出**的那几栏。
        手填的键排在它前面合并，所以两者同时出现时以库里重算出来的那个为准。
        """
        user = self._actor(username)
        current = self.theses.require(user.id, thesis_id)
        read, unavailable = self._read(
            current.market,
            current.code,
            as_of=as_of,
            style=current.style,
            roe_estimate=roe_estimate,
            roe_basis=roe_basis,
        )
        recomputed = {} if read is None or not from_plan else levels_from_plan(read.plan)
        merged = {**levels, **recomputed}
        thesis = self.theses.set_levels(
            user.id, thesis_id, actor_id=user.id, now=self.clock.now_utc(), **merged
        )
        return self._report(
            user.username,
            thesis,
            read=read,
            unavailable=unavailable,
            written=_written(merged),
            written_from=(
                None
                if not merged
                else (
                    f"当场算出的那条结论（截至 {read.as_of}）"
                    if recomputed
                    else "你手填的那几个数"
                )
            ),
        )

    # --- 读 --------------------------------------------------------------------------------

    def list(self, username: str, *, status: str | None = None) -> list[Thesis]:
        user = self._actor(username)
        return self.theses.list(user.id, status=status)

    def show(
        self,
        username: str,
        thesis_id: str,
        *,
        as_of: date | None = None,
        roe_estimate: float | None = None,
        roe_basis: str = ROE_BASIS_ANNUAL,
    ) -> ThesisReport:
        user = self._actor(username)
        thesis = self.theses.require(user.id, thesis_id)
        read, unavailable = self._read(
            thesis.market,
            thesis.code,
            as_of=as_of,
            style=thesis.style,
            roe_estimate=roe_estimate,
            roe_basis=roe_basis,
        )
        return self._report(user.username, thesis, read=read, unavailable=unavailable)

    def next_steps(self, status: str) -> tuple[str, ...]:
        """从这个状态能走到哪：命令行念的是仓库拒绝时用的同一张表，不是第二份名单。"""
        return TRANSITIONS[status]

    # --- 内部 ------------------------------------------------------------------------------

    def _actor(self, username: str) -> User:
        user = self.users.get_by_username((username or "").strip())
        if user is None:
            raise ValidationError(
                f"没有用户 {username!r}：先 `sams user add {username}`"
                "（归属只认用户名，屏幕上的 `--user` 也是它）"
            )
        if user.status != ACTIVE:
            raise ValidationError(
                f"用户 {username} 已被禁用：启用之后才能读写 thesis，"
                "否则「禁用即停止一切动作」这条就不成立"
            )
        return user

    def _slot_is_free(self, user: User, market: str, code: str, style: str) -> None:
        """同一只票、同一套模板，同时只留一条没走完的。

        键的形态要先归一再比：`hk/700` 与 `HK/00700` 是同一只票，不归一的话这条闸只对
        敲得标准的人生效，而重复记录恰恰是不标准那一次留下的。
        """
        try:
            cleaned = canonical_market(market)
            key = (cleaned, canonical_code(cleaned, code), style)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        for existing in self.theses.list(user.id):
            if existing.status == ARCHIVED:
                continue
            if (existing.market, existing.code, existing.style) == key:
                raise ValidationError(
                    f"这只票已经有一条没走完的 thesis（{existing.id}，状态 {existing.status}）："
                    "同一套模板留两条活的，M3 的派生规则会翻倍，回看时也分不清哪条才是当时的决定。"
                    "要重开请先走完那一条（`sams thesis advance`），或者换一套模板（`--style`）"
                )

    def _read(
        self,
        market: str,
        code: str,
        *,
        as_of: date | None,
        style: str,
        roe_estimate: float | None,
        roe_basis: str,
    ) -> tuple[ValuationRead | None, str | None]:
        """当前那份估值：算不出来时交回**原因**，而不是半个读数。

        这里只兜 `NotAdjustable`（库里没有这只票的 K 线）：那是数据缺失，不是这条命令写错了。
        其它异常一律冒出去——把参数错也咽成「没有价位」，用户就只剩一条空价位的 thesis 可猜。
        """
        try:
            read = self.valuation.read(
                market,
                code,
                as_of=as_of,
                style=style,
                roe_estimate=roe_estimate,
                roe_basis=roe_basis,
            )
        except NotAdjustable as exc:
            return None, str(exc)
        return read, None

    def _report(
        self,
        username: str,
        thesis: Thesis,
        *,
        read: ValuationRead | None,
        unavailable: str | None = None,
        written: tuple[str, ...] = (),
        written_from: str | None = None,
    ) -> ThesisReport:
        plan = None if read is None else read.plan
        rows = tuple(
            _row(thesis, plan, key=key, label=label, columns=columns, unavailable=unavailable)
            for key, label, columns in _ROWS
        )
        return ThesisReport(
            username=username,
            thesis=thesis,
            events=tuple(self.theses.events(thesis.user_id, thesis.id)),
            rows=rows,
            placement=_placement(username, thesis, None if read is None else read.price),
            notes=_notes(thesis, read, unavailable, written, written_from),
            written=written,
            valuation=read,
            unavailable=unavailable,
        )


# --- 对照与位置 ----------------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "–" if value is None else f"{value:.4g}"


def _written(columns: dict) -> tuple[str, ...]:
    """这一次实际写了哪几栏（按屏幕上的顺序说，不按 kwargs 的哈希顺序）。"""
    return tuple(label for _key, label, cols in _ROWS if any(c in columns for c in cols))


def _current(plan: Plan | None, key: str) -> tuple[tuple, str]:
    """当前这次算出的那一栏，以及它算不出时的原因。"""
    if plan is None:
        return (None, None), ""
    if key == "odds":
        return (
            ((plan.odds.value, plan.odds.value) if plan.odds.ok else (None, None)),
            plan.odds.reason,
        )
    leg = {"entry": plan.entry, "stop": plan.stop, "target": plan.target}[key]
    return ((leg.low, leg.high) if leg.ok else (None, None)), leg.reason


def _row(
    thesis: Thesis,
    plan: Plan | None,
    *,
    key: str,
    label: str,
    columns: tuple[str, ...],
    unavailable: str | None,
) -> LevelRow:
    stored = (getattr(thesis, columns[0]), getattr(thesis, columns[1]))
    current, why = _current(plan, key)
    if stored == current and current[0] is not None:
        note = "与库里那次一致"
    elif current == (None, None):
        reason = unavailable or why or "这条结论没给出这一栏"
        note = (
            f"当前这次算不出（{reason}）：库里那次原样留着"
            if stored != (None, None)
            else f"两次都给不出（{reason}）"
        )
    elif stored == (None, None):
        note = f"库里还没有这一栏，当前这次算出 {_fmt(current[0])}"
    else:
        delta = "" if not stored[0] else f"（{current[0] / stored[0] - 1:+.1%}）"
        note = f"从 {_fmt(stored[0])} 到 {_fmt(current[0])}{delta}"
    return LevelRow(
        key=key, label=label, stored=stored, current=current, note=note, columns=columns
    )


def _notes(
    thesis: Thesis,
    read: ValuationRead | None,
    unavailable: str | None,
    written: tuple[str, ...],
    written_from: str | None,
) -> tuple[str, ...]:
    found: list[str] = []
    if unavailable:
        found.append(f"当前这次给不出：{unavailable}")
    if read is not None:
        found.append(
            f"两栏是两次测量：库里那次写于 {_utc(thesis.created_at)}"
            f"（那一次用的输入没有存档），当前这次按 {read.as_of} 的库、"
            f"ROE 口径 {read.fundamental.roe_basis} 重算。它们不等不代表观点变了——"
            "漂移的判定要拿建仓时那份快照来比，而快照还没有写入方（D76）"
        )
        # 计划里那些「依附项没过条件」的话要说出来：它们不给任何一栏定价，只解释为什么
        # 那一栏是空的（分位样本掉到地板以下就是这一类），少了它们屏幕上只剩一个 –
        found.extend(read.plan.notes)
    if written:
        found.append(f"这一次落进去的是 {'、'.join(written)}，来源：{written_from}")
    return tuple(found)


def _utc(value) -> str:
    """落库的一刻统一按 UTC 说，并把时区写出来。

    DuckDB 回读 `TIMESTAMPTZ` 的偏移不稳定（`theses.created_at` 给 UTC、
    `thesis_events.ts` 给会话本地时区），按原样格式化会让同一屏上两个数看着差八小时，
    而这一屏要回答的正是「库里那次是什么时候的事」。
    """
    if value is None:
        return "未知"
    moment = value.astimezone(UTC) if value.tzinfo is not None else value
    return f"{moment:%Y-%m-%d %H:%M} UTC"


def _placement(username: str, thesis: Thesis, price: float | None) -> str:
    """现价落在计划的哪一段：只说库里那几列与现价的几何关系，不替用户决定要不要动手。

    给出的那两条 `advance` 命令带着 `--user`：屏幕上写一条当场敲不通的命令，
    等于让用户再回去读一遍帮助（同一条红线在 M2-A/C 被踩过好几次）。
    """
    back = f"`sams thesis advance {thesis.id} --user {username}`"
    columns = (thesis.entry_low, thesis.entry_high, thesis.stop_price, thesis.target_price)
    if all(value is None for value in columns):
        return (
            "计划里还没有价位，所以说不出现价落在哪一段：补齐它"
            f"（`sams thesis revise {thesis.id} --user {username} --from-plan`，"
            "或手填 `--stop/--target/--odds`）"
        )
    if price is None:
        return "没有现价（当前这次算不出），所以说不出现价落在哪一段"
    if thesis.stop_price is not None and price <= thesis.stop_price:
        return (
            f"现价 {_fmt(price)} 已在止损 {_fmt(thesis.stop_price)} 之下："
            f"按计划这一笔该了结了（{back} --to hit_stop）"
        )
    if thesis.target_price is not None and price >= thesis.target_price:
        return (
            f"现价 {_fmt(price)} 已到或超过目标价 {_fmt(thesis.target_price)}："
            f"分批止盈提醒（{back} --to hit_target）"
        )
    low, high = thesis.entry_low, thesis.entry_high
    if low is None and high is None:
        return (
            f"现价 {_fmt(price)} 还在止损与目标之间，而买入区没有填：说不清「到没到」"
            f"（`sams thesis revise {thesis.id} --user {username} --entry-low .. --entry-high ..`）"
        )
    if low is not None and high is not None and low <= price <= high:
        return f"现价 {_fmt(price)} 落在买入区 {_fmt(low)}~{_fmt(high)} 之内：计划里的买入条件成立"
    anchor, phrase = (high, "上限") if high is not None else (low, "下限")
    gap = price / anchor - 1
    if price > anchor:
        return (
            f"现价 {_fmt(price)} 还在买入区{phrase} {_fmt(anchor)} 之上（贵 {gap:+.1%}）："
            "计划里的买入条件还没成立"
        )
    return (
        f"现价 {_fmt(price)} 已低于买入区{phrase} {_fmt(anchor)}（{gap:+.1%}）："
        "先确认是数据或口径变了，而不是「更便宜所以更该买」"
    )
