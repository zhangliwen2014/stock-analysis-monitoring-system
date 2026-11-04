"""关键词与命中记录（S3-C，D245/D246）。

关键词只在**保存时**过一遍作用域与长度：`(用户, 市场, 代码)` 下同一个词只留一行，
大小写不同也算同一个。`code IS NULL` 是该市场的全部自选/持仓/观察——DuckDB 的唯一
索引把 NULL 当互不相同，所以查重走 `IS NOT DISTINCT FROM`，不建唯一索引。

命中记录是不可变的：同一 `(用户, 来源, 资讯)` 对同一个关键词只插一次。主键既是
去重键也是「重跑同一晚不会再推一遍」的保证；删除关键词不回删命中——命中是
「当时确实提醒过」的历史，不是关键词的附件。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import Conflict, ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

logger = logging.getLogger("sams.storage.news_keywords")

#: 关键词长度按字符数算：中文一个词两个字，按字节算会把正常词挡在门外
KEYWORD_MAX_CHARS = 40

_KEYWORD_COLUMNS = (
    "id, user_id, market, code, keyword, channel_ids, enabled, created_at"
)


def _moment(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValidationError("命中时刻必须带时区（TIMESTAMPTZ 会按会话时区解释裸值）")
    return value.astimezone(UTC)


def _channels(raw: object) -> tuple[str, ...]:
    try:
        values = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        logger.warning("news_keywords.channel_ids 不是 JSON：%r", raw)
        return ()
    if not isinstance(values, list):
        logger.warning("news_keywords.channel_ids 不是数组：%r", raw)
        return ()
    return tuple(str(value) for value in values if str(value).strip())


@dataclass(frozen=True)
class NewsKeyword:
    id: str
    user_id: str
    market: str
    code: str | None
    keyword: str
    channel_ids: tuple[str, ...]
    enabled: bool
    created_at: datetime


@dataclass(frozen=True)
class NewsHit:
    """一条命中的可读形状：摘要要印标题与原文链接，不只印 id。"""

    source: str
    item_id: str
    keyword_id: str
    keyword: str
    hit_at: datetime
    market: str
    code: str
    title: str
    url: str
    summary: str | None
    published_at: datetime | None


class NewsKeywordRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def add(
        self,
        user_id: str,
        market: str,
        keyword: str,
        *,
        code: str | None = None,
        channel_ids: tuple[str, ...] | list[str] = (),
        enabled: bool = True,
    ) -> NewsKeyword:
        if not user_id:
            raise ValidationError("关键词必须属于一个用户：没有主人就没有作用域")
        market = canonical_market(market)
        canonical = canonical_code(market, code) if code else None
        text = (keyword or "").strip()
        if not 1 <= len(text) <= KEYWORD_MAX_CHARS:
            raise ValidationError(
                f"关键词要 1–40 个字符（收到 {len(text)}）：{text!r}"
            )
        if self._duplicate(user_id, market, canonical, text):
            scope = f"{market}/{canonical}" if canonical else f"{market} 全部标的"
            raise Conflict(f"关键词 {text!r} 在 {scope} 下已经存在")
        keyword_id = new_id()
        self.db.execute(
            "INSERT INTO news_keywords"
            " (id, user_id, market, code, keyword, channel_ids, enabled, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                keyword_id,
                user_id,
                market,
                canonical,
                text,
                json.dumps(list(channel_ids), ensure_ascii=False),
                bool(enabled),
                self.clock.now_utc(),
            ],
        )
        got = self.get(keyword_id, user_id)
        assert got is not None  # 刚写完，同一条连接里一定读得回来
        return got

    def get(self, keyword_id: str, user_id: str | None = None) -> NewsKeyword | None:
        clauses, params = ["id = ?"], [keyword_id]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        rows = self.db.query(
            f"SELECT {_KEYWORD_COLUMNS} FROM news_keywords"
            f" WHERE {' AND '.join(clauses)}",
            params,
        )
        return self._to_keyword(rows.to_dicts()[0]) if rows.height else None

    def list_for(
        self,
        user_id: str,
        *,
        market: str | None = None,
        enabled_only: bool = False,
    ) -> list[NewsKeyword]:
        clauses, params = ["user_id = ?"], [user_id]
        if market:
            clauses.append("market = ?")
            params.append(canonical_market(market))
        if enabled_only:
            clauses.append("enabled = TRUE")
        rows = self.db.query(
            f"SELECT {_KEYWORD_COLUMNS} FROM news_keywords"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at, id",
            params,
        )
        return [self._to_keyword(row) for row in rows.to_dicts()]

    def users(self, market: str) -> list[str]:
        """这个市场上有启用关键词的用户（只取有词的人：没人关心就不必扫资讯）。"""
        rows = self.db.query(
            "SELECT DISTINCT user_id FROM news_keywords"
            " WHERE market = ? AND enabled = TRUE ORDER BY user_id",
            [canonical_market(market)],
        )
        return [str(value) for value in rows["user_id"].to_list()]

    def remove(self, keyword_id: str, user_id: str) -> bool:
        changed = self.db.scalar(
            "DELETE FROM news_keywords WHERE id = ? AND user_id = ? RETURNING id",
            [keyword_id, user_id],
        )
        return changed is not None

    def _duplicate(
        self, user_id: str, market: str, code: str | None, keyword: str
    ) -> bool:
        return bool(
            self.db.scalar(
                "SELECT EXISTS (SELECT 1 FROM news_keywords"
                " WHERE user_id = ? AND market = ? AND code IS NOT DISTINCT FROM ?"
                " AND lower(keyword) = lower(?))",
                [user_id, market, code, keyword],
            )
        )

    @staticmethod
    def _to_keyword(row: dict) -> NewsKeyword:
        return NewsKeyword(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            market=str(row["market"]),
            code=None if row["code"] is None else str(row["code"]),
            keyword=str(row["keyword"]),
            channel_ids=_channels(row["channel_ids"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )


class NewsHitRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def record(
        self,
        user_id: str,
        source: str,
        item_id: str,
        keyword_id: str,
        *,
        hit_at: datetime | None = None,
    ) -> bool:
        """返回 True 表示这一对是**新的**；重复命中是 False，不报错。"""
        if not all(part.strip() for part in (user_id, source, item_id, keyword_id)):
            raise ValidationError("命中必须说清用户、来源、资讯与关键词")
        moment = _moment(hit_at) if hit_at is not None else self.clock.now_utc()
        inserted = self.db.scalar(
            "INSERT INTO news_hits (user_id, source, item_id, keyword_id, hit_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING user_id",
            [user_id, source, item_id, keyword_id, moment],
        )
        return inserted is not None

    def has(self, user_id: str, source: str, item_id: str, keyword_id: str) -> bool:
        return bool(
            self.db.scalar(
                "SELECT EXISTS (SELECT 1 FROM news_hits"
                " WHERE user_id = ? AND source = ? AND item_id = ? AND keyword_id = ?)",
                [user_id, source, item_id, keyword_id],
            )
        )

    def list_window(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[NewsHit]:
        """`hit_at` 落在 `[since, until)` 的命中，连同资讯标题与关键词。

        资讯被清理后那一行不再出现在摘要里（INNER JOIN）：点不开的旧闻不该占一行。
        关键词被删掉时仍保留命中，但把词面写成「已删除」——摘要不能说一个空词。
        两端都可省略：个股卡要「最近 5 条」时没有窗口。
        """
        clauses, params = ["h.user_id = ?"], [user_id]
        if since is not None:
            clauses.append("h.hit_at >= ?")
            params.append(_moment(since))
        if until is not None:
            clauses.append("h.hit_at < ?")
            params.append(_moment(until))
        rows = self.db.query(
            "SELECT h.source, h.item_id, h.keyword_id, h.hit_at,"
            " coalesce(k.keyword, '(已删除)') AS keyword,"
            " n.market, n.code, n.title, n.url, n.summary, n.published_at"
            " FROM news_hits h"
            " INNER JOIN news_items n ON n.source = h.source AND n.item_id = h.item_id"
            " LEFT JOIN news_keywords k ON k.id = h.keyword_id"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY h.hit_at DESC, h.source, h.item_id, k.keyword, h.keyword_id",
            params,
        )
        return [
            NewsHit(
                source=str(row["source"]),
                item_id=str(row["item_id"]),
                keyword_id=str(row["keyword_id"]),
                keyword=str(row["keyword"]),
                hit_at=row["hit_at"],
                market=str(row["market"]),
                code=str(row["code"]),
                title=str(row["title"]),
                url=str(row["url"]),
                summary=None if row["summary"] is None else str(row["summary"]),
                published_at=row["published_at"],
            )
            for row in rows.to_dicts()
        ]
