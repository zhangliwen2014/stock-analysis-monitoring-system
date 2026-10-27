# 数据管理页与强制刷新 设计

日期：2026-10-27 · 状态：待评审
前身：SP1（2026-10-26-sp1-symbol-master-search-autosync）收尾时用户提出；
触发事件：新加票日线已在库、图却被公司行为覆盖闸拦下（`analysis.py` 三份事实缺一不出数），
而自动补数链只补日线不补公司行为——「数据错位或没有数据，管理员/用户要能立刻重刷」。

## 1. 决策

| # | 决策 | 理由 |
|---|------|------|
| D149 | 管理后台新增「数据管理」页 `/admin/data`（仅管理员）。可选范围：**所有用户的自选 + 持仓股 + 各市场配置的主要指数**（去重后的票表）。用户侧入口见 D152，用户只能刷自己的数据 | 管理员是全库修复的执行人；范围用「已关注的票」而不是代码表全量（5000+ 会把源打爆且无必要） |
| D150 | 对一只股票的强制刷新 = **四件套全链重拉**：日线（bars_daily）、公司行为（corp_actions + 覆盖确认）、按日估值、资金流。**财务不动**——财务链本来就每次从 1990 全量重拉，盘后自会追。指数票的重拉对象是 `index_daily` 全量 + 报价板即时值 | 这次事故的根因正是四件套缺了公司行为这一件；缺哪件图都不出。财务是另一条全量链，重刷它只增时长不增正确性 |
| D151 | 重拉窗口分两档，提交时二选一：**近 90 天（默认）** 与 **全历史（自 1990）**。日线/估值/资金流按所选窗口重拉；公司行为不受窗口约束（它的语义本来就是全历史，且覆盖确认要写到今天才放行出图） | 「错位」绝大多数发生在近期段落；全历史是核弹，留给怀疑历史数据被污染的场合，不该是默认 |
| D152 | 用户入口两个：① 个股页在现有「立即排一拍」旁加「**强制刷新这只**」（对该用户自选/持仓里的票可用，别的票 403）；② 自选页顶部「**刷新我的自选与持仓**」。两者提交的是同一件事：把这批票进刷新任务队列 | 普通用户遇到「我的票没数」不该只能等管理员；与 D144 的排一拍互补——排队管「从无到有」，刷新管「从坏到好」 |
| D153 | 执行模型 = **即时后台任务 + 进度条**，不做每拍队列：全局同时只跑一个刷新任务（内存单例，busy 时后来者收 409/「已有任务在跑」提示），任务在专用 daemon 线程上**串行**逐票重拉（一拍一只都嫌慢，串行+限速桶是唯一安全解）。页面 htmx 每 5s 拉进度 partial：`已完成 x/y · 当前 A/600519 · 失败 k 只（列前 5 条原因）`，带「取消」按钮（置 stop 标志，当前这只跑完就停） | 队列方案（每拍 3 只）几百只要排十几个小时且堵住新加票；同步跑在 HTTP 线程会超时。进度可见 + 可取消是管理员敢点「刷新全部」的前提 |
| D154 | 任务状态**只存内存**：serve 重启即失联，页面若发现「任务在跑」的旧观感与实际不符，以 health 心跳为准自然归零；每次提交与完成都写审计（`admin.data.refresh` / `user.data.refresh`，含范围、窗口档、只数、失败数） | 这是分钟级的运维修复动作，不是需要跨重启续命的账本；上表只会让人误以为能恢复断点（YAGNI） |
| D155 | 刷新走**既有服务函数**（`SyncService.sync_daily`、`CorpActionsService.sync_actions`、`FundamentalsService.sync_valuation/flow`、`SyncService.sync_index`、报价 board.update 由链自然带），**新代码不许第四条抄窗口逻辑**；与自动补数的区别只有一个字：**force**——跳过 `run_bars/run_actions/run_valuation/run_flow` 的「已覆盖就跳过」增量窗口判断，从所选窗口起点整段重拉（upsert 幂等，重写入不产生重复行） | 复制那四个窗口函数是四份会分叉的真相；force 只是窗口起点的例外，不值得新语法 |
| D156 | 失败按票隔离：一只票一条链断了记入 `errors`，任务继续下一只；任务整体不中断（与 D144 按只隔离同一条规矩）。连续源失败照旧进源健康记账、该发 SystemAlarm 发 SystemAlarm | 刷新几百只时坏一两只太常见了，中断整个任务的代价是剩下的全刷不完 |

## 2. 形状

### 2.1 服务：`services/data_refresh.py`

