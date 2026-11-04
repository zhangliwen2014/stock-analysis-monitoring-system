# S3 监控自动化与舆情（设计）

> 目标：让「自选/持仓」的公告与新闻**自动**每天入库，按用户关键词命中后走既有告警通道；
> 每条资讯都能点回原文，并带 `as_of` 与来源。没有源的地方明说「未覆盖」，不补零、不猜情绪。
>
> 依据：`docs/superpowers/specs/2026-10-31-expert-review.md` §3 的 S3 行（自选/持仓日更、
> 公告/新闻入库 + 关键词 + 原文 + as-of、预约披露日、静音分组）。
> 前置：S1-C 摘要与告警队列、S2 行业/同业读模型、现有东财 F10 公告能力。

## 1. 范围与切片

| 切片 | 内容 | 验收 |
| --- | --- | --- |
| S3-A | 资讯读模型：`news_items`（迁移 0038）+ `NewsRepo` + `InfoItem` 统一视图；公告仍读既有 `announcements` 表，不搬家 | 同一资讯重复同步 0 新增；按 `(market, code, published_at)` 倒序读出；字段齐全（标题/时间/摘要/来源/原文） |
| S3-B | 自选/持仓日更：新增 `news@A` 盘后 job（universe = 自选/持仓/观察），公告 job 已覆盖同一 universe；新闻源选东财个股新闻（A 股） | 当晚 job 跑过；同一晚重跑 0 新增；无源的 HK/US 在健康页显示「跳过（无源）」而非失败 |
| S3-C | 关键词与告警：`news_keywords` + `news_hits`（迁移 0039），命中写 `alerts`（`rule_id NULL`，payload `kind=news`），走既有渠道、去重与静音 | 同一资讯×关键词只告警一次；命中行可点原文；改关键词只影响之后的资讯 |
| S3-D | 页面：`/news` 列表（自选/持仓/观察、市场与代码过滤、关键词高亮）+ 个股页「资讯」卡（最近公告与新闻，命中标记） | 列表与个股卡同一读模型；空态/无源文案明确；分页保留过滤条件 |
| S3-E | 静音分组：新增 `alert_mute_scopes(user, market, code, kind)`，`kind ∈ all/rule/news/digest`；旧 `alert_mutes` 行一律视为 `all` | 静音「资讯」不影响价格规则告警；摘要不列被静音类别的条目 |
| S3-F | 披露日历补「预计」：有稳定源才接，无源时日历明说「预计披露日未接入」；全量回归 + 实机 | 不伪造日期；现有已披露列表不变；全量测试与 ruff 通过 |

## 2. 数据模型

```sql
-- 0038
CREATE TABLE IF NOT EXISTS news_items (
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    source       VARCHAR NOT NULL,
    item_id      VARCHAR NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    title        VARCHAR NOT NULL,
    summary      VARCHAR,
    url          VARCHAR NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source, item_id)
);
```

- 去重键用 `(source, item_id)`：同一篇文章在不同源出现算两条（来源不同、原文链接不同），
  列表层不跨源合并，避免把「谁先发」猜成事实。
- `item_id` 取源自身的 id；源没有 id 时用 `sha1(title|published_at)`，同一源内稳定。
- 公告不迁入 `news_items`：`announcements` 已被日历、个股页与 F10 使用，搬家只会制造两份真相。
  统一读模型在服务层拼 `InfoItem`，底层各读各的表。

```sql
-- 0039
CREATE TABLE IF NOT EXISTS news_keywords (
    id          VARCHAR PRIMARY KEY,
    user_id     VARCHAR NOT NULL,
    market      VARCHAR NOT NULL,
    code        VARCHAR,              -- NULL = 该市场全部自选/持仓/观察
    keyword     VARCHAR NOT NULL,
    channel_ids VARCHAR NOT NULL,     -- JSON 数组；空数组 = 只进摘要不推送
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS news_hits (
    user_id   VARCHAR NOT NULL,
    source    VARCHAR NOT NULL,
    item_id   VARCHAR NOT NULL,
    keyword_id VARCHAR NOT NULL,
    hit_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, source, item_id, keyword_id)
);
```

