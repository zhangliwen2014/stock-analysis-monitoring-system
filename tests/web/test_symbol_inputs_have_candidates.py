"""每个能敲标的的格子都必须带候选面板（代码 / 拼音首字母 / 名称）。

这条要求的失败方式不是「某天算错了」，而是**新加一张表单时没人回头接线**——
所以扫模板，而不是逐个点页面：扫到的每一条都是一个具体的、会被下次改动碰坏的格子。

只看 `<input>` 本身：htmx 的三个属性（`hx-get` / `hx-target` / `hx-trigger`）本来就挂在
输入框上，不依赖它外面套了几层 label 或 form。`hx-target` 必须以 `-candidates` 结尾
且在同一份模板里真有一个那个 id 的容器——app.js 的共用监听靠这个约定收起面板，
指到不存在的容器上，点了「填入」什么都不发生。

检索词只有这一条来路：**这个输入框自己的值，用他自己的名字**。htmx 不会自动带上整张
表单，所以格子名必须在端点认识的那几个里；面板种类走 `hx-get` 的路径段，不走 `hx-vals`
——后者盖在请求最上层，同名格子的值会被吃掉（见下面那两条测试）。
"""

from __future__ import annotations

import re
from pathlib import Path

from sams.web.pages.dashboard import PANEL_KINDS, TERM_FIELDS

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "src" / "sams" / "web" / "templates"

#: 敲进去的是一个标的的字段名 = 端点认识的那几个名字。两处必须是同一份，否则新加一页
#: 表单会「扫得到、发不出」：格子名不在端点那份里，屏上一行候选都不出。
SYMBOL_FIELDS = TERM_FIELDS

#: 候选面板自己不算输入框（它们是候选，不是入口）
PANELS = frozenset(
    {
        "partials/symbol_search.html",
        "partials/fill_candidates.html",
        "partials/pick_candidates.html",
        "partials/nav_search.html",
    }
)

#: 长得像但不是标的入口的检索格：笔记的正文关键词能搜「回购」两个字
NOT_SYMBOL = frozenset({("annotations.html", "q")})

INPUT = re.compile(r"<input\b[^>]*>", re.DOTALL)


def symbol_inputs() -> list[tuple[str, dict[str, str]]]:
    found: list[tuple[str, dict[str, str]]] = []
    for path in sorted(TEMPLATE_ROOT.rglob("*.html")):
        key = str(path.relative_to(TEMPLATE_ROOT))
        if key in PANELS:
            continue
        text = path.read_text(encoding="utf-8")
        for tag in INPUT.findall(text):
            name = re.search(r'name="([^"]+)"', tag)
            if not name or name.group(1) not in SYMBOL_FIELDS:
                continue
            if (key, name.group(1)) in NOT_SYMBOL:
                continue
            if 'type="hidden"' in tag:
                continue
            found.append((key, {"tag": tag, "field": name.group(1), "text": text}))
    return found


def test_every_symbol_input_offers_candidates() -> None:
    missing = [
        f"{key} 的 {item['field']}"
        for key, item in symbol_inputs()
        if not re.search(r'hx-get="/api/symbol-search', item["tag"])
    ]
    assert not missing, "这些格子敲进去只有一个空输入框：\n" + "\n".join(missing)


def test_every_candidate_target_is_a_real_box() -> None:
    broken = []
    for key, item in symbol_inputs():
        target = re.search(r'hx-target="#([^"]+)"', item["tag"])
        if target is None:
            broken.append(f"{key} 的 {item['field']} 没有 hx-target")
        elif f'id="{target.group(1)}"' not in item["text"]:
            broken.append(f'{key} 的 {item["field"]} 指向不存在容器 #{target.group(1)}')
    assert not broken, "\n".join(broken)


def test_hidden_code_fields_are_not_search_boxes() -> None:
    """形状判据本身要钉住：候选面板的隐藏 `code`（点「加入」那张表单）不该被算成入口。

    这条要是翻了，上面两条就会开始对着候选面板自己提要求。
    """
    names = [(key, item["field"]) for key, item in symbol_inputs()]
    assert ("watchlist.html", "code") in names, "自选页加票那张是入口，必须被扫到"
    assert all(key != "partials/symbol_search.html" for key, _ in names)
    assert all('type="hidden"' not in item["tag"] for _, item in symbol_inputs())


def test_no_hx_vals_may_shadow_what_the_box_typed() -> None:
    """面板种类走 URL 路径，不走 `hx-vals`：后者会盖掉同名的输入框值。

    真浏览器实测（`tmp/_cdp_pick.mjs`）：限额那格叫 `scope`，配上
    `hx-vals='{"scope": "pick"}'` 之后发出去的是 `?scope=pick`——htmx 只把「这个框自己的值
    + hx-include + hx-vals」发出去（它不自动带整张表单，成交那格靠 `hx-include` 才带上市场），
    于是用户敲的那串字一个字都没进请求，面板永远空着，而页面看起来接好了线。
    名字不再相撞，这种格子也就不需要为检索词抄一份隐藏域。
    """
    shadowed = [
        f"{key} 的 {item['field']}" for key, item in symbol_inputs() if "hx-vals" in item["tag"]
    ]
    assert not shadowed, "hx-vals 会把这格敲进去的检索词吃掉：\n" + "\n".join(shadowed)


def test_the_panel_kind_is_a_path_segment() -> None:
    """每一格要哪一种面板，只由 `hx-get` 的路径说了算。

    钉住「路径 ↔ 种类」这层接线：写一个端点不认识的种类不会报错，只是那张表单永远拿不到
    它要的那个按钮（`加入` / `填入` / 整行链接）。
    """
    broken = []
    for key, item in symbol_inputs():
        # `?back=` 那段是来路（spec 2026-11-03-back-link），种类仍然只在路径段上
        path = re.search(
            r'hx-get="(/api/symbol-search(?:/([a-z_]+))?(?:\?[^"]*)?)"\s*', item["tag"]
        )
        if path is None:
            broken.append(f"{key} 的 {item['field']}：hx-get 不是候选端点")
        elif (path.group(2) or "") not in PANEL_KINDS:
            broken.append(f"{key} 的 {item['field']}：种类 {path.group(2)} 端点不认识")
    assert not broken, "\n".join(broken)
