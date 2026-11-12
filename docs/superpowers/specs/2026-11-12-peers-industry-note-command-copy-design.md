# 同业「无行业」注记去命令（D348）

## 背景

R3-02：数据不全要由后台自动补，**不能把 `sams …` 命令印到网页上**——网页用户没有终端。
命令行那一侧保留命令是合理的（D343），网页这一侧的下一步是「补采状态」与排队按钮。

D336 的诚实规矩：那句「已排进队列 / 盘后会自动补」是**承诺**，只有真有一条机制覆盖它时
才许说。说一条没有接线的自动补，比印命令更糟——用户会一直等一个不会来的结果。

这句注记是在 D347 里被发现的：业务卡「行业地位」那一格的缺失注记，取的是同业服务
`peers.compare` 的 `position`，而它在「无行业」这一档带着 `sams sync profile` 命令，
把整张业务卡的「无命令」断言搅浑。D347 当时绕开了它（铺满同业让行业地位算得出草稿），
这一条就是来收拾它自己的。

## 现状

`src/sams/services/peers.py:644`，`_not_disclosed`：

```python
reason = "这只票没有行业（公司概况与主档都是空的）：先同步公司概况（`sams sync profile`）"
```

`compare` 在 `self_path is None`（概况与主档都没给行业，`_industry_of` 返回 None）时
返回 `_not_disclosed`，这条 `reason` 同时写进 `pe/pb/roe` 三个指标的 reason、`position`、
`notes`，`state="not_disclosed"`。

**两个消费者，都是网页，没有 CLI**（`grep peers cli/` 无命中）：

1. 同业卡 `partials/peers.html:28`：`not_disclosed` 那一档只印一行
   `{{ missing_label(position_missing) }}：{{ position or notes[0] }}`——`reason` 出现一次。
   三个指标的 reason 在这一档**不渲染**（表格与 notes 都在 else 分支）。
2. 业务卡「行业地位」格：`business.py:_position_draft` 在 `position_missing` 非空时取
   `position` 当 `missing_reason`——同一条 `reason` 又出现一次。

两块卡都挂在个股页（`symbol.html:84/89/91`），与 F10「公司概况」卡**同页**。

## 为什么不能照搬「盘后会自动补」

D345 给快照陈旧那一档写的是「盘后任务会自动采集最新快照」——那成立，因为 `run_spot`
是**全市场**盘后任务，覆盖每一只票，与它在不在票池、有没有源无关。

「无行业」这一档不一样：行业来自概况（`profile.industry`）或主档（`symbols.sector`）。
概况的自动补有两条闸——F10 卡开页时只在「行缺 + 有概况源 + 代码表认识」才排 PROFILE，
盘后 `run_profile` 只覆盖票池（关注 / 持仓 / 规则标的 / 论）。同业服务 `_not_disclosed`
**拿不到**这三件事（它只读 profiles/symbols 两张表），所以它无法判断这只票到底会不会被自动补。
无条件说「盘后会自动补概况」就是对一只可能没源、不在册的票撒谎（D336）。

D347 已经立过同样的判例：业务卡简介那一格不知道有无源 / 是否在册，于是**不自己承诺自动补**，
把补采状态指给同页才有三态的 F10「公司概况」卡。这里照同一条办。

## 改法

把 `reason` 换成渠道中性、不印命令、不无条件承诺自动补的一句，把补采状态指给同页 F10 卡：

```python
reason = (
    "这只票还没有行业（公司概况与主档都没给）："
    "补到行业后这一卡才出数；概况补采状态见本页「公司概况」卡"
)
```

逐句的诚实性：

- 「公司概况与主档都没给」——比原来的「都是空的」准：概况行可能在、只是没给行业
  （D347 区分过「行缺」与「行在但空」），这里两种都涵盖，说的是「没给行业」这个事实。
- 「补到行业后这一卡才出数」——**条件句**，不承诺采集一定给得出行业：源可能回一行没有行业的
  概况，那时这一卡照旧不出数。它只说「有了行业才算得出」，这是 `compare` 的真实行为。
- 「概况补采状态见本页「公司概况」卡」——F10 卡同页、且是唯一有三态（无源 / 已排队 / 代码表
  不认识）的权威面。把「会不会补、补到哪了」交给它，不在这句里替它下结论。

`_not_disclosed` 其余结构不动（`pe/pb/roe` 仍带这条 reason、`position_missing=NOT_DISCLOSED`、
`state="not_disclosed"`）：改的只是字符串，两个消费者各自渲染一次，无新增字段、无接线改动。

## 测试

- `tests/services/test_peers_service.py:347`：原来钉 `"sams sync profile" in notes`，
  改成钉命令**不在** + 指向 F10 卡的那句**在**：
  ```python
  joined = "；".join(report.notes)
  assert "sams sync profile" not in joined
  assert "公司概况" in joined and "还没有行业" in joined
  ```
- `tests/web/test_symbol_fundamentals.py` 新增同业卡一条：`_symbol(ctx)`（sector=None、无概况）
  ⇒ `not_disclosed`，钉「还没有行业」「公司概况」在、`sams sync profile` 不在。
- 同文件新增业务卡行业地位一条：同样无行业铺底，取 `/api/partials/business`，
  钉「还没有行业」在、`sams sync profile` 不在——这是 D347 那条被污染的断言如今能独立成立。

## 范围之外

`peers.py:493`「营收与市值都没有」那一档仍带 `sams sync financials / sams sync spot`，
是**另一条**（D349）：它混了两套机制（财务有 F10 财务卡排队 + 盘后票池；市值是全市场盘后快照），
诚实措辞要分别交代，不与本条混做。本条只收拾「无行业」这一档；业务卡行业地位格在
「有行业但营收市值都缺」时仍会印命令，那是 D349 的已知余量，不是本条的疏漏。

## 回归结果

* **TDD 红绿**：先改 `test_peers_service.py::test_a_stock_without_any_industry_is_not_disclosed`
  的断言（命令 `not in` + 「还没有行业」「公司概况」`in`），并在 `test_symbol_fundamentals.py`
  新增同业卡、业务卡行业地位格两条网页用例。三条一起跑 **RED：3 failed**（旧注记仍印命令、
  无「还没有行业」字样，失败原因正是缺新措辞）。改 `peers.py:644` 的 `reason` 后
  **GREEN**：`test_peers_service.py + test_business_profile.py + test_symbol_fundamentals.py`
  共 **41 passed**，blast radius 无回归。
* **ruff**：`ruff check peers.py + 两个测试文件` → All checks passed。
* **真数据副本验证**：复制 `data/sams.duckdb`(+wal) 到 `tmp/d348_verify.duckdb`，
  `SAMS_DB_PATH` 指副本、`SAMS_SECRET_KEY` 取自 `tmp/.sams_secret`，临时 `sams serve --port 8799`。
  登录（sp1check）后取 601333（D347 定位的真实无行业票）：
  * `/api/partials/peers?market=A&code=601333`：`sams sync profile` **不在**，渲染
    「还没有行业（公司概况与主档都没给）：补到行业后这一卡才出数；概况补采状态见本页「公司概况」卡」。
  * `/api/partials/business?market=A&code=601333`：行业地位格同句、命令**不在**。
  验证毕停 8799（生产 8765 `/healthz` 仍 200）、清理副本与脚本。
* **全量套件**：与 D349 合并复跑（两条同文件、同消费者的注记改动），定稿计数记于 D349 doc。