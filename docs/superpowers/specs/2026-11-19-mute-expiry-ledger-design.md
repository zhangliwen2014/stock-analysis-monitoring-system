# 单票静音必须到期，并有一张台账（D361）

**日期**：2026-11-19
**关联**：R3-10（第三轮专家实测：`/alerts/{id}` 的静音只有类别 + 备注，无到期；
全站没有「当前静音了什么、到何时、谁批的」清单）、D6（默认到期时长、台账位置、
是否记 actor）、D249（静音按类别分开）
**范围**：迁移 0054、`storage/repos/alert_mutes.py`、`web/pages/alerts.py`、
`web/templates/alert.html`、`web/templates/alerts.html`，以及两处测试。
不改引擎判路、不改通知通道。

## 背景

现在的静音是一次没有期限的免责：按下之后那只票的那一类提醒就一直静着，
只能回到某一条仍在列表里的告警详情页逐类解除。专家实测把它归成风控通道问题——
静音让告警不再出现，而「静音了什么」本身没有页面可查。

D6 当时留了三个问题，这里按下面的答案收口：

| 问题 | 决定 | 理由 |
|---|---|---|
| 默认到期时长 | **7 天**，下拉可选 1 / 3 / 7 / 30 天 | 静音是「这几天别吵」，不是「永远别吵」；7 天覆盖一个财报窗口，又不至于让人忘了它 |
| 是否允许永久 | **不再提供**（新写入必须有到期） | 没有期限就是 R3-10 本身；真要长期静音，30 天后再按一次，且台账会一直显示它 |
| 台账位置 | **`/alerts` 顶部一张卡** | 静音是告警通道的一部分，去告警页的人顺手就能看见；不另开一页增加入口 |
| 是否记 actor | **不新增列** | 静音只能由本人建、只作用于本人（`user_id` 即 actor）；台账展示的就是本人。将来出现代他人静音时再加 `created_by`，现在加一列只会永远是同一值 |
| 到期后怎么处理 | **行保留、判路失效** | 台账要回答「静着什么、到过何时」，删行就把历史抹了；判定读的是「到期时刻 > 现在」 |

历史行（迁移前写入的 `alert_mute_scopes` 与只读的旧表 `alert_mutes`）没有到期：
它们继续生效，台账里标「历史（无到期）」，人可以一键解除。迁移不替人补一个到期时刻——
那会在某天早上静默地改变用户明确按过的状态。

## 方案

### 1. 迁移 0054：`alert_mute_scopes.expires_at`

```sql
ALTER TABLE alert_mute_scopes ADD COLUMN expires_at TIMESTAMPTZ;
```

可空：`NULL` 只属于迁移前的行与旧表。新写入一律填。

### 2. `MuteRepo`：到期是一条读规则，不是一条清理任务

* `Mute` 增加 `expires_at: datetime | None`，以及
  `expired(now) -> bool`（`expires_at is not None and expires_at <= now`）。
* `get()`：按 kind 找到的静音若已过期，视同没有；`kind != all` 时继续落到 `all`，
  再落到旧表。旧表没有到期列，永远算生效。
* `add(..., days: int = 7)`：`expires_at = now + days`；`days <= 0` 是 `ValidationError`
  （页面下拉只给四档，仓库仍要拒绝 0 与负数，否则「静音到过去」会立刻失效且没人看得懂）。
  同一类已静音时幂等返回原行，**不延长**——重复按不该悄悄刷新一个用户没看见的期限。
* `list_for(user_id, *, include_expired: bool = False)`：默认只给生效中的，调用方
  （摘要、收件箱徽标）不需要各写一次到期判断；台账传 `True` 拿全量。
* `scopes()` 跟着默认走：详情页只列还能解的类别，过期的静音不占「解除」按钮。

### 3. 页面

* 告警详情页：静音表单加「到期」下拉（1/3/7/30 天，默认 7）；已静音的每一类
  显示「到 11-26 20:00」或「历史（无到期）」。
