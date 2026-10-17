"""公司行为的标准词汇与列集——采集层与仓库层共用的同一套拼写。

放 core 的理由是分层的：`datasource` 不许 import `storage`（分层守卫），而 kind 的拼写
在两侧必须完全一致。各写一套的症状不是报错，是采集侧给出 `"Dividend"`、仓库侧只认
`"dividend"`，中间把它当未知类型丢掉——因子少乘一次除权，价格序列照样画得出图，
只有跨那一天的区间收益悄悄偏掉（这正是 D34/D37 要拦的那类错数）。
"""

from __future__ import annotations

#: 已知的公司行为类型。新增一种要同时改因子推导（`analysis/adjust.py`）与校验（D34）。
#: `unknown` 是其中唯一「不推因子」的一种：它记的是源确实报了这么一件事，但它无法折算
#: 成每股现金或股数乘数（港股的以股代息、实物分派就是这种）。留空数值而不是硬解析一个
#: 数出来，是因为猜错的因子会一路乘进序列，而 `analysis.adjust` 看到 unknown 会直接拒绝出数。
ACTION_KINDS = frozenset({"dividend", "split", "bonus", "transfer", "rights", "unknown"})

#: 一条已展开、可直接入库的公司行为。数值列的语义见 D34：
#: `ratio` 是除权后/除权前的股数乘数，`price` 是每股新股应付的现金（只有 rights 用）。
ACTION_COLUMNS = [
    "market",
    "code",
    "ex_date",
    "kind",
    "cash_per_share",
    "ratio",
    "price",
    "src",
    "note",
]
