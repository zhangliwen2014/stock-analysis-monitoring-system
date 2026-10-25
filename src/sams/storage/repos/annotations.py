"""批注仓库：五个挂载目标的读写、可见性过滤与编辑历史（M4-B §4）。

可见性过滤下沉到 SQL：先按 `visibility/user_id` 过滤，再按目标过滤。author 校验
同样在这里（写成 `user_id = ?`），服务层只负责回答「这个目标是不是你的」。
「查全量再让上层过滤」的接口形态一旦存在，第一个忘了过滤的入口不会报错，
只会把别人的私有笔记显示在屏幕上。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import Conflict, ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

logger = logging.getLogger("sams.storage.annotations")

#: 五个挂载目标（spec §4.1）。`team` 只对 symbol 生效，判据写在读取 SQL 里。
TARGET_TYPES = ("symbol", "thesis", "rule", "alert", "order")
SYMBOL, THESIS, RULE, ALERT, ORDER = TARGET_TYPES
VISIBILITIES = ("private", "team")
PRIVATE, TEAM = VISIBILITIES
MAX_PINNED = 3

_ANNOTATION_COLUMNS = (
    "id, user_id, target_type, target_ref, target_label, body_md, tags_json, pinned,"
    " visibility, created_at, updated_at, deleted_at, deleted_by, purged_at"
)
_REVISION_COLUMNS = "id, annotation_id, user_id, before_md, after_md, meta_json, edited_at"

#: 标签精确匹配、大小写不敏感。`%`/`_` 在这里没有特殊含义——这正是「搜索不是 LIKE」。
_TAG_MATCH = (
    "list_contains(list_transform("
    "CAST(from_json(tags_json, '[\"VARCHAR\"]') AS VARCHAR[]), x -> lower(x)), lower(?))"
)


@dataclass(frozen=True)
class Annotation:
    id: str
    user_id: str
    target_type: str
    target_ref: str
    target_label: str
    body_md: str
    tags: list[str]
    pinned: bool
    visibility: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    deleted_by: str | None = None
    purged_at: datetime | None = None


@dataclass(frozen=True)
class Revision:
    id: str
    annotation_id: str
    user_id: str
    before_md: str
    after_md: str
    meta: dict
    edited_at: datetime


def _require_user(user_id: str) -> str:
    if not user_id:
        raise ValidationError("user_id 不能为空")
    return user_id


def _moment(value: datetime) -> datetime:
    """带时区才收（与 `repos/alerts.py` 同一条规则）。"""
    if value.tzinfo is None:
        raise ValidationError("时刻必须带时区（例如 2026-10-25T09:00+08:00）")
    return value.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    """库里读出来的值按 UTC 解释；裸值只可能来自旧行，不当成本地时间。"""
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _reject_team_on_non_symbol(target_type: str, visibility: str) -> None:
    if visibility == TEAM and target_type != SYMBOL:
        raise ValidationError(
            "visibility=team 只对 symbol 生效：别的目标上一条团队批注"
            "等于泄露「这个对象存在」"
        )


def _json_list(raw: object) -> list[str]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
    except json.JSONDecodeError:
        logger.warning("批注的 tags_json 解不开：%r", raw)
        return []
    return [str(item) for item in parsed or []]


def _json_dict(raw: object) -> dict:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
    except json.JSONDecodeError:
        logger.warning("批注 revision 的 meta_json 解不开：%r", raw)
        return {}
    return dict(parsed or {})


class AnnotationRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    # --- 写 -----------------------------------------------------------------------------

    def create(
        self,
        user_id: str,
        *,
        target_type: str,
        target_ref: str,
        target_label: str,
        body_md: str,
        tags: Sequence[str] = (),
        pinned: bool = False,
        visibility: str = PRIVATE,
    ) -> Annotation:
        _require_user(user_id)
        if target_type not in TARGET_TYPES:
            raise ValidationError(
                f"未知批注目标 target_type={target_type!r}：只有 {list(TARGET_TYPES)}"
            )
        ref = (target_ref or "").strip()
        if not ref:
            raise ValidationError("target_ref 不能为空：一条批注必须说清它是关于什么的")
        if visibility not in VISIBILITIES:
            raise ValidationError(f"未知可见性 {visibility!r}：只有 {list(VISIBILITIES)}")
        _reject_team_on_non_symbol(target_type, visibility)
        body = body_md or ""
        if not body.strip():
            raise ValidationError("批注正文不能为空")
        if pinned and self._pinned_count(target_type, ref) >= MAX_PINNED:
            raise ValidationError(
                f"同一个目标最多置顶 {MAX_PINNED} 条：再多，置顶就不再是「先看这个」"
            )
        moment = self.clock.now_utc()
        annotation_id = new_id()
        self.db.execute(
            "INSERT INTO annotations"
            " (id, user_id, target_type, target_ref, target_label, body_md, tags_json,"
            " pinned, visibility, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                annotation_id,
                user_id,
                target_type,
                ref,
                (target_label or "").strip(),
                body,
                json.dumps(list(tags), ensure_ascii=False),
                bool(pinned),
                visibility,
                moment,
                moment,
            ],
        )
        created = self.get(annotation_id)
        assert created is not None  # 刚写完，同一条连接里一定读得回来
        return created

    def update(
        self,
        annotation_id: str,
        *,
        user_id: str,
        body_md: str,
        tags: Sequence[str],
        visibility: str,
        expected_updated_at: datetime | None = None,
    ) -> Annotation:
        """编辑正文/标签/可见性，并在同一个事务里写一条 revision。

        乐观并发：表单带回来的 `updated_at` 与服务端不一致就 409，不静默覆盖——
        两个人同时开着编辑器时，后提交的那个人的整段改写会吞掉前一个人的理由。
        护栏在 UPDATE 的 WHERE 里而不是只在读的时候：读与写之间隔着一次网络往返，
        只靠先读后判，两个人同时提交仍然会双双成功、只有一条 revision 说得清发生了什么。
        """
        row = self.get(annotation_id)
        if row is None:
            raise ValidationError(f"批注 {annotation_id} 不存在或已删除")
        if row.user_id != user_id:
            raise ValidationError(f"批注 {annotation_id} 不属于 {user_id}")
        if expected_updated_at is not None and _moment(expected_updated_at) != _utc(
            row.updated_at
        ):
            raise Conflict(
                f"批注 {annotation_id} 在你编辑期间被改过：刷新后再改，"
                "这里不合并、不覆盖"
            )
        if visibility not in VISIBILITIES:
            raise ValidationError(f"未知可见性 {visibility!r}：只有 {list(VISIBILITIES)}")
        _reject_team_on_non_symbol(row.target_type, visibility)
        body = body_md or ""
        if not body.strip():
            raise ValidationError("批注正文不能为空")
        moment = self.clock.now_utc()
        previous_updated = _utc(row.updated_at)
        if moment <= previous_updated:
            # 同一时钟刻度里的第二次编辑也要让 updated_at 前进，否则条件更新分不出新旧
            moment = previous_updated + timedelta(microseconds=1)
        meta = {
            "before": {"tags": row.tags, "visibility": row.visibility},
            "after": {"tags": list(tags), "visibility": visibility},
        }
        with self.db.transaction():
            if expected_updated_at is not None:
                # 条件写入：版本在读到之后又变了，这一句就一行都不改，不会覆盖别人的编辑。
                sql = (
                    "UPDATE annotations SET body_md = ?, tags_json = ?, visibility = ?,"
                    " updated_at = ? WHERE id = ? AND updated_at = ? RETURNING id"
                )
                params = [
                    body,
                    json.dumps(list(tags), ensure_ascii=False),
                    visibility,
                    moment,
                    annotation_id,
                    previous_updated,
                ]
                changed = self.db.scalar(sql, params)
                if changed is None:
                    raise Conflict(
                        f"批注 {annotation_id} 在你编辑期间被改过：刷新后再改，"
                        "这里不合并、不覆盖"
                    )
            else:
                # 内部调用方（迁移、服务批量改写）不带版本号：按 id 改，仍在同一事务里。
                self.db.execute(
                    "UPDATE annotations SET body_md = ?, tags_json = ?, visibility = ?,"
                    " updated_at = ? WHERE id = ?",
                    [
                        body,
                        json.dumps(list(tags), ensure_ascii=False),
                        visibility,
                        moment,
                        annotation_id,
                    ],
                )
            # 只有更新真的落地过才写 revision：否则「改过」的版本会多出一条没发生的编辑。
            self.db.execute(
                "INSERT INTO annotation_revisions"
                " (id, annotation_id, user_id, before_md, after_md, meta_json, edited_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    new_id(),
                    annotation_id,
                    user_id,
                    row.body_md,
                    body,
                    json.dumps(meta, ensure_ascii=False),
                    moment,
                ],
            )
        return self._reload(annotation_id)

    def set_pinned(self, annotation_id: str, *, user_id: str, pinned: bool) -> Annotation:
        """置顶不是编辑：不写 revision，也不动 `updated_at`（开着编辑器的人不该因此 409）。"""
        row = self.get(annotation_id)
        if row is None:
            raise ValidationError(f"批注 {annotation_id} 不存在或已删除")
        if row.user_id != user_id:
            raise ValidationError(f"批注 {annotation_id} 不属于 {user_id}")
        if bool(row.pinned) == bool(pinned):
            return row
        if pinned:
            count = self._pinned_count(
                row.target_type, row.target_ref, exclude_id=annotation_id
            )
            if count >= MAX_PINNED:
                raise ValidationError(
                    f"同一个目标最多置顶 {MAX_PINNED} 条：再多，置顶就不再是「先看这个」"
                )
        self.db.execute(
            "UPDATE annotations SET pinned = ? WHERE id = ?", [bool(pinned), annotation_id]
        )
        return self._reload(annotation_id)

    def soft_delete(self, annotation_id: str, *, user_id: str) -> None:
        """软删除：列表与目标页立即隐藏；正文保留 30 天，prune 再硬清除（D136）。"""
        row = self.get(annotation_id)
        if row is None:
            raise ValidationError(f"批注 {annotation_id} 不存在或已删除")
        if row.user_id != user_id:
            raise ValidationError(f"批注 {annotation_id} 不属于 {user_id}")
        self.db.execute(
            "UPDATE annotations SET deleted_at = ?, deleted_by = ?, updated_at = ?"
            " WHERE id = ?",
            [self.clock.now_utc(), user_id, self.clock.now_utc(), annotation_id],
        )

    # --- 读 -----------------------------------------------------------------------------

    def get(self, annotation_id: str, *, include_deleted: bool = False) -> Annotation | None:
        sql = f"SELECT {_ANNOTATION_COLUMNS} FROM annotations WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL AND purged_at IS NULL"
        rows = self.db.query(sql, [annotation_id])
        return self._to_annotation(rows.to_dicts()[0]) if rows.height else None

    def list_visible(
        self,
        viewer_id: str,
        *,
        team_sharing: bool = False,
        mine_only: bool = False,
        target_type: str | None = None,
        target_ref: str | None = None,
        q: str | None = None,
        tag: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Annotation]:
        _require_user(viewer_id)
        if target_type is not None and target_type not in TARGET_TYPES:
            raise ValidationError(
                f"未知批注目标 target_type={target_type!r}：只有 {list(TARGET_TYPES)}"
            )
        if limit <= 0:
            raise ValidationError("limit 必须为正数")
        clauses = ["deleted_at IS NULL", "purged_at IS NULL"]
        params: list[object] = []
        if mine_only or not team_sharing:
            clauses.append("user_id = ?")
            params.append(viewer_id)
        else:
            # team 只对 symbol 生效：别的目标上一条 team 批注等于泄露「这个对象存在」
            clauses.append("(user_id = ? OR (visibility = 'team' AND target_type = 'symbol'))")
            params.append(viewer_id)
        if target_type is not None:
            clauses.append("target_type = ?")
            params.append(target_type)
        if target_ref is not None:
            clauses.append("target_ref = ?")
            params.append(target_ref)
        if q:
            clauses.append("contains(lower(body_md), lower(?))")
            params.append(q)
        if tag:
            clauses.append(_TAG_MATCH)
            params.append(tag)
        sql = (
            f"SELECT {_ANNOTATION_COLUMNS} FROM annotations"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY pinned DESC, created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params += [int(limit), int(offset)]
        return [self._to_annotation(row) for row in self.db.query(sql, params).to_dicts()]

    def revisions(self, annotation_id: str) -> list[Revision]:
        rows = self.db.query(
            f"SELECT {_REVISION_COLUMNS} FROM annotation_revisions"
            " WHERE annotation_id = ? ORDER BY edited_at, id",
            [annotation_id],
        )
        return [
            Revision(
                id=str(row["id"]),
                annotation_id=str(row["annotation_id"]),
                user_id=str(row["user_id"]),
                before_md=str(row["before_md"] or ""),
                after_md=str(row["after_md"] or ""),
                meta=_json_dict(row["meta_json"]),
                edited_at=row["edited_at"],
            )
            for row in rows.to_dicts()
        ]

    def _pinned_count(
        self, target_type: str, target_ref: str, *, exclude_id: str | None = None
    ) -> int:
        sql = (
            "SELECT count(*) FROM annotations"
            " WHERE target_type = ? AND target_ref = ? AND pinned"
            " AND deleted_at IS NULL AND purged_at IS NULL"
        )
        params: list[object] = [target_type, target_ref]
        if exclude_id is not None:
            sql += " AND id <> ?"
            params.append(exclude_id)
        return int(self.db.scalar(sql, params) or 0)

    def _reload(self, annotation_id: str) -> Annotation:
        row = self.get(annotation_id)
        assert row is not None  # 刚写完，同一条连接里一定读得回来
        return row

    def _to_annotation(self, row: dict) -> Annotation:
        return Annotation(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            target_type=str(row["target_type"]),
            target_ref=str(row["target_ref"]),
            target_label=str(row["target_label"] or ""),
            body_md=str(row["body_md"] or ""),
            tags=_json_list(row["tags_json"]),
            pinned=bool(row["pinned"]),
            visibility=str(row["visibility"] or PRIVATE),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
            deleted_by=row["deleted_by"],
            purged_at=row["purged_at"],
        )
