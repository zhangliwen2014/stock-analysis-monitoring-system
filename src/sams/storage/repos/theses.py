"""thesis 仓库：状态机在 storage 层强制，每次变更落一条事件（spec §9.1、§14 M2 第 5 条）。

为什么不把「未建仓不能直接归档」这类判断放到服务层：这条链上会有多个写入口（命令行、
周复检、M3 的规则命中），一个忘了查状态的入口留下的不是报错而是一条**看起来正常的假时间线**
——回看「我当时为什么买」时，一个从未建仓的观点带着 `archived` 站在那里，读的人无从分辨。

用户隔离沿用 watchlists 的形态：除 `list_all`（只读审计，没有配套写方法）外，
所有读与写都带 `user_id`，不存在「查全量再由上层过滤」的接口。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

OBSERVING = "observing"
PROPOSED = "proposed"
OPENED = "opened"
TRACKING = "tracking"
HIT_TARGET = "hit_target"
HIT_STOP = "hit_stop"
INVALIDATED = "invalidated"
ARCHIVED = "archived"

#: 状态词表只有一份：命令行、服务、派生规则都读它，未知名字在仓库层就拒。
STATUSES = (OBSERVING, PROPOSED, OPENED, TRACKING, HIT_TARGET, HIT_STOP, INVALIDATED, ARCHIVED)

#: 合法跳转表。这张表是「这一步现在能不能走」的唯一出处（见模块开头为什么不放到服务层）。
TRANSITIONS: dict[str, tuple[str, ...]] = {
    OBSERVING: (PROPOSED, INVALIDATED),
    PROPOSED: (OPENED, INVALIDATED),
    OPENED: (TRACKING, HIT_TARGET, HIT_STOP, INVALIDATED),
    TRACKING: (HIT_TARGET, HIT_STOP, INVALIDATED),
    HIT_TARGET: (ARCHIVED,),
    HIT_STOP: (ARCHIVED,),
    INVALIDATED: (ARCHIVED,),
    ARCHIVED: (),
}

#: 建仓那一步要求齐备的三项：缺哪几个就点名哪几个（说「不能建仓」而不说缺什么是废提示）。
OPENED_REQUIRED = (("stop_price", "止损"), ("target_price", "目标"), ("reward_risk", "赔率"))

#: 可以改的那几列价位（`win_rate` 与样本数是 M5 回填的，这里只保证它们落成小数口径）。
LEVEL_COLUMNS = (
    "entry_low",
    "entry_high",
    "stop_price",
    "target_price",
    "reward_risk",
    "win_rate",
    "win_rate_samples",
)

PRICES = ("entry_low", "entry_high", "stop_price", "target_price")

_COLUMNS = (
    "id, user_id, market, code, style, status, entry_low, entry_high, stop_price, "
    "target_price, reward_risk, win_rate, win_rate_samples, levels_on, created_at, updated_at, "
    "archived_at, baseline_score, baseline_items, baseline_on"
)


def missing_levels_for_opened(row: Thesis) -> tuple[str, ...]:
    """建仓那道闸还缺哪几列（列名，按 `OPENED_REQUIRED` 的顺序）。

    名单只写一遍：仓库的 `advance` 用它决定能不能建仓，服务那屏的 `complete` 用它决定退出码。
    两处各抄一份时，改一栏只会让「屏幕上说完整」与「`--to opened` 照样拒」分叉。
    """
    return tuple(column for column, _ in OPENED_REQUIRED if getattr(row, column) is None)


@dataclass(frozen=True)
class Thesis:
    id: str
    user_id: str
    market: str
    code: str
    style: str
    status: str
    entry_low: float | None = None
    entry_high: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    reward_risk: float | None = None
    win_rate: float | None = None
    win_rate_samples: int | None = None
    #: 库里那几列价位属于哪一天的读数（0013）；手填的数留 None
    levels_on: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: 周复检的基线（0016 / D96）：建仓时那一个总分、那几路分项、属于哪一天。
    #: 三者同进同出，所以 `recheck` 只判 `baseline_score is None`。
    baseline_score: float | None = None
    baseline_items: tuple[tuple[str, float], ...] = ()
    baseline_on: date | None = None
    archived_at: datetime | None = None


@dataclass(frozen=True)
class ThesisEvent:
    """时间线上的一跳。`from_status` 只有开局那一条是 None。"""

    id: str
    thesis_id: str
    from_status: str | None
    to_status: str
    actor_id: str
    note: str | None = None
    ts: datetime | None = None


def _require_user(user_id: str, *, what: str = "user_id") -> str:
    if not user_id:
        raise ValidationError(f"{what} 不能为空")
    return user_id


def _norm(market: str, code: str) -> tuple[str, str]:
    """键的形态与 bars / watchlists 同一个（D46、D50）：不归一的行落得下去、永远读不到。"""
    if not market or not code:
        raise ValidationError("market 与 code 不能为空")
    try:
        cleaned = canonical_market(market)
        return cleaned, canonical_code(cleaned, code)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _status(name: str) -> str:
    if name not in STATUSES:
        raise ValidationError(
            f"状态 {name!r} 不在这条链上：能用的是 {'、'.join(STATUSES)}"
        )
    return name


def _price(column: str, value: float | None) -> float | None:
    """价位要么没有，要么是一个正的有限数。"""
    if value is None:
        return None
    if not isfinite(value) or value <= 0:
        raise ValidationError(f"{column}={value} 不是一个能用的价位：要正的有限数")
    return float(value)


def _ratio(column: str, value: float | None) -> float | None:
    """`win_rate` 只收小数口径（D58 那一族）：0.55 写成 55 会让整条链读一个 100 倍的数。"""
    if value is None:
        return None
    if not isfinite(value) or value < 0 or value > 1:
        raise ValidationError(
            f"{column}={value} 不是一个概率：要 0 ≤ x ≤ 1（55% 写 0.55，不是 55）"
        )
    return float(value)


def _odds(value: float | None) -> float | None:
    if value is None:
        return None
    if not isfinite(value) or value <= 0:
        raise ValidationError(f"赔率 reward_risk={value} 不是一个能用的数：要正的有限数")
    return float(value)


def _samples(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 0:
        raise ValidationError(f"win_rate_samples={value} 不能为负：样本数是数出来的")
    return int(value)


def _score(label: str, value: object) -> float:
    """0~100 的分项分：`True` 与 `"50"` 不是数，NaN 写进 JSON 之后连读都读不回来。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} 要是一个数（收到 {value!r}）")
    number = float(value)
    if not isfinite(number) or not 0.0 <= number <= 100.0:
        raise ValidationError(f"{label} 要在 0~100（收到 {value}）：分项分就是这个刻度")
    return number


