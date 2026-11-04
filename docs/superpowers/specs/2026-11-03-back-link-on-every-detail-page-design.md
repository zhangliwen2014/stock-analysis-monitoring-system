# 每一条「列表→详情」都要有回去的地方

日期：2026-11-03 ｜ 来源：用户指令 + #369 报告 R3-24 ｜ 状态：已实现（§3.3 与 §4 为实现后回写）

## 1. 用户原话与判据

> 从列表，到查看列表的某一个详情，都应该有返回的地方，类似选股时看某只股票，最上面有个返回链接

选股页那条「← 返回」（`symbol.html:4`，带 `back=` 参数还原条件与页码）就是形状参照。
判据落成三条，后面每条实现都要能对上：

1. **凡是从列表点出去的详情，顶上第一屏就能回去**，不用靠浏览器的后退（后退在 htmx 局部刷新、
   新标签页、以及「点进去只为看一眼再回来」这三种场景下都靠不住）。
2. **「返回」这两个字只在真知道来路时才能出现**。不知道来路（直接贴 URL 进来、从另一张表点进来）
   时，链接的标签写清它去哪里（「告警列表」），不写「返回」——这是本仓库一贯的那条规矩：
   宁可不画，不说错话。
3. **回去之后那一屏不该已经变了**：选股带 `page` 与条件（已做），其余列表至少要回到那张列表本身，
   而不是回到看板。

## 2. 现状盘点（本轮逐条 grep 出来的，不是推测）

已有的返回机制只有一处：`back=` 查询参数 + `_local_back()`（`web/pages/symbol.py:41`，
只收本站路径、拒绝协议/`//host`/反斜杠/控制字符）。带 `back=` 的出链只有三个模板：
`screen.html:138-139`、`calendar.html:16,41`、`partials/portfolio_attribution.html:34`。

### 2.1 详情页有没有返回

| 详情页 | 模板 | 从哪些列表进 | 现在有没有回去的地方 |
| --- | --- | --- | --- |
| 个股 `/symbol/{m}/{c}` | `symbol.html:4` | 选股/日历/归因（带 back）、自选、持仓、资讯、同业、搜索候选、论点 | **只有前三个有**；其余是「← 返回」不出现，只剩顶栏导航 |
| 告警 `/alerts/{id}` | `alert.html` | `/alerts` 的「详情」、`/review` 三处、`/symbol` 的告警表 | **完全没有** |
| 论点 `/theses/{id}` | `thesis.html` | `/theses` 两列、`/review`、`/symbol` | **完全没有** |
| 摘要 `/digest/{id}` | `digest_detail.html` | `/digest` | **完全没有** |
| 规则试算 `/rules/{id}/check` | `rule_check.html` | `/rules` | 有（已写「返回」） |
| 规则表单 `/rules/new`、`/rules/{id}/edit` | `rule_form.html` | `/rules` | 有 |
| 通道编辑 | `channel_edit.html` | 通道列表 | 有 |
| 批注详情（管理） | `admin_annotation_detail.html` | 批注列表 | 有 |

### 2.2 出链缺 `back=` 的列表（进个股页时把来路丢了）

| 列表 | 位置 | 该带回哪 |
| --- | --- | --- |
| 自选/看板报价表 | `partials/quotes.html:13` | `/watchlist`（含页内锚点不需要，但看板那份也要 `/`） |
| 持仓/账户 | `portfolio.html:168,169,197` | `/portfolio` |
| 资讯 | `news.html:93` | 当时的 `/news?…`（市场/关键词/页码） |
| 同业对比 | `partials/peers.html:25` | 回原来那只票的页面：`back=/symbol/{market}/{code}`（原票，不是同业） |
| 四个候选下拉 | `partials/{symbol_search,pick_candidates,fill_candidates,nav_search}.html` | 发起它的那一页（表单所在页） |
| 论点详情里的「这只票的现场」 | `thesis.html:8` | `/theses/{id}` |

## 3. 方案

### 3.1 机制：把 `back=` 从个股页的一处实现，升级为整站的一处实现

- `_local_back` 从 `web/pages/symbol.py` 提到公共模块（`web/back.py` 或 `web/forms.py` 同级），
  告警/论点/摘要三个详情页共用**同一个守门函数**。
  留两份实现的后果是可预见的：一处放宽（比如哪天允许带 query 的完整 URL），另一处没跟上，
  症状不是报错而是某个页面的「返回」变成一条能跳出站的链接。
- 每条列表出链统一写成 `?back={{ <本页完整 URL>|urlencode }}`，
  其中带 query 的列表（选股、资讯、摘要筛选）必须把条件与页码一起带上——
  这正是 `screen.html` 已经做的事，照它的样子做，不另发明。