```python
@dataclass(frozen=True)
class RefreshRequest:
    units: tuple[tuple[str, str], ...]   # (market, code)，指数代码也在内（sh000001 这类）
    full_history: bool                   # D151 两档窗口
    actor: str                           # 提交人（审计）
    scope: str                           # "admin" | "watchlist" | "positions" | "symbol"（审计措辞）

class DataRefreshService:
    """单任务后台刷新。接口只有三个方法：submit / progress / cancel。"""
    def submit(self, req: RefreshRequest) -> int:  # 返回任务号；已有任务在跑 → BusyError
    def progress(self) -> RefreshProgress | None:  # None = 从未提交过任务
    def cancel(self) -> bool:                      # True = stop 标志已置
```

- `RefreshProgress(total, done, current, ok, errors: tuple[str, ...], finished: bool)`——内存快照，
  任务结束后保留最后一次（页面刷新还能看到「上次任务：完成 312/312，失败 2 只」，直到下一次 submit 覆盖）。
- worker 线程逐票跑链：指数票（`looks_like_index_code`）→ `sync_index` 全量/近 90 天；股票票 →
  `sync_daily` → `sync_actions`（窗口固定 HISTORY_START，不受档约束——覆盖确认必须写到今天）→
  `sync_valuation` → `sync_flow`。
- 起点：`full_history` → `HISTORY_START`；否则 `今天 - 90 天`。终点一律今天（本地日，与 jobs 同口径）。
- 「跳过判断」只有一个入口：现有 jobs 里那些 `_window(...)` 返回 None 就不拉的逻辑，在刷新里替换为
  「按所选起点硬拉」——不给服务层再造一套窗口函数（D155）。

### 2.2 Web

- 页面（admin）：`GET /admin/data` —— 范围选择（全部自选+持仓+指数 / 单只输入 market+code）、窗口两档单选、
  提交 POST `/admin/data/submit`（csrf + `require_admin` + audit）；busy → 回显「已有任务在跑」。
  `GET /admin/data/progress` partial（htmx 5s 轮询）+ POST `/admin/data/cancel`。
- 页面（用户）：个股页按钮 POST `/symbol/{m}/{c}/refresh`（csrf；票必须在该用户 watchlist∪positions，
  否则 403）；自选页按钮 POST `/watchlist/refresh-mine`（范围 = 我的自选+我的持仓，窗口固定近 90 天——
  用户侧不给核弹档）。两者与管理员共用同一个单例任务：用户提交时 busy 同样吃「已有任务在跑」。
  个股/自选页顶部各自显示自己的进度条（同一个 progress 快照，页面文案不撒谎：谁提交都在同一个队列里排队）。
- 模板：`admin_data.html`（extends admin 底座）+ `partials/data_refresh_progress.html`。

### 2.3 组装

- `ServiceContext` 增 `data_refresh: DataRefreshService`（bootstrap 构造，注入 sync/corp_actions/
  fundamentals/registry 已有件）；线程在 bootstrap 内**懒起**——submit 时才起 worker（无常驻线程）。
- 限速：worker 用与调度线程同一个 `rate_limit_per_s` 令牌桶实例（bootstrap 已建；不给刷新单独开额度）。

## 3. 边界与安全

- 「用户只能刷自己的」的裁判：提交时解析该用户 `watchlists` ∪ `sim_positions` 的代码集合，
  请求里的票不在集合内 → 403（不静默剔除——静默会让用户以为刷过了）。管理员范围不筛。
- 指数串在用户侧不可单独提交（指数无「我的持仓」可言，只走管理员的指数范围）。
- 任务串行 = 一次一只，写库走 serve 单连接既有队列，不与调度线程抢锁（同一进程、同一 Database，
  写写并发由库内锁序列化，无需新锁）。
- 取消语义：当前这只跑完就停，不掐断中途的单条链（半途断链会留下比错位更糟的半截区间）。

## 4. 测试钉（关键例）

1. submit 起任务、progress 单调推进、完成后保留上次快照；二次 submit → BusyError（D153）。
2. force 窗口：给定已有 484 根日线的票，近 90 天刷新后区间起点 = 今天-90，行数不增不减（upsert 幂等）、
   覆盖确认写到今天（出图闸放行——正是这次事故的回归钉）。
3. 按票隔离：假源让第二只票的 valuation 抛 SamsError → errors 记一条、任务继续跑到第三只（D156）。
4. 用户越权：trader1 提交非自选非持仓的票 → 403（D152/§3）。
5. 指数票走 `sync_index`，不落 `bars_daily`（防撞车老规矩 D145）。
6. cancel：置标志后 progress 出现 stopped，done 不再增长，当前那只的链跑完才结束（§3）。
7. 审计：admin/user 两种 scope 各落一行 audit。

## 5. 不做（明确）

- 不做跨重启恢复、不做多任务并行、不做财务重拉、不做按数据类的细粒度勾选（四件套是原子语义）、
  不给用户全历史档。热度排序/全市场快照仍归 SP3。