def _check_levels(values: dict) -> dict:
    """把七列各过各自的闸，再查买入区两端的顺序（写反了屏幕上是一个空区间）。"""
    cleaned: dict = {}
    for column in LEVEL_COLUMNS:
        if column not in values:
            continue
        value = values[column]
        if column in PRICES:
            cleaned[column] = _price(column, value)
        elif column == "reward_risk":
            cleaned[column] = _odds(value)
        elif column == "win_rate":
            cleaned[column] = _ratio(column, value)
        else:
            cleaned[column] = _samples(value)
    low, high = cleaned.get("entry_low"), cleaned.get("entry_high")
    if low is not None and high is not None and low > high:
        raise ValidationError(
            f"买入区写反了：下限 {low} 高于上限 {high}（这一对在屏幕上是一个空区间）"
        )
    stop, target = cleaned.get("stop_price"), cleaned.get("target_price")
    if stop is not None and target is not None and stop >= target:
        raise ValidationError(
            f"止损 {stop} 不在目标 {target} 之下：这一对让赔率成为负数，"
            "而现价会同时满足「已在止损之下」与「已到目标价」——"
            "两条相反的了结路都被这屏背书（D81）"
        )
    return cleaned


class ThesisRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _reject_unknown(self, levels: dict) -> None:
        """写入口对列名一样严格（D81）：咽下一个 `stop_pirce`，屏幕上就像改好了而库里没有。"""
        unknown = sorted(set(levels) - set(LEVEL_COLUMNS))
        if unknown:
            raise ValidationError(
                f"thesis 没有这些列：{'、'.join(unknown)}；能改的是 {'、'.join(LEVEL_COLUMNS)}"
            )

    def _live_on_the_same_key(self, user_id: str, market: str, code: str, style: str) -> str | None:
        """同一只票、同一套模板，同时只留一条没走完的（D81）。

        这条约束在仓库而不在服务：写入口会有第二个、第三个（M3 的规则命中、周复检），
        而一个绕得过的约束等于没有——开出的第二条让派生规则把同一个观点算两遍，
        两条在屏幕上看起来都正常。
        """
        for existing in self.list(user_id):
            if existing.status == ARCHIVED:
                continue
            if (existing.market, existing.code, existing.style) == (market, code, style):
                return existing.id
        return None

    def create(
        self,
        *,
        user_id: str,
        market: str,
        code: str,
        style: str,
        actor_id: str,
        now: datetime,
        status: str = OBSERVING,
        note: str | None = None,
        levels_on: date | None = None,
        **levels: float | int | None,
    ) -> Thesis:
        """开局只有 `observing` 与 `proposed` 两种：从链条中段开局等于跳过「观点怎么来的」。"""
        _require_user(user_id)
        _require_user(actor_id, what="actor_id")
        name = _status(status)
        if name not in (OBSERVING, PROPOSED):
            raise ValidationError(
                f"新建的 thesis 只能从 {' 或 '.join((OBSERVING, PROPOSED))} 开始，"
                f"收到 {name!r}：建仓（{OPENED}）要走 `advance`，它要校验价位齐备"
            )
        if not style:
            raise ValidationError("style 不能为空：它决定这份结论是按哪套权重读出来的")
        self._reject_unknown(levels)
        cleaned = _check_levels(levels)
        market_norm, code_norm = _norm(market, code)
        clash = self._live_on_the_same_key(user_id, market_norm, code_norm, style)
        if clash is not None:
            raise StorageError(
                f"{market_norm}/{code_norm} 已经有一条没走完的 thesis（{clash}，"
                "同一套模板留两条活的，派生规则会把同一个观点算两遍，"
                "回看时也分不清哪条才是当时的决定）。要重开请先走完那一条（`advance`），"
                "或者换一套模板（style）"
            )
        thesis = Thesis(
            id=new_id(),
            user_id=user_id,
            market=market_norm,
            code=code_norm,
            style=style,
            status=name,
            levels_on=levels_on,
            created_at=now,
            updated_at=now,
            **cleaned,
        )
        self.db.execute(
            """
            INSERT INTO theses
                (id, user_id, market, code, style, status, entry_low, entry_high,
                 stop_price, target_price, reward_risk, win_rate, win_rate_samples,
                 levels_on, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                thesis.id,
                user_id,
                market_norm,
                code_norm,
                style,
                name,
                thesis.entry_low,
                thesis.entry_high,
                thesis.stop_price,
                thesis.target_price,
                thesis.reward_risk,
                thesis.win_rate,
                thesis.win_rate_samples,
                levels_on,
                now,
                now,
            ],
        )
        self._event(thesis.id, None, name, actor_id, note, now)
        return thesis

    def get(self, user_id: str, thesis_id: str) -> Thesis | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM theses WHERE user_id = ? AND id = ?",
            [_require_user(user_id), thesis_id],
        )
        found = _to_theses(rows)
        return found[0] if found else None

    def list(self, user_id: str, *, status: str | None = None) -> list[Thesis]:
        """某用户名下的 thesis；`status` 给定时只留那一个状态（名字不认识的当场拒）。"""
        _require_user(user_id)
        if status is None:
            rows = self.db.query(
                f"SELECT {_COLUMNS} FROM theses WHERE user_id = ? ORDER BY created_at, id",
                [user_id],
            )
        else:
            rows = self.db.query(
                f"SELECT {_COLUMNS} FROM theses WHERE user_id = ? AND status = ? "
                "ORDER BY created_at, id",
                [user_id, _status(status)],
            )
        return _to_theses(rows)

    def list_all(self) -> list[Thesis]:
        """admin 审计视图：全部用户的 thesis。只读，没有配套写方法（同 watchlists）。"""
        return _to_theses(
            self.db.query(f"SELECT {_COLUMNS} FROM theses ORDER BY user_id, created_at, id")
        )

    def events(self, user_id: str, thesis_id: str) -> list[ThesisEvent]:
        """时间线按发生顺序返回；归属先查（别人的 thesis 连事件都读不到）。

        次序是 `ts` 再用物理行号兜：两步落在同一个瞬间时（冻住的时钟、脚本、M3 的触发器），
        按 `id` 排就是按 uuid4 排，时间线会把自己说反。
        """
        self.require(user_id, thesis_id)
        rows = self.db.query(
            "SELECT id, thesis_id, from_status, to_status, actor_id, note, ts "
            "FROM thesis_events WHERE thesis_id = ? ORDER BY ts, rowid",
            [thesis_id],
        )
        return [
            ThesisEvent(
                id=r["id"],
                thesis_id=r["thesis_id"],
                from_status=r["from_status"],
                to_status=r["to_status"],
                actor_id=r["actor_id"],
                note=r["note"],
                ts=r["ts"],
            )
            for r in rows.to_dicts()
        ]

    def set_levels(
        self,
        user_id: str,
        thesis_id: str,
        *,
        actor_id: str,
        now: datetime,
        levels_on: date | None = None,
        **levels: float | int | None,
    ) -> Thesis:
        """改那几列价位而不改状态，因此**不写事件**：`thesis_events.to_status` 非空，
        一次没有状态变化的写入要么伪造一条跳转、要么什么都留不下——留不下是对的，
        这条表要回答的是「我当时为什么买」，不是每个字段的修订史。

        `levels_on` 由调用方说清「这次落的数属于哪一天的读数」（0013）：手填就传 None。
        默认值不是今天是有意的——把写入日当测量日，一屏会把「今天填的数」说成
        「今天重新量过一遍」。
        """
        _require_user(actor_id, what="actor_id")
        current = self.require(user_id, thesis_id)
        if current.status == ARCHIVED:
            raise StorageError(
                f"{thesis_id} 已经归档：`{ARCHIVED}` 是终点，改它不是在修订一条观点，"
                "而是在重写历史（要修正就另开一条，两条各自带时间线）"
            )
        self._reject_unknown(levels)
        merged = {**{c: getattr(current, c) for c in LEVEL_COLUMNS}, **levels}
        cleaned = _check_levels(merged)
        self.db.execute(
            "UPDATE theses SET entry_low = ?, entry_high = ?, stop_price = ?, "
            "target_price = ?, reward_risk = ?, win_rate = ?, win_rate_samples = ?, "
            "levels_on = ?, updated_at = ? WHERE user_id = ? AND id = ?",
            [
                cleaned["entry_low"],
                cleaned["entry_high"],
                cleaned["stop_price"],
                cleaned["target_price"],
                cleaned["reward_risk"],
                cleaned["win_rate"],
                cleaned["win_rate_samples"],
                levels_on,
                now,
                current.user_id,
                current.id,
            ],
        )
        found = self.get(user_id, thesis_id)
        assert found is not None  # 同一个库里刚 UPDATE 过
        return found

    def set_baseline(
        self,
        user_id: str,
        thesis_id: str,
        *,
        score: float | None,
        items: Mapping[str, float],
        on: date,
        now: datetime,
    ) -> Thesis:
        """写下复检的基准（0016）。写一次就不再改：已经有基线的那条直接拒。

        `score=None` 会被拒：那不是「0 分」而是「这一趟没算出分」，把它写成基线等于给一条
        论钉死一个永远达不到的基准——此后每趟掉档都是它的错。
        归档那条也拒，理由与 `set_levels` 同一条：`archived` 是终点，改它是在重写历史。
        """
        if score is None:
            raise ValidationError("基线分数不能是空的：那是「没比过」，不是「最差」")
        number = _score("基线分数", score)
        current = self.require(user_id, thesis_id)
        if current.status == ARCHIVED:
            raise StorageError(
                f"{thesis_id} 已经归档：基线是「建仓时那一眼」，归档的论不再有那一眼"
            )
        if current.baseline_score is not None:
            raise StorageError(
                f"{thesis_id} 已经有基线（{current.baseline_on}）：改基线等于改历史，"
                "要换基准就另开一条论"
            )
        cleaned = {str(key): _score(f"分项 {key}", value) for key, value in items.items()}
        self.db.execute(
            "UPDATE theses SET baseline_score = ?, baseline_items = ?, baseline_on = ?,"
            " updated_at = ? WHERE user_id = ? AND id = ? AND baseline_score IS NULL",
            [
                number,
                json.dumps(cleaned, ensure_ascii=False, sort_keys=True),
                on,
                now,
                current.user_id,
                current.id,
            ],
        )
        found = self.get(user_id, thesis_id)
        assert found is not None  # 同一个库里刚 UPDATE 过
        return found

    def advance(
        self,
        user_id: str,
        thesis_id: str,
        to: str,
        *,
        actor_id: str,
        now: datetime,
        note: str | None = None,
    ) -> Thesis:
        """走一跳；不在跳转表上的那一步当场拒，且**不改状态**（拒绝不能留下半个变更）。"""
        _require_user(actor_id, what="actor_id")
        current = self.require(user_id, thesis_id)
        target = _status(to)
        allowed = TRANSITIONS[current.status]
        if target == current.status:
            raise StorageError(
                f"{current.status} → {target} 不是一次变更：状态没动，"
                "而时间线上会多出一条读不出含义的自环"
            )
        if target not in allowed:
            raise StorageError(self._why_blocked(current, target, allowed))
        if target == OPENED:
            missing = [
                label
                for column, label in OPENED_REQUIRED
                if column in missing_levels_for_opened(current)
            ]
            if missing:
                # 状态前置条件不成立，不是参数打错（D81）：补上价位就能重来 ⇒ 退出码 1
                raise StorageError(
                    f"建仓前要先给齐：{'、'.join(missing)}"
                    f"（{'、'.join(column for column, _ in OPENED_REQUIRED)} 三列现在是空的）。"
                    "落进来有两条路：`sams thesis revise <id> --stop .. --target .. --odds ..` "
                    "手填，或 `sams thesis revise <id> --from-plan` 按现在的库重算一次"
                )
        archived = target == ARCHIVED
        self.db.execute(
            "UPDATE theses SET status = ?, updated_at = ?, archived_at = ? "
            "WHERE user_id = ? AND id = ?",
            [
                target,
                now,
                now if archived else None,
                current.user_id,
                current.id,
            ],
        )
        self._event(current.id, current.status, target, actor_id, note, now)
        moved = self.get(user_id, thesis_id)
        assert moved is not None
        return moved

    def _event(
        self,
        thesis_id: str,
        from_status: str | None,
        to_status: str,
        actor_id: str,
        note: str | None,
        ts: datetime,
    ) -> None:
        self.db.execute(
            "INSERT INTO thesis_events "
            "(id, thesis_id, from_status, to_status, actor_id, note, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [new_id(), thesis_id, from_status, to_status, actor_id, note, ts],
        )

    def require(self, user_id: str, thesis_id: str) -> Thesis:
        """按用户取，取不到就说取不到：不存在与不是你的，在这一层是同一种回答。"""
        found = self.get(user_id, thesis_id)
        if found is None:
            raise StorageError(
                f"没有 id 为 {thesis_id!r} 的 thesis（属于别人或不存在，这里都不返回内容）"
            )
        return found

    @staticmethod
    def _why_blocked(current: Thesis, target: str, allowed: tuple[str, ...]) -> str:
        if not allowed:
            return (
                f"`{current.status}` 是终点：一条 thesis 的第二次生命周期会把它的时间线"
                "读成噪声。要再看这只票就另开一条"
            )
        if target == ARCHIVED:
            return (
                f"{current.status} 不能归档：`{ARCHIVED}` 只接 {'、'.join(ARCHIVE_FROM)} "
                "那三个了结状态，而这条还没到那一步（未建仓的观点没有可了结的仓位）"
            )
        return (
            f"{current.status} 只能走到 {', '.join(allowed)}，不能到 {target}："
            "这一步跳过了中间那些状态，它们各自都要留下事件"
        )


#: 能进 `archived` 的那三个（由跳转表推出来，不另写一遍，否则两处会分叉）。
ARCHIVE_FROM = tuple(s for s, nxt in TRANSITIONS.items() if ARCHIVED in nxt)


def _to_theses(rows) -> list[Thesis]:
    return [
        Thesis(
            id=r["id"],
            user_id=r["user_id"],
            market=r["market"],
            code=r["code"],
            style=r["style"],
            status=r["status"],
            entry_low=r["entry_low"],
            entry_high=r["entry_high"],
            stop_price=r["stop_price"],
            target_price=r["target_price"],
            reward_risk=r["reward_risk"],
            win_rate=r["win_rate"],
            win_rate_samples=r["win_rate_samples"],
            levels_on=r["levels_on"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            archived_at=r["archived_at"],
            baseline_score=r["baseline_score"],
            baseline_items=tuple(
                sorted(
                    (str(key), float(value))
                    for key, value in json.loads(r["baseline_items"] or "{}").items()
                )
            ),
            baseline_on=r["baseline_on"],
        )
        for r in rows.to_dicts()
    ]