* `/alerts` 顶部新增「静音台账」卡：
  * 列：标的、类别、理由、静音时刻、到期时刻（过期行标「已过期」）、解除按钮；
  * 解除走新路由 `POST /alerts/mutes/off`（表单带 market/code/kind，不需要一条仍在
    列表里的告警——这正是 R3-10 抱怨的点）；成功后 303 回 `/alerts`；
  * 没有静音时整张卡不出现，不占版面。
* 台账的解除与详情页的解除写同一种审计事件（`alert.unmute`），target 用
  `market/code`：没有告警 id 可写，也不该借一条无关告警的 id。

## 接口与不变式

1. 到期是读规则：`get()` 返回 `None` 即「不静音」，规则/资讯/选股/摘要四条路
   不需要各自再判一次。
2. 行不删：台账能回答历史；`remove()` 仍是那个明确的删除动作。
3. 幂等：同一类重复静音保留第一次的时刻、到期与备注。
4. 历史行不猜到期，继续生效，台账可见。
5. 旧表只读的现状不变；解除 `all` 仍会删旧表行。

## 测试（先红后绿）

* `tests/storage/test_alert_mutes_repo.py`：
  * `add(..., days=0)` 拒；正数得到 `expires_at ≈ now + days`；
  * 用固定时钟把到期拨到过去 → `get()` 为 `None`、`list_for()` 默认不含它、
    `include_expired=True` 含它且 `expired()` 为真；
  * 过期的 `rule` 静音不遮住仍生效的 `all`（到期只废掉它自己那一类）；
  * 旧表行没有到期，永远生效且出现在台账（`include_expired=True`）。
* `tests/web/test_alerts_evidence_web.py`：
  * 详情页静音表单有到期下拉；POST `days=3` 后详情页出现「到 …」；
  * `/alerts` 显示静音台账（标的、类别、理由、到期），POST `/alerts/mutes/off`
    解除后台账里不再有生效行；
  * 已过期的静音不让收件箱分组带「已静音」徽标，台账里标「已过期」。

## 验证

* targeted + 全量分批（同前）；
* 生产库副本：先 `db migrate` 让 0054 落地，再用页面/仓库确认到期筛选；
* 正式库：停 serve → `sams db migrate` → 重启 serve → `/alerts` 台账可见，
  历史静音标「历史（无到期）」；不新建静音以免改用户数据。

## 范围外

* R3-07（超限是否走告警管道）：另一条决定；
* 代他人静音与 `created_by`：功能尚不存在；
* 静音到期前提醒：多一条通知通道，不是本次要回答的问题；
* 把过期行自动清理：台账需要它们。

## 回归结果

* `ruff check`：仓库、页面与两处测试全部通过。
* targeted：`tests/storage/test_alert_mutes_repo.py` 16 passed、
  `tests/web/test_alerts_evidence_web.py` 15 passed；再加收件箱/闭环/资讯三份网页测试
  共 62 passed（静音徽标与详情页旧行为未回退）。
* 全量分批（`tmp/full_d361_run.sh`，61 个 chunk）：5924 passed，0 failed，
  21 deselected（live 标记）。其中 analysis 305、core 273、storage 801、
  datasource 660（21 deselected）、cli 470、services 1563、monitor 120、
  web 736、根目录 996。唯一的非 0 返回码仍是 `tests/web/conftest.py` 的 rc=5
  （glob 扫到无测试文件），不是失败。
* 生产库副本 `tmp/d361`：`db migrate` 只应用 0054，库内原有静音 0 行（生产目前
  没有按过静音），迁移不替任何人补期限；补种一条 3 天静音与一条已过期静音后，
  `/alerts` 台账同屏显示「静音台账 / 价格规则 / 到期 / 已静音」与「已过期」，
  在台账里 POST `/alerts/mutes/off` 后生效行消失、过期行保留；验证用 8766 已停。
* 正式库：停 `serve`（PID 75993）→ `db migrate` 应用 0054（已应用 54、待应用 0）
  → 重启 `serve`（PID 97100），`/healthz` 200。生产没有静音行，按设计不在正式库
  新建静音，台账要等用户第一次按下才会出现。
