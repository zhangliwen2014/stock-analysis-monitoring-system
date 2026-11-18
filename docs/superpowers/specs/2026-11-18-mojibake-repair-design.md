# 存量乱码的一次性修复：可逆项先干跑，再回填（D360）

**日期**：2026-11-18
**关联**：R3-26（第三轮专家实测：自选备注「éª…」、论点时间线「ç°åºéªè¯」）、
R-03（第二轮：部分写入口绕过 `web/forms.form_data` 的 latin-1→UTF-8 修复，
浏览器正常、裸 UTF-8 客户端乱码并落库；当轮只修了写入侧，历史乱码留着）、
D283（网页不出现命令行；这条命令只给运维，不上网页）
**范围**：`src/sams/core/text.py`（新）、`src/sams/web/forms.py`、
`src/sams/services/text_repair.py`（新）、`src/sams/bootstrap.py`、
`src/sams/cli/db.py`，以及三处测试。不改写入协议、不改页面。

## 背景

R-03 的结论是：乱码发生在**绕过修复层的写入口**，修复层本身（`form_data` 的
`latin-1 → UTF-8` 折返）是对的。第三轮验收又看到两条历史乱码：

* 自选备注：`éª\x8cæ\x94¶-æ··æ\x8e\x92` → 应为「验收-混排」；
* 论点时间线：`ç\x8e°å\x9cºéª\x8cè¯\x81` → 应为「现场验证」。

在生产库副本里按同一判据扫一遍，命中就是这两行（`watchlists.note` 与
`thesis_events.note`）；其他用户填写的文本列都没有命中。R-03 当时写的处置是
「历史乱码先干跑列出可逆项，再按清单回填，不可逆的不猜」，这一步一直没做。

## 现状与约束

* `web/forms.py` 的 `_repair_utf8` 是判据本身：整串能编回 latin-1（每字符
  < U+0100）且那些字节是合法 UTF-8 才折；严格失败就原样保留。浏览器提交的
  真中文（> U+00FF）会撞 `UnicodeEncodeError`，原样返回。
* 判据现在只在写入口用；没有读侧或修复侧的第二份。
* 库是单写者：修复要在 `sams serve` 停下时跑（或由 CLI 自己拿写锁），
  与 `db prune` 的运维方式一致。
* 哪些列是「用户写的」不是靠猜：按表列白名单。新闻/公告/行情/F10 是源数据，
  即使出现同样的字节序列也不在这里改——那是源的问题，猜着改会污染证据链。

## 方案

### 1. 判据归到 `core.text`

新建 `src/sams/core/text.py`：

```python
def repair_mojibake(value: str) -> str:
    """latin-1 读出来的裸 UTF-8 字节折回正确字符；折不了就原样返回。"""
```

内容就是 `forms._repair_utf8` 的原样搬迁。`forms.py` 改为
`from sams.core.text import repair_mojibake`，删除私有副本——判据只有一份，
写入口与修复命令不会在某次改动后各说各话。

### 2. `services/text_repair.py`：白名单、扫描、回填

```python
#: 用户填写的文本列（表, 主键列, 文本列）。源数据与系统生成列不进来。
TARGETS = (
    ("watchlists", ("user_id", "market", "code"), ("note",)),
    ("thesis_events", ("id",), ("note",)),
    ...
)
```

实际清单（只含用户输入）：

| 表 | 主键 | 列 |
|---|---|---|
| `watchlists` | user_id, market, code | note |
| `thesis_events` | id | note |
| `thesis_assumptions` | id | note |
| `annotations` | id | body_md |
| `annotation_revisions` | id | before_md, after_md |
| `rules` | id | name |
| `screener_presets` | id | name, description |
| `channels` | id | name |
| `alert_events` | id | note |
| `portfolio_limits` | id | note |
| `users` | id | display_name |

服务两个方法：

* `scan() -> tuple[Repair, ...]`：只读，逐表逐列把 `repair_mojibake(value) != value`
  的行收进来。`Repair` 带 `table / column / key / before / after`，`key` 是主键值
  用 `/` 拼起来（只给人看，回填用参数化条件）。
* `apply()`：在一个 `Database.transaction()` 里，对 `scan()` 的每一项执行
  `UPDATE <table> SET <column> = ? WHERE <pk…> = ?…`。**回填清单就是干跑清单**：
  两处共用同一个扫描路径，不会出现「预演说 2 处、实修 3 处」。

不可逆的行（`repair_mojibake` 折不动）不报也不改：它们不是这一条判断能回答的东西。

### 3. `sams db repair-encoding`

`cli/db.py` 增加子命令，挂在 `db` 组下：

```
sams db repair-encoding           # 干跑：列出可逆项（before 用 repr，控制字符不上屏）
sams db repair-encoding --apply   # 写回
```