- 详情页渲染时：有 `back` → 「← 返回」指过去；无 `back` → 见 §3.2。

### 3.2 不知道来路时说什么

| 详情页 | 无 `back` 时 | 标签 |
| --- | --- | --- |
| `/alerts/{id}` | `/alerts` | 「告警列表」（不叫返回） |
| `/theses/{id}` | `/theses` | 「论点列表」 |
| `/digest/{id}` | `/digest` | 「每日摘要」 |
| `/symbol/{m}/{c}` | **不画** | 贴 URL 进来时无从知道来路，画一条指自选的「返回」是假话；顶栏本就有「自选」 |
| 候选下拉指向的个股页 | 由发起页带 `back` 解决，落不到「不画」那一档 |

区别只在标签上：`← 返回` = 我知道你来哪儿；`告警列表` = 这是一条去那里的路。两种都算「有回去的地方」。

### 3.3 顶栏「选股」把条件清掉这一条（R3-24）

现状：`/screen?…条件…` 是可复原的，但顶栏那颗「选股」指向裸 `/screen`，一律空表。

原想的做法（空态多一条「上次跑的筛选」）**经查不成立**，理由是库里没有那个事实：
`screener_presets.last_run_at` 只由 `services/screener_monitor.py:192` 的 `mark_run` 写入，
记的是「监控最后一次真的看过市场」，不是「用户最后一次看的这一屏」；
手动跑一次筛选（`/screen` 表单提交、`run_preset`）不写这个字段，也不在任何地方留下那组条件。
所以「上次跑的筛选」只有两种实现：指向监控时间戳那个 preset（可能是几周前、且用户根本没在看），
或者新存一份 per-user 的「上次 URL」。前者会在某些时候说谎，后者是为一条链接引入一个会写坏的状态。

改成的做法：**方案（preset）的名字本身是链接**（`screen.html:183` → `/screen?preset={{ id }}`）。
条件存在 `conditions_json` 里，URL 只带主键，回来时逐字重建那一屏，不需要新的状态。
顶栏「选股」仍指向裸 `/screen`（不动导航语义）；空态那一屏下方就是方案列表，
点名字即回到刚才那一屏。是一次没存进方案的临时筛选 → 系统确实不知道那组条件，也就没有那条链接，
表单值还在输入框里，这是这一刻唯一诚实的答案。

## 4. 测试

按「一处红→一处绿」走，全部在 `tests/web/test_back_links_web.py`（24 条），都通过真实渲染
（走 `logged.client` 的 HTTP 请求，不测内部函数）。下面每条对上一到三个测试：

1. **列表把来路带进详情**：`/alerts`、`/theses`、`/digest` 各自的出链要把「这一屏」整条 URL
   （含条件与页码）编进 `back=` —— `test_the_alert_list_hands_the_detail_a_way_back` 等三条。
2. **详情知道来路才说「返回」**：`test_alert_detail_uses_the_back_it_was_given` 与论点/摘要各一条。
3. **不知道来路时说的是表名**：`test_alert_detail_without_a_back_names_the_table` 与论点/摘要各一条，
   断言的是「出现『告警列表』且不出现『返回』」，不是只断言有链接。
4. **守门覆盖三个详情页 + 候选面板**：`https://evil.example/`、`//evil.example`、`/a\bb`、带控制字符
   四种恶意值各详情页一条（`*_refuses_a_back_that_leaves_the_site`）。断言用
   `assert_no_offsite_href`：不许有任何 href **以恶意值开头**——早先写成「整页不含这段字符串」，
   结果被详情页往下游带的 `back=`（本就该原样回声）撞红，那一次红是对的，它指出了断言太宽。
5. **每一张列表都要过一遍**：报价表按所在页分别钉（`/watchlist` 与看板 `/` 两条，
   `test_the_quote_table_links_back_to_the_page_it_s_on`）、持仓页三个出口
   （`test_the_portfolio_page_links_back_to_itself`）、资讯把市场/关键词/页码带进 `back`
   （`test_the_news_page_carries_its_filters_into_the_back`）、`/review` 三处、
   同业表回到**原来那只票**（`test_the_peers_table_links_back_to_the_symbol_you_came_from`）、
   个股页下方两张子表的两跳链（`test_the_symbol_page_hands_its_own_url_to_the_rows_under_it`：
   自选→个股→告警→个股→自选，用真实 GET 走完全程）。
