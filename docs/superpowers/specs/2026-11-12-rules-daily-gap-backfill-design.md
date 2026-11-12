# 建规则屏的日线缺口接补采队列（D340）

## 需求

R3-02 把网页里的命令行话术逐条点名。D336–D339 清掉了个股页财务/分红/估值/概况/
公告/新闻六格，规则页还剩两处在印 `sams sync daily`：

* 建规则屏的现价格（`pages/rules.py::_price_panel`）：标的库里没有日线时印
  `先跑 sams sync daily --market … --code …，或换一只票。`
* 一键示例（`pages/rules.py::rule_demo`）：没有日线时一条规则都不建，错误文案同样是
  `先跑 sams sync daily …，或换一只票。`

仍是同一条冲突：网页用户没有终端，`sams serve` 占着库时命令行只会拿到
`DatabaseLocked`。而「有人对着这只票想建规则/试示例」与 D144「规则挂在票上就排补数拍」
是同一个意图——规则**保存**那一刻已经在排 `bars`（`test_rules_web.py` 钉着
`backfills_store.open_for("A","600519") is not None`），只是「看这一屏」与「试示例」
这两步还没接上。这一片把这两处也接到既有的 `bars` 活上，沿用 D283「打开就排」、
D336/D338/D339「那句话是承诺，队列里必须真有那一行」的形状。

组合 NAV 卡的 `sams nav rebuild`、组合成交的 `sams sync fx`、新闻列表页的
`sams sync news` 是另几处不同子系统的话术，不在这一片。

## 取舍

1. **复用既有 `bars` 活，不新增 kind。** 日线缺口的消费者早已存在
   （`BackfillConsumer._fetch` 的兜底分支跑 `sync_daily`，730 天窗口）。这一片只在
   网页侧补一个准入闸过的排队 helper，与 `symbol.py::_queue_*` 同形：

   ```python
   def _queue_bars(ctx, market: str, code: str) -> bool:
       if not ctx.symbol_list_store.admits(market, code):
           return False
       ctx.backfills_store.enqueue(market, code, BARS)
       return True
   ```

   `enqueue` 对已有开放行返回 False（幂等）：现价格随表单反复刷新不会刷屏，也不会
   把一条已经在排的行重置。
2. **现价格（`_price_panel`）：`view.price is None` 时排，文案按准入分支。**
   * 排上了 → `{market}/{code} 库里还没有日线，已排进后台补采队列：补完刷新这一屏。（{state_label}）`
   * 代码表不认识 → `{market}/{code} 库里还没有日线；代码表里没有这只票，补采排不上——先等代码表同步。（{state_label}）`

   保留 `state_label(view.state)` 括注：它解释的是此刻为什么没有价（休市/未采集），
   与「排没排」是两件事，不该一起删。
3. **一键示例（`rule_demo`）：仍然不建规则，但排队并改话术。** 「没有日线时一条规则
   都不建」这条不动——一条永远判不了的示例比没有示例更难查。只是把「先跑命令」换成
   「已排进后台补采队列：补完再试一次」；代码表不认识时说「补采排不上——先等代码表
   同步」。示例不因此假装成功：HTTP 仍 400，规则仍不落库。
4. **有没有排问准入，不按市场名猜。** 与 D314 同一道闸：`admits` 在整表未同步的市场
   返回 True（新装库第一晚不该什么都别采），认识的市场只认 `is_active` 的那一行。

## 契约

* `pages/rules.py` 增 `from sams.storage.repos.symbol_backfills import BARS` 与
  `_queue_bars(ctx, market, code) -> bool`（准入闸 + 幂等 enqueue）。
* `_price_panel` 的 `view.price is None` 分支：先 `queued = _queue_bars(...)`，按
  `queued` 选两句文案之一；不再出现 `sams sync daily`。
* `rule_demo` 的 `not price or price <= 0` 分支：先 `queued = _queue_bars(...)`，按
  `queued` 选两句错误文案之一；仍 `status=400`、仍不建规则。
* 模板无需改：这两句文案都在 Python 字符串里，不经 Jinja 变量。

## 测试

* `test_rules_form_price.py::test_the_form_without_daily_bars_says_what_to_run` 改成
  排队断言：页面没有 `sams sync daily`、有「已排进后台补采队列」、
  `backfills_store.open_for("A","000001")` 非 None（空代码表 → admits True）。
* `test_rules_web.py::test_the_demo_without_bars_creates_nothing_and_says_what_to_run`
  改成排队断言：仍 400、仍不建规则、没有 `sams sync daily`、有「已排进」、队列有 bars 行。
* 新增：代码表认识的市场里一只不在表的票（先播种一条 active 的 `symbol_master` 使
  `count>0`，再用表外代码）→ 现价格说「代码表里没有这只票」、不排、不说「已排进」。
* 新增：已有日线的票打开建规则屏 → 照常显示现价，不排 bars（`open_for` 为 None）。
* 既有「保存即排 bars」（`test_create_bumps_the_rules_version_and_redirects`）不动。

## 验证

* 串行 pytest（`test_rules_form_price.py` + `test_rules_web.py`）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：找一只库里没有日线的票，打开建规则屏与一键示例，确认
  话术与队列行；不操作生产通道。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。