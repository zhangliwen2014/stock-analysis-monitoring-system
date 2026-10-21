"""`rules` 与 `rule_revisions`（spec §10.1、§9.2、§4.2）。

这两张表从 `0001` 就在库里而一个写入方都没有。校验因此**全放在仓库层**：
M3-D 的派生写入方与 M4 的后台都不经过 `services`，挡在服务层的约束对它们是空的（D81）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import StorageError, ValidationError
from sams.core.notify import SEVERITIES
from sams.core.rules import (
    TARGET_TYPES,
    template_for,
    validate_filters,
    validate_params,
)
from sams.storage.db import Database
from sams.storage.ids import new_id

#: 只有这一种条件（§10.4：`expression` 是 M5 的事）
CONDITION_KIND = "template"

_COLUMNS = (
    "id, user_id, account_id, name, enabled, target_type, target_ref, kind,"
    " condition_kind, params_json, filters_json, cooldown_min, severity, channel_ids,"
    " thesis_id, created_at, updated_at"
)

#: 这些 target 的 `target_ref` 恒为「整个集合」，写法里带上 `*` 是为了让它可断言
#: （空串会展开成零个目标，那条规则从此静默不响，而屏幕上它看着是启用的）
_WILDCARD_TARGETS = ("watchlist", "position", "portfolio")


@dataclass(frozen=True)
class Rule:
    id: str
    user_id: str
    name: str
    kind: str
    enabled: bool
    target_type: str
    target_ref: str
    params: dict
    filters: dict
    cooldown_min: int
    severity: str
    channel_ids: list[str]
    account_id: str | None = None
    thesis_id: str | None = None
    condition_kind: str = CONDITION_KIND
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def describe(self) -> str:
        """一行说得清这条规则在等什么（屏幕上那条列表与 `--json` 读同一句）。"""
        return f"{template_for(self.kind).label}｜{self.target_type}:{self.target_ref}"


@dataclass(frozen=True)
class Revision:
    id: str
    rule_id: str
    changed_by: str
    before: dict
    after: dict
    changed_at: datetime | None


def _to_rule(row: Mapping[str, object]) -> Rule:
    return Rule(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        enabled=bool(row["enabled"]),
        target_type=str(row["target_type"]),
        target_ref=str(row["target_ref"]),
        params=json.loads(str(row["params_json"] or "{}")),
        filters=json.loads(str(row["filters_json"] or "{}")),
        cooldown_min=int(row["cooldown_min"]),
        severity=str(row["severity"]),
        channel_ids=list(json.loads(str(row["channel_ids"] or "[]"))),
        account_id=row["account_id"],
        thesis_id=row["thesis_id"],
        condition_kind=str(row["condition_kind"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _snapshot(row: Rule) -> dict:
    """修订史里存的是**整份规则**，不是 diff（§4.2 要回答的是「当时那条线是多少」）。"""
    return {
        "name": row.name,
        "kind": row.kind,
        "params": row.params,
        "filters": row.filters,
        "severity": row.severity,
        "cooldown_min": row.cooldown_min,
        "channel_ids": row.channel_ids,
        "enabled": row.enabled,
        "target_type": row.target_type,
        "target_ref": row.target_ref,
        "account_id": row.account_id,
        "thesis_id": row.thesis_id,
    }


def _to_checked(row: Rule) -> dict:
    """把库里那一行还原成 `_validate` 的那个形状。

    只为了给 `update` 做一次「有没有实质变化」的比较。必须与 `_validate` 的返回键
    一一对应（`user_id` 与 `condition_kind` 也在内）：少一个键，`_deltas` 就会把它
    当成「新出现的字段」，于是每一次改成同样的值都写一条空修订。
    """
    return {
        "user_id": row.user_id,
        "name": row.name,
        "kind": row.kind,
        "params": row.params,
        "filters": row.filters,
        "severity": row.severity,
        "cooldown_min": row.cooldown_min,
        "channel_ids": row.channel_ids,
        "target_type": row.target_type,
        "target_ref": row.target_ref,
        "account_id": row.account_id,
        "thesis_id": row.thesis_id,
        "condition_kind": row.condition_kind,
    }


def _deltas(before: dict, checked: dict) -> dict:
    """两个形状相同的字典之间，哪些字段真的变了。

    只比 `update` 改得到的那些字段：`enabled` 走 `set_enabled`，时间戳每次都在动，
    把它们算进「实质变化」就等于每条规则每天都有一条修订史。
    """
    return {key: value for key, value in checked.items() if before.get(key) != value}


class RuleRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    # --- 写 -----------------------------------------------------------------------------

    def create(
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
        cooldown_min: int = 30,
        enabled: bool = True,
        condition_kind: str = CONDITION_KIND,
    ) -> Rule:
        checked = self._validate(
            user_id=user_id,
            name=name,
            kind=kind,
            target_type=target_type,
            target_ref=target_ref,
            params=params,
            filters=filters,
            account_id=account_id,
            thesis_id=thesis_id,
            severity=severity,
            channel_ids=channel_ids,
            cooldown_min=cooldown_min,
            condition_kind=condition_kind,
        )
        rule_id = new_id()
        now = self.clock.now_utc()
        self.db.execute(
            "INSERT INTO rules"
            " (id, user_id, account_id, name, enabled, target_type, target_ref, kind,"
            " condition_kind, params_json, filters_json, cooldown_min, severity, channel_ids,"
            " thesis_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                rule_id,
                checked["user_id"],
                checked["account_id"],
                checked["name"],
                bool(enabled),
                checked["target_type"],
                checked["target_ref"],
                checked["kind"],
                checked["condition_kind"],
                json.dumps(checked["params"], ensure_ascii=False),
                json.dumps(checked["filters"], ensure_ascii=False),
                checked["cooldown_min"],
                checked["severity"],
                json.dumps(checked["channel_ids"], ensure_ascii=False),
                checked["thesis_id"],
                now,
                now,
            ],
        )
        created = self.get(rule_id)
        assert created is not None
        return created

    #: 允许挂在整个组合上的 kind（现在一个都没有：组合级指标缺输入，U28）
    _PORTFOLIO_KINDS: tuple[str, ...] = ()

    def _validate(
        self,
        *,
        user_id: str,
        name: str,
        kind: str,
        target_type: str,
        target_ref: str,
        params: Mapping[str, object] | None,
        filters: Mapping[str, object] | None,
        account_id: str | None,
        thesis_id: str | None,
        severity: str | None,
        channel_ids: Sequence[str],
        cooldown_min: int,
        condition_kind: str = CONDITION_KIND,
    ) -> dict:
        """`create` 与 `update` 共用这一套校验。

        抽出来不是为了少写几行：`update` 必须把**旧值与新值合并后**的那一份重走一遍创建时
        的全部判据，而两处各写一遍是这个文件最容易分叉的地方——分叉的后果长成「创建时不许
        的东西，改的时候改进去了」。返回校验过的同名字典，调用方只管落库。
        """
        if not user_id:
            raise ValidationError("规则必须属于一个用户：没有归属就没有路由")
        label = (name or "").strip()
        if not label:
            raise ValidationError("规则要有个名字：`sams alerts` 里那一条要说清是谁在响")
        built = template_for(kind)  # 未知 kind 在这里就拒，顺带把名单的回显给出去
        if condition_kind != CONDITION_KIND:
            raise ValidationError(
                f"condition_kind 只能是 {CONDITION_KIND!r}：受限表达式是 M5 的事（§10.4），"
                "现在收进来只会得到一条没人求值的规则"
            )
        if target_type not in TARGET_TYPES:
            raise ValidationError(
                f"未知对象类型 target_type={target_type!r}：只有 {', '.join(TARGET_TYPES)}"
            )
        if target_type not in built.targets:
            allowed = "/".join(built.targets)
            raise ValidationError(
                f"{kind} 不能挂在 {target_type} 上：这一种只认 {allowed}。"
                "名单收在模板表里而不是放开全部五种，因为 `expand()` 要能对每一种给出确定的"
                "展开方式——挂错对象的规则不会报错，只会静默地不响（§1 那张表的第一行）"
            )
        self._require_target_shape(target_type, target_ref)
        if built.needs_position and not account_id:
            raise ValidationError(
                f"{kind}（{built.label}）必须挂在某个账户上：去重键含 account_id，"
                "缺了它，同一只票在两个账户里的破位会互相吞（§10.1）"
            )
        if target_type == "portfolio" and not account_id:
            raise ValidationError("组合类规则必须说明是哪个账户的组合")
        if kind not in self._PORTFOLIO_KINDS and target_type == "portfolio":
            raise ValidationError(
                f"{kind} 不支持挂在整个组合上：这一层没有组合级指标的输入（U28）"
            )
        if built.needs_thesis and not thesis_id:
            raise ValidationError(f"{kind} 必须挂在一条论上：它读的是那条论的价位与分项")
        if target_type == "thesis" and thesis_id is not None and target_ref != thesis_id:
            raise ValidationError(
                "target_type=thesis 时 target_ref 就是那条论的 id："
                "两者不一致会让 expand() 取到一条论而校验另一条"
            )
        self._require_thesis(user_id, thesis_id)
        checked_params = validate_params(kind, params)
        checked_filters = validate_filters(filters)
        level = built.severity if severity is None else severity
        if level not in SEVERITIES:
            raise ValidationError(f"未知严重级别 severity={level!r}：只有 {list(SEVERITIES)}")
        if int(cooldown_min) < 0:
            raise ValidationError(f"cooldown_min 不能为负（收到 {cooldown_min}）")
        return {
            "user_id": user_id,
            "name": label,
            "kind": kind,
            "target_type": target_type,
            "target_ref": target_ref,
            "params": checked_params,
            "filters": checked_filters,
            "severity": level,
            "cooldown_min": int(cooldown_min),
            "channel_ids": list(channel_ids),
            "account_id": account_id,
            "thesis_id": thesis_id,
            "condition_kind": condition_kind,
        }

    def update(
        self,
        rule_id: str,
        *,
        changed_by: str,
        name: str | None = None,
        kind: str | None = None,
        params: Mapping[str, object] | None = None,
        filters: Mapping[str, object] | None = None,
        severity: str | None = None,
        channel_ids: Sequence[str] | None = None,
        cooldown_min: int | None = None,
        target_type: str | None = None,
        target_ref: str | None = None,
    ) -> Rule:
        """一次改动写一条修订史（§4.2）。没有实质变化 ⇒ 不写。"""
        before = self._require(rule_id)
        patch = {
            key: value
            for key, value in {
                "name": name,
                "kind": kind,
                "params": params,
                "filters": filters,
                "severity": severity,
                "channel_ids": channel_ids,
                "cooldown_min": cooldown_min,
                "target_type": target_type,
                "target_ref": target_ref,
            }.items()
            if value is not None
        }
        if not patch:
            raise ValidationError("什么都没要改：`rule edit` 至少给一个参数")
        # 用新值重走一遍创建时的那整套校验，所以「改 kind 而旧参数不合」在这里就红
        merged = {
            "user_id": before.user_id,
            "name": patch.get("name", before.name),
            "kind": patch.get("kind", before.kind),
            "target_type": patch.get("target_type", before.target_type),
            "target_ref": patch.get("target_ref", before.target_ref),
            "params": patch.get("params", before.params),
            "filters": patch.get("filters", before.filters),
            "severity": patch.get("severity", before.severity),
            "cooldown_min": patch.get("cooldown_min", before.cooldown_min),
            "account_id": before.account_id,
            "thesis_id": before.thesis_id,
            "channel_ids": patch.get("channel_ids", before.channel_ids),
        }
        if kind is not None and params is None:
            # 换 kind 而不给新参数：旧参数对新 kind 多半是野键，让它在这里就红，
            # 而不是带着一个没人读的键进库（屏幕上那条规则于是显示着一个不生效的数）
            raise ValidationError(
                f"改成 {kind} 必须同时给新的 params：旧的那些属于 {before.kind}，"
                f"留着就是屏幕上一个不生效的键。{template_for(kind).describe()}"
            )
        checked = self._validate(**merged)
        if not _deltas(_to_checked(before), checked):
            # 一次「改成同样的值」不在修订史里留一行：§4.2 那张表要能回答「这条线改过几次」，
            # 把空改动算进去就是在数橡皮擦的印子
            return before
        self.db.execute(
            "UPDATE rules SET name = ?, kind = ?, target_type = ?, target_ref = ?,"
            " params_json = ?, filters_json = ?, severity = ?, cooldown_min = ?,"
            " channel_ids = ?, updated_at = ? WHERE id = ?",
            [
                checked["name"],
                checked["kind"],
                checked["target_type"],
                checked["target_ref"],
                json.dumps(checked["params"], ensure_ascii=False),
                json.dumps(checked["filters"], ensure_ascii=False),
                checked["severity"],
                checked["cooldown_min"],
                json.dumps(checked["channel_ids"], ensure_ascii=False),
                self.clock.now_utc(),
                before.id,
            ],
        )
        after = self.get(before.id)
        assert after is not None
        self.db.execute(
            "INSERT INTO rule_revisions (id, rule_id, changed_by, before_json, after_json,"
            " changed_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                new_id(),
                before.id,
                changed_by,
                json.dumps(_snapshot(before), ensure_ascii=False),
                json.dumps(_snapshot(after), ensure_ascii=False),
                self.clock.now_utc(),
            ],
        )
        return after

    def set_enabled(self, rule_id: str, enabled: bool, *, user_id: str | None = None) -> Rule:
        row = self._require(rule_id, user_id=user_id)
        self.db.execute(
            "UPDATE rules SET enabled = ?, updated_at = ? WHERE id = ?",
            [bool(enabled), self.clock.now_utc(), row.id],
        )
        return self._require(rule_id, user_id=user_id)

    def remove(self, rule_id: str, *, user_id: str | None = None) -> bool:
        row = self._require(rule_id, user_id=user_id)
        if row.thesis_id:
            raise ValidationError(
                f"规则 {row.name} 是从一条观点派生出来的，不能删除，只能停用"
                "（`sams rule disable`）：「悄悄关掉止损提醒」必须留下一条能查到的记录（§9.2）"
            )
        self.db.execute("DELETE FROM rule_revisions WHERE rule_id = ?", [row.id])
        self.db.execute("DELETE FROM rules WHERE id = ?", [row.id])
        return True

    # --- 读 -----------------------------------------------------------------------------

    def get(self, rule_id: str, *, user_id: str | None = None) -> Rule | None:
        sql = f"SELECT {_COLUMNS} FROM rules WHERE id = ?"
        params: list[object] = [rule_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        rows = self.db.query(sql, params)
        return _to_rule(rows.to_dicts()[0]) if rows.height else None

    def list_for(self, user_id: str, *, enabled_only: bool = False) -> list[Rule]:
        sql = f"SELECT {_COLUMNS} FROM rules WHERE user_id = ?"
        params: list[object] = [user_id]
        if enabled_only:
            sql += " AND enabled = TRUE"
        sql += " ORDER BY created_at, id"
        return [_to_rule(row) for row in self.db.query(sql, params).to_dicts()]

    def by_thesis(self, thesis_id: str) -> list[Rule]:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM rules WHERE thesis_id = ? ORDER BY created_at, id",
            [thesis_id],
        )
        return [_to_rule(row) for row in rows.to_dicts()]

    def revisions(self, rule_id: str) -> list[Revision]:
        rows = self.db.query(
            "SELECT id, rule_id, changed_by, before_json, after_json, changed_at"
            " FROM rule_revisions WHERE rule_id = ? ORDER BY changed_at, id",
            [rule_id],
        )
        return [
            Revision(
                id=str(row["id"]),
                rule_id=str(row["rule_id"]),
                changed_by=str(row["changed_by"]),
                before=json.loads(str(row["before_json"] or "{}")),
                after=json.loads(str(row["after_json"] or "{}")),
                changed_at=row["changed_at"],
            )
            for row in rows.to_dicts()
        ]

    def counts(self, user_id: str) -> dict[str, int]:
        rows = self.db.query(
            "SELECT count(*) total,"
            " coalesce(sum(CASE WHEN enabled THEN 1 ELSE 0 END), 0) enabled_n"
            " FROM rules WHERE user_id = ?",
            [user_id],
        ).to_dicts()[0]
        return {"total": int(rows["total"]), "enabled": int(rows["enabled_n"])}

    # --- 内部 ---------------------------------------------------------------------------

    def _require(self, rule_id: str, *, user_id: str | None = None) -> Rule:
        row = self.get(rule_id, user_id=user_id)
        if row is None:
            tail = "" if user_id is None else "（或不属于这个用户）"
            raise StorageError(f"规则 {rule_id} 不存在{tail}")
        return row

    def _require_target_shape(self, target_type: str, target_ref: str) -> None:
        ref = (target_ref or "").strip()
        if not ref:
            raise ValidationError(
                f"target_ref 不能是空的：{target_type} 类型的规则要能展开成一个具体对象"
                "（空引用会展开成零个目标，那条规则从此静默不响）"
            )
        if target_type in _WILDCARD_TARGETS:
            if ref != "*":
                raise ValidationError(
                    f"target_type={target_type} 时 target_ref 要写 `*`（整个集合），收到 {ref!r}"
                )
            return
        if target_type == "symbol" and "/" not in ref:
            raise ValidationError(
                f"target_ref 要写成 市场/代码（例如 A/601088），收到 {ref!r}："
                "只有代码时 A 股与港股会撞同一个键"
            )

    def _require_thesis(self, user_id: str, thesis_id: str | None) -> None:
        if thesis_id is None:
            return
        from sams.storage.repos.theses import ThesisRepo

        if ThesisRepo(self.db).get(user_id, thesis_id) is None:
            raise ValidationError(
                f"观点 {thesis_id} 不存在（或不属于这个用户）：一条读不到价位的规则"
                "会永远显示成「没触发」"
            )
