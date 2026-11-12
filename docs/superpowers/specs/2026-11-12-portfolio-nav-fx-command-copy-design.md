# 组合净值卡与汇率提示去命令（D342）

## 需求

R3-02 点名网页里的命令行话术。D336–D341 清掉了个股页六格、建规则屏两处、资讯列表页。
组合页还剩两处印命令：

* 净值卡（`templates/partials/portfolio_nav.html`）：区间没有算得出来的净值时印
  `也可以跑 sams nav rebuild --account {{ account.id }}`。
* 换汇/成交表单的汇率提示（`pages/portfolio.py::_rate_hint`）：库里两个方向都没有汇率时印
  `填券商给你的成交汇率，或先 sams sync fx`。

仍是同一条冲突：网页用户没有终端，`sams serve` 占着库时命令行只会拿到 `DatabaseLocked`。

## 取舍

1. **这两处与 D336–D341 不同：后台 job 早已全自动，不需要新队列。** 调度里
   `run_nav`（每日，逐账户重建最近 `NAV_WINDOW_DAYS` 天）与 `run_fx`（按账户币种对
   补汇率）都是登记在册的自动 job。净值算不出来是因为缺价或缺汇率——价由 bars 盘后
   job 与补采队列（D283/D340）补、汇率由 `run_fx` 补，补齐后 `run_nav` 那晚自然重建。
   所以这一片**只去命令、如实指向自动 job**，不引入 `nav`/`fx` 队列活（那会是给已有
   自动机制叠床架屋）。
2. **净值卡文案。** 去掉 `也可以跑 …` 那一截，改为：
   `这一区间还没有算得出来的净值：缺价或缺汇率的那一天整日不计。盘后 job 每晚重建最近 {{ nav_window_days }} 天；缺的价与汇率由各自的盘后任务补齐，补上后净值随之重建。`
   `nav_window_days` 仍用变量（D281：天数不抄常数）。
3. **汇率提示文案。** `_rate_hint` 最后一支改为：
   `库里没有 {base}→{quote} 的汇率：填券商给你的成交汇率；盘后任务会自动补这一对的汇率。`
   「填券商成交汇率」这条出路保留——它本来就是比等盘后更准的那一手（D207）。
4. **范围只钉这两处纯网页出口。** 归因卡与服务注记里的 `sams nav rebuild`
   （`test_portfolio_attribution_web.py` / `test_limits.py` / `test_attribution.py` 钉着）
   是 web 与 CLI **共用**的注记字符串：CLI 那一头用户有终端、命令是合法操作指南，不能
   一并删。那需要把注记按 web/CLI 分支，是另一片（不在这里顺手改，免得把 CLI 契约弄红）。
   CLI 自身的 `sams nav rebuild` / `sams sync fx` 输出（`test_*_cli.py`） likewise 不动。

## 契约

* `portfolio_nav.html` 第 9–10 行那一句去掉 `<code>sams nav rebuild …</code>`，按 §2 改写。
* `pages/portfolio.py::_rate_hint` 的 `return "", f"…"` 那一支按 §3 改写；签名与其余两支不动。
* 不改任何服务注记、不改 CLI、不新增队列活。

## 测试

* `test_portfolio_nav_web.py::test_the_nav_card_says_so_when_nothing_has_been_rebuilt_yet`：
  把 `assert f"sams nav rebuild --account {account_id}" in text` 改成
  `assert "sams nav rebuild" not in text`，并断「盘后 job 每晚重建最近 400 天」仍在。
* 新增（`test_portfolio_web.py`）：开一个账户、库里不放任何汇率，`GET /portfolio` →
  汇率提示无 `sams sync fx`、有「盘后任务会自动补」、保留「填券商给你的成交汇率」。
* 既有 `test_the_fx_form_prefills_the_latest_stored_rate`（有汇率那一支）不动。
* CLI 与归因/服务注记的相关测试一律不动（不在本片范围）。

## 验证

* 串行 pytest（`test_portfolio_nav_web.py` + `test_portfolio_web.py`）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：找一个净值算不出的账户与一个无汇率的币种对，确认两处
  话术无命令；不操作生产通道。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。