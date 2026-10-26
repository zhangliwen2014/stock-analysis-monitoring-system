# M4-B 批注、推送上下文与复盘闭环 · 设计

日期：2026-10-23
状态：**设计已定，待实施**。前置为 M4-A（见 [M4-A 设计](./2026-10-23-m4a-intraday-dashboard-design.md)）；M4-A 不实现本文任何写入入口。
关联文档：[总体设计](./2026-10-17-stock-analysis-monitoring-system-design.md)（下称「总 spec」）。

---

## 0. 决策摘要

| 编号 | 决定 | 理由 |
|---|---|---|
| D121 | 批注复用 0001 的 `annotations`/`annotation_revisions`，迁移 0020 补软删除、目标快照与附件表；`team` 可见性**只对 `symbol` 生效**，thesis/rule/alert/order 一律私有；admin 默认只读元数据，私有正文走 D139 break-glass | 观点、规则、告警、成交都是个人决策数据，把一条 team 批注挂到别人的私有 thesis 上，等于用一条批注泄露「这个 thesis 存在」。symbol 是全实例共享的研究对象，才谈得上团队 |
| D122 | Markdown 存源码、服务端渲染；禁用原始 HTML、图片语法与危险链接协议（只放行 http/https/mailto）；推送上下文用同一份源码抽纯文本 | 批注是用户输入，直接渲染等于把 XSS 交给每个团队成员。禁用 HTML 比「先渲染再清洗」少一层依赖与一类漏网；截图只走附件，不走 Markdown 图片 |
| D123 | 附件只收 PNG/JPEG/WebP，每张 ≤ 5 MB、每条 ≤ 5 张、每人 ≤ 512 MB；按魔数校验后用 Pillow 重编码（去 EXIF），存 `<db_dir>/attachments`，下载必须登录且过批注可见性 | 「截图」不需要 SVG/GIF/PDF；重编码同时解决伪造扩展名、图片炸弹与截图里的 GPS/设备元数据 |
| D124 | 告警状态扩为 `new → acknowledged → handled/dismissed`；保留 `handled` 作为总 spec 的 **acted**（不迁移旧数据）；`dismissed` 必须同时有原因码与说明；终态不可回退 | 现有 M3 的 `handled` 数据与 CLI 不动，少一次有风险的数据迁移；ack 与 acted 分开，才能回答「看到了但没做」与「根本没看到」 |
| D125 | acted 必须选一个动作类型：`trade`/`thesis`/`rule`/`watch`/`other`；`trade` 在同一事务里录成交并闭环；thesis/rule 的动作引用必须属于本人且与告警标的相符；`other` 必填说明 | 「已处置」如果只是一句话，复盘时仍然不知道做了什么。把动作做成结构化的引用，才能从告警跳到成交/观点/规则，也才能统计哪类动作真正被使用 |
| D126 | 推送在发送前取「接收人可见的该标的最近 3 条批注」，置顶优先，逐条压成 120 字纯文本；只进文本通道（企微/钉钉/PushPlus/邮件），语音与桌面不变；取批注失败**不阻断**告警；实际带入的 id 与文本写 `notify_log.context_json` | 推送带上下文的价值在「不用打开网页就知道前因」，但隐私边界必须按接收人算，且不能因为批注查询坏了就漏发告警 |
| D127 | 管理员共享策略放 DB 的 `app_settings`（首期只有 `annotations.team_sharing`），改完即生效；关闭后**新** team 批注被拒、既有 team 批注对非作者隐藏（数据保留），重新打开即恢复 | 常驻期间不能要求改配置文件再重启（D117）；关闭是可见性策略而非删除指令，保留数据才可回退 |
| D128 | 新增 `/review`：最近 7/30 天或本周的告警闭环率、忽略率、忽略原因、动作类型、超 24 小时未处理清单、本周批注；规则忽略率与胜率的深度分析仍归 M5 | 复盘闭环的最小可用形态是「这周哪些没关、为什么关、关了之后做了什么」；横向参数比较需要跨期样本，M4 没有 |
| D129 | 批注不触发通知、不做 @ 提及、不做实时协作；发布与编辑后靠页面局部刷新可见 | 批注是研究留痕，不是 IM。把批注接进通知会在告警之外再造一条打扰源，而 M4 要解决的是告警疲劳 |
| D135 | `/review` 首屏以**清单**为主：超 24 小时未处理、本周「忽略/触发」比最高的规则、本周处置→成交/观点/规则；已读率/闭环率/平均关闭时长降为脚注，并给出分母 | 产品评审指出四个比率不可行动：已读率受「是否点已读」影响、平均关闭时长混合分钟与隔夜、忽略数没有触发分母。复盘要回答「下一步改什么」，不是看仪表盘 |
| D136 | 批注正文在软删 30 天后**硬清除**（正文、标签、revision 与附件；保留 id/目标/作者的墓碑）；写作框旁常驻「可见范围」说明；admin 对私有正文默认不可读，break-glass 见 D139 | 「删除」若只是隐藏、管理员还能永久读，用户就不会写真实想法。墓碑保留计数与审计，正文不无限期留着 |
| D137 | 推送上下文取「接收人可见的 symbol 批注 + 该告警同标的 thesis 批注」，合计最多 3 条；`alerts` 增 `thesis_id`。闭环时可一键「同时写一条标的批注」，预填告警与处置结论 | 交易者真正想先看到的是「为什么买/等」的观点批注，而不是标的页上零散的最新三条；处置说明只留在告警里，第二天推送与复盘都看不到，等于白写 |
| D138 | 新增非终态 `defer`（`deferred_until`，到点重回收件箱，不计忽略率）与终态后 5 分钟 `undo`；所有 ack/defer/acted/dismiss/undo 写入 `alert_events` | 手机误点、开会中先放一放都是真实动作，二选一「处置/忽略」会逼出假数据。动作历史表同时解决「谁在什么时候改了什么」——现有列只有最后一态 |
| D139 | admin 默认只读**元数据**（作者、目标类型、可见性、时间、计数与占用）；`team` 批注在策略开启时可直接读，`private`（以及策略关闭时的 `team`）正文/附件默认不可读。确需查看时对**单条**批注 break-glass：填理由（≥4 字）、授权 15 分钟、每管理员每小时最多 5 次；授权写 `admin_access_log`（谁、何时、哪条、理由、到期），附件与 revision 同权；不做批量查看/导出 | 总 spec 的「admin 全量只读」会让用户不敢写真实想法。break-glass 保留合规调查与滥用处置能力，又用单条、限时、限次、留痕把默认边界写死，避免管理员变成无痕浏览全部私人笔记 |