默认干跑而不是默认写：这是一次改写用户原文的动作，且清单要人看一眼（R-03 的
「按清单回填」）。输出：

* 干跑：每行 `watchlists.note A/600519 'éª…' -> 验收-混排`，末行
  `预计修复 N 处；确认后加 --apply 写回`；
* 实修：同样逐行，末行 `已修复 N 处`；没有可逆项时 `没有可修复的乱码`。

`TextRepairService` 经 `ServiceContext` 装配（与 `prune` 同一位置），CLI 不直接
碰仓库，符合「命令模块只通过容器拿服务」。

## 接口与不变式

1. 写入口行为不变：`form_data` 仍只在「urlencoded 且未声明 charset」时折返。
2. 修复只动白名单里的用户文本列；新闻、公告、行情、F10、系统 note 不碰。
3. `scan()` 幂等：修好之后再扫是 0 项；`apply()` 重复跑不产生第二次写入。
4. 干跑与实修共用同一个扫描；`--apply` 之外不写库。
5. 网页不加「修复乱码」按钮（D283 的精神：运维动作留在 CLI；页面不出现命令）。

## 测试（先红后绿）

* `tests/core/test_text.py`：
  * `"现场验证".encode("utf-8").decode("latin-1")` 折回「现场验证」；
  * ASCII、真中文、真 latin-1（`café`）原样返回。
* `tests/services/test_text_repair.py`：
  * 种一条乱码自选备注、一条乱码论点 note、一条干净行 → `scan()` 恰好 2 项，
    键与列对得上；
  * `apply()` 后库里读回正确中文，干净行不变；再 `scan()` 为 0；
  * 新闻标题式的非白名单列即使有同样字节也不在清单里（用 `news_items` 一行验边界）。
* `tests/cli/test_repair_encoding_cli.py`：
  * 默认干跑打印清单、库内仍是乱码；
  * `--apply` 后库内是正确中文，输出「已修复 2 处」；
  * 再跑一次输出「没有可修复的乱码」。

## 验证

* targeted + 全量分批（同前）；
* 生产库副本 `tmp/d360` 上先干跑、再 `--apply`，两条命中分别变回
  「验收-混排」「现场验证」；
* 正式库：停 `serve` → 干跑 → `--apply`（只改这两行）→ 重启 `serve` → 页面确认
  自选备注与论点时间线可读；全程不打印密钥、不碰生产通道。

## 范围外

* 源数据（新闻标题、公告标题、F10 文本）里的乱码：不是我们写的，不猜着改。
* 批注 `tags_json` 里的编码：JSON 字符串需要结构化修复，本次不做；`body_md` 已覆盖。
* 读侧自动折返：会在每次显示时掩盖真实存量，且无法回答「库里到底坏没坏」。
* 新增网页入口：一次性的数据修复不值得一个常驻按钮。

## 回归结果

* `ruff check`：新增与改动文件全部通过（含 `bootstrap.py` 的 import 排序）。
* targeted：`tests/core/test_text.py` + `tests/services/test_text_repair.py` +
  `tests/cli/test_repair_encoding_cli.py` + `tests/web/test_forms_web.py` 共 16 passed；
  另有跑容器的 `test_prune_cli` / `test_cli` / `test_serve` 102 passed（确认 `ServiceContext`
  多一个字段没有把装配打散）。
* 全量分批（`tmp/full_d360_run.sh`，按目录 + web 逐文件 + 根文件共 61 个 chunk）：
  5916 passed，0 failed，21 deselected（live 标记）。
  其中 analysis 305、core 273、storage 797、datasource 660（21 deselected）、
  cli 470、services 1563、monitor 120、web 732、根目录 996。
  唯一的非 0 返回码是 `tests/web/conftest.py` 的 rc=5——它被 glob 扫进来但没有测试用例，
  不是失败。
* 生产库副本 `tmp/d360`：干跑恰好命中两行——
  `watchlists.note 9d28…/HK/03690: 'éª\x8cæ\x94¶-æ··æ\x8e\x92' -> 验收-混排`、
  `thesis_events.note b49d…: 'ç\x8e°å\x9cºéª\x8cè¯\x81' -> 现场验证`；
  `--apply` 后读回两条正确中文，再扫输出「没有可修复的乱码」。
  两列都是用户私有数据，副本上的 HTTP 页面对他人按 404 处理（不是修复问题），
  所以副本验证以库内读回为准。
* 正式库：停 `serve` → 干跑同样两行 → `--apply` 输出「已修复 2 处」→ 再扫
  「没有可修复的乱码」→ 重启 `serve`（PID 75993），`/healthz` 200。
  全程未打印密钥、未动通道与账号配置。

