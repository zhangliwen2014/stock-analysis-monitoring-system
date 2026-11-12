# 同业「营收与市值都没有」注记去命令（D349）

## 背景

R3-02：数据不全由后台自动补，**不把 `sams …` 命令印到网页上**。D336：「会自动补」是承诺，
只有真有一条机制覆盖时才许说。

这是 D348 明确留下的余量：同业服务 `peers.py` 有两条带命令的缺失注记，D348 收拾了
「无行业」那一档（`_not_disclosed`，644），这一条收拾「有行业、但营收与市值都没有」那一档
（`_position` 的拒绝出口，491–496）。两条互斥（无行业会在 171 行提前返回），不会同屏出现。

## 现状

`src/sams/services/peers.py:491`，`_position` 在营收与市值都取不到时：

```python
return (
    "这只票的营收与市值都没有：行业地位无法计算"
    "（先 `sams sync financials` / `sams sync spot`）",
    "",
    NOT_DISCLOSED,
)
```

`compare` 把这条文本写进 `position`、`position_missing=NOT_DISCLOSED`。此时 `state` 是
`ok`/`thin`（行业在、同业选得出），不是 `not_disclosed`。

**两个消费者，都是网页，没有 CLI**：

1. 同业卡 `partials/peers.html:63`：else 分支 `{% if report.position %}<p>{{ report.position }}</p>`。
2. 业务卡「行业地位」格：`business.py:_position_draft` 在 `position_missing` 非空时取 `position`。

两块卡都与 F10「主要财务指标」卡同页（`symbol.html:84/89/91`，财务卡嵌在 F10 内）。

## 两套机制，要分别交代

这条注记缺的是**两个不同来源**的数，自动补的覆盖面不一样，不能一句话笼统承诺：

- **营收 ← 财务（financials）**：盘后 `run_financials` 走 `_units`，**只覆盖票池**
  （关注 / 持仓 / 规则标的 / 论）；F10 财务卡开页时只在「无财务数据 + 有财务源 + 代码表认识」
  才排 FINANCIALS（`symbol.py:_queue_financials`）。同业服务拿不到这三件事，无法判断这只票
  会不会被自动补——与 D348 同一条判例：**不自己承诺**，把补采状态指给同页才有三态
  （无源 / 已排队 / 代码表不认识，`f10_financials.html:65–73`）的「主要财务指标」卡。
- **市值 ← 快照（spot）**：盘后 `run_spot` 是**全市场**横截面（`jobs.py:480`，
  `deps.snapshots.sync(ctx.market)`），不按只、不在票池闸内，覆盖每一只票。D345 已经为
  快照陈旧那一档写过「盘后任务会自动采集最新快照」并钉了测试——这里照同一条说，是诚实的。

注意：同业市值只认**快照**的亿元（D239），不认估值行的市值；F10 财务卡头那行市值是估值口径，
与这里缺的快照市值不是同一个数。所以市值那句不指给财务卡，而说盘后自动采集（与同业卡自己
`peers.html:15–16` 的快照注记一致）。

## 改法

```python
return (
    "这只票的营收与市值都没有：行业地位无法计算；"
    "财务补采状态见本页「主要财务指标」卡，市值快照由盘后任务自动采集",
    "",
    NOT_DISCLOSED,
)
```

逐句的诚实性：

- 「营收与市值都没有：行业地位无法计算」——事实，不动。
- 「财务补采状态见本页「主要财务指标」卡」——营收会不会补、补到哪了，交给同页有三态的财务卡，
  不在这句里替它下结论（D348 同一条）。卡名与 `f10_financials.html:49` 的标题一致。
- 「市值快照由盘后任务自动采集」——`run_spot` 全市场盘后，无条件覆盖，D345 判例。这不是空话。

`_position` 其余出口（营收分位、市值分位、样本不足）都不动；只换这一条拒绝文本。
无新增字段、无接线改动。

## 测试

这一档目前**没有测试**（`seed_baijiu` 默认 `self_revenue=6e10`，即便 `with_snapshot=False`
也走营收分位，到不了 493）。新增：

- `tests/services/test_peers_service.py`：`seed_baijiu(db, self_revenue=None, with_snapshot=False)`
  ⇒ 自己营收 None、无快照 ⇒ `position_missing == NOT_DISCLOSED`，钉
  `"sams sync financials" not in position`、`"sams sync spot" not in position`、
  `"主要财务指标" in position`、`"盘后任务自动采集" in position`。
- `tests/web/test_symbol_fundamentals.py`：同业卡铺「有行业、自己无营收无快照」，取
  `/api/partials/peers`，钉两条命令都不在、指向财务卡那句在。业务卡行业地位格同铺底取
  `/api/partials/business`，钉同样几句——D348 之后，这是业务卡行业地位格最后一处命令。

## 与 D348 的关系

D348（无行业）与本条（有行业但营收市值都缺）合起来，业务卡「行业地位」格与同业卡的
缺失注记才**全部**无命令。两条各自独立、互斥，可分别实施分别回归；本条不依赖 D348 的改动。

## 回归结果

* **TDD 红绿**：新增 `test_peers_service.py::test_position_without_revenue_or_cap_defers_to_cards_not_commands`
  （`seed_baijiu(self_revenue=None, with_snapshot=False)` 首次覆盖 493 这一档）与
  `test_symbol_fundamentals.py` 同业卡、业务卡行业地位格两条网页用例（新铺底 helper
  `_baijiu_self_missing_revenue_and_cap`）。三条一起跑 **RED：3 failed**（旧注记仍印
  `sams sync financials/spot`、无「主要财务指标」字样）。改 `peers.py:491` 拒绝文本后
  **GREEN**：`test_peers_service.py + test_business_profile.py + test_symbol_fundamentals.py`
  共 **44 passed**（含 D348 的 41），blast radius 无回归。
* **ruff**：`ruff check peers.py + 两个测试文件` → All checks passed。
* **真数据副本验证**：prod 库里**没有**天然的 493 票（最新快照 2026-11-11 尚新，有行业的票
  要么有营收要么有快照市值），故在副本上构造：复制 `data/sams.duckdb`(+wal) 到
  `tmp/d349_verify.duckdb`，删 600519 的 financials、把其 2026-11-11 快照市值置 NULL
  （行业「食品饮料-饮料-白酒」仍在）⇒ 命中 493。`SAMS_DB_PATH` 指副本、`SAMS_SECRET_KEY`
  取自 `tmp/.sams_secret`，临时 `sams serve --port 8799`，登录（sp1check）后取 600519：
  * `/api/partials/peers` 与 `/api/partials/business`：`sams sync financials`、`sams sync spot`
    均**不在**，渲染「营收与市值都没有：行业地位无法计算；财务补采状态见本页「主要财务指标」卡，
    市值快照由盘后任务自动采集」。
  验证毕停 8799（生产 8765 `/healthz` 仍 200）、清理副本与脚本。
* **全量套件**：与 D348 合并复跑 **5917 passed, 21 deselected**（56:18，0 失败 0 错误；
  较 D347 基线 5912 净增 5 条新用例：D348 两条网页 + D349 一条服务两条网页）。
* **生产重启**：`sams serve`（8765）重启后 `/healthz` 200、`/login` 200、登录 POST 303；
  线上实测 601333 同业卡与业务卡行业地位格均渲染 D348 新注记、`sams sync profile` 不在；
  600519 同业卡照常出表（无回归）。D349 的 493 档 prod 无天然票，已在副本构造验证（见上）。