---

## 1. 目标与非目标

### 1.1 目标（M4-B 完成时）

1. 在标的、观点、规则、告警、成交五个对象上写批注：Markdown、标签、截图、置顶、编辑历史；默认仅自己可见，标的批注可标为团队可见。
2. 告警推送到手机时，正文里带该标的最近 3 条**接收人可见**的批注；打开网页能看到完整上下文。
3. 每条告警都能走完「已读 → 已处置 / 已忽略」：忽略必须说原因；处置必须说做了什么，能直接跳到那笔成交、那条观点或那条规则。
4. 每周在一屏里看清：多少告警没关、忽略都为什么、处置动作都是什么、这周写了什么。
5. admin 能开关团队共享、看每个人的附件占用与批注元数据；私有正文默认不可读，确需查看时逐条 break-glass（理由、限时、限次、留痕），且改不了别人的内容。

### 1.2 非目标（M4-B 不做）

- 批注的通知、@ 提及、点赞、评论串、实时多人编辑。
- 团队/项目/角色的细粒度 ACL：本版 `team` 就是「本实例所有启用用户」。
- 全文检索引擎；搜索用 `contains(lower(body_md), lower(?))`，数据量在单机万级以内。
- PDF/CSV/视频附件；只收截图。
- 批注版本 diff UI（展示前后两版全文与附件 id 即可）。
- 规则忽略率跨期分析、止损参数横向比较、胜率回填（M5）。
- 告警自动生成批注；复盘长文仍由用户自己写。
- 周报自动推送（留作 M5 的运营增强；本版 `/review` 是主动打开的一页）。

---

## 2. 核心旅程

**盘中（手机收到推送）**
1. 推送标题仍是「哪只票、哪条规则、什么价/什么线」；正文多一段「最近批注」，知道这次异动是不是在预期内。
2. 点深链打开告警详情（配置了 `public_base_url` 才有链接）。
3. 看一眼该标的的批注与最近成交，选择：录入成交 / 关联观点 / 调规则 / 加自选 / 忽略（填原因）。
4. 闭环后页面回到「下一条未处理」，而不是回首页重新找。

**盘后（研究留痕）**
1. 在标的页写下「为什么买/为什么等」，贴一张日线截图，打上 `逻辑`、`风险` 标签，置顶。
2. 第二天这条批注自动进入该标的告警的推送上下文。

**周五（复盘）**
1. 打开 `/review`：本周新增/已读/已处置/已忽略、忽略原因分布、超 24 小时未处理清单。
2. 点开一条被忽略三次的规则，直接去改参数；点开一条已处置告警，跳到当时那笔成交核对。
3. 在标的或观点上补一条复盘批注，结束这一周。

---

## 3. 数据模型与迁移 0020

迁移 `0020_annotations_review.sql`，只加列/加表/加索引，不改既有行：

```sql
-- 告警闭环
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS action_kind VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS action_ref VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS dismiss_code VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS thesis_id VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS deferred_until TIMESTAMPTZ;

-- 闭环动作历史：ack/defer/acted/dismiss/undo 各一行，终态前的最后一条是 undo 的依据
CREATE TABLE IF NOT EXISTS alert_events (
    id         VARCHAR PRIMARY KEY,
    alert_id   VARCHAR NOT NULL,
    user_id    VARCHAR NOT NULL,
    action     VARCHAR NOT NULL,
    from_status VARCHAR NOT NULL,
    to_status   VARCHAR NOT NULL,
    note       VARCHAR,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alert_events_alert
    ON alert_events (alert_id, ts);

-- 通知审计：这一次发送实际带入了哪些批注
ALTER TABLE notify_log ADD COLUMN IF NOT EXISTS context_json VARCHAR;

-- 批注
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS target_label VARCHAR NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS deleted_by VARCHAR;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;
ALTER TABLE annotation_revisions ADD COLUMN IF NOT EXISTS meta_json VARCHAR NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS annotation_attachments (
    id            VARCHAR PRIMARY KEY,
    annotation_id VARCHAR NOT NULL,
    user_id       VARCHAR NOT NULL,
    filename      VARCHAR NOT NULL,
    media_type    VARCHAR NOT NULL,
    size_bytes    BIGINT NOT NULL,
    sha256        VARCHAR NOT NULL,
    storage_path  VARCHAR NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_annotations_target
    ON annotations (target_type, target_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_annotations_user
    ON annotations (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_annotation_attachments_annotation
    ON annotation_attachments (annotation_id, position);

-- 运行时策略（首期只有团队共享开关）
CREATE TABLE IF NOT EXISTS app_settings (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR
);

-- 管理员 break-glass 授权审计：正文与附件内容不入日志
CREATE TABLE IF NOT EXISTS admin_access_log (
    id            VARCHAR PRIMARY KEY,
    admin_user_id VARCHAR NOT NULL,
    annotation_id VARCHAR NOT NULL,
    owner_user_id VARCHAR NOT NULL,
    action        VARCHAR NOT NULL DEFAULT 'break_glass',
    reason        VARCHAR NOT NULL,
    granted_until TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_access_admin
    ON admin_access_log (admin_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_admin_access_annotation
    ON admin_access_log (annotation_id, created_at);
```

- `annotations.attachments_json`（0001 既有列）保持写入 `'[]'`；附件的唯一出处是 `annotation_attachments`。不删旧列是为了迁移只增不改。
- `target_label` 是创建时的显示名快照：标的改名、规则被删、成交页不再可达时，批注列表仍能说清「这是关于什么的」。目标还能解析时优先显示实时名称。
- `alerts.handled_at` 的语义在 M4-B 明确为「人把它关上（处置或忽略）的时刻」，不是「处置」专属；旧数据本来就是这么写的。`acknowledged_at` 单独记「已读」，`deferred_until` 记「到点再提醒」。
- `alerts.thesis_id` 由规则评估时从快照带入（规则经 thesis 展开时才有）；用于推送上下文与告警详情跳转，不改变告警的归属键。
- `alert_events` 是写多读少的审计表；`alerts` 上的状态列仍是当前结论，列表与统计主要读列，详情页读事件。
- `admin_access_log` 既是 break-glass 的审计，也是授权与限次的唯一依据：授权时写一行，`granted_until` 决定该条是否仍可读；重启不丢授权，不存正文/附件内容，行不可删改。
- 迁移幂等；`handled` 旧行保持原值，不重写。

---

## 4. 批注领域

