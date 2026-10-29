"""个股资料（F10）的标准列与类型拼写——采集帧与仓库表共用同一份词表。

放 core 的理由与 `core/fundamentals.py` 相同：`storage` 只许 import `core`，而
`datasource` 与仓库层必须对同一张表的列名、日期列与可空类型有同一个答案。两侧各写
一份名单时，漏写的那一侧不会报错：采集写进来、读出去少一列，或者一个 INTEGER 列被
当成 Float64 补 null，屏幕上只会说「这只票没有概况」。
"""

from __future__ import annotations

#: `company_profiles` 的写入列（不含 `fetched_at`：它不是源给的业务时间，由入库时的 now() 写）
PROFILE_COLUMNS = (
    "market",
    "code",
    "name",
    "full_name",
    "name_en",
    "industry",
    "industry_csrc",
    "exchange",
    "board",
    "listed_on",
    "founded_on",
    "isin",
    "lot_size",
    "chairman",
    "president",
    "secretary",
    "legal_person",
    "phone",
    "email",
    "website",
    "address",
    "reg_address",
    "employees",
    "auditor",
    "fiscal_year_end",
    "hk_connect",
    "stock_type",
    "summary",
    "source",
)

PROFILE_DATE_COLUMNS = ("listed_on", "founded_on")
PROFILE_INT_COLUMNS = ("lot_size", "employees")
PROFILE_BOOL_COLUMNS = ("hk_connect",)

#: `announcements` 的写入列。`url` 指向东财详情页，正文与 PDF 留外链（D216）。
ANNOUNCEMENT_COLUMNS = (
    "market",
    "code",
    "art_code",
    "published_on",
    "title",
    "category",
    "url",
    "source",
)

ANNOUNCEMENT_DATE_COLUMNS = ("published_on",)
