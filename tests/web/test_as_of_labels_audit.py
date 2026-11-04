"""模板审计（D288）：屏幕上那句「截至 X」只能是它旁边那个数的数据日。

这一轮的缺陷不是「有人写错了日期」，是「每个页面各自觉得这句话没问题」——所以规矩
写成代码：任何一处渲染出来的「截至 {{...}}」都要在下面这张表里交代那句说的是谁的数，
交代不出来的就不该上屏。与 `test_back_links_web.py` 那条出处审计同一个写法。
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "sams" / "web" / "templates"

#: 模板 → 那句「截至」是谁的数据日 / 谁的窗口右端。两类都合法，第三类不存在
ALLOWED = {
    # 分位 = 「截至这一天往回 N 年的历史里排第几」，窗口右端本身就是那个数的口径
    "partials/valuation.html": "分位读数窗口的右端",
    # 总资产的换算锚日 = 持仓里最新的行情日（`services/portfolio._as_of`），是数据日
    "portfolio.html": "那笔总资产的换算锚日",
    # 「截至 X，回看 N 天」：X 是窗口右端，而这一屏的内容就是这个窗口
    "calendar.html": "事件窗口的右端",
}

#: 渲染进 HTML 的那种「截至」：后面跟着一个表达式，也就是跟着一个日期
RENDERED = re.compile(r"截至\s*\{\{")


def test_every_rendered_as_of_label_is_adjudicated() -> None:
    found = {
        path.relative_to(TEMPLATES).as_posix()
        for path in sorted(TEMPLATES.rglob("*.html"))
        if RENDERED.search(path.read_text("utf-8"))
    }
    undocumented = sorted(found - set(ALLOWED))
    stale = sorted(set(ALLOWED) - found)
    assert (undocumented, stale) == ([], []), (
        f"新增一处「截至」需要先交代它说谁的数：{undocumented}；"
        f"表里那句已经不在模板上了：{stale}"
    )


def test_no_template_calls_the_read_day_the_data_day() -> None:
    """「数据截至 X」是这一轮拆掉的两句之一（同业卡头与图注），不许再被写回来。

    「数据」两个字加「截至」＝向屏幕保证 X 是底下那些数的日子；而这两处的 X
    一次是读卡日、一次是库里的进度，都不是画面上那个数的数据日。
    """
    offenders = [
        path.relative_to(TEMPLATES).as_posix()
        for path in sorted(TEMPLATES.rglob("*.html"))
        if "数据截至" in path.read_text("utf-8")
    ]
    assert offenders == []