### 4.1 挂载目标与授权

`target_type` 五值，`target_ref` 的形状固定：

| target_type | target_ref | 创建/读取时的归属校验 | 团队可见 |
|---|---|---|---|
| `symbol` | `A:600519`（canonical `市场:代码`） | 登录即可；标的无需在库 | 允许（受策略） |
| `thesis` | thesis id | 必须是本人的 thesis | 强制私有 |
| `rule` | rule id | 必须是本人的 rule | 强制私有 |
| `alert` | alert id | 必须是发给本人的 alert | 强制私有 |
| `order` | sim_order id | 必须挂在本人账户下 | 强制私有 |

- 非 symbol 目标写入 `visibility=team` 直接 `ValidationError`；读取时也按私有过滤，防止绕过 UI。
- admin 对批注只读：`team` 且策略开启可直接读；`private` 与策略关闭时的 `team` 默认只读元数据，正文/附件需逐条 break-glass（D139）；不能创建到别人的目标上，不能编辑/删除别人的批注。
- 目标不存在、不属于当前用户，统一 404（不泄露存在性）。

### 4.2 可见性与共享策略

- 默认 `private`；symbol 批注可由作者选择 `team`。
- 读取规则：
  - 本人的全部；
  - `team` 且策略开：本实例所有启用用户可读；
  - 其他人的 `private`：不可读；
  - admin：`team` 且策略开可读；`private` 与策略关闭时的 `team` 默认只读元数据，break-glass 后 15 分钟内可读该条正文、revision 与附件。
- 策略 `annotations.team_sharing`（`app_settings`，默认 `on`）：
  - `off` 时新写 `team` 被拒；
  - 既有 `team` 行不删，对**非作者**隐藏；作者仍可见并显示「团队共享当前已关闭」；
  - 重新 `on` 后恢复可见；
  - 开关动作写应用日志（操作者、旧值、新值、时间）。
- 策略在每次读取时取（带 5 秒进程内缓存），不做一次性快照。

### 4.3 生命周期与编辑历史

- `create`：正文 1–20,000 字（可配置）；标签 ≤ 10 个；附件 ≤ 5 张。
- `edit`：只有作者；可改正文、标签、可见性、附件集合（增删）；每次编辑写一条 `annotation_revisions`，`before_md`/`after_md` 与 `meta_json`（前后标签、可见性、附件 id）一并落库。
- 乐观并发：表单带隐藏的 `updated_at`；与服务端不一致返回 409 并保留用户输入提示刷新，不做静默覆盖。
- `pin`：作者可置顶/取消；同一目标最多 3 条置顶；置顶不是编辑，不写 revision。
- `delete`：软删除（`deleted_at`/`deleted_by`），作者可删；列表与目标页立即隐藏；admin 默认只看到墓碑元数据，私有正文需 break-glass（D139）；附件保留 30 天后由 prune 回收。
- 编辑/删除被删批注返回 409/404；终态清晰。

### 4.4 标签

- 输入逗号分隔；去首尾空白、去前导 `#`、压缩内部空白；大小写不敏感去重，保留第一次出现的写法；单个 ≤ 20 字。
- 存储为 `tags_json` 数组；筛选用精确匹配（大小写不敏感）。
- 不建标签表：单机规模下，用户级标签云由最近 200 条批注的标签聚合即可。
- 标签是检索辅助，不参与权限与推送筛选。

### 4.5 列表、搜索与排序

- 目标页默认 `pinned DESC, created_at DESC`；分页 20。
- `/annotations` 支持 `q`（正文子串）、`tag`、`type`、`scope=mine|team`；默认 `mine`；普通用户的 `team` 只含 symbol 的 team 批注。
- 搜索参数化；大小写不敏感；`%`/`_` 不作通配符（用 `contains(lower(...), lower(?))`）。
- 已删除、不可见、目标已删的批注不出现在普通列表；目标已删但批注可见的，显示 `target_label` 与「目标已删除」。

---

## 5. Markdown 与附件

### 5.1 渲染

- 依赖新增 `markdown-it-py`（服务端）。
- 渲染器配置：`html=False`（原始 HTML 一律转义）、禁用 `image` 规则、禁用 `linkify`、启用 `table` 与 `strikethrough`。
- 链接只放行 `http`/`https`/`mailto`；渲染后统一加 `rel="noopener noreferrer"`、`target="_blank"`；其他协议（`javascript:`、`data:`、`file:`）降级为纯文本。
- 代码块只做样式，不做语法高亮（少一个前端依赖）。
- 预览走 `POST /annotations/preview`（带 CSRF），返回与详情相同的 HTML；失败时返回转义后的原文。
- 推送纯文本：遍历 token，标题/段落/列表项/代码块取文字，链接取链接文字，图片规则已禁用；每条压成一行 120 字。

### 5.2 附件校验与存储

- 接受类型：`image/png`、`image/jpeg`、`image/webp`。SVG/GIF/PDF 一律拒绝并说明原因。
- 单张 ≤ 5 MB，每条 ≤ 5 张，每用户总量 ≤ 512 MB（均可在 `[annotations]` 调整）。
- 校验顺序：大小 → 魔数 → Pillow `Image.open(...).verify()` → 重编码。重编码同时完成：
  - 去 EXIF（GPS、设备、拍摄时间）；
  - 长边缩到 ≤ 2560 px；
  - 统一输出 PNG（带透明）或 JPEG/WebP（质量 85）；
  - 拒绝解压后像素数超过 40MP 的图片（图片炸弹）。
- 存储：`<attachments_dir>/<user_id>/<yyyy>/<mm>/<attachment_id>.<ext>`；`storage_path` 存相对路径，换机器只改根目录。
- 写入顺序：暂存目录 → 校验/重编码 → 移入最终路径 → 写 DB 行。DB 失败则删文件；进程在两步之间崩溃只留下孤儿文件，由 prune 回收，不会出现「库里有行、磁盘没文件」。
- 文件名只用于展示；下载响应用 `attachment_id` 定位，`filename*` 按 RFC 5987 编码。

### 5.3 下载与展示

- `GET /annotations/{annotation_id}/attachments/{attachment_id}`：先按批注可见性鉴权，再按 `storage_path` 读文件。
- 响应头：正确 `Content-Type`、`Content-Length`、`X-Content-Type-Options: nosniff`、`Cache-Control: private, max-age=300`；图片用 `inline`，未知类型不提供。
- 缩略图：直接用原图 + CSS `max-width`，不生成缩略图文件（单机规模够用）；列表页只加载前 `max_attachments` 张（默认 5，与张数上限同源——编辑器要能勾选到每一张）。
- 文件缺失：条目显示「附件缺失」，不 500；admin 健康页统计缺失数。
- CSP 保持 `img-src 'self' data:`；附件同源，不开 `blob:`/外链。

