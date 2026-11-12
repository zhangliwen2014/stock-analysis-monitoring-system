# 业务卡公司简介注记去命令（D347）

## 需求

R3-02 点名网页里的命令行话术。个股页「核心业务」卡的第一行「核心业务描述」在没有
概况简介时，经**服务层字符串**印 `sams sync profile`：

* `services/business.py::_summary_draft`（212-224 行）：概况摘不出简介时回
  `missing_reason="公司概况没有公司简介（先 \`sams sync profile\`）"`。它经
  `BusinessField.missing_reason` → `partials/business.html:28` 渲染到屏幕上。
* 这句话把**两种不同的「没有」糊成一句**：
  * **概况行根本没采到**（`_profile` 回 `None`）——还没问过源；
  * **概况行在、但源没给简介**（`summary` 空）——问过源了，源答不上来。
  两者的下一步完全不同：前者会被自动补采，后者再采一次还是空。
* 该服务**只被网页消费**（`bootstrap.py` 接线 + `pages/symbol.py:846 business_partial`），
  没有任何 CLI 命令读它，故无需考虑 CLI 补回。

## 取舍

1. **按「概况行在不在」分两支，各说诚实的下一步，都去掉命令：**
   * **行在、简介空**：`公司概况源没有给公司简介（可在本卡「编辑六项」自行补全）`。
     源已经问过、答的是空——**不承诺自动补**（再采一次还是空），也不印命令；这一格
     本就是可编辑卡，用户自己补全即可。
   * **行根本没采到**：`公司概况还没采集到；补采状态见本页「公司概况」卡`。
     **不在这里承诺自动补**：业务卡只读 `CompanyProfileRepo`，不知道本市场有没有概况源、
     代码表认不认识这只票，无条件说「盘后会自动补」对 US（无源）与未在册票（D314）是假话
     （D336）。同一页的 F10「公司概况」卡（`f10.html:37-43`）才有这三态
     （无源／已排进补采队列／代码表没它排不上），把补采状态交给那张权威卡说。
2. **不新增队列活，也不给业务卡注入新依赖。** 开个股页时 F10 卡随页自动加载
   （`symbol.html:84 hx-trigger="load"`），`_queue_profile`（`symbol.py:646-653`）在
   「行缺 + 有源 + 在册」时已排 PROFILE（D339）；盘后 `run_profile` 也盖 universe
   （`jobs.py:382`）。补采机制本就存在，业务卡只需去掉命令、把状态指给 F10 卡，
   不必自己再排一次、也不必为了承诺自动补而注入 `F10Read`/`symbol_list_store`。
3. **`missing_kind` 两支都保持 `NOT_DISCLOSED`。** 这一档的屏幕标签（`missing_label`）
   不在本次范围；诚实的区分落在 `missing_reason` 文案上，不动缺失档语义，避免牵动
   别处对标签的断言。
4. **草稿截断、逐字确认保来源等其余逻辑一律不动**（`_first_sentences`、`save` 等）。

## 契约

* `services/business.py::_summary_draft(profile: dict | None)`：
  * `profile is None`（行缺）→ `_Draft(missing_kind=NOT_DISCLOSED,
    missing_reason="公司概况还没采集到；补采状态见本页「公司概况」卡")`。
  * `profile` 在但 `summary` 空 → `_Draft(missing_kind=NOT_DISCLOSED,
    missing_reason="公司概况源没有给公司简介（可在本卡「编辑六项」自行补全）")`。
  * 有简介 → 原样（截断草稿 + `SOURCE_PROFILE_SUMMARY` + `fetched_at`）。
  * 判「行缺」用 `profile is None`，判「简介空」沿用现有 `text` 空判断；函数签名不动。
* 模板、`business_partial`、CLI 均无需改（原样转述 `missing_reason`）。

## 测试