6. **异步局部（裸片段）不能把自己的 URL 当来路**：候选面板的「这一屏」是
   `/api/symbol-search/…` 那段裸片段，报价板的 10 秒一刷是 `/api/partials/quotes…`，
   拿它们当来路会把「返回」送到一段没有外壳的表——来路由发起页在 `hx-get` 里报上来，
   端点守门后再交给片段；报不上（直接 GET 那段）就**不画**返回。
   `test_a_candidate_panel_returns_to_the_form_that_asked_for_it`、
   `test_a_candidate_panel_drops_a_back_that_leaves_the_site`、
   `test_the_form_asks_for_its_own_url_to_be_the_back`（`/watchlist` 走真实页面）、
   `test_the_ten_second_refresh_does_not_lose_the_page_it_s_on`（同一张板刷新前后必须是同一处）。
   其余每一格由模板审计兜住：`test_every_candidate_box_asks_for_its_own_page_as_the_back`
   扫 `templates/**/*.html`，任何 `hx-get="/api/symbol-search…"` 少了 `?back=` 就点名，
   唯一的例外是 `base.html` 顶栏那一格（理由见 D287）。
   逐页 GET 需要给每张表灌数据，为一条链接不值。
7. **§3.3 的方案名即入口**：`test_a_saved_preset_name_rebuilds_that_screen`——
   裸 `/screen` 那一屏里，存过的方案名渲染成 `/screen?preset={id}` 的链接。
8. **实机**：`tmp/_rq_back.py` 对着跑起来的 `sams serve`（真库、真 HTTP）把上面七条走一遍，
   2026-11-04 全绿（24 项）。它比单测多回答一件事：用户此刻点得到点不到。

## 5. 明确不做

* **不做 Referer 兜底**：浏览器可能在跨页时剥掉 Referer，也可能带着上一张无关的列表；
  用它做「返回」会稳定地在某些时候说谎。
* **不做会话级「上次列表」状态表**：§3.3 用已有的 preset 记录就够，新存一份 per-user 的
  「上次 URL」会为一条链接引入一个会写坏的状态。
* **不改顶栏导航本身**（不动「选股」指向裸 `/screen`）：顶栏是「去这个功能」，不是「回上一屏」，
  两者混在一起会让 R3-24 变成导航语义的争论。
* 不做滚动位置还原、不做「返回列表后保留勾选」的批量态（那是 #369 报告 R3-13 之外的另一件事，
  需要的是勾选态入 URL，不在这片顺手做）。

## 6. 实施顺序（已按此走完）

1. 提公共 `back` 守门（`src/sams/web/back.py`：`local_back` + `page_url`）+ 详情页三条
   （告警/论点/摘要）——纯增量，先解「完全没有」那三个。
2. 列表出链补 `back=`：`partials/quotes.html`、`portfolio.html`（三处）、`news.html`、
   `partials/peers.html`、四个候选 partial、`alerts.html`、`theses.html`、`digest.html`、
   `review.html`（三处）、`symbol.html` 的两张子表、`thesis.html` 的「这只票的现场」。
   `page_url` 进 `WebState.render` 的模板上下文；异步局部不带它自己的 URL，
   由发起页报上来路（peers 用 `origin_url`，报价板用 `hx-get` 上的 `back=`）。
3. §3.3：方案名即入口（`screen.html:183`）。**这一条与原设计的「上次跑的筛选」不同**，
   理由与改动见 §3.3。
4. 全量回归 + 实机：§4 的七条逐一走过，再 `tmp/_rq_back.py` 对真库走一遍。

## 7. 决策

* **D286**：**异步局部不许把自己的 URL 当「来路」**。裸片段（`/api/partials/quotes…`、
  `/api/symbol-search…`）有自己的端点地址，那不是用户站着的那一屏。症状不是报错，是
  **同一张表在刷新前后给出两种「返回」**——首屏 `{% include %}` 进去的那一份带着正确的
  页面 URL，10 秒后 htmx 换进来的那一份把人送去一段没有外壳的表。于是规则定为：来路由
  发起页在 `hx-get` 里报上（守门之后再交给片段），报不上就**不画**这一条。
  少一个返回入口，好过一个会说错话的返回入口（同 §1 判据 2）。
* **D287**：**顶栏那格全局搜索不带 `back`**。它长在每一屏上，包括报错页和带着陌生参数的
  管理页；带上 `page_url` 等于把「当前请求的完整 URL」原样印进 HTML，而本仓库有一条更早的
  规矩：没被页面认下来的值不许出现在页面上
  （`tests/web/test_admin_web.py::test_an_unknown_feedback_value_is_never_put_on_the_page`
  就是钉这一条的，实现 `back=` 时它红了）。顶栏搜索的语义是「跳到任何一只票」，
  不是「从我刚看的那张表点出去」，本来就没有来路可回——不给它 `back` 不是妥协，是它没有。