### 5.4 删除与回收

- 软删除后批注与附件立即对普通列表/下载不可见；管理员默认只看到墓碑元数据，私有正文在 break-glass 且未硬清除时可读（D136/D139）。
- 软删满 30 天后由 prune 硬清除：正文置空、标签清空、`annotation_revisions` 删除、附件行与文件删除；保留 `id`、`user_id`、`target_type`、`target_ref`、`target_label`、`created_at`、`deleted_at`、`purged_at` 作为墓碑（计数与审计仍能成立）。
- `sams db prune --annotations-purged-days 30 --attachments-orphan-days 30`：
  - 硬清除到期的软删批注；
  - 删除无对应批注行的孤儿文件；
  - `--dry-run` 打印将删批注数、文件数与字节数。
- 用户删除账号不在 M4-B 范围；admin 禁用用户不删批注，只阻止登录。

---

## 6. Web 页面与动作

### 6.1 新增/变更路由

| 路径 | 方法 | 内容 | 权限 |
|---|---|---|---|
| `/annotations` | GET | 搜索/筛选/分页 | 登录 |
| `POST /annotations` | POST | 新建（multipart：正文、标签、可见性、文件） | 登录 |
| `POST /annotations/{id}/edit` | POST | 改正文/标签/可见性/附件 | 作者 |
| `POST /annotations/{id}/delete` | POST | 软删除 | 作者 |
| `POST /annotations/{id}/pin` | POST | 置顶切换 | 作者 |
| `POST /annotations/preview` | POST | Markdown 预览 fragment | 登录 |
| `GET /annotations/{id}/attachments/{att_id}` | GET | 附件下载 | 可见者 |
| `GET /partials/annotations` | GET | 目标页批注面板 fragment | 登录 |
| `POST /alerts/{id}/ack` | POST | 已读 | 收件人 |
| `POST /alerts/{id}/acted` | POST | 处置（动作类型/引用/说明；trade 可同事务录成交；可选同步写批注） | 收件人 |
| `POST /alerts/{id}/dismiss` | POST | 忽略（原因码 + 说明；可选同步写批注） | 收件人 |
| `POST /alerts/{id}/defer` | POST | 稍后（1h/3h/下一开盘） | 收件人 |
| `POST /alerts/{id}/undo` | POST | 5 分钟内撤销终态 | 收件人 |
| `GET /review` | GET | 复盘工作台 | 登录 |
| `GET /admin/annotations` | GET | 批注元数据、策略、用量、最近 break-glass 记录 | admin |
| `POST /admin/annotations/policy` | POST | 团队共享开关 | admin |
| `GET /admin/annotations/{id}` | GET | 元数据 + 正文（team 且策略开，或有效 break-glass） | admin |
| `POST /admin/annotations/{id}/break-glass` | POST | 填理由，授权 15 分钟查看该条私有正文 | admin |

- 所有 POST 走 CSRF；所有写操作记应用日志（user_id、动作、目标、结果）。
- 标的/观点/规则/告警/成交详情页嵌入批注面板；告警页额外显示「该标的推送上下文」（与推送同源的 3 条）。
- `/alerts` 状态筛选增加「已读」；导航显示 `new + acknowledged` 计数。
- 所有页面空状态给出一句可执行的话，不留空白区块。

### 6.2 批注面板

- 顶部：作者头像位（用户名首字）、输入框、标签输入、可见性选择（仅 symbol 显示「团队可见」）、附件选择、预览/发布。
- 列表：置顶优先；每条显示作者、时间（`display_tz`）、可见性徽标、标签、正文、附件缩略图、编辑/删除/置顶（作者）。
- 筛选：全部/我的/团队（仅 symbol）；标签下拉取最近标签。
- 编辑：行内展开编辑器，保留原值；冲突 409 时展示服务端最新版并保留草稿。
- 空状态：「还没有批注。写下这只票的逻辑、风险或今天的异动，告警推送会自动带上最近 3 条。」

### 6.3 告警详情与闭环面板

- 顶部：标题行、状态徽标、触发时间、规则、账户、标的、价格/阈值、盘中标记与报价来源（M4-A）。
- 上下文区：该标的最近 3 条可见批注（与推送同源）、最近一笔成交、相关观点。
- 动作区：
  - `new`：按钮「已读」「处置」「忽略」「稍后」。
  - `acknowledged`：按钮「处置」「忽略」「稍后」。
  - 稍后：1 小时/3 小时/明天开盘，写 `deferred_until`，状态不变；到点重回收件箱顶部并显示「你让它稍后提醒」。
  - 终态：只读显示处置动作、说明、时间、操作者（目前即收件人）与关联对象链接；5 分钟内显示「撤销」，撤销后回到上一状态并记 `alert_events`。
- 处置表单：
  - 动作类型五选一；
  - `trade`：展开成交表单（账户、方向默认按规则类型、数量、价格、费用、时间），提交时同一事务写 `sim_orders` 并闭环；A 股 T+1 不可卖、数量非整手、跌停/涨停、港股每手与美股零股只做**警告**（不阻止用户记录真实成交）；
  - `thesis`：下拉本人同标的的观点；
  - `rule`：下拉本人规则；
  - `watch`：一键加入自选；
  - `other`：说明必填。
  - 所有处置/忽略表单带「同时写一条标的批注」复选框（默认关），勾选后预填「告警摘要 + 我的处置/原因 + 链接」，避免复盘时只剩一句状态。
- 忽略表单：原因码必选 + 说明；`misreport`/`other` 的说明必填（≥ 2 字），其余可选；展示「这条原因会进入本周忽略率」。
- 终态可 5 分钟内撤销；超过 5 分钟只能新写批注说明（不把审计当编辑器）。

### 6.4 复盘工作台 `/review`

- 窗口：`window=7d`（默认）/`30d`/`week`（本周一 00:00 起），边界按 `server.display_tz`。
- **首屏是清单，不是仪表盘**（D135）：
  1. 超 24 小时未处理（`new`/`acknowledged`，按触发时间升序，前 20 条，带快捷「已读/处置/忽略/稍后」）；
  2. 本周「忽略/触发」比最高的 5 条规则（样本 < 3 条单列「样本不足」；点击去规则页改参）；
  3. 本周已处置（动作与关联的成交/观点/规则链接）；
  4. 本周批注（本人 + 可见 team）。