* `tests/services/test_business_profile.py`：
  * 改 `test_a_profile_without_a_summary_leaves_business_not_disclosed`（`profile(db,
    summary=None)` = 行在简介空）：断言 `"公司概况源没有给公司简介" in missing_reason`、
    `"sams sync profile" not in missing_reason`，`missing_kind == NOT_DISCLOSED` 不变。
  * 新增 `test_a_missing_profile_row_defers_collection_status_to_the_f10_card`：不铺概况行
    （`service(db).read(...)`，`_profile` 回 `None`）→ 断言 `"还没采集到" in missing_reason`、
    `"公司概况" in missing_reason`（指向 F10 卡）、`"sams sync profile" not in missing_reason`。
* `tests/web/test_symbol_fundamentals.py`：新增一条——`_symbol(ctx)` 只铺主档、不铺概况，
  `GET /api/partials/business?market=A&code=600519`：文本含「还没采集到」，不含
  `sams sync profile`（行缺支经网页渲染同样去命令）。

## 验证

* 串行 pytest（`test_business_profile.py` + `test_symbol_fundamentals.py`）、
  `ruff check src tests`。
* 生产库副本 + 临时服务 8799：挑一只主档在册、无概况行的票，业务卡注记无命令、
  指向「公司概况」卡；再挑一只概况行在但简介空的票（或构造），注记说「源没有给公司简介」。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。

## 回归结果

2026-11-12 实测（TDD：服务两条 + 网页一条先见证 RED——3 failed，旧注记
「公司概况没有公司简介（先 `sams sync profile`）」仍在；再实现 GREEN）。网页测试改用
「行在简介空 + 铺满同业」隔离行业地位格的 peers 命令（D348），并临时回退旧码单独见证
该条 RED（1 failed）后复原。

* 串行 pytest：`test_business_profile.py` + `test_symbol_fundamentals.py` **26 passed**；
  扩大 blast radius（thesis / layering / screen / screener / business）**1094 passed**；
  分层 + screener + screen cli 复验 **847 passed**；`ruff check src tests` All checks passed。
* 生产库副本（`tmp/d347.duckdb`）+ 临时服务 8799，登录 sp1check：
  * 601333（广深铁路，主档在册、无概况行）业务格印「公司概况还没采集到；补采状态见本页
    「公司概况」卡」——业务格无命令；
  * 600519（副本上构造空简介）业务格印「公司概况源没有给公司简介（可在本卡「编辑六项」
    自行补全）」，整卡无命令（行业地位算得出草稿）。
  * 601333 卡里残留的 `sams sync profile` 经定位在**行业地位**格（`peers.py:644`
    「这只票没有行业…先同步公司概况」），属 D348，不在本次范围。验证毕清理副本。
* **全量套件复跑揪出 3 处失败，均非 D347 引入**（D347 只改 `business.py`，全量复跑里
  business 相关 0 失败）：
  * `test_theses_service.py::test_add_still_records_the_thesis_when_no_price_can_be_computed`
    与 `test_thesis_cli.py::test_add_on_a_stock_without_bars_still_records_the_view_but_exits_one`：
    **D346 遗漏**——仍断言已去掉的 `sams sync daily`。改断言中性注记（`not in` + wrong-code
    支的「确认代码」），与 D346「NotAdjustable 出口多态、不补回命令」一致。
  * `test_layering.py::test_imports_point_downward[cli/screen.py]`：**screen 功能遗留**分层
    越界——`cli/screen.py` 直接 `from sams.storage.repos.symbol_backfills import …`。
    令 `services/screener.py` 以 `as` 重导出 `ACTIONS/BARS/FINANCIALS` 作服务层门面
    （它本就把这三类汇成 `MissingGroup.fix`），`cli/screen.py` 改经 `services.screener` 取常量。
  * 三处修复后最终全量复跑确认：**5912 passed, 21 deselected**（1:03:49，0 失败 0 错误）。
* 生产 `sams serve`（8765）重启后 `/healthz` 200、`/login` 200、登录 POST 303；线上实测
  601333 业务格已印新中性注记（重启确实加载了新码）。