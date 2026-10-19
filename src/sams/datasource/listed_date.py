"""「这只票哪天上市」的采集契约：一个方法、一个探针、一条拒绝。

这一格不是第四条链，而是主档的一列：库里 `symbols.listed_on` 有值，D37 的上市豁免就能
自动生效；没值时读用例仍按 D56 拒绝并让人手传 `--listed-on`，而不是静默放宽那道闸。
所以这里的适配器返回 `date | None`，而不是带日期窗口的帧：

* `None` 是「这个源没有这只票的上市日期」——一句答复，链上下一个源还可以有；
* 抛 `DatasourceError` 是「这个源答不上来」——不能替它写成「这只票没有上市日期」。

把两者混成一种，屏幕上会出现「这只票 2007 年上市」和「这只票从没上市」两个同样可信的数。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol


class ListedDateAdapter(Protocol):
    """能给「上市首日」的源（A 股走东财 F10 的公司概况）。"""

    name: str

    def listed_date(self, market: str, code: str) -> date | None:
        """上市首日；这个源查无记录时返回 None（不抛）。"""
        ...


def has_listed_date(obj: Any) -> bool:
    """按方法存在与否判断，与 `has_bars`/`has_financials` 同一约定（D38）。"""
    return callable(getattr(obj, "listed_date", None))