- 次屏折叠区：忽略原因分布、动作类型分布、新增/已读率/闭环率/平均关闭时长，每项标注分子分母与窗口；空分母显示「—」。
- 顶部只有三个数字：未处理总数、超 24h 数、本周新增；不做四张同权重的比率卡。
- 空状态：「本周没有告警。可以去看看自选与持仓，或写一条复盘批注。」
- 所有数字可点进带筛选的 `/alerts`；复盘页本身不做写操作（除快捷闭环外）。

### 6.5 管理页 `/admin/annotations`

- 策略卡：团队共享开关（二次确认文案说明「关闭后既有 team 批注对他人隐藏，数据保留」）。
- 用量：每用户批注数、附件数、占用字节；总计与配额。
- 列表：按用户/目标类型/可见性/时间筛选，只出元数据——作者、可见性、目标类型、时间、revision 数、附件数与字节、标签数、是否软删。`team` 且策略开启时显示正文、标签与 `target_label`；`private` 与策略关闭时的 `team` 一律遮蔽正文、标签、附件内容与 `target_label`（symbol 只显示代码，非 symbol 显示「私有 thesis/rule/alert/order」），避免元数据本身泄露策略。
- Break-glass：被遮蔽的行（`private` 或策略关闭时的 `team`）有「紧急查看」；必须填理由（4–200 字），提交后写 `admin_access_log` 并授权 **该条** `break_glass_minutes` 分钟（默认 15，治理参数）。有效期内详情页显示正文、revision 与附件缩略图/下载，顶部常驻「临时授权至 HH:MM，理由：…」横幅；不做批量选择、列表内联正文或导出。过期后详情只回元数据，再看得重新授权。
- 限次：每管理员每小时最多 5 次授权（按 `admin_access_log` 滑动窗口），超限 429 并给出等待时间；理由是自由文本，不做「常用理由」预设以免敷衍。
- 附件下载：`GET /annotations/{id}/attachments/{att_id}` 对 admin 复用同一授权判断；无有效授权按不存在返回 404。
- 审计：页面底部列出最近 50 条 break-glass（管理员、批注、作者、理由、授权时间与到期）；只读，不可删改。硬清除后的批注不再可授权，记录仍保留。
- 回收：显示最近一次 prune 的附件回收结果；手动触发走后台任务，不在请求线程里删文件。

---

## 7. 推送带上下文

### 7.1 选择与隐私

- 只在 `market != 'SYS'` 且 `code` 非空的规则告警上附加。
- 取两处**接收人可见**的批注合并：该 `(market, code)` 的 symbol 批注（本人私有 + 策略开启时的 team）与该告警 `thesis_id` 的 thesis 批注（本人；别人读不到这条 thesis）；排除软删除与已清除（D137）。
- 排序：置顶优先，其余按 `created_at DESC`；合计取 3 条。来源不单独标注，作者与时间已足够判断。
- 同一批注不会重复计入；thesis 与 symbol 都命中时按 id 去重。
- 作者显示名：本人显示「我」，他人显示 `display_name`（空则回退用户名）；不显示用户 id。
- 每条内容：`MM-DD HH:MM 作者 [标签] 正文摘要`，正文压成单行 ≤ 120 字；不附附件、不附链接。
- 上下文**不写入** `alerts.payload_json`（告警创建时还不知道发送时刻的批注），只写 `notify_log.context_json`。

### 7.2 消息与通道

- `core.notify` 新增 `AnnotationContext`（`annotation_id`、`created_at`、`author_label`、`body_text`、`tags`、`pinned`）与 `format_annotation_context(...) -> str`；`AlertMessage` 本身不加字段。
- 组装发生在 `NotifyService.deliver_existing(alert_id)`：先从 `alerts` 行重建 `AlertMessage`（不再复用内存对象），再向注入的 `AlertContextProvider` 要 3 条，把格式化后的块追加到 `message.body`，最后交给各通道。
- 文本通道（wecom/dingtalk/pushplus/email）在 `message.body` 末尾追加：

  ```
  —— 最近批注 ——
  · 10-22 14:03 我 [逻辑] 渠道库存见底，Q4 提价…
  · 10-21 09:30 张三 [风险] 大客户流失…
  ```

  由于追加在末尾，现有按通道字节上限的 `trim` 会先剪掉最旧的一条，符合「标题与判据优先」。发送前先按通道上限预留标题与判据的字节；若剩余空间连一条都放不下，整块上下文不发，而不是把半行截断成乱码。
- 语音、桌面不追加（`LIMITS` 只有 140/200 字节，放不下且价值低）。
- 若 `public_base_url` 非空，文本通道末尾再加一行 `详情：<base>/alerts/<id>`；为空则不加，不猜地址。
- 组装失败（查询异常、渲染异常）：记 warning，按无上下文发送；绝不因为批注查询失败而不发告警。
- `notify_log.context_json` 记录：`{"ids": [...], "dropped": 0, "rendered_chars": N}`；每个通道的投递行各记一份。发送失败也保留上下文记录，便于判断「是不是上下文太长被剪」。

### 7.3 与队列的关系

- M4-A 的 `QueuedAlertSender` 只做两件事：`NotifyService.record(...)` 落 `alerts` 行 → 把 `alert_id` 入队。
- `NotifyWorker` 消费 `alert_id` 后调用 `NotifyService.deliver_existing(alert_id)`，在**发送时**取批注上下文；这样排队 2 分钟后再发，带的是当时的上下文，而不是入队时的。
- `NotifyService.dispatch` 收窄为 `record + deliver_existing` 的组合，CLI/盘后路径行为不变。
- 重启丢失内存队列：告警行仍在（`status='new'`），admin 可手动重发；重发走同一 `deliver_existing`，上下文重新取。

---

## 8. 复盘闭环

### 8.1 状态机

```
new ──ack──> acknowledged ──act──> handled
 │                │                  
 ├──act───────────┘                  
 ├──defer──> (原状态 + deferred_until，到点回来)
 ├──dismiss──────────────────────> dismissed
 └───────────────────────────────> 5 分钟内 undo 回上一态
```