静音分组（0039 同迁移）：

```sql
CREATE TABLE alert_mute_scopes (
    user_id VARCHAR NOT NULL,
    market  VARCHAR NOT NULL,
    code    VARCHAR NOT NULL,
    kind    VARCHAR NOT NULL,   -- all / rule / news / digest
    note    VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, market, code, kind)
);
```

旧 `alert_mutes` 行在读取时并入 `kind=all`；新写入只写新表，旧表保留只读（迁移不搬历史，
避免一次全表重写把旧行的时间戳改掉）。

## 3. 服务与接线

- `NewsRepo`：`upsert_many`（幂等）、`latest(market, code, limit)`、`since(as_of)`、`prune(days)`。
- `NewsService`：
  - `sync(market, code, as_of)`：调数据源取最近一页，落 `news_items`，返回 `written`；
  - `feed(user, market, codes, keyword_only=False)`：按自选/持仓/观察拼 `InfoItem`（公告 + 新闻），
    每条带 `source/as_of/url`；`keyword_only` 时只留命中行；
  - `match(user, items)`：按用户关键词匹配（见 §4），写 `news_hits` 与 `alerts`。
- `InfoItem`（core）：`market/code/kind(announcement|news)/title/summary/published_at/source/url/as_of`。
- 调度：`build_jobs` 在 `announcements@` 之后登记 `news@A`；HK/US 也登记，源答不上来记「跳过」
  （与 `industry` 的回填 0 行同一处理方式），健康页看得见。
- 告警：命中写 `alerts`，`rule_id` 为 NULL，`stage='hit'`，payload 含
  `kind=news`、`title`、`url`、`keyword`、`published_at`、`source`；渠道取关键词行的 `channel_ids`，
  队列与投递沿用 S1-B 的 `AlertSender`。告警列表把 `rule_id IS NULL AND payload.kind=news`
  显示为「资讯」并链到原文；既有价格告警路径不变。
- 摘要：`DigestService` 在每栏末尾追加「资讯」行（最多 2 条/标的），命中关键词的排前；
  被 `kind=news` 静音的条目不进摘要。

## 4. 关键词语义（v0）

- 纯文本包含匹配，大小写不敏感；中文不分词、不支持正则与布尔表达式（先让「可预期」赢过「强大」）。
- 关键词长度 1–40 字；同一用户同一市场同一代码下不允许重复词（保存时报错）。
- `code` 为空表示该市场所有自选/持仓/观察；保存时仍保留空作用域，不给每只票复制行。
- 只匹配**标题与摘要**，不抓正文；命中行必须带原文链接，点开是源页面。
- 只对**入库之后**的新资讯生效：关键词改动不会回扫历史（回扫等于把「昨天的旧闻」伪装成今天的提醒）。
- 一条资讯命中多个关键词时，每个关键词各留一条 `news_hits`（用户可看到命中了哪几个词），
  但推送合并为一条，避免同一标题在群里刷屏。

## 5. 页面

- `/news`：顶部过滤（市场、代码/名称、只看命中），正文按「今天 / 昨天 / 更早」分组；
  每行：时间（本地时区）、代码与名称、标题（`<a target="_blank" rel="noopener">`）、来源、命中词。
  无源市场显示「该市场新闻源未接入」，不是空列表。
- `/news` 的「关键词」折叠卡：新增（词、可选代码、可选推送通道）与逐行删除；保存失败在卡内
  回显原因（重复 409、长度/非法 400）并保留草稿。不选通道即「只进摘要」；这里同时是 S3-C
  命中与推送的生产者入口。
