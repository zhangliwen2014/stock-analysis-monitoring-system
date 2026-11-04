"""核心业务六字段的词表：屏幕、表单、仓库取同一份 key 与长度（D240）。

六个字段回答「它靠什么赚钱、收入怎么构成、客户是谁、产能多少、行业里什么位置、
行业往哪走」。词表放 core 的理由与 `core/f10.py` 相同：`storage` 只许 import `core`，
而表单、仓库、读模型必须对字段名与长度有同一个答案——某一侧漏登记时不会报错，
只会有一个字段在屏幕上永远显示「尚未录入」。

`v0 来源`是默认草稿，不是限制：任何字段都可以人工填写/覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_CHARS = 2000


@dataclass(frozen=True)
class BusinessFieldSpec:
    key: str
    label: str
    hint: str
    max_chars: int = MAX_CHARS


BUSINESS_FIELDS: tuple[BusinessFieldSpec, ...] = (
    BusinessFieldSpec(
        key="business",
        label="核心业务描述",
        hint="靠什么赚钱：主要产品/服务与收入模式",
    ),
    BusinessFieldSpec(
        key="revenue_mix",
        label="收入构成",
        hint="各业务/产品的收入占比与增速（年报口径）",
    ),
    BusinessFieldSpec(
        key="customers",
        label="主要客户",
        hint="前五大客户、集中度、渠道结构",
    ),
    BusinessFieldSpec(
        key="capacity",
        label="产能与供给",
        hint="产能、产量、在建与利用率",
    ),
    BusinessFieldSpec(
        key="position",
        label="行业地位",
        hint="市占率/排名；v0 用营收分位代理",
    ),
    BusinessFieldSpec(
        key="trend",
        label="行业趋势",
        hint="行业增速、渗透率、政策与供需",
    ),
)

BUSINESS_FIELD_KEYS = tuple(spec.key for spec in BUSINESS_FIELDS)

#: 每个字段的来源只认这三种：概况摘要草稿、系统计算草稿、用户录入。
#: 「系统」不单独存在——草稿被确认后保留它原来的来源，屏幕才分得清
#: 「用户写的」与「用户确认了公司简介」。
SOURCE_PROFILE_SUMMARY = "company_profiles.summary"
SOURCE_COMPUTED = "computed"
SOURCE_USER = "user"

BUSINESS_SOURCES = (SOURCE_PROFILE_SUMMARY, SOURCE_COMPUTED, SOURCE_USER)

SOURCE_LABELS = {
    SOURCE_PROFILE_SUMMARY: "公司概况",
    SOURCE_COMPUTED: "系统计算",
    SOURCE_USER: "用户录入",
}

_BY_KEY = {spec.key: spec for spec in BUSINESS_FIELDS}


def field_spec(key: str) -> BusinessFieldSpec:
    return _BY_KEY[key]


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)