| 迁移 | 条件 | 写入 |
|---|---|---|
| `new → acknowledged` | 收件人 | `acknowledged_at=now`；可重复调用（幂等） |
| `new/acknowledged → handled` | 收件人，必须 `action_kind` | `handled_at=now`、`handle_note`、`action_kind`、`action_ref` |
| `new/acknowledged → dismissed` | 收件人，必须 `dismiss_code`；`misreport`/`other` 必须有说明 | `handled_at=now`、`dismiss_code`、`dismiss_reason` |
| `new/acknowledged → defer` | 收件人 | `deferred_until=now+1h/3h/下一开盘`；状态不变；不计忽略率 |
| 终态 → 上一态（undo） | 收件人，距终态 < 5 分钟 | 清 `handled_at`/动作/原因，写一条 `undo` 事件 |
| 终态 → 其他任何 | 拒绝 409 | — |

- 每个动作（ack/defer/acted/dismiss/undo）都往 `alert_events` 写一行 `from_status`/`to_status`/`note`；`alerts` 上的列是当前结论。
- `handled` 在 UI/推送里显示「已处置（acted）」；内部名保留是为了兼容 M3 的 CLI 与旧数据。
- `ack` 是可选中间态；直接处置/忽略/稍后合法。
- **`no_open_alert` 从 M4-B 起把 `acknowledged` 也算未处理**：已读不等于已处置，否则点一下「已读」会让同票其他规则重新开火。`defer` 仍算 open（它只是稍后提醒，不是关闭）。
- 操作者：`alert_events.user_id` 就是操作者；本版收件人即操作者，admin 不能代操作。若将来需要代操作，接口与审计已经能接。
- CLI：`sams alert handle` 映射 acted（`action_kind=other` 且说明必填）；`sams alert dismiss --reason` 映射 `dismiss_code=other`；新增 `sams alert ack` 与 `sams alert defer --until`。

### 8.2 动作类型与引用

| action_kind | 含义 | 必填引用 | 校验 |
|---|---|---|---|
| `trade` | 已成交 | 新成交的 order id | 同一事务创建；order 的账户属于本人、market/code 与告警一致 |
| `thesis` | 调整/推进了观点 | thesis id | 本人且 market/code 一致 |
| `rule` | 调整了规则 | rule id | 本人；规则可跨标的（watchlist），不强制同标的 |
| `watch` | 加入自选 | 无 | 幂等；已存在不报错 |
| `other` | 其他动作 | 无 | 说明必填（≥ 2 字） |

- `thesis`/`rule` 的动作本身在各自页面完成；告警页只做「关联 + 说明」。这样不复制观点状态机与规则参数校验。
- `trade` 是唯一在告警页内直接写业务数据的动作，因为它最常用且需要原子性（§8.3）。
- 关联对象被删除后，告警详情显示「关联对象已删除」+ `action_ref`，不 500。

### 8.3 成交 + 闭环的原子流程

`ReviewService.act_with_fill(user, alert, account_id, side, qty, price, fee, ts, note)`：

1. 校验告警归属、状态、账户归属、标的与告警一致、数量/价格 > 0。
2. 在同一个 `Database` 事务里：
   - `LedgerService.record(...)` 写 `sim_orders`（沿用现有费用/汇率/持仓更新）；
   - `AlertRepo.act(..., action_kind='trade', action_ref=order.id, note=...)`；
   - 写一条 `alert_events(action='act', from_status, to_status='handled')`。
3. 任一校验或写入失败整体回滚；页面保留表单并显示原因。
4. 成功后 `VersionGate.bump("ledger")`（M4-A 的盘中缓存失效），并回到下一条未处理告警。

表单默认值：账户取告警的 `account_id`（无则必选）；方向在止损/止盈类规则下默认卖出，其余默认买入；数量默认该账户该标的持仓（卖出时），不自动填价格。T+1、整手、涨跌停、港股每手、美股零股只提示不拦截：这是模拟账本，用户可能在补记真实成交；只有数量/价格非正、账户不属于本人、标的与告警不一致会挡。

### 8.4 复盘口径

窗口内（按 `triggered_at` 落在 `[since, until)`）：

- **新增** = 全部告警数。
- **已读率** = `acknowledged_at IS NOT NULL` 的数量 / 新增。
- **闭环率** = `status IN ('handled','dismissed')` 的数量 / 新增。
- **处置率** = `handled` / (`handled` + `dismissed`)。
- **忽略率** = `dismissed` / (`handled` + `dismissed`)；分母为 0 时显示「—」，不显示 0%。
- **中位关闭时长** = `median(handled_at - triggered_at)`，仅终态；显示小时/天，不用平均数（隔夜与分钟级混在一起时平均数没有意义）。
- **超 24 小时未处理** = `new/acknowledged` 且 `triggered_at < now - 24h`；不受窗口限制；`deferred` 仍算未处理，但到点前的行排在后面。
- **忽略原因分布** = 按 `dismiss_code` 计数；`other` 单独一列。
- **规则「忽略/触发」比** = 窗口内该规则 `dismissed` / 该规则全部告警；样本 < 3 条显示「样本不足」，不排名。
- **动作类型分布** = 窗口内 `handled` 按 `action_kind` 计数。
- **旧版行**：M4-B 之前产生的 `handled`（无 `action_kind`）与 `dismissed`（无 `dismiss_code`）单列「旧版」，不并进 `other`，也不进动作/原因分布的分母；审计升级不伪造历史。
- `undo` 之后的状态以当前列为准：撤销过的终态不再计入闭环，`alert_events` 仍保留那次动作。

这些是**描述性统计**，不生成「规则好坏」结论；结论留给 M5 的跨期样本与参数比较。

### 8.5 忽略原因码

| code | 显示 | 用途 |
|---|---|---|
| `misreport` | 误报（规则/数据错） | 最需要回看规则的一类 |
| `expected` | 预期内波动 | 正常噪音，用于评估阈值是否过紧 |
| `data_issue` | 数据异常 | 指向行情源/复权/停牌 |
| `not_applicable` | 已离场/不适用 | 持仓或观点已变 |
| `duplicate` | 重复告警 | 冷却/去重键需要调 |
| `other` | 其他 | 必须写说明 |

原因码是枚举，必选；说明是自由文本，`misreport` 与 `other` 必填，其余可选。CLI 只给 `--reason` 时落 `other`，并在提示里列出原因码；不给原因码直接拒绝。

---

## 9. 配置

```toml
[annotations]
attachments_dir = ""        # 空 = <db 所在目录>/attachments
max_attachments = 5
max_attachment_mb = 5
max_user_total_mb = 512
max_body_chars = 20000
break_glass_minutes = 15       # admin 单条私有批注的临时查看时长
break_glass_max_per_hour = 5   # 每管理员每小时最多授权次数

[server]
display_tz = "Asia/Shanghai"   # 复盘窗口与页面「今天」的边界
public_base_url = ""           # 推送深链根地址；空 = 不附链接
```

