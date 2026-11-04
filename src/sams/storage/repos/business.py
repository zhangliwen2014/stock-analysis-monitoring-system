"""核心业务六字段的存储（D240）。

一票 × 一字段：每个字段各自带 `source` / `as_of` / `confirmed_by` / `confirmed_at`，
重写是覆盖。空值不落行——读取侧靠「缺行」回落草稿或缺失；写一行空串会让屏幕以为
「用户确认了一个空答案」。字段名与长度只从 `core.business` 取，不在这里另抄一份。
"""

from __future__ import annotations

from datetime import date, datetime

from sams.core.business import BUSINESS_FIELD_KEYS, BUSINESS_SOURCES, field_spec
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import ValidationError
from sams.storage.db import Database


class CompanyBusinessRepo:
    TABLE = "company_business"

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(
        self,
        market: str,
        code: str,
        field: str,
        value: str,
        *,
        source: str,
        as_of: date,
        confirmed_by: str | None,
        confirmed_at: datetime | None,
    ) -> None:
        market = canonical_market(market)
        code = canonical_code(market, code)
        if field not in BUSINESS_FIELD_KEYS:
            raise ValidationError(
                f"不认识的核心业务字段 {field!r}：已登记的是 {', '.join(BUSINESS_FIELD_KEYS)}"
            )
        if source not in BUSINESS_SOURCES:
            raise ValidationError(f"不认识的来源 {source!r}：只认 {', '.join(BUSINESS_SOURCES)}")
        spec = field_spec(field)
        text = (value or "").strip()
        if not text:
            raise ValidationError(f"『{spec.label}』为空：清除请走删除，不要把空串写成已回答")
        if len(text) > spec.max_chars:
            raise ValidationError(
                f"『{spec.label}』最多 {spec.max_chars} 字，收到 {len(text)} 字"
            )
        if not isinstance(as_of, date):
            raise ValidationError(
                f"『{spec.label}』的来源日期必须是 date，收到 {type(as_of).__name__}"
            )
        self.db.execute(
            f"""
            INSERT INTO {self.TABLE}
                (market, code, field, value, source, as_of, confirmed_by, confirmed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())
            ON CONFLICT (market, code, field) DO UPDATE SET
                value        = EXCLUDED.value,
                source       = EXCLUDED.source,
                as_of        = EXCLUDED.as_of,
                confirmed_by = EXCLUDED.confirmed_by,
                confirmed_at = EXCLUDED.confirmed_at,
                updated_at   = now()
            """,
            [market, code, field, text, source, as_of, confirmed_by, confirmed_at],
        )

    def delete(self, market: str, code: str, field: str) -> bool:
        market = canonical_market(market)
        code = canonical_code(market, code)
        if field not in BUSINESS_FIELD_KEYS:
            raise ValidationError(f"不认识的核心业务字段 {field!r}")
        written = self.db.scalar(
            f"DELETE FROM {self.TABLE} WHERE market = ? AND code = ? AND field = ?",
            [market, code, field],
        )
        return bool(written)

    def all_for(self, market: str, code: str) -> dict[str, dict]:
        market = canonical_market(market)
        code = canonical_code(market, code)
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE market = ? AND code = ? ORDER BY field",
            [market, code],
        )
        return {str(row["field"]): row for row in rows.to_dicts()}

    def count(self, market: str | None = None) -> int:
        sql = f"SELECT count(*) FROM {self.TABLE}"
        params: list = []
        if market:
            sql += " WHERE market = ?"
            params.append(canonical_market(market))
        return int(self.db.scalar(sql, params) or 0)
