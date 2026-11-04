"""核心业务六字段的读模型与保存（S2 / D240）。

屏幕上的每一行只有三种状态，而这三件事在数据库里长得几乎一样：

* **已保存/已确认**：`company_business` 有行。来源可能是用户、计算或概况摘要——
  逐字确认草稿不会把来源改成 `user`，否则下一个人会以为那句话是人写的。
* **系统草稿**：没有行，但能从概况摘出一段、或从同业算出一个地位。草稿必须带
  `draft=True`，屏幕上不许用高亮冒充事实。
* **缺失**：既没有行也没有草稿。四种缺失档由 `core.missing` 给，理由是那一刻的
  下一步动作（同步概况、录入、换市场）。

保存是六字段的整组覆盖：空串删除那一行（回到草稿/缺失），非空写入并记确认人。
「有没有改过」按**当前有效值**逐字比较——拿已保存行去比会漏掉「刚把草稿改了一个字」。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sams.core.business import (
    BUSINESS_FIELD_KEYS,
    BUSINESS_FIELDS,
    BUSINESS_SOURCES,
    SOURCE_COMPUTED,
    SOURCE_PROFILE_SUMMARY,
    SOURCE_USER,
    BusinessFieldSpec,
    field_spec,
    source_label,
)
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import ValidationError
from sams.core.missing import NOT_DISCLOSED, UNCOMPUTABLE
from sams.services.peers import PeerService
from sams.storage.repos.business import CompanyBusinessRepo
from sams.storage.repos.f10 import CompanyProfileRepo

#: 概况摘多长为「核心业务描述」草稿。再长就不是一句话，读的人不会读完。
DRAFT_CHARS = 200

#: 草稿截断时优先在这些标点后收口，避免把一句话拦腰砍断
_SENTENCE_ENDS = "。！？；"


@dataclass(frozen=True)
class BusinessField:
    spec: BusinessFieldSpec
    value: str | None
    source: str = ""
    source_label: str = ""
    #: 来源日期：概况抓取日 / 计算日 / 用户录入日
    as_of: date | None = None
    confirmed: bool = False
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    #: True = 系统给的、尚未确认；屏幕上必须与「已确认」是两种样子
    draft: bool = False
    missing_kind: str = ""
    missing_reason: str = ""


@dataclass(frozen=True)
class BusinessRead:
    market: str
    code: str
    fields: tuple[BusinessField, ...]
    notes: tuple[str, ...] = ()

    def reading(self, key: str) -> BusinessField | None:
        for field in self.fields:
            if field.spec.key == key:
                return field
        return None


@dataclass(frozen=True)
class _Draft:
    value: str | None = None
    source: str = ""
    as_of: date | None = None
    missing_kind: str = ""
    missing_reason: str = ""


class BusinessProfileService:
    def __init__(
        self,
        business: CompanyBusinessRepo,
        profiles: CompanyProfileRepo,
        peers: PeerService,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.business = business
        self.profiles = profiles
        self.peers = peers
        self.clock = clock

    def read(self, market: str, code: str) -> BusinessRead:
        market = canonical_market(market)
        code = canonical_code(market, code)
        stored = self.business.all_for(market, code)
        drafts = self._drafts(market, code)

        fields: list[BusinessField] = []
        for spec in BUSINESS_FIELDS:
            row = stored.get(spec.key)
            if row is not None:
                fields.append(
                    BusinessField(
                        spec=spec,
                        value=row["value"],
                        source=row["source"],
                        source_label=source_label(row["source"]),
                        as_of=_as_date(row.get("as_of")),
                        confirmed=True,
                        confirmed_by=row.get("confirmed_by"),
                        confirmed_at=_as_datetime(row.get("confirmed_at")),
                        draft=False,
                    )
                )
                continue
            draft = drafts.get(spec.key) or _missing(spec)
            fields.append(
                BusinessField(
                    spec=spec,
                    value=draft.value,
                    source=draft.source,
                    source_label=source_label(draft.source),
                    as_of=draft.as_of,
                    draft=draft.value is not None,
                    missing_kind=draft.missing_kind,
                    missing_reason=draft.missing_reason,
                )
            )
        return BusinessRead(market=market, code=code, fields=tuple(fields))

    def save(
        self,
        market: str,
        code: str,
        values: Mapping[str, str],
        *,
        user_id: str,
    ) -> None:
        """六字段整组覆盖：空串删除，非空 upsert；逐字未改的保留原来源与来源日期。"""
        market = canonical_market(market)
        code = canonical_code(market, code)
        unknown = [key for key in values if key not in BUSINESS_FIELD_KEYS]
        if unknown:
            raise ValidationError(
                f"不认识的核心业务字段 {unknown[0]!r}："
                f"已登记的是 {', '.join(BUSINESS_FIELD_KEYS)}"
            )
        texts = {key: (values.get(key) or "").strip() for key in BUSINESS_FIELD_KEYS}
        for key, text in texts.items():
            spec = field_spec(key)
            if len(text) > spec.max_chars:
                raise ValidationError(
                    f"『{spec.label}』最多 {spec.max_chars} 字，收到 {len(text)} 字"
                )

        current = {field.spec.key: field for field in self.read(market, code).fields}
        today = self.clock.today()
        now = self.clock.now_utc()
        for key, text in texts.items():
            if not text:
                # 「清除」不是把空串写成已回答：缺行才会让读取回落到草稿/缺失
                self.business.delete(market, code, key)
                continue
            before = current[key]
            if before.value == text and before.source in BUSINESS_SOURCES:
                source, as_of = before.source, before.as_of or today
            else:
                source, as_of = SOURCE_USER, today
            self.business.upsert(
                market,
                code,
                key,
                text,
                source=source,
                as_of=as_of,
                confirmed_by=user_id,
                confirmed_at=now,
            )

    # --- 草稿 ---------------------------------------------------------------------------

    def _drafts(self, market: str, code: str) -> dict[str, _Draft]:
        comparison = self.peers.compare(market, code)
        return {
            "business": _summary_draft(self._profile(market, code)),
            "position": _position_draft(comparison, self.clock.today()),
        }

    def _profile(self, market: str, code: str) -> dict | None:
        frame = self.profiles.get(market, code)
        if frame.is_empty():
            return None
        return frame.row(0, named=True)


def _missing(spec: BusinessFieldSpec) -> _Draft:
    return _Draft(
        missing_kind=NOT_DISCLOSED,
        missing_reason=f"『{spec.label}』尚未录入",
    )


def _summary_draft(profile: dict | None) -> _Draft:
    raw = (profile or {}).get("summary")
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return _Draft(
            missing_kind=NOT_DISCLOSED,
            missing_reason="公司概况没有公司简介（先 `sams sync profile`）",
        )
    return _Draft(
        value=_first_sentences(text, DRAFT_CHARS),
        source=SOURCE_PROFILE_SUMMARY,
        as_of=_as_date((profile or {}).get("fetched_at")),
    )


def _first_sentences(text: str, limit: int) -> str:
    """在 `limit` 内收口：先找最后一个句末标点，没有才硬截。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(mark) for mark in _SENTENCE_ENDS)
    return head[: cut + 1] if cut >= 0 else head


def _position_draft(comparison: object, today: date) -> _Draft:
    missing = getattr(comparison, "position_missing", "")
    text = getattr(comparison, "position", "") or ""
    if missing:
        reason = text or next(iter(getattr(comparison, "notes", ()) or ()), "")
        return _Draft(
            missing_kind=missing,
            missing_reason=reason or "行业地位无法计算",
        )
    if not text:
        return _Draft(missing_kind=UNCOMPUTABLE, missing_reason="行业地位无法计算")
    return _Draft(value=text, source=SOURCE_COMPUTED, as_of=today)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _as_datetime(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None