- `display_tz` 用 `ZoneInfo` 校验；行情时间仍按各市场时区显示，两者不混用。
- `public_base_url` 只接受 `http://`/`https://`，去掉末尾 `/`；LAN 部署时填 `http://192.168.x.x:8765`。
- `app_settings` 白名单只有 `annotations.team_sharing`（`on`/`off`）；未知键读写都拒绝。策略变更立即生效，不需要重启。
- `attachments_dir` 在启动时创建并检查可写；相对路径按当前工作目录解析，帮助里写明。
- `break_glass_minutes` 与 `break_glass_max_per_hour` 必须为正整数；调大即放宽管理员的临时查看边界，README 提示这是治理参数而不是性能参数。

---

## 10. 安全与隐私

- **越权读取**：所有批注查询先按 `visibility`/`user_id` 过滤，再按目标归属过滤；附件下载复用同一条可见性判断。URL 里的 id 一律服务端重查。
- **XSS**：Markdown 禁 HTML/图片，链接白名单；Jinja 自动转义；CSP 无 `unsafe-inline` 脚本（样式允许内联，沿用 M4-A）。
- **上传**：大小/数量/配额/魔数/像素上限/重编码五道；文件名不参与路径拼接；禁止 SVG。
- **隐私**：推送只带接收人可见批注；team 只对 symbol；写作框旁常驻「可见范围：仅自己 / 本实例用户（仅标的）/ 管理员默认只见元数据」；admin 查看私有正文必须逐条 break-glass，理由与授权写 `admin_access_log`，附件与 revision 同权；软删 30 天后正文硬清除。日志不打印正文与附件路径。
- **审计**：创建/编辑/删除/置顶、策略变更、闭环动作、成交闭环均记应用日志；`admin_access_log` 记录每次 break-glass 的授权人、批注、作者、理由与到期；批注 revision 与 `alert_events` 是不可变历史（正文按 D136 到期清除）。
- **CSRF/会话**：沿用 M4-A；预览接口也要 CSRF。
- **速率**：登录限速沿用；批注创建按用户 30 条/分钟、上传 10 张/分钟做进程内限速，超限 429（防止脚本刷盘）。
- **数据备份**：附件在 DB 外，备份须知包含 `attachments_dir`；README 写明。

---

## 11. 错误处理与边界

- **目标已删除**：批注仍可读（`target_label`），目标页 404；告警动作引用失效显示「已删除」。
- **批注文件缺失**：条目降级显示，不 500；admin 健康页计数。
- **编辑冲突**：409 + 服务端最新内容；不合并、不覆盖。
- **策略切换并发**：读取时取最新策略；已提交的 team 批注不会被删。
- **附件写成功但 DB 失败**：删文件；删不掉记日志，留给孤儿回收。
- **prune 与上传并发**：prune 只处理软删超过 30 天的批注与孤儿文件，按 id 快照处理，不碰新上传。
- **通知上下文查询失败**：按无上下文发送并记 warning；告警的 `thesis_id` 已被删或不属于接收人时，只取 symbol 批注。
- **成交闭环失败**：整体回滚，告警保持原状态；表单值回填。
- **稍后/撤销越界**：对终态点「稍后」、超过 5 分钟点「撤销」、或撤销已被别人改过的终态，返回 409 并刷新页面，不静默改。
- **硬清除失败**：正文已清、文件删不掉时记日志并让下一轮 prune 重试；墓碑行不丢。
- **批注正文超长/标签超限/附件超限**：400 + 字段级说明；不回写半条。
- **break-glass 拒绝**：理由为空或不足 4 字 → 400；每小时超 5 次 → 429 并给等待秒数；批注已硬清除 → 409；无有效授权时管理详情页只返回元数据，附件直链返回 404（不泄露存在性）。授权到期后页面回到元数据，不自动续期。
- **admin 关闭团队共享**：不删数据、不通知用户；页面徽标说明当前策略。

---

## 12. 测试与验收

### 12.1 离线闸

- 迁移 0020：旧库幂等、旧 `handled` 行不变、`context_json` 可空、`app_settings` 默认值、`admin_access_log` 可建可查。
- 批注仓库/服务：五类目标的归属校验；team 仅 symbol；策略开关的写拒/读隐/恢复；标签规范化；置顶上限；软删除；revision 记录前后值与附件 id；目标删除后的 `target_label`。
- Markdown：`<script>`、`<img onerror>`、`javascript:` 链接、`data:` 链接、原始 HTML、图片语法全部不产生可执行内容；表格/代码块正常。
- 附件：魔数不符、SVG/GIF、超大、超数量、超配额、像素炸弹拒绝；EXIF 去除；下载越权 404；文件缺失降级；prune 保留边界。
- 推送：上下文只含接收人可见；symbol + thesis 合并去重；置顶优先；最多 3 条；软删/已清除排除；SYS 排除；thesis 不可读时降级；context 查询失败仍发送；`context_json` 记录；语音/桌面不带；`public_base_url` 深链。
- 闭环：ack 幂等；new→acted/dismissed；终态拒绝重复处置；dismiss 缺码拒绝、`misreport`/`other` 缺说明拒绝；defer 到点重回收件箱且不计忽略率；5 分钟内 undo 回上一态、超时拒绝；`alert_events` 每个动作一行；`no_open_alert` 把 acknowledged 算未处理；trade 同事务成功与回滚；thesis/rule/watch/other 的校验；关联删除后展示。
- 复盘：各比率的分子分母；窗口边界按 `display_tz`；空分母显示「—」；超 24h 清单；忽略原因与动作分布；规则忽略/触发比与样本不足；旧版行单列；undo 后不计闭环。
- 批注删除：软删立即隐藏；30 天后正文/revision/附件硬清除且墓碑保留；管理页不能再读已清除正文。
- 闭环写批注：勾选后预填内容、生成 symbol 批注并关联；失败不改变告警状态。
- Web：所有新页面 200；CSRF 缺失 403；A 用户不能读 B 的私有批注/附件/告警动作；非 admin 访问管理页 403；admin 不能编辑他人批注；策略开关即时生效。
- admin 访问边界：默认管理页只返回私有批注的元数据，正文/标签/附件/非 symbol 的 `target_label` 均不出现；`team` 在策略开时可读、策略关时同样遮蔽；break-glass 缺理由 400、超每小时 5 次 429、授权 15 分钟到期后详情只回元数据（附件直链 404）、授权只覆盖单条（相邻批注仍不可读）、附件与 revision 同权；每次授权恰写一条 `admin_access_log`；硬清除后不可授权。
- 分层：`web` 不写 SQL；新模块进分层守卫；`monitor` 通过注入的 context provider 拿批注，不 import `services/annotations` 的具体实现。