- 个股页「资讯」卡：最近 5 条（公告 + 新闻合并），每条带日期与来源；「全部」链到 `/news?market=…&code=…`。
- 导航：与「摘要」相邻；未登录不可见。

## 6. 缺失与失败

- 无源：`not_covered`，文案「{市场}新闻源未接入」；不显示空表头。
- 源失败：健康页与摘要沿用「数据未更新」口径；`news@` job 的失败只影响新闻，公告照常。
- 关键词无命中：列表顶部一句「暂无命中」；不是错误。
- 预约披露日：没有稳定源时，日历只显示已披露日期，并在说明处写「预计披露日未接入」；
  不拿「去年同日的披露日」冒充今年预约。

## 7. 测试（红→绿，中文名）

| 文件 | 关键行为 |
| --- | --- |
| `tests/storage/test_news_repo.py` | 幂等 upsert；`item_id` 回退指纹稳定；按日倒序；清理按天 |
| `tests/services/test_news_service.py` | 资讯统一视图含公告与新闻；关键词大小写/作用域；只对新资讯生效；多词合并推送 |
| `tests/services/test_news_alerts.py` | 同一资讯×关键词只告警一次；payload 带 url/as-of；静音 news 不发 |
| `tests/services/test_digest_news.py` | 资讯行进对应栏、最多 2 条；news 静音不出现 |
| `tests/web/test_news_web.py` | 过滤与分页保留；原文链接；无源文案；未登录 303；关键词增删/重复/市场隔离 |
| `tests/test_scheduler_acceptance.py` | job 列表含 `news@A/HK/US`，位置在 `announcements@` 之后、`industry@` 之前 |
| `tests/cli/test_sync_news.py` | `sams sync news --market A --code …` 输出新增计数，二次 0 |

## 8. 实机验收

1. `sams db migrate` → 0038/0039。
2. `sams sync news --market A --code 600519` → 新增 >0；再跑一次 0。
3. 建两个关键词（「回购」「减持」），在 600519 上命中一条公告/新闻：告警列表出现「资讯」，
   点标题开原文；同晚重跑 0 新增。
4. `/news` 过滤到 600519 与「只看命中」各返回对应行；AAPL 显示「美股新闻源未接入」。
5. 静音 600519 的「资讯」：新命中不进摘要、不发渠道；价格规则告警不受影响。
6. 全量 `pytest` + `ruff check src tests`；更新评审文档 §7 的 S3 行。

## 9. 决策记录

- **D243**：资讯 v0 只做事实流（标题/摘要/时间/来源/原文/as-of），不做情绪分、利好利空标签与 LLM 摘要。
- **D244**：新闻去重键 `(source, item_id)`，跨源不合并；公告沿用 `(market, code, art_code)`。
- **D245**：关键词是用户级、市场级、可选代码级；纯包含匹配；只对新资讯生效；多词合并推送。
- **D246**：关键词命中复用告警中心（`rule_id NULL` + `payload.kind=news`），不另起一套通知；
  列表与详情对该类告警显示「资讯」并链原文。
- **D247**：新闻源 v0 只接 A 股东财个股新闻；HK/US 无源显示「未接入」。
- **D248**：预约披露日无稳定源时不接、不猜；日历明说未接入。
- **D249**：静音分组新表 `alert_mute_scopes(kind)`，旧单票静音视为 `all`；`digest` 类静音只影响摘要。
- **D250**：资讯保留 400 天，进每日清理；`news_hits` 随资讯删除级联清理（按 `source,item_id`）。

## 10. 明确不做（S3 范围内）

- 不做正文抓取/全文检索/PDF 解析；不做情绪分与自动摘要。
- 不做社区、股吧、研报库与付费资讯源；不做港美新闻源采购。
- 不做关键词正则、布尔表达式、机器学习分类。
- 不做邮件/短信等新通道；沿用现有渠道与队列。
- 不做「预计披露日」的推算或历史均值代替。
