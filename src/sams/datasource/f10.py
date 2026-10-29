"""公司概况与公告的采集契约：标准列、空答复形状、纯解析。

这两类数据都不参与任何计算（D214/D216），但「源没给」与「源答了没有」仍然要分开：
概况缺 `jbzl` 是接口改版（`FieldDrift`），`jbzl` 为空是「这只票没有概况记录」（0 行帧）。
把两者混成一个 0 行帧，改版的那一天屏幕上只会说「这只票没有概况」。

解析放这里而不是适配器里：A 股与港股走的是同一族东财接口，只差字段块名；把两套
映射写在同一个纯函数里，录一份 payload 就能测出字段漂移，不必联网。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import polars as pl

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.core.f10 import (
    ANNOUNCEMENT_COLUMNS,
    ANNOUNCEMENT_DATE_COLUMNS,
    PROFILE_BOOL_COLUMNS,
    PROFILE_COLUMNS,
    PROFILE_DATE_COLUMNS,
    PROFILE_INT_COLUMNS,
)

logger = logging.getLogger("sams.datasource.f10")

_PROFILE_DATES = PROFILE_DATE_COLUMNS
_PROFILE_INTS = PROFILE_INT_COLUMNS
_PROFILE_BOOLS = PROFILE_BOOL_COLUMNS


def has_profile(obj: Any) -> bool:
    """按方法存在与否判断能力，与其余 `has_*` 同一约定（D38）。"""
    return callable(getattr(obj, "company_profile", None))


def has_announcements(obj: Any) -> bool:
    return callable(getattr(obj, "announcements", None))


def _profile_schema() -> dict[str, pl.DataType]:
    return {
        column: (
            pl.Date
            if column in _PROFILE_DATES
            else pl.Int64
            if column in _PROFILE_INTS
            else pl.Boolean
            if column in _PROFILE_BOOLS
            else pl.Utf8
        )
        for column in PROFILE_COLUMNS
    }


def empty_profile() -> pl.DataFrame:
    return pl.DataFrame(schema=_profile_schema())


def empty_announcements() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            column: pl.Date if column in ANNOUNCEMENT_DATE_COLUMNS else pl.Utf8
            for column in ANNOUNCEMENT_COLUMNS
        }
    )


def _text(value: object) -> str | None:
    """空串与全空白按「源没给」处理；非空值原样保留（简介里的空格有语义）。"""
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _as_date(value: object) -> date | None:
    """源侧日期有三种写法：`1999-11-20 00:00:00`、`1999-11-23`、`2004/6/16 0:00:00`。"""
    if value is None:
        return None
    text = str(value).strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _first_block(value: object) -> dict[str, Any] | None:
    """接口有时给对象、有时给单元素列表；空块返回 None。"""
    if isinstance(value, dict):
        return value or None
    if isinstance(value, list) and value:
        first = value[0]
        return first if isinstance(first, dict) else None
    return None


def parse_profile(
    payload: object, *, market: str, code: str, source: str
) -> pl.DataFrame:
    """东财 F10 概况 payload → 一行 `PROFILE_COLUMNS`；「查无记录」返回 0 行帧。"""
    if not isinstance(payload, dict):
        raise FieldDrift(source, f"概况响应不是对象: {type(payload).__name__}")
    canonical = canonical_market(market)
    if canonical == "A":
        return _parse_a_profile(payload, market=canonical, code=code, source=source)
    if canonical == "HK":
        return _parse_hk_profile(payload, market=canonical, code=code, source=source)
    raise DatasourceError(source, f"{source} 不提供 {market} 的概况")


def _parse_a_profile(
    payload: dict[str, Any], *, market: str, code: str, source: str
) -> pl.DataFrame:
    block_name = "jbzl"
    if block_name not in payload:
        raise FieldDrift(source, f"{market} {code} 概况缺少 {block_name}（接口可能改版）")
    if not isinstance(payload[block_name], (dict, list)):
        raise FieldDrift(source, f"{market} {code} 的 {block_name} 不是对象或列表")
    base = _first_block(payload[block_name])
    if base is None:
        return empty_profile()
    extra = _first_block(payload.get("fxxg")) or {}

    row = {
        "market": market,
        "code": canonical_code(market, code),
        "name": _text(base.get("SECURITY_NAME_ABBR")),
        "full_name": _text(base.get("ORG_NAME")),
        "name_en": _text(base.get("ORG_NAME_EN")),
        "industry": _text(base.get("EM2016")),
        "industry_csrc": _text(base.get("INDUSTRYCSRC1")),
        "exchange": _text(base.get("TRADE_MARKET")),
        "board": _text(base.get("SECURITY_TYPE")),
        "listed_on": _as_date(extra.get("LISTING_DATE")),
        "founded_on": _as_date(extra.get("FOUND_DATE")),
        "isin": None,
        "lot_size": None,
        "chairman": _text(base.get("CHAIRMAN")),
        "president": _text(base.get("PRESIDENT")),
        "secretary": _text(base.get("SECRETARY")),
        "legal_person": _text(base.get("LEGAL_PERSON")),
        "phone": _text(base.get("ORG_TEL")),
        "email": _text(base.get("ORG_EMAIL")),
        "website": _text(base.get("ORG_WEB")),
        "address": _text(base.get("ADDRESS")),
        "reg_address": _text(base.get("REG_ADDRESS")),
        "employees": _as_int(base.get("EMP_NUM")),
        "auditor": _text(base.get("ACCOUNTFIRM_NAME")),
        "fiscal_year_end": None,
        "hk_connect": None,
        "stock_type": None,
        "summary": _text(base.get("ORG_PROFILE")),
        "source": source,
    }
    return pl.DataFrame([row], schema=_profile_schema())


def _parse_hk_profile(
    payload: dict[str, Any], *, market: str, code: str, source: str
) -> pl.DataFrame:
    block_name = "zqzl"
    if block_name not in payload:
        raise FieldDrift(source, f"{market} {code} 概况缺少 {block_name}（接口可能改版）")
    security = _first_block(payload[block_name])
    if security is None:
        return empty_profile()
    company = _first_block(payload.get("gszl")) or {}

    connect = security.get("sfhgtbd")
    row = {
        "market": market,
        "code": canonical_code(market, code),
        "name": _text(security.get("zqjc")),
        "full_name": _text(company.get("gsmc")),
        "name_en": _text(company.get("ywmc")),
        "industry": _text(company.get("sshy")),
        "industry_csrc": None,
        "exchange": _text(security.get("jys")),
        "board": _text(security.get("bk")),
        "listed_on": _as_date(security.get("ssrq")),
        "founded_on": _as_date(company.get("gsclrq")),
        "isin": _text(security.get("isin")),
        "lot_size": _as_int(security.get("zxjydw")),
        "chairman": _text(company.get("dsz")),
        "president": None,
        "secretary": _text(company.get("gsms")),
        "legal_person": None,
        "phone": _text(company.get("lxdh")),
        "email": _text(company.get("email")),
        "website": _text(company.get("gswz")),
        "address": _text(company.get("bgdz")),
        "reg_address": _text(company.get("zcdz")),
        "employees": _as_int(company.get("ygrs")),
        "auditor": _text(company.get("hss")),
        "fiscal_year_end": _text(company.get("njr")),
        # 「否」与「源没给」是两句话：前者是有答复的 False，后者留 NULL
        "hk_connect": (
            None
            if connect is None or not str(connect).strip()
            else str(connect).strip() == "是"
        ),
        "stock_type": _text(security.get("zqlx")),
        "summary": _text(company.get("gsjs")),
        "source": source,
    }
    return pl.DataFrame([row], schema=_profile_schema())


def parse_announcements(
    payload: object, *, market: str, code: str, source: str
) -> pl.DataFrame:
    """东财公告 payload → `ANNOUNCEMENT_COLUMNS`；`data.list = []` 是 0 行答复。

    只取接口这一页（适配器限 100 条）：公告不是计算或回测的输入（D216），
    这里缺的是更早的条目，不是某个数。
    """
    if not isinstance(payload, dict):
        raise FieldDrift(source, f"公告响应不是对象: {type(payload).__name__}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FieldDrift(source, f"{market} {code} 公告响应缺少 data 节点（接口可能改版）")
    items = data.get("list")
    if items is None:
        raise FieldDrift(source, f"{market} {code} 公告响应缺少 data.list（接口可能改版）")
    if not isinstance(items, list):
        raise FieldDrift(source, f"{market} {code} 的 data.list 不是列表")
    if not items:
        return empty_announcements()

    canonical = canonical_market(market)
    canonical_request = canonical_code(canonical, code)
    rows = []
    for item in items:
        if not isinstance(item, dict):
            logger.warning("%s %s 公告列表混进非对象条目，丢弃", source, code)
            continue
        codes = item.get("codes")
        echoed = codes[0].get("stock_code") if isinstance(codes, list) and codes else None
        if echoed and canonical_code(canonical, str(echoed)) != canonical_request:
            raise FieldDrift(
                source,
                f"公告响应代码 {echoed} 与请求代码 {code} 不一致（串包或缓存错位）",
            )
        art_code = _text(item.get("art_code"))
        title = _text(item.get("title"))
        published = _as_date(item.get("notice_date") or item.get("display_time"))
        if not art_code or not title or published is None:
            logger.warning(
                "%s %s 丢弃一条公告（art_code=%r title=%r date=%r）",
                source,
                code,
                art_code,
                title,
                published,
            )
            continue
        category = "、".join(
            str(column.get("column_name"))
            for column in (item.get("columns") or [])
            if isinstance(column, dict) and column.get("column_name")
        )
        rows.append(
            {
                "market": canonical,
                "code": canonical_request,
                "art_code": art_code,
                "published_on": published,
                "title": title,
                "category": category or None,
                "url": f"https://data.eastmoney.com/notices/detail/{canonical_request}/{art_code}.html",
                "source": source,
            }
        )

    if not rows:
        raise EmptyResponse(source, f"{market} {code} 的公告没有一条能解析出日期与标题")

    return (
        pl.DataFrame(rows, schema=dict(empty_announcements().schema))
        .unique(subset=["art_code"], keep="last")
        .sort("published_on", descending=True)
    )