### 12.2 真实运行验收（人工）

1. 在标的页写一条带截图与标签的批注并置顶；构造该标的的价格告警，确认推送正文出现这条批注（文本通道），语音/桌面无。
2. 用第二个账号把同一标的的私有批注改为 team：第一个账号能看到；关掉团队共享后看不到，作者仍能看到；重新打开恢复。
3. 告警 `new → 已读 → 处置`：在告警页录入一笔卖出成交并闭环；持仓与成交页出现该笔，告警详情能跳到它；成交失败（如数量为 0）时告警仍未处理。
4. 忽略一条告警必须选原因码并写说明；`/review` 的忽略率与原因分布与手工计数一致。
5. 超过 24 小时未处理的告警出现在复盘清单首屏；快捷闭环后从清单消失。
6. 附件越权：退出登录或用另一个账号直接请求附件 URL，得到 404/403，不泄露文件。
7. admin 默认看不到他人私有正文；对一条批注填理由 break-glass 后能看到正文与附件，15 分钟后自动失效；审计列表出现这次授权；另一条私有批注仍需单独授权。
8. Windows/Linux：上传、预览、下载、prune 路径正常，无 macOS 专有命令。
9. 备份：复制 DB 与 `attachments_dir` 到另一台机器，批注与附件可读。

---

## 13. 实施顺序（供写计划）

M4-B 分两批上线：**B1 先让「私有批注 + 推送上下文 + 闭环 + 复盘」真实可用**；B2 再加团队协作与全量管理。这样 M4-B 不必等团队权限模型打磨完才能开始用。

**B1（首个可用集）**

1. **迁移与仓库**：0020、`AnnotationRepo`/`AttachmentRepo`/`AlertRepo` 扩展、`AlertEventRepo`；闭环状态机、defer/undo、审计字段；测试迁移与状态迁移。
2. **批注服务（私有）**：五类目标归属校验、标签、revision、软删除；Markdown 渲染与纯文本抽取；只允许 `private` 写入（team 字段先留在 schema，UI 不开放）；测试权限矩阵与 XSS。
3. **附件**：校验/重编码/存储/下载/配额/prune；测试伪造类型、EXIF、越权。
4. **Web 批注**：面板、编辑器、预览、筛选；测试 CSRF/隔离/空状态。
5. **推送上下文**：`NotifyService` record/deliver 拆分、context provider（symbol + thesis）、通道追加与审计；测试隐私/失败降级。
6. **闭环动作**：ack/defer/acted/dismiss/undo、trade 原子流程、watch/thesis/rule 关联、告警页 UI、可选同步批注；测试回滚与终态。
7. **复盘页与收尾**：`/review` 清单与统计、口径文案、真实运行验收、README/备份说明、总 spec 同步。

**B2（协作层）**

8. **团队共享与治理**：开放 `visibility=team`（仅 symbol）、`app_settings` 策略开关、admin 元数据页 + break-glass（理由、15 分钟、每小时 5 次、`admin_access_log`）、全局 `/annotations` 搜索与标签云、附件编辑增删；补策略、越权与授权到期测试。

每步独立可测、可提交；B1 完成后即可进入真实使用，B2 不与 M5 绑定。第 5 步会改动 M4-A 的通知接线，必须先完成 M4-A 的队列实现。

---

## 14. 总 spec 需要同步的改动

- §4.2 权限矩阵：批注的 team 仅 symbol；admin 默认只读元数据，私有正文逐条 break-glass（理由、15 分钟、每小时 5 次、`admin_access_log`、无批量/导出），软删 30 天后正文清除。
- §5.2 schema：`annotations` 增 `target_label`/软删除/`purged_at`；新增 `annotation_attachments`、`app_settings`、`alert_events`、`admin_access_log`；`alerts` 增 ack/action/dismiss/thesis/defer 列。
- §11 通知：文本通道附接收人可见的最近 3 条（symbol + thesis）；`notify_log.context_json`；`NotifyService.record/deliver_existing` 的拆分。
- M4 范围：M4-A + M4-B 均已落设计，M4 不再有「待设计」项；M4-B 分 B1（私有批注/推送/闭环/复盘）与 B2（团队共享/管理/搜索）。M5 保留忽略率跨期分析、胜率回填与参数横向比较。
- §10 内置模板与 §9：ack/defer 加入告警生命周期；`no_open_alert` 的「未处理」含 acknowledged；「被忽略最多的规则」改为「忽略/触发比 + 样本门槛」。
- D12：补「team 仅 symbol」与运行时策略表；D12 的「admin 只读可见全部」**改为**「默认只读元数据 + 单条 break-glass（理由、限时、限次、`admin_access_log`）」，到期清除不变。
- U7/U27：按 M4-A 的闭合口径更新；附件/批注回收并入 U27 的保留清单。

---

## 15. 已知限制与风险

- **团队 = 全实例**：没有项目/角色 ACL；用户多起来之前够用，之后要重做可见性模型。
- **break-glass 是流程约束，不是密码学隔离**：默认元数据、单条授权 15 分钟、每小时 5 次、全量留痕；但拥有 DB 文件的人仍可直接读，应用层无法阻止。作者在 B2 不会收到「管理员查看过」的通知，若团队规模扩大再评估。
- **删除 30 天后才彻底**：软删到硬清除之间，管理员 break-glass 后仍可读；要即时彻底删除只能停服走维护命令（M5 可加）。
- **附件在 DB 外**：备份/迁移必须同时复制目录；只复制 `.duckdb` 会得到「有行无图」。备份仍需停服（M4-A 的已知限制）。
- **推送上下文固定 3 条**：重要的旧批注若不置顶会被更新内容挤出；界面要引导「重要结论置顶」。
- **undo 只有 5 分钟**：超过窗口只能写批注更正；这是为了不让审计表变成可随意改写的编辑器。
- **复盘统计是描述性的**：样本少时忽略率波动大，页面用「样本不足」挡住排名，不把计数包装成规则质量结论。
- **无周报推送/收盘摘要**：复盘页要主动打开；自动摘要留到 M5。
- **真实验收不可替代**：推送通道、手机端换行、LAN 深链与附件下载必须在真实设备上走一遍。
