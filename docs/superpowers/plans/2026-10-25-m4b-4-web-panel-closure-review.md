# M4-B 计划 4：Web 批注面板、告警闭环 UI 与复盘页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M4-B 的 B1 从「库里能写能读」推到「人能在网页上用」：五类目标的批注面板（读、写、预览、附件下载）、告警详情闭环全套（已读/处置五动作/忽略原因码/稍后/撤销/事件历史/推送上下文区）、`/review` 复盘清单，最后拔除 `AlertRepo.set_status` 这条旧路。

**Architecture:** 面板是一枚 htmx fragment（`partials/annotations.html`），页面只带一行容器，读写都回到同一 `_render_panel`——「发布与编辑后靠页面局部刷新可见」（D129）。闸全部在仓库与服务层（D81），web 层只把拒绝的话带回那一屏。复盘统计拆成两半：SQL 在 `AlertRepo` 的四个新查询（原始列，不解 payload），比率在 `services/review_stats.py` 的纯函数——页面只管渲染与「—」的措辞。

**Tech Stack:** Python 3.13、FastAPI + Jinja2Templates + htmx（已 vendored）、DuckDB、`markdown-it-py`（计划 1 已引入）、pytest（`starlette.testclient.TestClient`）、uv、ruff。

**前置：** M4-B 计划 1/2/3 已完成并推送（HEAD ≥ `6b0b261`）。批注仓库/服务、附件服务、`ReviewService`、推送 provider、`AppSettingsRepo`、`alert_events`、0020 迁移全部就位；本计划**不新增存储层能力**，只有三处查询加法（Task 5）与一处旧方法删除（Task 6）。

**计划切分（本文件只做第 4 份）：**

1. 计划 1：0020 + 状态机 + 批注仓库/服务 + Markdown + 配置（已完成）。
2. 计划 2：附件存储链路（校验/重编码/配额/prune，已完成；Web 下载路由按计划 3 的排除清单落在本计划 Task 3）。
3. 计划 3：推送上下文 + 闭环服务层 + CLI（已完成）。
4. **本计划**：Web 批注面板、告警闭环 UI、`/review`、拔除 `set_status`（spec 步骤 4、6 的页面、7）。
5. 计划 5：B2 团队共享、`/admin/annotations` + break-glass、全局 `/annotations` 搜索与标签云、附件编辑增删、总 spec §14 同步（spec 步骤 8）。

**本计划明确不做（防越界）**：`visibility=team` 的写入口（服务层 `create` 恒私有，B2 放宽）；`/annotations` 全局搜索页；`/admin/annotations` 与 break-glass；成交表单里 T+1/整手/涨跌停的**前端警告文案**只做一句静态说明、不做逐条判定（§6.3 本来也说不拦）；「闭环后回到下一条未处理」（旅程 §2 的糖，本版闭环仍回详情页）；成交/持仓页上的 order 目标批注面板（没有 order 详情页可挂，`order` 目标仍可由服务层写入，UI 入口留给计划 5 一并设计）。

---

## File Structure

**新建**

| 路径 | 职责 |
|---|---|
| `src/sams/web/pages/annotations.py` | 批注面板 fragment：读、创建（multipart）、编辑、置顶、删除、预览、附件下载 |
| `src/sams/web/templates/partials/annotations.html` | 面板 fragment（不 extends base） |
| `src/sams/web/pages/review.py` | `/review` 页 + 导航未处理数 fragment |
| `src/sams/web/templates/review.html` | 复盘工作台（清单为主，D135） |
| `src/sams/services/review_stats.py` | §8.4 复盘口径的纯函数（分子分母、中位数、规则比、旧版单列） |
| `tests/web/test_annotations_web.py` | 面板读/写/预览/限速/越权/下载 |
| `tests/web/test_alerts_closure_web.py` | 闭环端点全集：ack/defer/acted 五动作/dismiss 码/undo/上下文区/事件历史 |
| `tests/web/test_review_web.py` | `/review` 窗口、清单、比率与空状态 |
| `tests/services/test_review_stats.py` | 口径单测：分母 0 → None、旧版行不进分布、undo 后不计闭环、样本不足 |
| `tests/storage/test_alerts_review_queries.py` | `list_window`/`list_overdue`/`count_overdue`/`count_open` 契约 |

**修改**

| 路径 | 改动 |
|---|---|
| `src/sams/web/app.py` | 注册 `annotations`/`review` 两个 router；`WebState` 加两张限速卡；Jinja 全局加 `action_label`/`dismiss_label` |
| `src/sams/web/templates/base.html` | 导航加「复盘」与「告警」未处理数徽标 |
| `src/sams/web/display.py` | `ACTION_LABELS`/`DISMISS_LABELS` 与两个取值函数（与 `ALERT_STATUS_LABELS` 同一命名法） |
| `src/sams/web/pages/alerts.py` | `acted`/`dismiss` 迁到结构化闭环；新增 `ack`/`defer`/`undo`；`_detail_page` 扩上下文区/表单数据；`/api/partials/open-count` |
| `src/sams/web/templates/alert.html` | 动作区重写（五动作 + 忽略码 + 稍后 + 撤销）、上下文区、事件历史、终态展示、批注面板 |
| `src/sams/web/templates/symbol.html` / `thesis.html` / `rule_form.html` | 各嵌一段批注面板容器 |
| `src/sams/web/static/app.css` | 面板与缩略图样式 |
| `src/sams/storage/repos/alerts.py` | Task 5 加四个窗口查询；Task 6 删 `set_status` |
| `src/sams/storage/repos/annotations.py` | `list_visible` 加 `since` |
| `src/sams/services/annotations.py` | `list_recent`（复盘「本周批注」用） |
| `tests/web/test_alerts_web.py` | 旧 `acted`/`dismiss` 用例补上新必填字段 |
| `tests/storage/test_alerts_repo.py`、`tests/cli/test_alert_cli.py` | `set_status` 调用点迁到 `act`/`dismiss`（Task 6） |
| `README.md` | 「备份与附件」一节（Task 6） |

---

## Task 1: 批注面板读取——fragment 端点、模板与三处嵌入

**Files:**
- Create: `src/sams/web/pages/annotations.py`
- Create: `src/sams/web/templates/partials/annotations.html`
- Modify: `src/sams/web/app.py`（import + `include_router`）
- Modify: `src/sams/web/templates/symbol.html`（尾部 `{% endblock %}` 前）、`thesis.html`（同）、`rule_form.html`（同，`{% if rule_id %}` 守卫）
- Modify: `src/sams/web/static/app.css`（追加样式）
- Test: `tests/web/test_annotations_web.py`

- [ ] **Step 1: 写失败的测试**

```python
"""批注面板（读取路）：fragment、可见性、Markdown 安全渲染、目标校验。"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.web.test_annotations_web import *  # noqa  ← 不是真代码，见下方说明；实际文件不要这行
```

上面那行只是提醒：**实际文件从下面开始写**，不要通配导入自己。完整内容：

```python
"""批注面板（读取路）：fragment、可见性、Markdown 安全渲染、目标校验。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.notify import MID
from tests.web.conftest import *  # ← 同样不要真写这行；fixture 靠 conftest 自动注入


def _alice(ctx):
    return ctx.users.get_by_username("alice")


def _seed_annotation(ctx, user, *, code="600519", body="**渠道**库存见底", tags=("逻辑",), **kw):
    return ctx.annotations.create(
        user.id, target_type="symbol", target_ref=f"A:{code}", body_md=body, tags=list(tags), **kw
    )
```

（注意：测试文件里**不 import conftest**——fixture 由 pytest 自动注入；上面两段夹注是写给执行者的路标，成品文件里不许出现 `import *`。成品如下。）

```python
"""批注面板（读取路）：fragment、可见性、Markdown 安全渲染、目标校验。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.notify import MID


def _alice(ctx):
    return ctx.users.get_by_username("alice")


def _seed_annotation(ctx, user, *, body="**渠道**库存见底", tags=("逻辑",)):
    return ctx.annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md=body, tags=list(tags)
    )


def test_panel_renders_visible_annotations_as_fragment(logged):
    ctx = logged.context
    _seed_annotation(ctx, _alice(ctx))
    resp = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    assert resp.status_code == 200
    # Markdown 源码渲染成 HTML：粗体活着，星号不该原样出现在正文里
    assert "<strong>渠道</strong>" in resp.text
    assert "逻辑" in resp.text  # 标签
    assert "我" in resp.text  # 自己的批注作者位显示「我」，与推送同一口径


def test_panel_requires_a_known_target(logged):
    resp = logged.client.get("/api/partials/annotations", params={"target_type": "symbol"})
    assert resp.status_code == 400
    resp = logged.client.get(
        "/api/partials/annotations", params={"target_type": "planet", "target_ref": "x"}
    )
    assert resp.status_code == 400


def test_panel_hides_raw_html_and_images(logged):
    """D122 的 web 侧证：库里存的源码在 fragment 里也只能长成转义后的样子。"""
    ctx = logged.context
    user = _alice(ctx)
    ctx.annotations.create(
        user.id,
        target_type="symbol",
        target_ref="A:600519",
        body_md='<img src=x onerror=alert(1)> <script>bad()</script> ![pic](http://x/y.png)',
    )
    resp = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    assert "<script>bad()</script>" not in resp.text
    assert "onerror=alert(1)" in resp.text  # 转义后作为文本还在——源码不丢
    assert "onerror=\"alert" not in resp.text


def test_panel_hides_other_users_private_notes(logged):
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    ctx.annotations.create(
        bob.id, target_type="symbol", target_ref="A:600519", body_md="bob 的私有观点"
    )
    resp = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    assert "bob 的私有观点" not in resp.text


def test_symbol_page_embeds_the_panel_container(logged):
    """嵌入是一行 htmx 容器：fragment 没加载也不白屏。"""
    ctx = logged.context
    ctx.symbols.upsert("A", "600519", name="贵州茅台")
    page = logged.client.get("/symbol/A/600519")
    assert page.status_code == 200
    assert "target_type=symbol" in page.text and "A%3A600519" in page.text.replace("&colon;", "")
```

最后那条断言里 `A:600519` 是否被 urlencode 取决于模板写法——**模板里直接写 `target_ref={{ market }}:{{ code }}` 不转义**（market/code 都是 canonical 形状，不含需转义字符），所以断言改成 `"target_ref=A:600519" in page.text`。执行者按此收敛，删掉夹注段。`ctx.symbols.upsert` 的真实签名以 `storage/repos/symbols.py` 为准（看板测试里有用例可抄）；若名字不同（如 `put`/`save`），用测试里既有的写法。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/web/test_annotations_web.py -q`
Expected: FAIL（404/路由不存在）

- [ ] **Step 3: 写 `src/sams/web/pages/annotations.py`（本 Task 只到读取路）**

```python
"""批注面板与批注写入（M4-B §6.1/§6.2）。

面板是一枚 htmx fragment：页面只嵌一行容器，读与写都回到 `_render_panel` 这一个出口——
GET 与被拒后的重渲染共用一套行结构，分两份就会分叉（措辞、可见性、时区折法各漂一处）。

闸不在这一层：可见性在 `AnnotationRepo.list_visible`（SQL 里挡），归属在
`AnnotationService`，正文安全在 `render_markdown`（D122 的配置：禁 HTML/图片、链接白名单）。
模板里那个 `| safe` 因此是安全的——那一串 HTML 只可能是 render_markdown 自己产的。

作者标签与推送上下文同一口径：本人「我」，他人 display_name 回退用户名（§7.1）。
时刻全部在 Python 里折到 display_tz 才上屏（`moment_label`），模板不碰 datetime。

限速是进程内的两张窗卡（`WebState.annotation_writes`，§10「30 条/分钟」）：挡的是
连点的脚本，重启即重置——与 test_alerts 那三张卡同一个取舍。

仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from sams.services.markdown import render_markdown
from sams.storage.repos.annotations import TARGET_TYPES
from sams.web.auth import require_user
from sams.web.display import moment_label

router = APIRouter()

#: 面板一屏 20 条：它是「看几条 + 写一条」的地方，不是归档柜；更多去 /annotations（计划 5）
PANEL_LIMIT = 20


def _panel_id(target_type: str, target_ref: str) -> str:
    """把目标键折成一个 DOM id：`A:600519` 里的冒号进不了 `#选择器`，htmx 的 swap 会抓空。"""
    return re.sub(r"[^0-9A-Za-z]+", "-", f"annotations-{target_type}-{target_ref}").strip("-")


def _author_label(ctx, made, viewer_id: str) -> str:
    if made.user_id == viewer_id:
        return "我"
    author = ctx.users.get(made.user_id)
    if author is None:
        return "未知作者"
    return author.display_name or author.username


def _render_panel(
    request: Request,
    *,
    target_type: str,
    target_ref: str,
    error: str = "",
    draft: dict | None = None,
    status_code: int = 200,
):
    """面板的唯一渲染出口。`draft` 是被拒表单的原文回填——交回人刚敲的那份，不是折过的。"""
    web = request.app.state.web
    ctx = web.contexts.current()
    user = request.state.sams_user
    sharing = ctx.app_settings.team_sharing()
    tz = ctx.settings.server.display_tz
    items = ctx.annotations.list_target(
        user.id, target_type, target_ref, team_sharing=sharing, limit=PANEL_LIMIT
    )
    rows = []
    for made in items:
        rows.append(
            {
                "id": made.id,
                "author": _author_label(ctx, made, user.id),
                "at": moment_label(made.created_at, tz),
                "pinned": made.pinned,
                "visibility": "团队" if made.visibility == "team" else "私有",
                "tags": made.tags,
                "body_html": render_markdown(made.body_md),
                "body_md": made.body_md,
                "own": made.user_id == user.id,
                "updated_at": made.updated_at.isoformat(),
            }
        )
    return web.render(
        request,
        "partials/annotations.html",
        status_code=status_code,
        target_type=target_type,
        target_ref=target_ref,
        panel_id=_panel_id(target_type, target_ref),
        rows=rows,
        error=error,
        draft=draft or {},
        empty_note="还没有批注。写下这只票的逻辑、风险或今天的异动，告警推送会自动带上最近 3 条。",
    )


@router.get("/api/partials/annotations")
def annotations_panel(request: Request):
    require_user(request)
    query = request.query_params
    target_type = (query.get("target_type") or "").strip()
    target_ref = (query.get("target_ref") or "").strip()
    if target_type not in TARGET_TYPES or not target_ref:
        # 缺目标的面板没有意义：回 400 而不是回一枚空白 fragment，
        # 「加载失败」与「还没有批注」在屏幕上是两句不同的话
        raise HTTPException(
            status_code=400,
            detail="批注面板要知道挂在哪个目标：target_type 与 target_ref 都必填",
        )
    return _render_panel(request, target_type=target_type, target_ref=target_ref)
```

- [ ] **Step 4: 写模板 `src/sams/web/templates/partials/annotations.html`**

```html
{# 批注面板 fragment（§6.2）：不 extends base——htmx 把整块 swap 进容器，套着导航就成了画中画。 #}
<div id="{{ panel_id }}" class="annotations-panel card">
  <h2>批注</h2>
  {% if error %}<p class="flash-error">{{ error }}</p>{% endif %}
  <form class="ann-editor" hx-post="/annotations" hx-encoding="multipart/form-data"
        hx-target="#{{ panel_id }}" hx-swap="outerHTML">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <input type="hidden" name="target_type" value="{{ target_type }}">
    <input type="hidden" name="target_ref" value="{{ target_ref }}">
    <textarea name="body_md" rows="4"
      placeholder="Markdown 源码：**粗体**、列表、表格都支持；原始 HTML 与图片语法会被转义，截图走下面的附件">
      {{- draft.body_md or "" -}}</textarea>
    <div class="ann-editor-row">
      <input name="tags" placeholder="标签，逗号分隔（≤10 个）" value="{{ draft.tags or "" }}">
      <input type="file" name="files" accept="image/png,image/jpeg,image/webp" multiple>
    </div>
    <p class="muted">可见范围：仅自己（团队共享在计划 5 开放）。</p>
    <div class="ann-editor-row">
      <button type="button" hx-post="/annotations/preview" hx-include="closest form"
              hx-target="#{{ panel_id }}-preview" hx-swap="innerHTML">预览</button>
      <button type="submit">发布</button>
    </div>
    <div id="{{ panel_id }}-preview" class="md-preview"></div>
  </form>
  {% for row in rows %}
  <article class="ann-row">
    <header>
      <span class="ann-avatar">{{ row.author[0]|upper }}</span>
      <strong>{{ row.author }}</strong>
      <span class="muted">{{ row.at }}</span>
      {% if row.pinned %}<span class="badge">置顶</span>{% endif %}
      <span class="badge">{{ row.visibility }}</span>
      {% for tag in row.tags %}<span class="tag">{{ tag }}</span>{% endfor %}
    </header>
    {# safe 的依据在 `services/markdown.py`：那份配置没有产出可执行内容的分支（D122） #}
    <div class="md-body">{{ row.body_html | safe }}</div>
    {% if row.own %}
    <footer class="ann-actions">
      <form hx-post="/annotations/{{ row.id }}/pin" hx-target="#{{ panel_id }}" hx-swap="outerHTML">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="pinned" value="{{ '0' if row.pinned else '1' }}">
        <button type="submit">{{ "取消置顶" if row.pinned else "置顶" }}</button>
      </form>
      <form hx-post="/annotations/{{ row.id }}/delete" hx-target="#{{ panel_id }}" hx-swap="outerHTML"
            hx-confirm="软删除这条批注？正文保留 30 天后由 prune 回收。">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">删除</button>
      </form>
      <details>
        <summary>编辑</summary>
        <form hx-post="/annotations/{{ row.id }}/edit" hx-target="#{{ panel_id }}" hx-swap="outerHTML">
          <input type="hidden" name="csrf" value="{{ csrf }}">
          <input type="hidden" name="updated_at" value="{{ row.updated_at }}">
          <textarea name="body_md" rows="4">{{ row.body_md }}</textarea>
          <input name="tags" value="{{ row.tags|join(', ') }}">
          <button type="submit">保存</button>
        </form>
      </details>
    </footer>
    {% endif %}
  </article>
  {% else %}
  <p class="muted">{{ empty_note }}</p>
  {% endfor %}
</div>
```

- [ ] **Step 5: 注册 router 与三处嵌入**

`src/sams/web/app.py`：`from sams.web.pages import admin, alerts, annotations, dashboard, ...`（按字母序，ruff 会管），`include_router` 加一行：

```python
    app.include_router(annotations.router)
```

（排在 `auth_router` 之后、`admin.router` 之前，与其余 import 顺序一致。）

三处模板在最后一个 `{% endblock %}` **之前**追加同款容器（变量名以各模板上下文为准：`symbol.html` 有 `market`/`code`，`thesis.html` 有 `thesis`，`rule_form.html` 有 `rule_id`——新建规则那一屏 `rule_id` 是空串，用守卫跳过）：

```html
{# 批注面板：fragment 单独拉取，页面主体不等批注查询（与 quotes 部分同一取舍） #}
<section class="card">
  <div hx-get="/api/partials/annotations?target_type=symbol&target_ref={{ market }}:{{ code }}"
       hx-trigger="load" hx-swap="outerHTML">
    <p class="muted">批注加载中…</p>
  </div>
</section>
```

`thesis.html` 用 `target_type=thesis&target_ref={{ thesis.id }}`；`rule_form.html` 整段包 `{% if rule_id %}…{% endif %}`，用 `target_type=rule&target_ref={{ rule_id }}`。

- [ ] **Step 6: 追加 CSS（`src/sams/web/static/app.css` 文件尾）**

```css
/* 批注面板（M4-B §6.2） */
.annotations-panel .ann-editor textarea { width: 100%; font-family: inherit; }
.ann-editor-row { display: flex; gap: 8px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
.ann-row { border-top: 1px solid var(--line, #ddd); padding: 10px 0; }
.ann-row header { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.ann-avatar { display: inline-block; width: 24px; height: 24px; border-radius: 50%;
  background: #666; color: #fff; text-align: center; line-height: 24px; font-size: 13px; }
.tag { background: rgba(128,128,128,.15); border-radius: 6px; padding: 0 6px; font-size: 12px; }
.md-body p:first-child { margin-top: 0; }
.md-body img { max-width: 100%; }
.ann-actions { display: flex; gap: 8px; align-items: center; margin-top: 6px; }
.md-preview { border: 1px dashed var(--line, #bbb); padding: 8px; margin-top: 8px; }
```

- [ ] **Step 7: 跑测试确认通过**

Run: `uv run pytest tests/web/test_annotations_web.py -q`
Expected: PASS（5 条）。顺带跑嵌入页的既有测试确认没破：`uv run pytest tests/web -q`。

- [ ] **Step 8: 提交**

```bash
git add src/sams/web/pages/annotations.py src/sams/web/templates/partials/annotations.html \
  src/sams/web/app.py src/sams/web/templates/symbol.html src/sams/web/templates/thesis.html \
  src/sams/web/templates/rule_form.html src/sams/web/static/app.css tests/web/test_annotations_web.py
git commit -m "feat(web): 批注面板 fragment——标的/观点/规则页各嵌一行容器（§6.2）"
```

---

## Task 2: 批注写入——POST /annotations、预览、编辑/置顶/删除与两张限速卡

**Files:**
- Modify: `src/sams/web/pages/annotations.py`（追加写路端点与 import）
- Modify: `src/sams/web/app.py`（`WebState` 加两张 `LoginLimiter`，`create_app` 里装）
- Test: `tests/web/test_annotations_web.py`（追加）

闸全部在 `AnnotationService` / `AttachmentService` / `AnnotationRepo`：正文非空、标签 ≤10、
乐观并发 409、配额与重编码。这一层只做三件仓库看不见的事：**拒附件前先不建批注**
（半份草稿丢失比多一次往返贵得多）、**部分失败要说清哪几张被为什么拒**、
**429/409/400 都回整块面板**而不是 error.html（htmx swap 的 target 是面板，回整页就成了画中画）。

限速用 `LoginLimiter`（`sams.web.auth`，窗口计数）而不是 `RateCard`（最小间隔）：
§10 说的是「30 条/分钟」，RateCard(2) 会把它变成「连点 30 次全被按在 2 秒间隔上」——
不是同一句话。语义：`blocked_for(key)==0` 就放行并 `record_failure(key)` 记一次**尝试**
（方法名叫 failure 是登录留下的，这里当窗口计数器复用，注释里写明）。
键：`(user.id, "annotation")` 管 create/edit/pin/delete，`(user.id, "attachment")` 管带文件的 create。

- [ ] **Step 1: 写失败的测试（追加到 `tests/web/test_annotations_web.py`）**

```python
import io

from PIL import Image


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    return buf.getvalue()


def _post_annotation(logged, *, body="今天放量，逻辑没变", files=None):
    """multipart 建批注的公共入口：`files=None` 也发一枚空文件名分片来强制 multipart，
    端点按「文件名为空」丢掉它——与浏览器不选文件直接提交的形状一致。"""
    data = {
        "csrf": logged.csrf,
        "target_type": "symbol",
        "target_ref": "A:600519",
        "body_md": body,
        "tags": "逻辑, 放量",
    }
    if files is None:
        files = [("files", ("", b""))]
    return logged.client.post("/annotations", data=data, files=files)


def test_create_writes_annotation_and_rerenders_panel(logged):
    resp = _post_annotation(logged, body="渠道**库存见底**")
    assert resp.status_code == 200
    assert "<strong>库存见底</strong>" in resp.text
    ctx = logged.context
    alice = _alice(ctx)
    rows = ctx.annotations.list_target(alice.id, "symbol", "A:600519")
    assert len(rows) == 1
    assert rows[0].tags == ["逻辑", "放量"]


def test_create_rejects_oversized_file_before_writing_the_annotation(logged):
    """§10 的次序闸：尺寸在**建批注之前**判——人都传完才说「批注存了、图没了」
    等于让草稿断在一半。"""
    big = b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024 + 1)
    resp = _post_annotation(logged, files=[("files", ("big.png", big, "image/png"))])
    assert resp.status_code == 400
    assert "5 MB" in resp.text
    ctx = logged.context
    rows = ctx.annotations.list_target(_alice(ctx).id, "symbol", "A:600519")
    assert rows == []  # 一条没写：被拒的那次不留半份草稿
    # 原文回填：刚敲的正文还在 textarea 里
    assert "今天放量" in resp.text


def test_create_keeps_annotation_but_names_each_rejected_file(logged):
    """批注成立之后才拦下的附件（内容不是图）不回滚闭环：说清「哪张、为什么」。"""
    resp = _post_annotation(
        logged, files=[("files", ("note.gif", b"GIF89a-fake", "image/gif"))]
    )
    assert resp.status_code == 200
    assert "批注已保存，但这些附件被拒" in resp.text
    assert "note.gif" in resp.text
    ctx = logged.context
    rows = ctx.annotations.list_target(_alice(ctx).id, "symbol", "A:600519")
    assert len(rows) == 1


def test_create_stores_a_real_png_attachment(logged):
    resp = _post_annotation(logged, files=[("files", ("shot.png", _png_bytes(), "image/png"))])
    assert resp.status_code == 200
    assert "被拒" not in resp.text
    ctx = logged.context
    made = ctx.annotations.list_target(_alice(ctx).id, "symbol", "A:600519")[0]
    atts = ctx.attachments.list_for(_alice(ctx).id, made.id)
    assert len(atts) == 1
    assert atts[0].filename == "shot.png"


def test_edit_with_fresh_stamp_saves(logged):
    ctx = logged.context
    alice = _alice(ctx)
    made = _seed_annotation(ctx, alice, body="旧正文")
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={
            "csrf": logged.csrf,
            "body_md": "新正文**带强调**",
            "tags": "更正",
            "updated_at": made.updated_at.isoformat(),
        },
    )
    assert resp.status_code == 200
    assert "新正文" in resp.text and "<strong>带强调</strong>" in resp.text


def test_edit_with_stale_stamp_rerenders_panel_with_409(logged):
    ctx = logged.context
    alice = _alice(ctx)
    made = _seed_annotation(ctx, alice, body="v1")
    stale = made.updated_at.isoformat()
    ctx.annotations.edit(alice.id, made.id, body_md="v2 由另一台设备改过")
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={"csrf": logged.csrf, "body_md": "v3", "tags": "", "updated_at": stale},
    )
    assert resp.status_code == 409
    assert "在你编辑期间被改过" in resp.text
    assert "v2 由另一台设备改过" in resp.text  # 面板重渲染：屏幕上看到的是最新的


def test_pin_and_delete_roundtrip(logged):
    ctx = logged.context
    alice = _alice(ctx)
    made = _seed_annotation(ctx, alice)
    resp = logged.client.post(f"/annotations/{made.id}/pin", data={"csrf": logged.csrf, "pinned": "1"})
    assert resp.status_code == 200 and "置顶" in resp.text
    resp = logged.client.post(f"/annotations/{made.id}/delete", data={"csrf": logged.csrf})
    assert resp.status_code == 200
    assert "还没有批注" in resp.text  # empty_note（§6.2：删除是软删，面板立刻隐身）
    assert ctx.annotations.list_target(alice.id, "symbol", "A:600519") == []


def test_edit_foreign_annotation_is_404(logged):
    """别人的批注没有第二种回答：404，与「不存在」同一句（服务层的折法搬上来）。"""
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    made = ctx.annotations.create(
        bob.id, target_type="symbol", target_ref="A:600519", body_md="bob 的私有"
    )
    resp = logged.client.post(
        f"/annotations/{made.id}/edit",
        data={"csrf": logged.csrf, "body_md": "改", "tags": "", "updated_at": made.updated_at.isoformat()},
    )
    assert resp.status_code == 404


def test_preview_returns_rendered_html(logged):
    resp = logged.client.post(
        "/annotations/preview", data={"csrf": logged.csrf, "body_md": "**粗** 与 <script>bad()</script>"}
    )
    assert resp.status_code == 200
    assert "<strong>粗</strong>" in resp.text
    assert "<script>bad()</script>" not in resp.text


def test_create_is_limited_to_30_per_minute(logged):
    for i in range(30):
        resp = _post_annotation(logged, body=f"第 {i} 条")
        assert resp.status_code == 200, i
    resp = _post_annotation(logged, body="第 31 条")
    assert resp.status_code == 429
    assert "30 秒" in resp.text or "秒后" in resp.text
    # 429 也回面板本体（htmx swap 的是面板容器），不是 error.html
    assert "annotations-panel" in resp.text
```

（`_alice` 与 `_seed_annotation` 已在 Task 1 的成品文件里；这里直接用。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/web/test_annotations_web.py -q`
Expected: 新增用例 FAIL（404/405——写路端点不存在）

- [ ] **Step 3: `WebState` 加两张卡（`src/sams/web/app.py`）**

`WebState` 在 `admin_source_probes` 之后追加两个字段（dataclass 字段无默认值，
`create_app` 里必须都装上）：

```python
    #: §10 批注写入限速：每分钟 30 条批注、10 次附件上传。用 `LoginLimiter` 而不是
    #: `RateCard`：这张是「窗口内计数」，那张是「最小间隔」——「30 条/分钟」要的是前者，
    #: 用 RateCard(2) 会变成「每 2 秒放行一条」，同一句话的两种限速差着一整个连点场景。
    #: `record_failure` 的名字是登录留下的，这里记的是每一次尝试（计数不分成败）：
    #: 挡连点的脚本不关心它那次有没有写进库
    annotation_writes: LoginLimiter
    attachment_uploads: LoginLimiter
```

`create_app` 里 `WebState(...)` 构造处（`admin_source_probes=RateCard(60)` 之后）：

```python
        annotation_writes=LoginLimiter(max_failures=30, window_s=60),
        attachment_uploads=LoginLimiter(max_failures=10, window_s=60),
```

- [ ] **Step 4: 写路实现（追加到 `src/sams/web/pages/annotations.py`）**

import 区补齐（与 Task 1 的 import 合并、按 isort 序）：

```python
import io
from datetime import datetime

from fastapi import Depends, File, Form, Response, UploadFile

from sams.core.errors import Conflict, DatabaseLocked, NotFound, SamsError
from sams.services.attachments import MB
from sams.web.auth import audit, csrf_protect
```

（`APIRouter, HTTPException, Request` Task 1 已有；`sams.web.auth` 那行是把 Task 1 的
`from sams.web.auth import require_user` 扩成一行三个。）

```python
# --- 写 ----------------------------------------------------------------------------------


def _clean_files(files: list[UploadFile]) -> list[UploadFile]:
    """丢掉没有文件名的分片：不选文件直接提交时浏览器也带一个空分片。"""
    return [item for item in files if (item.filename or "").strip()]


def _part_size(item: UploadFile) -> int:
    # 用 seek/tell 而不是 read()：先量个头再决定要不要把它整个读进内存——
    # 5 MB 的闸如果靠「读出来再数」来执行，那闸本身就是内存放大器
    item.file.seek(0, io.SEEK_END)
    size = item.file.tell()
    item.file.seek(0)
    return size


def _limit_hit(web, user_id: str, *, uploads: bool) -> int:
    """返回要等的秒数；0 表示放行，放行即记一次尝试。两张卡一起判：
    带附件的那次要同时过「批注 30/分」与「上传 10/分」。"""
    write_key = (user_id, "annotation")
    upload_key = (user_id, "attachment")
    wait = web.annotation_writes.blocked_for(write_key)
    if not wait and uploads:
        wait = web.attachment_uploads.blocked_for(upload_key)
    if wait:
        return wait
    web.annotation_writes.record_failure(write_key)
    if uploads:
        web.attachment_uploads.record_failure(upload_key)
    return 0


@router.post("/annotations")
def create_annotation(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    target_type: str = Form(""),
    target_ref: str = Form(""),
    body_md: str = Form(""),
    tags: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    user = require_user(request)
    if target_type not in TARGET_TYPES or not target_ref:
        raise HTTPException(
            status_code=400,
            detail="批注面板要知道挂在哪个目标：target_type 与 target_ref 都必填",
        )
    web = request.app.state.web
    ctx = web.contexts.current()
    clean = _clean_files(files)
    draft = {"body_md": body_md, "tags": tags}

    def fail(message: str, status_code: int):
        return _render_panel(
            request,
            target_type=target_type,
            target_ref=target_ref,
            error=message,
            draft=draft,
            status_code=status_code,
        )

    wait = _limit_hit(web, user.id, uploads=bool(clean))
    if wait:
        return fail(
            f"写得太快：请 {wait} 秒后再来（每分钟 30 条批注、10 次附件上传）", 429
        )
    limits = ctx.settings.annotations
    if len(clean) > limits.max_attachments:
        return fail(
            f"一条批注最多 {limits.max_attachments} 张附件（收到 {len(clean)} 张）："
            "更多截图请写成第二条批注",
            400,
        )
    oversize = [item.filename for item in clean if _part_size(item) > limits.max_attachment_mb * MB]
    if oversize:
        return fail(
            f"单张附件最多 {limits.max_attachment_mb} MB，"
            f"{'、'.join(oversize)} 超了：这条批注一个字都没写，去一半再传",
            400,
        )
    try:
        made = ctx.annotations.create(
            user.id,
            target_type=target_type,
            target_ref=target_ref,
            body_md=body_md,
            tags=tags,
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        # 正文空、标签超限、目标不存在……都回面板 + 原文回填，不回 error.html
        return fail(str(exc), 400)
    rejected: list[str] = []
    for item in clean:
        try:
            ctx.attachments.add(
                user.id, made.id, filename=item.filename or "", data=item.file.read()
            )
        except DatabaseLocked:
            raise
        except SamsError as exc:
            rejected.append(f"{item.filename}：{exc}")
    audit(
        request,
        action="annotations.create",
        target=made.id,
        ok=True,
        detail=f"附件 {len(clean) - len(rejected)}/{len(clean)} 张",
    )
    # 挂在规范化后的目标上重渲染：`A:600519` 与 `a:600519` 是同一个面板
    error = ("批注已保存，但这些附件被拒：" + "；".join(rejected)) if rejected else ""
    return _render_panel(
        request, target_type=made.target_type, target_ref=made.target_ref, error=error
    )


@router.post("/annotations/preview")
def preview_annotation(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    body_md: str = Form(""),
):
    """预览与上屏同一条渲染管线（`render_markdown`）：两套渲染器迟早分叉，
    「预览里好看」就不是「库里安全」。"""
    require_user(request)
    return Response(render_markdown(body_md), media_type="text/html; charset=utf-8")


def _load_for_write(request: Request, annotation_id: str):
    """编辑/置顶/删除的公共前置：找到批注并带回它挂在哪个目标上。

    「不存在」与「不是你的」折成同一个 404：id 探测不该有第二种回答
    （`ReviewService._owned` 是同一条原则）。失败也要重渲染面板，所以目标必须在这一步取到。
    """
    web = request.app.state.web
    ctx = web.contexts.current()
    user = require_user(request)
    try:
        made = ctx.annotations.get(user.id, annotation_id, team_sharing=True)
    except NotFound:
        raise HTTPException(
            status_code=404, detail="批注不存在或不可见"
        ) from None
    if made.user_id != user.id:
        raise HTTPException(status_code=404, detail="批注不存在或不可见")
    return web, ctx, user, made


@router.post("/annotations/{annotation_id}/edit")
def edit_annotation(
    request: Request,
    annotation_id: str,
    _csrf: None = Depends(csrf_protect),
    body_md: str = Form(""),
    tags: str = Form(""),
    updated_at: str = Form(""),
):
    web, ctx, user, made = _load_for_write(request, annotation_id)
    wait = _limit_hit(web, user.id, uploads=False)
    if wait:
        return _render_panel(
            request,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=f"改得太快：请 {wait} 秒后再来",
            status_code=429,
        )
    stamp: datetime | None = None
    if updated_at:
        try:
            stamp = datetime.fromisoformat(updated_at)
        except ValueError:
            return _render_panel(
                request,
                target_type=made.target_type,
                target_ref=made.target_ref,
                error="编辑基线的时间戳读不出来：关闭编辑器、重新打开再改",
                status_code=400,
            )
    try:
        ctx.annotations.edit(
            user.id, annotation_id, body_md=body_md, tags=tags, expected_updated_at=stamp
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        status = 409 if isinstance(exc, Conflict) else 400
        return _render_panel(
            request,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=str(exc),
            status_code=status,
        )
    audit(request, action="annotations.edit", target=annotation_id, ok=True)
    return _render_panel(
        request, target_type=made.target_type, target_ref=made.target_ref
    )


@router.post("/annotations/{annotation_id}/pin")
def pin_annotation(
    request: Request,
    annotation_id: str,
    _csrf: None = Depends(csrf_protect),
    pinned: str = Form("1"),
):
    web, ctx, user, made = _load_for_write(request, annotation_id)
    wait = _limit_hit(web, user.id, uploads=False)
    if wait:
        return _render_panel(
            request,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=f"改得太快：请 {wait} 秒后再来",
            status_code=429,
        )
    try:
        ctx.annotations.pin(user.id, annotation_id, pinned=pinned == "1")
    except DatabaseLocked:
        raise
    except SamsError as exc:
        # MAX_PINNED=3 的拒绝也是这一句：面板重渲染，错误在 flash 位
        return _render_panel(
            request,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=str(exc),
            status_code=400,
        )
    audit(request, action="annotations.pin", target=annotation_id, ok=True)
    return _render_panel(
        request, target_type=made.target_type, target_ref=made.target_ref
    )


@router.post("/annotations/{annotation_id}/delete")
def delete_annotation(request: Request, annotation_id: str, _csrf: None = Depends(csrf_protect)):
    web, ctx, user, made = _load_for_write(request, annotation_id)
    wait = _limit_hit(web, user.id, uploads=False)
    if wait:
        return _render_panel(
            request,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=f"改得太快：请 {wait} 秒后再来",
            status_code=429,
        )
    try:
        ctx.annotations.delete(user.id, annotation_id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        return _render_panel(
            request,
            target_type=made.target_type,
            target_ref=made.target_ref,
            error=str(exc),
            status_code=400,
        )
    audit(request, action="annotations.delete", target=annotation_id, ok=True)
    return _render_panel(
        request, target_type=made.target_type, target_ref=made.target_ref
    )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/web/test_annotations_web.py -q`
Expected: PASS（Task 1 的 5 条 + 本 Task 的 10 条）
顺带：`uv run pytest tests/web -q`（别的页面上没有 `/annotations` 路由冲突——
`app.py` 的 `include_router` 只加过一次）。

- [ ] **Step 6: 提交**

```bash
git add src/sams/web/pages/annotations.py src/sams/web/app.py tests/web/test_annotations_web.py
git commit -m "feat(web): 批注写入路——附件先验尺寸、部分失败点名、窗口计数限速（§6.2/§10）"
```

---

## Task 3: 附件下载路与面板缩略图

**Files:**
- Modify: `src/sams/web/pages/annotations.py`（GET 下载端点 + `_render_panel` 的行数据加图）
- Modify: `src/sams/web/templates/partials/annotations.html`（正文下加缩略图块）
- Modify: `src/sams/web/static/app.css`（`.ann-imgs`）
- Test: `tests/web/test_annotations_web.py`（追加）

授权只有一个入口：`AttachmentService.resolve`（先证明批注可见、再证明附件行属于那条批注）。
这一层不查归属——查了就是第二份闸，两份闸会分叉。「不存在」「不是你的」「不属于这条批注」
全部折成 404；库里行在、磁盘文件没了也是 404，但话说两句（一句是权限，一句是数据状态）。
`Content-Disposition: inline` + RFC 5987 `filename*=UTF-8''…`：中文文件名要能原样下载，
而 `inline` 让浏览器里点开就是看图，不是先落一份临时文件。

- [ ] **Step 1: 写失败的测试（追加）**

```python
def _make_annotation_with_png(logged, filename="截图-首页.png", count=1):
    """走真实写路建一条带图批注，返回 (Annotation, [Attachment])。

    文件名避开空格与 `>`：断言里要拿它比 percent-encoding，少一个变量少一处错。
    """
    resp = _post_annotation(
        logged,
        files=[("files", (filename, _png_bytes(), "image/png"))] * count,
    )
    assert resp.status_code == 200 and "被拒" not in resp.text
    ctx = logged.context
    alice = _alice(ctx)
    # list_visible 的 ORDER BY created_at DESC：新条目在 [0]
    made = ctx.annotations.list_target(alice.id, "symbol", "A:600519")[0]
    return made, ctx.attachments.list_for(alice.id, made.id)


def test_attachment_download_serves_png_inline_with_rfc5987_name(logged):
    made, atts = _make_annotation_with_png(logged)
    resp = logged.client.get(f"/annotations/{made.id}/attachments/{atts[0].id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    # inline + RFC 5987：中文名原样带出，浏览器里直接看图而不是先落盘
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("inline;")
    assert "filename*=UTF-8''" in disposition
    assert "%E6%88%AA%E5%9B%BE" in disposition  # 「截图」的 percent-encoding
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "private" in resp.headers["cache-control"]


def test_attachment_download_folds_foreign_to_404(logged):
    ctx = logged.context
    bob = ctx.users.create("bob", "correct-horse-battery")
    made = ctx.annotations.create(
        bob.id, target_type="symbol", target_ref="A:600519", body_md="bob 的图"
    )
    att = ctx.attachments_store.create(
        made.id,
        bob.id,
        filename="b.png",
        media_type="image/png",
        size_bytes=1,
        sha256="0" * 64,
        storage_path="bob/x.png",
        position=0,
    )
    resp = logged.client.get(f"/annotations/{made.id}/attachments/{att.id}")
    assert resp.status_code == 404  # 不可见就是不可见：不回答「文件在不在」


def test_download_of_missing_file_says_data_status_not_permission(logged):
    made, atts = _make_annotation_with_png(logged)
    path = logged.context.attachments.path_of(atts[0])
    hidden = path.with_suffix(".hidden")
    path.rename(hidden)
    try:
        resp = logged.client.get(f"/annotations/{made.id}/attachments/{atts[0].id}")
        assert resp.status_code == 404
        assert "缺失" in resp.text
    finally:
        hidden.rename(path)


def test_panel_renders_thumbnails_and_degrades_missing_file(logged):
    made, atts = _make_annotation_with_png(logged)
    url = f"/annotations/{made.id}/attachments/{atts[0].id}"
    panel = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    assert url in panel.text  # 缩略图与下载是同一个 URL：CSP img-src 'self' 放行
    path = logged.context.attachments.path_of(atts[0])
    hidden = path.with_suffix(".hidden2")
    path.rename(hidden)
    try:
        panel = logged.client.get(
            "/api/partials/annotations",
            params={"target_type": "symbol", "target_ref": "A:600519"},
        )
        assert "附件缺失" in panel.text
        assert url not in panel.text  # 坏图不留 <img>：碎图标比那句话更没信息量
    finally:
        hidden.rename(path)


def test_panel_caps_thumbnails_at_three_with_overflow_note(logged):
    made, atts = _make_annotation_with_png(logged, count=4)
    panel = logged.client.get(
        "/api/partials/annotations", params={"target_type": "symbol", "target_ref": "A:600519"}
    )
    # 每张缩略图的 URL 出现两次（<a href> 与 <img src>），第四张完全不出现
    for att in atts[:3]:
        assert panel.text.count(att.id) == 2
    assert atts[3].id not in panel.text
    assert "还有 1 张" in panel.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/web/test_annotations_web.py -q`
Expected: 新增 5 条 FAIL（下载路由不存在；`test_attachment_download_folds_foreign_to_404`
会「碰巧」经 app.py 的 catch-all 也回 404？不会——Starlette 对未注册路径给 404 走
`StarletteHTTPException` 分支渲染 error.html，正文没有「附件不存在或不可见」，
但断言只看 status_code——真闸由 200 那条用例兜住，执行者不必为此改测试）

- [ ] **Step 3: 下载端点（追加到 `src/sams/web/pages/annotations.py`）**

import 补 `from urllib.parse import quote`（`sams.core.errors` 那行 Task 2 已含全部所需）。

```python
# --- 附件 --------------------------------------------------------------------------------


@router.get("/annotations/{annotation_id}/attachments/{attachment_id}")
def download_attachment(request: Request, annotation_id: str, attachment_id: str):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    try:
        att = ctx.attachments.resolve(
            user.id,
            annotation_id,
            attachment_id,
            team_sharing=ctx.app_settings.team_sharing(),
        )
    except DatabaseLocked:
        raise
    except SamsError:
        # 「不存在」「不是你的」「不属于这条批注」全是 404：id 探测没有第二种回答
        raise HTTPException(status_code=404, detail="附件不存在或不可见") from None
    data = ctx.attachments.load(att)
    if data is None:
        # 行在、文件没了：这是数据状态，不是权限，话说两句
        raise HTTPException(
            status_code=404, detail="附件文件缺失：库里还有这一行，磁盘上的图已被清理"
        )
    return Response(
        content=data,
        media_type=att.media_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(att.filename, safe='')}",
            "Cache-Control": "private, max-age=300",
        },
    )
```

- [ ] **Step 4: `_render_panel` 的行数据加缩略图**

文件常量区加：

```python
#: 面板一屏最多给 3 张缩略图：它是「看几条 + 写一条」的地方，不是图片墙；
#: 更多走下载链接或计划 5 的详情页
THUMB_LIMIT = 3
```

`rows = []` 到 `rows.append({...})` 结束的整段替换为：

```python
    rows = []
    for made in items:
        # N+1 查询是有意的：面板 20 条上限、本地 DuckDB、本地磁盘，一页一屏——
        # 攒 JOIN 换来的复杂度买不回可感知的延迟
        atts = ctx.attachments.list_for(user.id, made.id, team_sharing=sharing)
        thumbs = []
        for att in atts[:THUMB_LIMIT]:
            if ctx.attachments.path_of(att).exists():
                # 缺文件不进 thumbs：碎图标不说话，「附件缺失」那句话才说
                thumbs.append(
                    {
                        "url": f"/annotations/{made.id}/attachments/{att.id}",
                        "filename": att.filename,
                    }
                )
        rows.append(
            {
                "id": made.id,
                "author": _author_label(ctx, made, user.id),
                "at": moment_label(made.created_at, tz),
                "pinned": made.pinned,
                "visibility": "团队" if made.visibility == "team" else "私有",
                "tags": made.tags,
                "images": thumbs,
                "missing": max(0, len(atts) - len(thumbs)),
                "extra": max(0, len(atts) - THUMB_LIMIT),
                "body_html": render_markdown(made.body_md),
                "body_md": made.body_md,
                "own": made.user_id == user.id,
                "updated_at": made.updated_at.isoformat(),
            }
        )
```

（`missing` 与 `extra` 是两句话：「有 M 张文件已经不在了」与「还有 N 张没地方放」。
注意 `extra` 按 `THUMB_LIMIT` 裁而不按 thumbs 的实际条数——第 4 张缺文件时它既算
missing 也不该再报 extra；用例「藏起第一张」只有 1 张附件，`extra=0`、`missing=1`，
断言的是「附件缺失」四字，不与此冲突。）

- [ ] **Step 5: 模板与样式**

`partials/annotations.html`，`<div class="md-body">{{ row.body_html | safe }}</div>` 之后插入：

```html
    {% if row.images or row.missing %}
    <div class="ann-imgs">
      {% for img in row.images %}
      <a href="{{ img.url }}"><img src="{{ img.url }}" alt="{{ img.filename }}" loading="lazy"></a>
      {% endfor %}
      {% if row.missing %}<span class="muted">附件缺失 {{ row.missing }} 张</span>{% endif %}
      {% if row.extra %}<span class="muted">还有 {{ row.extra }} 张</span>{% endif %}
    </div>
    {% endif %}
```

`app.css` 追加：

```css
.ann-imgs { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; align-items: center; }
.ann-imgs img { max-height: 120px; max-width: 160px; border-radius: 6px; }
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/web/test_annotations_web.py -q`
Expected: PASS（累计 20 条）

- [ ] **Step 7: 提交**

```bash
git add src/sams/web/pages/annotations.py src/sams/web/templates/partials/annotations.html \
  src/sams/web/static/app.css tests/web/test_annotations_web.py
git commit -m "feat(web): 附件下载走 resolve 授权 + 面板缩略图（3 张封顶、缺文件降级成一句话）"
```

---

## Task 4: 告警闭环全套——ack/defer/undo/五动作/dismiss 码、详情重写、同步批注

**Files:**
- Modify: `src/sams/web/display.py`（`ACTION_LABELS`/`DISMISS_LABELS`/`EVENT_ACTION_LABELS` + 两个取值函数）
- Modify: `src/sams/web/app.py`（Jinja 全局加 `action_label`/`dismiss_label`）
- Modify: `src/sams/web/pages/alerts.py`（`acted`/`dismiss` 迁到结构化闭环；新增 `ack`/`defer`/`undo`；`_detail_page` 扩上下文；`_row` 加 `deferred`）
- Modify: `src/sams/web/templates/alert.html`（整页重写）
- Modify: `src/sams/web/templates/alerts.html`（行徽标一行）
- Modify: `tests/web/test_alerts_web.py`（旧三条用例补新必填字段）
- Test: `tests/web/test_alerts_closure_web.py`（新建）

状态机的闸全在 `AlertRepo`（ack 幂等/终态不可回退/引用必填/原因码/undo 窗口），
「引用存在且相符」的闸在 `ReviewService`——这一层只补仓库看不见的那几样：
表单形状（thesis/rule 用**分开命名的引用选择器**而不是共用一个 `action_ref` 输入框：
没有 JS 的浏览器也要能把三种引用的来源各交各的，而 `trade` 的引用是新建的成交 id，压根不该从表单来）、
「同时写一条标的批注」的**预填正文**在这里组装（服务层只收成品）、
以及被拒重渲染时交回人刚敲的那份。

`/acted` 从此只走 `ReviewService`（`act`/`act_with_fill`），不再碰 `AlertRepo.set_status`
——那正是 Task 6 能拔除旧路的前提。终态后的撤销按钮按 `can_undo`（仓库的
`UNDO_WINDOW` 与 `ctx.clock`）显隐：窗口外按钮消失，而不是按下去吃一句 409。

- [ ] **Step 1: 写失败的测试（新建 `tests/web/test_alerts_closure_web.py`）**

```python
"""闭环命令与详情（M4-B §6.3/§8.1）：ack/defer/undo、五动作、忽略码、事件历史、同步批注。

seed 走仓库不走页面：这一批测的是闭环那一屏怎么按，不是告警怎么发出来。
`_seed` 与 `test_alerts_web.py` 同款——不建 tests/web/support.py（那会遮住根目录的
`support`，见该文件模块注释），重复的是二十行装配，不是逻辑。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sams.core.notify import MID
from sams.storage.repos.alerts import ACKNOWLEDGED, DISMISSED, HANDLED, NEW


def _seed(ctx, *, user_id, market="A", code="600519", **overrides):
    kwargs = dict(
        rule_id=None,
        rule_name="止损线",
        account_id=None,
        account_name="",
        market=market,
        code=code,
        symbol_name="贵州茅台",
        severity=MID,
        stage="close",
        value_text="1500.00",
        threshold_text="1550.00",
        action_note="考虑减仓",
        triggered_at=datetime.now(UTC),
    )
    kwargs.update(overrides)
    return ctx.alerts_store.create(user_id, **kwargs)


def _alice(ctx):
    return ctx.users.get_by_username("alice")


def _acted(logged, alert_id, **overrides):
    data = {"csrf": logged.csrf, "action_kind": "other", "note": "按提示减半仓"}
    data.update(overrides)
    return logged.client.post(f"/alerts/{alert_id}/acted", data=data, follow_redirects=False)


def test_ack_marks_read_idempotent_and_lands_in_events(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    for _ in range(2):  # 手机连点两下不该多出一条历史
        resp = logged.client.post(
            f"/alerts/{alert.id}/ack", data={"csrf": logged.csrf}, follow_redirects=False
        )
        assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == ACKNOWLEDGED and row.acknowledged_at is not None
    events = ctx.alerts_store.events.list_for(alert.id)
    assert [e.action for e in events] == ["ack"]
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert "动作历史" in detail.text and "已读" in detail.text
    # 「已读」按钮只在 new 上出现；处置/忽略在 acknowledged 上仍是按钮
    assert f"/alerts/{alert.id}/ack" not in detail.text
    assert "处置" in detail.text


def test_ack_after_terminal_is_409_and_says_which_door(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    ctx.alerts_store.act(alert.id, user_id=_alice(ctx).id, action_kind="other", note="先关上")
    resp = logged.client.post(f"/alerts/{alert.id}/ack", data={"csrf": logged.csrf})
    assert resp.status_code == 409
    assert "不能再标已读" in resp.text  # 仓库那句话原样上屏：哪扇门走不通说得清


def test_defer_choices_write_future_moments_without_touching_status(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)

    resp = logged.client.post(
        f"/alerts/{alert.id}/defer", data={"csrf": logged.csrf, "until": "1h"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == NEW  # 稍后不是终态（D138）
    now = datetime.now(UTC)
    assert now + timedelta(minutes=55) <= row.deferred_until <= now + timedelta(minutes=65)

    resp = logged.client.post(
        f"/alerts/{alert.id}/defer", data={"csrf": logged.csrf, "until": "open"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    sh = row.deferred_until.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (sh.hour, sh.minute) == (9, 30)  # StubCalendar：工作日 09:30 开盘
    assert sh.weekday() < 5
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert "稍后至" in detail.text  # 「你让它稍后提醒」那一格

    bad = logged.client.post(
        f"/alerts/{alert.id}/defer", data={"csrf": logged.csrf, "until": "5m"}
    )
    assert bad.status_code == 400
    assert "明天开盘" in bad.text  # 拒绝的话里带着那三个合法档


def test_acted_requires_a_known_action_kind(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    blank = logged.client.post(f"/alerts/{alert.id}/acted", data={"csrf": logged.csrf})
    assert blank.status_code == 400
    assert "未知动作类型" in blank.text
    assert ctx.alerts_store.get(alert.id).status == NEW
    zz = _acted(logged, alert.id, action_kind="zzz")
    assert zz.status_code == 400
    # 名单上屏时说的是人话不是键名（`ACTION_LABELS` 与 `ACTION_KINDS` 一一对应）
    assert "成交" in zz.text and "加入自选" in zz.text


def test_acted_other_requires_note(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    resp = _acted(logged, alert.id, note="   ")
    assert resp.status_code == 400
    assert "other 必须写一句说明" in resp.text
    assert ctx.alerts_store.get(alert.id).status == NEW


def test_acted_watch_adds_to_watchlist_atomically(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    resp = _acted(logged, alert.id, action_kind="watch", note="")
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == HANDLED and row.action_kind == "watch"
    assert ctx.watchlists.contains(alice.id, "A", "600519")


def test_acted_thesis_must_be_yours_and_about_the_same_symbol(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    missing = _acted(logged, alert.id, action_kind="thesis", thesis_ref="no-such")
    assert missing.status_code == 400 and "不存在" in missing.text

    other = ctx.theses_store.create(
        user_id=alice.id, market="A", code="000001", style="value",
        actor_id=alice.id, now=datetime.now(UTC),
    )
    crossed = _acted(logged, alert.id, action_kind="thesis", thesis_ref=other.id)
    assert crossed.status_code == 400 and "不符" in crossed.text

    mine = ctx.theses_store.create(
        user_id=alice.id, market="A", code="600519", style="value",
        actor_id=alice.id, now=datetime.now(UTC),
    )
    ok = _acted(logged, alert.id, action_kind="thesis", thesis_ref=mine.id)
    assert ok.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.action_kind == "thesis" and row.action_ref == mine.id
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert f"/theses/{mine.id}" in detail.text  # 关联对象可点（§6.3 终态区）


def test_acted_rule_links_its_edit_page(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    rule = ctx.rules_store.create(
        alice.id, kind="stop.fixed", name="茅台止损",
        target_type="symbol", target_ref="A/600519", params={"price": 1400.0},
    )
    resp = _acted(logged, alert.id, action_kind="rule", rule_ref=rule.id)
    assert resp.status_code == 303
    assert ctx.alerts_store.get(alert.id).action_ref == rule.id
    assert f"/rules/{rule.id}/edit" in logged.client.get(f"/alerts/{alert.id}").text


def test_acted_trade_fills_and_closes_in_one_go(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    account = ctx.ledger.open_account("alice", "测试户")
    ctx.ledger.deposit(
        account.id, "CNY", 100_000.0, username="alice", reason="初始", actor_id=alice.id
    )
    resp = _acted(
        logged, alert.id,
        action_kind="trade", account_id=account.id, side="buy",
        qty="100", price="1500", fee="5", note="",
    )
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == HANDLED and row.action_kind == "trade"
    assert ctx.db.scalar(
        "SELECT count(*) FROM sim_orders WHERE account_id = ? AND code = ?",
        [account.id, "600519"],
    ) == 1
    # 成交单号在终态区可读（没有 order 详情页，留明文，见计划头「明确不做」）
    assert row.action_ref in logged.client.get(f"/alerts/{alert.id}").text


def test_acted_trade_bad_number_writes_neither_order_nor_closure(logged):
    """一半成一半败不允许存在：数量读不出数字时，闭环与成交都不该落。"""
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    account = ctx.ledger.open_account("alice", "测试户")
    resp = _acted(
        logged, alert.id,
        action_kind="trade", account_id=account.id, side="buy",
        qty="abc", price="1500", note="",
    )
    assert resp.status_code == 400
    assert "数量 要是数字" in resp.text
    assert ctx.alerts_store.get(alert.id).status == NEW
    assert ctx.db.scalar(
        "SELECT count(*) FROM sim_orders WHERE account_id = ?", [account.id]
    ) == 0


def test_sync_annotation_checkbox_writes_a_prefilled_symbol_note(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    resp = _acted(logged, alert.id, sync_annotation="on")
    assert resp.status_code == 303
    made = ctx.annotations.list_target(alice.id, "symbol", "A:600519")[0]
    assert "闭环" in made.body_md
    assert "按提示减半仓" in made.body_md  # 处置结论进了正文（D137）
    assert alert.id in made.body_md  # 带回链


def test_dismiss_requires_code_then_records_it_with_words(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)

    blank = logged.client.post(f"/alerts/{alert.id}/dismiss", data={"csrf": logged.csrf})
    assert blank.status_code == 400
    assert "忽略必须先选原因码" in blank.text
    assert "误报" in blank.text  # 名单上屏用人话

    thin = logged.client.post(
        f"/alerts/{alert.id}/dismiss", data={"csrf": logged.csrf, "code": "misreport"}
    )
    assert thin.status_code == 400
    assert "misreport 必须写一句说明" in thin.text

    ok = logged.client.post(
        f"/alerts/{alert.id}/dismiss",
        data={"csrf": logged.csrf, "code": "expected", "sync_annotation": "on"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == DISMISSED and row.dismiss_code == "expected"
    # 「符合预期」不强制说明；勾选同步批注照样写
    made = ctx.annotations.list_target(alice.id, "symbol", "A:600519")[0]
    assert "忽略（符合预期）" in made.body_md
    assert "这条原因会进入本周忽略率" in logged.client.get(f"/alerts/{alert.id}").text


def test_undo_restores_previous_state(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    _acted(logged, alert.id)
    resp = logged.client.post(
        f"/alerts/{alert.id}/undo", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == NEW  # 上一态是 new（events.last 的 from_status）
    assert row.handled_at is None and row.action_kind is None
    assert [e.action for e in ctx.alerts_store.events.list_for(alert.id)] == ["act", "undo"]


def test_undo_after_window_hides_the_button(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    _acted(logged, alert.id)
    # 把终态时刻拨回 6 分钟前：按钮随窗口消失，而不是按下去吃 409
    ctx.db.execute(
        "UPDATE alerts SET handled_at = ? WHERE id = ?",
        [datetime.now(UTC) - timedelta(minutes=6), alert.id],
    )
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert f"/alerts/{alert.id}/undo" not in detail.text
    forced = logged.client.post(f"/alerts/{alert.id}/undo", data={"csrf": logged.csrf})
    assert forced.status_code == 409
    assert "撤销窗口已过" in forced.text
    assert ctx.alerts_store.get(alert.id).status == HANDLED


def test_detail_embeds_the_annotation_panel_and_thesis_picker(logged):
    """上下文区与推送同源：详情嵌的就是那枚批注面板 fragment（§6.3）。"""
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    ctx.annotations.create(
        alice.id, target_type="symbol", target_ref="A:600519", body_md="**渠道**库存见底"
    )
    ctx.theses_store.create(
        user_id=alice.id, market="A", code="600519", style="value",
        actor_id=alice.id, now=datetime.now(UTC),
    )
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert "target_type=symbol" in detail.text and "A:600519" in detail.text
    # 引用选择器把本人的同标的观点列进下拉
    assert "关联的观点" in detail.text and "value" in detail.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/web/test_alerts_closure_web.py -q`
Expected: FAIL（405/404 或断言不符——新端点与字段都还不存在）

- [ ] **Step 3: `src/sams/web/display.py` 加三张名单与两个取值函数**

贴在 `ALERT_STATUS_LABELS`/`alert_status` 之后（同一命名法：常量表 + 取值函数）：

```python
#: 处置动作与忽略原因的中文名：与 `repos/alerts.py` 的 `ACTION_KINDS`/`DISMISS_CODES`
#: 一一对应。新增一档忘配表时，屏幕上出现的是原键名而不是空白——漏配看得见
ACTION_LABELS = {
    "trade": "成交",
    "thesis": "关联观点",
    "rule": "调整规则",
    "watch": "加入自选",
    "other": "其他处置",
}
DISMISS_LABELS = {
    "misreport": "误报",
    "expected": "符合预期",
    "data_issue": "数据问题",
    "not_applicable": "不适用",
    "duplicate": "重复提醒",
    "other": "其他",
}
#: `alert_events.action` 那一列的读法（D138：五个动作各写一行）
EVENT_ACTION_LABELS = {
    "ack": "已读",
    "act": "处置",
    "dismiss": "忽略",
    "defer": "稍后",
    "undo": "撤销",
}


def action_label(value: str | None) -> str:
    if not value:
        return "—"
    return ACTION_LABELS.get(value, value)


def dismiss_label(value: str | None) -> str:
    if not value:
        return "—"
    return DISMISS_LABELS.get(value, value)
```

`src/sams/web/app.py` 的 `_templates()`：`from sams.web.display import` 补
`action_label, dismiss_label`，`env.globals.update(...)` 那一行末尾加
`action_label=action_label, dismiss_label=dismiss_label`。
（`EVENT_ACTION_LABELS` **不**注册成全局：事件折成 dict 在端点里做，模板只印成品。）

- [ ] **Step 4: `src/sams/web/pages/alerts.py`——import、常量、`_row`、`_detail_page`**

import 区改动（合并进现有行）：`from datetime import UTC, datetime, timedelta`；
`from sams.core.errors import Conflict, DatabaseLocked, SamsError, ValidationError`；
`from sams.storage.repos.alerts` 那行扩为
`ACTION_KINDS, ACKNOWLEDGED, DISMISS_CODES, DISMISSED, HANDLED, NEW, STATUS, UNDO_WINDOW, Alert`；
`from sams.web.display` 补 `action_label, dismiss_label, EVENT_ACTION_LABELS`；
`from sams.web.forms` 补 `checked`；再加 `import logging` 与
`logger = logging.getLogger("sams.web.alerts")`。

模块 docstring 第一段末尾补一句（旧路已死的声明，Task 6 删方法时靠它对齐口径）：

```
2026-10-25 起本页只走 `ReviewService` 与 `acknowledge/defer/undo`：`set_status` 的
最后两个调用点（旧 acted/dismiss）已在闭环改造里迁走。
```

常量区追加：

```python
#: 稍后的三个快捷档（§6.3）。`open` 不是时长是时刻：走日历算下一段开盘，
#: 且只认「明天开盘」这四个字——「几天后」不在 spec 的名单里，别自创
DEFER_HOURS = {"1h": 1, "3h": 3}
DEFER_OPEN = "open"
DEFER_CHOICES = (("1h", "1 小时"), ("3h", "3 小时"), (DEFER_OPEN, "明天开盘"))
```

`_row` 的 dict 里 `"is_test"` 之后加一键：

```python
        # 「你让它稍后提醒」在列表里也要认得出来：稍后不改状态，这一格是它唯一的痕迹
        "deferred": moment_label(alert.deferred_until, tz) if alert.deferred_until else "",
```

`_detail_page` **整体替换**为（`dispatch` 无注解的理由保留在新 docstring 里）：

```python
def _detail_page(
    request: Request,
    *,
    status_code: int = 200,
    error: str = "",
    dispatch=None,
    reason: str = "",
    draft: dict | None = None,
):
    """详情这一屏。`dispatch` 不带注解：它的类（`DispatchResult`）住在 `sams.monitor`，
    而 web 不许 import 那一层（`tests/test_layering.py`）——同一折法见 `bootstrap.py`。

    表单要的数据在这里备齐（本人的同标的观点、本人的规则、本人的账户、两张码表），
    模板只管印——模板里循环仓库是第二份取数逻辑，分页/过滤会漂出第一份的视线。
    """
    user = request.state.sams_user
    web = request.app.state.web
    ctx = web.contexts.current()
    alert = ctx.alerts_store.get(request.path_params["alert_id"], user_id=user.id)
    if alert is None:
        raise HTTPException(status_code=404, detail="没有这条告警")
    tz = ctx.settings.server.display_tz
    payload = alert.payload
    extra = payload.get("extra") or {}
    deliveries = [
        {
            # 那一列到秒：同一条告警的三次尝试只差几秒，只到分钟会印成一模一样的一列
            "at": moment_label(row.ts, tz, seconds=True),
            "channel_id": row.channel_id or "",
            "outcome": row.outcome,
            "attempts": row.attempts,
            "error": row.error,
        }
        for row in ctx.alerts_store.deliveries(alert.id)
    ]
    events = [
        {
            "at": moment_label(row.ts, tz, seconds=True),
            "action": EVENT_ACTION_LABELS.get(row.action, row.action),
            "from": alert_status(row.from_status),
            "to": alert_status(row.to_status),
            "note": row.note,
        }
        for row in ctx.alerts_store.events.list_for(alert.id)
    ]
    terminal = alert.status in (HANDLED, DISMISSED)
    can_undo = bool(
        terminal
        and alert.handled_at is not None
        and ctx.clock.now_utc() - alert.handled_at < UNDO_WINDOW
    )
    # 引用跳转：trade 没有 order 详情页（计划头「明确不做」），终态区留明文单号
    ref_url = ""
    if alert.action_kind == "thesis" and alert.action_ref:
        ref_url = f"/theses/{alert.action_ref}"
    elif alert.action_kind == "rule" and alert.action_ref:
        ref_url = f"/rules/{alert.action_ref}/edit"
    related_theses = [
        {"id": t.id, "label": f"{t.style} · {(t.note or t.id)[:24]}"}
        for t in ctx.theses.list(user.username)
        if (t.market, t.code) == (alert.market, alert.code)
    ]
    return web.render(
        request,
        "alert.html",
        status_code=status_code,
        title=f"告警 {alert.market}/{alert.code}",
        alert=alert,
        deliveries=deliveries,
        dispatch=dispatch,
        value_text=str(payload.get("value_text") or ""),
        threshold_text=str(payload.get("threshold_text") or ""),
        action_note=str(payload.get("action_note") or ""),
        body=str(extra.get("body") or ""),
        triggered_at=moment_label(alert.triggered_at, tz, seconds=True),
        handled_at=moment_label(alert.handled_at, tz, seconds=True),
        deferred=moment_label(alert.deferred_until, tz) if alert.deferred_until else "",
        intraday=bool(payload.get("intraday")),
        quote_source=str(payload.get("quote_source") or ""),
        # 闭环动作区：已读只在 new 出现；处置/忽略/稍后在 new 与 acknowledged 都可按（§6.3）
        can_act=alert.status in (NEW, ACKNOWLEDGED),
        can_ack=alert.status == NEW,
        terminal=terminal,
        can_undo=can_undo,
        events=events,
        action_kind_text=action_label(alert.action_kind),
        action_ref_url=ref_url,
        dismiss_code_text=dismiss_label(alert.dismiss_code),
        # 表单的两张码表与三个下拉（全部取自服务层，模板零查询）
        action_kinds=[(k, action_label(k)) for k in ACTION_KINDS],
        dismiss_codes=[(c, dismiss_label(c)) for c in DISMISS_CODES],
        defer_choices=DEFER_CHOICES,
        related_theses=related_theses,
        rules=[
            {"id": r.id, "label": f"{r.name}（{r.kind}）"}
            for r in ctx.rules_store.list_for(user.id)
        ],
        accounts=[
            {"id": a.id, "label": a.name} for a in ctx.sim_accounts.list_for(user.id)
        ],
        error=error,
        # 被拒的重渲染交回人刚敲的那份原文（同 `pages/rules.py` 的 `_values`）
        reason=reason,
        draft=draft or {},
    )
```

- [ ] **Step 5: 闭环命令端点（替换旧 `acted`/`dismiss` 两个函数并追加三个）**

`@router.post("/alerts/{alert_id}/acted")` 与 `@router.post("/alerts/{alert_id}/dismiss")`
两个函数**整体删除**，换成下面这一整块（放在 `resend` 之前）。旧的
「备注可以空」「驳回必须写原因」两段注释随函数一起去掉——它们说的话被
`ACTION_LABELS` 的必填闸取代（other 反而**必须**写两句以上，见仓库）。

```python
def _draft(data: dict[str, list[str]]) -> dict[str, str]:
    """被拒重渲染交回的草稿：只交闭环表单会用到的那几格，模板按名字取。"""
    return {
        key: one(data, key)
        for key in ("action_kind", "note", "qty", "price", "fee", "code")
    }


def _number(raw: str, label: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValidationError(f"{label} 要是数字（收到 {raw!r}）") from exc


def _trade_ts(raw: str, tz_name: str) -> datetime | None:
    """`datetime-local` 控件交来的裸时刻落在 display_tz；空 = 让账本按此刻记。
    补记真实成交的人，留空比替他猜一个时刻诚实。"""
    if not raw:
        return None
    try:
        local = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"成交时间读不出来：{raw!r}") from exc
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(tz_name))
    return local


def _sync_text(ctx, alert: Alert, outcome: str) -> str:
    """「同时写一条标的批注」的预填正文（D137）：告警摘要 + 这次的结论 + 回链。
    成品在这里组装，`ReviewService._sync_annotation` 与下面的 best-effort 只收文本。"""
    base = ctx.settings.server.public_base_url
    link = f"{base}/alerts/{alert.id}" if base else f"/alerts/{alert.id}"
    return "\n".join(
        [
            f"[闭环] {alert.market}:{alert.code} {alert.symbol_name or ''}".strip(),
            f"规则：{alert.rule_name or '—'}；现值 {str(alert.payload.get('value_text') or '—')}"
            f" / 阈值 {str(alert.payload.get('threshold_text') or '—')}",
            outcome,
            f"告警详情：{link}",
        ]
    )


def _act_trade(
    ctx, user, alert: Alert, data: dict[str, list[str]], note: str, sync: str | None
):
    """trade 的引用是这一次**新造的**成交 id，只有 `act_with_fill` 给得出（D125）。"""
    account_id = one(data, "account_id").strip()
    if not account_id:
        raise ValidationError("trade 要选一个账户：成交得落在某个人的持仓上")
    fee_text = one(data, "fee").strip()
    return ctx.review.act_with_fill(
        user.id,
        alert.id,
        account_id=account_id,
        side=one(data, "side").strip(),
        qty=_number(one(data, "qty"), "数量"),
        price=_number(one(data, "price"), "价格"),
        fee=_number(fee_text, "费用") if fee_text else 0.0,
        ts=_trade_ts(one(data, "ts").strip(), ctx.settings.server.display_tz),
        note=note,
        sync_annotation=sync,
    )


@router.post("/alerts/{alert_id}/acted")
def acted(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    """处置：五类动作（D125）。全部走 `ReviewService`——`set_status` 这条路从此无人走。

    引用选择器按动作分开命名（`thesis_ref`/`rule_ref` 而不是共用 `action_ref` 输入框）：
    没有 JS 的浏览器也要能把三种引用的来源各交各的，而 trade 的引用压根不该从表单来。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    kind = one(data, "action_kind").strip()
    note = one(data, "note").strip()
    if kind not in ACTION_KINDS:
        return _detail_page(
            request,
            status_code=400,
            error="未知动作类型：只有 " + "、".join(action_label(k) for k in ACTION_KINDS),
            draft=_draft(data),
        )
    sync = (
        _sync_text(
            ctx, alert, f"处置：{action_label(kind)}" + (f"——{note}" if note else "")
        )
        if checked(data, "sync_annotation")
        else None
    )
    try:
        if kind == "trade":
            moved = _act_trade(ctx, user, alert, data, note, sync)
        else:
            ref = one(data, "thesis_ref" if kind == "thesis" else "rule_ref").strip()
            moved = ctx.review.act(
                user.id,
                alert.id,
                action_kind=kind,
                action_ref=ref,
                note=note,
                sync_annotation=sync,
            )
    except DatabaseLocked:
        raise
    except Conflict as exc:
        # 「态不对」是 409：与批注的乐观并发同一口径
        return _detail_page(request, status_code=409, error=str(exc), draft=_draft(data))
    except SamsError as exc:
        return _detail_page(request, status_code=400, error=str(exc), draft=_draft(data))
    audit(
        request,
        action="alert.acted",
        target=alert.id,
        ok=True,
        detail=f"{kind}:{moved.action_ref or ''}",
    )
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


@router.post("/alerts/{alert_id}/dismiss")
def dismiss(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    """忽略：原因码必选（D124），`misreport`/`other` 的说明由仓库挡（≥2 字）。"""
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    code = one(data, "code").strip()
    typed = one(data, "reason")
    reason = typed.strip()
    if code not in DISMISS_CODES:
        return _detail_page(
            request,
            status_code=400,
            error="忽略必须先选原因码：" + "、".join(dismiss_label(c) for c in DISMISS_CODES),
            reason=typed,
            draft=_draft(data),
        )
    sync = (
        _sync_text(
            ctx,
            alert,
            f"忽略（{dismiss_label(code)}）" + (f"——{reason}" if reason else ""),
        )
        if checked(data, "sync_annotation")
        else None
    )
    try:
        moved = ctx.alerts_store.dismiss(alert.id, user_id=user.id, code=code, reason=reason)
    except DatabaseLocked:
        raise
    except Conflict as exc:
        return _detail_page(request, status_code=409, error=str(exc), reason=typed)
    except SamsError as exc:
        return _detail_page(request, status_code=400, error=str(exc), reason=typed)
    if sync:
        # 与 `ReviewService._sync_annotation` 同方向：批注写崩不把已成立的忽略拖回去（§12.1）
        try:
            ctx.annotations.create(
                user.id,
                target_type="symbol",
                target_ref=f"{alert.market}:{alert.code}",
                body_md=sync,
            )
        except SamsError as exc:
            logger.warning(
                "告警 %s 忽略已成立，但同步批注失败（不影响忽略）：%s", alert.id, exc
            )
    # 驳回是「忽略率」唯一的输入，而那一列库里就有：日志只记「谁按的、按了哪一条」
    audit(request, action="alert.dismiss", target=alert.id, ok=True, detail=code)
    return RedirectResponse(f"/alerts/{moved.id}", status_code=303)


@router.post("/alerts/{alert_id}/ack")
def ack(request: Request, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    try:
        ctx.alerts_store.acknowledge(alert.id, user_id=user.id)
    except DatabaseLocked:
        raise
    except Conflict as exc:
        return _detail_page(request, status_code=409, error=str(exc))
    audit(request, action="alert.ack", target=alert.id, ok=True)
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


def _defer_until(ctx, alert: Alert, choice: str) -> datetime:
    """`open` = 下一个开盘时刻：今天还没开盘就用今天，否则顺着日历往后找（§6.3）。"""
    if choice in DEFER_HOURS:
        return ctx.clock.now_utc() + timedelta(hours=DEFER_HOURS[choice])
    probe = ctx.calendar.day(alert.market, ctx.clock.now_utc().date())
    tz = ZoneInfo(probe.tz)
    local_now = ctx.clock.now_utc().astimezone(tz)
    day = ctx.calendar.day(alert.market, local_now.date())
    window = next((w for w in day.windows if w.name == "day"), None)
    if day.is_trading and window is not None:
        hour, minute = (int(part) for part in window.open.split(":")[:2])
        open_at = datetime(day.day.year, day.day.month, day.day.day, hour, minute, tzinfo=tz)
        if open_at > local_now:
            return open_at.astimezone(UTC)
    nxt = ctx.calendar.next_trading_day(alert.market, local_now.date())
    nday = ctx.calendar.day(alert.market, nxt)
    nwindow = next((w for w in nday.windows if w.name == "day"), None)
    if nwindow is None:
        raise ValidationError("日历里找不到那段交易时段：改用 1 小时或 3 小时")
    hour, minute = (int(part) for part in nwindow.open.split(":")[:2])
    return datetime(
        nxt.year, nxt.month, nxt.day, hour, minute, tzinfo=ZoneInfo(nday.tz)
    ).astimezone(UTC)


@router.post("/alerts/{alert_id}/defer")
def defer(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    choice = one(data, "until").strip()
    if choice not in DEFER_HOURS and choice != DEFER_OPEN:
        return _detail_page(
            request,
            status_code=400,
            error="未知稍后档位：只有 " + "、".join(label for _, label in DEFER_CHOICES),
        )
    try:
        ctx.alerts_store.defer(
            alert.id, user_id=user.id, until=_defer_until(ctx, alert, choice)
        )
    except DatabaseLocked:
        raise
    except SamsError as exc:
        status = 409 if isinstance(exc, Conflict) else 400
        return _detail_page(request, status_code=status, error=str(exc))
    audit(request, action="alert.defer", target=alert.id, ok=True, detail=choice)
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)


@router.post("/alerts/{alert_id}/undo")
def undo(request: Request, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    alert = _require_own(ctx, request.path_params["alert_id"], user.id)
    try:
        ctx.alerts_store.undo(alert.id, user_id=user.id)
    except DatabaseLocked:
        raise
    except Conflict as exc:
        # 窗口过期/最新事件不是关闭动作：都是 409，那句话由仓库说
        return _detail_page(request, status_code=409, error=str(exc))
    audit(request, action="alert.undo", target=alert.id, ok=True)
    return RedirectResponse(f"/alerts/{alert.id}", status_code=303)
```

- [ ] **Step 6: 重写 `src/sams/web/templates/alert.html`（整文件替换）**

投递记录那一节从旧文件**原样保留**（`<section class="card"> <h2>投递记录</h2>` 到
`{% endblock %}` 之前——含两段「重发」说明与注释，那是 M3 挣来的措辞，动它要新的理由）。
其余整页替换为：

```html
{% extends "base.html" %}
{% block title %}{{ title }}{% endblock %}
{% block content %}
<section class="card">
  <h1>{{ alert.market }}/{{ alert.code }}
    {% if alert.symbol_name %}<span class="muted">{{ alert.symbol_name }}</span>{% endif %}
  </h1>
  {% if error %}<p class="flash-error">{{ error }}</p>{% endif %}
  <p>
    <span class="badge">{{ alert.severity }}</span>
    {{ alert_status(alert.status) }} ·
    {# 时刻在 Python 里折到 display_tz 并排成字符串：模板拿到的一律不是 datetime #}
    <span class="muted">{{ triggered_at }}</span>
    {% if alert.is_test %}<span class="badge">测试</span>{% endif %}
    {% if intraday %}<span class="badge">盘中</span>{% endif %}
    {% if deferred %}<span class="badge">你让它稍后提醒：稍后至 {{ deferred }}</span>{% endif %}
  </p>
  <table>
    <tr><th>项</th><th>内容</th></tr>
    <tr><td>规则</td><td>{{ alert.rule_name or "—" }}</td></tr>
    <tr><td>账户</td><td>{{ alert.account_name or "—" }}</td></tr>
    {# 现值/阈值/建议动作在 payload 里，不是列（措辞改版不动表），取的工作在端点里做完了 #}
    <tr><td>现值 / 阈值</td><td class="num">{{ value_text }} / {{ threshold_text }}</td></tr>
    <tr><td>建议动作</td><td>{{ action_note or "—" }}</td></tr>
    <tr><td>阶段</td><td>{{ alert.stage or "—" }}</td></tr>
    <tr><td>正文</td><td>{{ body or "—" }}</td></tr>
    {% if quote_source %}<tr><td>报价来源</td><td class="muted">{{ quote_source }}</td></tr>{% endif %}
    {% if terminal %}
    <tr><td>处置动作</td><td>{{ action_kind_text }}
      {% if action_ref_url %}<a href="{{ action_ref_url }}">{{ alert.action_ref }}</a>
      {% elif alert.action_ref %}<span class="muted">{{ alert.action_ref }}</span>{% endif %}
    </td></tr>
    <tr><td>说明</td><td>{{ alert.handle_note or "—" }}</td></tr>
    <tr><td>关上时刻</td><td class="muted">{{ handled_at }}</td></tr>
    {% endif %}
    {% if alert.dismiss_code %}
    <tr><td>忽略原因</td><td>{{ dismiss_code_text }}：{{ alert.dismiss_reason or "（未填）" }}</td></tr>
    <tr><td></td><td class="muted">这条原因会进入本周忽略率。</td></tr>
    {% endif %}
  </table>
  {% if can_undo %}
  <form method="post" action="/alerts/{{ alert.id }}/undo" class="inline-form">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    {# 窗口外这个按钮根本不渲染（`can_undo` 读 UNDO_WINDOW）：让人按下去吃 409 是拿拒绝当交互 #}
    <button type="submit">撤销（终态 5 分钟内）</button>
  </form>
  {% endif %}
</section>

{% if can_act %}
<section class="card">
  <h2>闭环</h2>
  {% if can_ack %}
  <form method="post" action="/alerts/{{ alert.id }}/ack" class="inline-form">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit">已读</button>
  </form>
  {% endif %}
  <form method="post" action="/alerts/{{ alert.id }}/defer" class="inline-form">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>稍后 <select name="until">
      {% for value, label in defer_choices %}<option value="{{ value }}">{{ label }}</option>{% endfor %}
    </select></label>
    <button type="submit">让它稍后提醒</button>
  </form>

  <details {% if draft.action_kind %}open{% endif %}>
    <summary>处置——做了什么（动作五选一，能指到实物的必须指到）</summary>
    <form method="post" action="/alerts/{{ alert.id }}/acted">
      <input type="hidden" name="csrf" value="{{ csrf }}">
      <label>动作 <select name="action_kind">
        <option value="">选择动作…</option>
        {% for value, label in action_kinds %}
        <option value="{{ value }}" {% if draft.action_kind == value %}selected{% endif %}>{{ label }}</option>
        {% endfor %}
      </select></label>
      <label>说明 <input name="note" value="{{ draft.note }}"
             placeholder="其他处置必填一句；其余可空"></label>
      {% if related_theses %}
      <label>关联的观点 <select name="thesis_ref">
        {% for t in related_theses %}<option value="{{ t.id }}">{{ t.label }}</option>{% endfor %}
      </select></label>
      {% else %}<p class="muted">这只票还没有你的观点。</p>{% endif %}
      {% if rules %}
      <label>关联的规则 <select name="rule_ref">
        {% for r in rules %}<option value="{{ r.id }}">{{ r.label }}</option>{% endfor %}
      </select></label>
      {% endif %}
      <div class="closure-trade">
        <p class="muted">录一笔成交（成交与闭环同一条事务；标的取自这条告警）：</p>
        <label>账户 <select name="account_id">
          {% for a in accounts %}<option value="{{ a.id }}">{{ a.label }}</option>{% endfor %}
        </select></label>
        <label>方向 <select name="side"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
        <label>数量 <input name="qty" size="6" value="{{ draft.qty }}"></label>
        <label>价格 <input name="price" size="8" value="{{ draft.price }}"></label>
        <label>费用 <input name="fee" size="6" value="{{ draft.fee }}"></label>
        <label>成交时间 <input type="datetime-local" name="ts">（空 = 此刻）</label>
      </div>
      {# §6.3：T+1/整手/涨跌停只警告不拦截——模拟账本常在补记真实成交，这句话得静态挂着 #}
      <p class="muted">A 股 T+1、整手、涨跌停：这里不做拦截，照实记。</p>
      <label><input type="checkbox" name="sync_annotation" value="on">
        同时写一条标的批注（预填这条告警与我的结论）</label>
      <button type="submit">记为已处置</button>
    </form>
  </details>

  <details {% if reason %}open{% endif %}>
    <summary>忽略——为什么不处理</summary>
    <form method="post" action="/alerts/{{ alert.id }}/dismiss">
      <input type="hidden" name="csrf" value="{{ csrf }}">
      <label>原因码 <select name="code">
        <option value="">选择原因…</option>
        {% for value, label in dismiss_codes %}
        <option value="{{ value }}" {% if draft.code == value %}selected{% endif %}>{{ label }}</option>
        {% endfor %}
      </select></label>
      <label>说明 <input name="reason" value="{{ reason }}"
             placeholder="误报/其他必填一句；其余可空"></label>
      <label><input type="checkbox" name="sync_annotation" value="on">
        同时写一条标的批注（预填这条告警与我的原因）</label>
      <button type="submit">驳回</button>
      <p class="muted">这条原因会进入本周忽略率。忽略率只统计「驳回」，稍后不算。</p>
    </form>
  </details>
</section>
{% endif %}

<section class="card">
  <h2>上下文</h2>
  {# 与推送同一源的批注：嵌的就是那枚 fragment，不为详情另写一条读批注的路（§6.3） #}
  <div hx-get="/api/partials/annotations?target_type=symbol&target_ref={{ alert.market }}:{{ alert.code }}"
       hx-trigger="load" hx-swap="outerHTML">
    <p class="muted">批注加载中…</p>
  </div>
  <p class="muted">最近一笔成交请看 <a href="/portfolio">持仓与流水</a>。</p>
</section>

<section class="card">
  <h2>动作历史</h2>
  <table>
    <tr><th>时刻</th><th>动作</th><th>状态</th><th>说明</th></tr>
    {% for row in events %}
    <tr>
      <td class="muted">{{ row.at }}</td>
      <td>{{ row.action }}</td>
      <td>{{ row.from }} → {{ row.to }}</td>
      <td>{{ row.note or "—" }}</td>
    </tr>
    {% else %}
    <tr><td colspan="4" class="muted">还没有动作：这条还挂着。</td></tr>
    {% endfor %}
  </table>
</section>

{# ==== 从这里往下：投递记录一节，从旧 alert.html 原样搬过来，一字不改 ==== #}
{% endblock %}
```

`alerts.html`（收件箱列表）在 `{% if row.is_test %}…测试…{% endif %}` 徽标之后加一行：

```html
{% if row.deferred %}<span class="badge">稍后至 {{ row.deferred }}</span>{% endif %}
```

`app.css` 追加：

```css
.closure-trade { border-left: 3px solid var(--line, #ddd); padding-left: 10px; margin: 8px 0; }
.card details summary { cursor: pointer; margin: 6px 0; }
```

- [ ] **Step 7: 迁移 `tests/web/test_alerts_web.py` 的旧用例**

`test_acted_marks_handled_and_leaves_default_inbox`：POST 数据补必填字段——

```python
    resp = logged.client.post(
        f"/alerts/{alert.id}/acted",
        data={"csrf": logged.csrf, "action_kind": "other", "note": "已减仓"},
        follow_redirects=False,
    )
```

其余断言不动（`已处理` 是状态名、`已减仓` 是 `handle_note`，两列都还在写）。

`test_dismiss_requires_reason_then_records_it` 整函数替换为：

```python
def test_dismiss_requires_code_then_records_it(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    blank = logged.client.post(f"/alerts/{alert.id}/dismiss", data={"csrf": logged.csrf})
    assert blank.status_code == 400
    # 那句话长在表单旁边（原因码下拉旁边），而不是一屏 error.html
    assert "忽略必须先选原因码" in blank.text
    assert ctx.alerts_store.get(alert.id).status == NEW

    ok = logged.client.post(
        f"/alerts/{alert.id}/dismiss",
        data={"csrf": logged.csrf, "code": "expected", "reason": "假信号：还没碰到止损线"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == DISMISSED and row.dismiss_reason == "假信号：还没碰到止损线"
    assert "已驳回" in logged.client.get(f"/alerts/{alert.id}").text
```

`test_dismiss_400_refills_what_was_typed`：POST 数据加 `"code": "other"`，
断言里 `"驳回必须写原因"` 换成 `"other 必须写一句说明"`（那句现在归仓库说），
`'value="   "'` 的交回断言不动——docstring 里「今天真会拒的那一句是空原因」照旧成立。

`test_other_users_alert_is_404` 里那次 `/acted` 只带 `note` 不带 `action_kind`：归属闸在
动作校验**之前**（`_require_own` 是第一句），404 照旧——不用改。

- [ ] **Step 8: 跑测试确认通过**

Run: `uv run pytest tests/web/test_alerts_closure_web.py tests/web/test_alerts_web.py -q`
Expected: PASS（新 16 条 + 旧文件全量）
再跑 `uv run pytest tests/web -q`：别的页面不读 alert.html，旧 resend/筛选用例应全绿。

- [ ] **Step 9: 提交**

```bash
git add src/sams/web/display.py src/sams/web/app.py src/sams/web/pages/alerts.py \
  src/sams/web/templates/alert.html src/sams/web/templates/alerts.html \
  src/sams/web/static/app.css tests/web/test_alerts_closure_web.py tests/web/test_alerts_web.py
git commit -m "feat(web): 告警闭环全套——五动作/忽略码/稍后/撤销/事件历史/同步批注（§6.3/§8.1）"
```

---

## Task 5: `/review` 复盘工作台——窗口原始行查询、`review_stats` 纯函数与导航徽标

**Files:**
- Modify: `src/sams/storage/repos/alerts.py`（`_REVIEW_COLUMNS`、`OVERDUE_AFTER` 与 `list_window`/`list_overdue`/`count_overdue`/`count_open`，加在 `latest_open` 之后的读区）
- Modify: `src/sams/storage/repos/annotations.py`（`list_visible` 加 `since`）
- Modify: `src/sams/services/annotations.py`（加 `list_recent`）
- Create: `src/sams/services/review_stats.py`
- Create: `src/sams/web/pages/review.py`
- Create: `src/sams/web/templates/review.html`
- Modify: `src/sams/web/app.py`（注册 `review` router）
- Modify: `src/sams/web/pages/alerts.py`（加 `/api/partials/open-count`）
- Modify: `src/sams/web/templates/base.html`（导航加「复盘」与「告警」未处理数徽标）
- Test: `tests/storage/test_alerts_review_queries.py`（新建）
- Test: `tests/services/test_review_stats.py`（新建）
- Test: `tests/web/test_review_web.py`（新建）
- Modify: `tests/storage/test_annotations_repo.py`、`tests/services/test_annotations_service.py`（各补一条）

复盘的拆法是三层，各测各的：**SQL 在仓库**——四个新查询只读原始 dict，不构造 `Alert`
（`_to_alert` 会把窗口里每一行的 payload json.loads 一遍，而 D135 的首屏只要十几列）；
**口径在纯函数**——`review_stats` 里每一条「分母空 → None」「旧版行不进分布」「撤销按当前
状态算」都能在没有数据库的地方断言；**页面只做窗口算术、拼行与那句「—」**，模板不碰
datetime、也不算比率（与收件箱同一规矩，`display.moment_label` 在 Python 里折）。

两个措辞上的定夺，写进代码注释也写进这里：

- **快捷闭环在本页只内联「已读」**。§6.4 说超 24h 清单「带快捷已读/处置/忽略/稍后」——
  后三个都带着表单（五类动作、原因码、时间选项），内联到清单等于把详情页再写一遍；
  「快捷」落在「每一行都是一个可直接按下去的入口」，已读是唯一不需要输入的，就内联它。
- **规则比的「旧版 dismiss」仍进比、不进分布**。§8.4 只把分布的分母摘掉旧版；
  「忽略/触发」比读的是状态列，撤销过就不算关也是同一句话——`summarize` 里两条都
  是「按当前 status」的自动推论，函数不认识 `alert_events`。

导航徽标读的是 `count_open`——与本页顶部「未处理总数」同一个数、同一条 `is_test`
排除（D131）：htmx 每 60 秒只取那一个数，不为徽标整页刷新。

- [ ] **Step 1: 写失败的仓库测试（新建 `tests/storage/test_alerts_review_queries.py`）**

```python
"""复盘的四条只读查询：原始行、半开窗口、24 小时那道闸、带测试的排除。

这些都是 `/review` 那一屏替不掉的断言：窗口的半开区间、`is_test` 的排除、
未到点排后面——漂移了屏幕上的数照样像个数，只是是假的。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from support import FrozenClock

from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)  # 北京 09:00，2026-10-25 是周日

CREATE_KW = {
    "rule_id": "r1",
    "rule_name": "止损线",
    "account_id": None,
    "account_name": "",
    "market": "A",
    "code": "600519",
    "symbol_name": "贵州茅台",
    "severity": "mid",
    "stage": "close",
    "value_text": "1500.00",
    "threshold_text": "1550.00",
    "action_note": "考虑减仓",
    "triggered_at": NOW,
}


@pytest.fixture
def clock():
    return FrozenClock(NOW)


@pytest.fixture
def repo(db, clock):
    return AlertRepo(db, clock=clock)


@pytest.fixture
def seeded_user(db):
    return UserRepo(db).create("alice", "correct-horse-battery").id


def _mk(repo, user, *, at=NOW, **over):
    kw = dict(CREATE_KW)
    kw["triggered_at"] = at
    kw.update(over)
    return repo.create(user, **kw)


def test_list_window_is_half_open_and_carries_rule_name(repo, seeded_user):
    since = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)
    until = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    first = _mk(repo, seeded_user, at=since)  # 恰在起点：含
    _mk(repo, seeded_user, at=since - timedelta(microseconds=1))
    _mk(repo, seeded_user, at=until)  # 恰在上界：排（与 `list_for` 同规）
    mine = _mk(repo, seeded_user, at=NOW)
    _mk(repo, "other", at=NOW)  # 别人的不进我的窗口
    rows = repo.list_window(seeded_user, since=since, until=until)
    assert [r["id"] for r in rows] == [first.id, mine.id]  # 升序
    assert rows[0]["rule_name"] == "止损线"  # 原始行直接带名字，页面不再查规则表


def test_list_window_excludes_test_alerts(repo, seeded_user):
    _mk(repo, seeded_user, is_test=True)
    real = _mk(repo, seeded_user)
    rows = repo.list_window(
        seeded_user, since=NOW - timedelta(days=1), until=NOW + timedelta(days=1)
    )
    assert [r["id"] for r in rows] == [real.id]  # /settings 发的那条不属于任何一周的「新增」（D131）


def test_list_overdue_skips_fresh_and_sorts_not_yet_due_last(repo, seeded_user):
    old = _mk(repo, seeded_user, at=NOW - timedelta(hours=30))
    older = _mk(repo, seeded_user, at=NOW - timedelta(hours=40))
    _mk(repo, seeded_user, at=NOW - timedelta(hours=2))  # 不到 24h，不列
    deferred = _mk(repo, seeded_user, at=NOW - timedelta(hours=50))
    repo.defer(deferred.id, user_id=seeded_user, until=NOW + timedelta(hours=5))
    rows = repo.list_overdue(seeded_user, now=NOW)
    # 到点的按最老在前；让人说了「稍后」而还没到点的那条排最后——它仍算未处理
    assert [r["id"] for r in rows] == [older.id, old.id, deferred.id]


def test_count_overdue_is_the_full_number_behind_the_truncated_list(repo, seeded_user):
    for index in range(23):
        _mk(repo, seeded_user, at=NOW - timedelta(hours=25, minutes=index))
    assert len(repo.list_overdue(seeded_user, now=NOW)) == 20  # 清单只列 20
    assert repo.count_overdue(seeded_user, now=NOW) == 23  # 顶上那个数必须是全量


def test_count_open_counts_new_and_acknowledged_excluding_test(repo, seeded_user):
    first = _mk(repo, seeded_user)
    second = _mk(repo, seeded_user)
    repo.acknowledge(second.id, user_id=seeded_user)
    _mk(repo, seeded_user, is_test=True)
    repo.dismiss(first.id, user_id=seeded_user, code="expected")
    assert repo.count_open(seeded_user) == 1  # 只剩那条已读：终态与测试都不算


def test_list_overdue_excludes_terminal(repo, seeded_user):
    old = _mk(repo, seeded_user, at=NOW - timedelta(hours=30))
    repo.act(old.id, user_id=seeded_user, action_kind="watch")
    assert repo.list_overdue(seeded_user, now=NOW) == []
```

- [ ] **Step 2: 跑一遍确认失败**

Run: `uv run pytest -q tests/storage/test_alerts_review_queries.py`
Expected: FAIL —`AttributeError: 'AlertRepo' object has no attribute 'list_window'`（失败只应来自缺方法，不是缺参数键）

- [ ] **Step 3: 实现仓库的四条查询**

在 `src/sams/storage/repos/alerts.py` 的模块常量区（`UNDO_WINDOW` 之后）加：

```python
#: 「积压」的那道闸：触发后 24 小时还没人管的，复盘首屏第一个清单就是它（§6.4）
OVERDUE_AFTER = timedelta(hours=24)
```

在 `_ALERT_COLUMNS` 之后加列集，在 `latest_open` 之后（仍在「读」区）加四个方法：

```python
#: `/review` 的原始行列集：统计与清单要的那十几列。`payload_json` 整列不带回来，
#: 只把 `rule_name`/`symbol_name` 两格用 `json_extract_string` 挖出来——清单要说
#: 「哪条规则的哪只票」，而 `_to_alert` 那样的整包解析在这一屏没有买家（D135）。
_REVIEW_COLUMNS = (
    "id, rule_id, market, code, triggered_at, status, severity,"
    " acknowledged_at, handled_at, action_kind, action_ref, dismiss_code,"
    " thesis_id, deferred_until,"
    " json_extract_string(payload_json, '$.rule_name') AS rule_name,"
    " json_extract_string(payload_json, '$.symbol_name') AS symbol_name"
)
```

```python
    def list_window(self, user_id: str, *, since: datetime, until: datetime) -> list[dict]:
        """复盘窗口的原始行：`triggered_at` 落在 `[since, until)`，升序。

        返回 dict 而不是 `Alert`：复盘要的是列，`_to_alert` 的 payload 解析、名称折算
        这一屏一样都用不上。`until` 开区间与 `list_for` 同一个约定——零点那一条不该在
        相邻的两个窗口里各出现一次。测试告警排除（D131）：它不属于任何一周的「新增」。
        """
        rows = self.db.query(
            f"SELECT {_REVIEW_COLUMNS} FROM alerts"
            " WHERE user_id = ? AND triggered_at >= ? AND triggered_at < ?"
            " AND coalesce(is_test, FALSE) = FALSE"
            " ORDER BY triggered_at ASC, id ASC",
            [user_id, _moment(since), _moment(until)],
        )
        return rows.to_dicts()

    def list_overdue(self, user_id: str, *, now: datetime, limit: int = 20) -> list[dict]:
        """超 24 小时未处理：`new/acknowledged` 且早于 `now - OVERDUE_AFTER`（§8.4，不受窗口限制）。

        `deferred_until` 未到点的排在后面：稍后不改状态，它仍算未处理——但这份清单是
        用来「现在就办」的，人说了明天再看的条目不占第一行。`CASE` 只是那一层排序，
        主序仍是触发时间升序（压 oldest 先）。
        """
        moment = _moment(now)
        rows = self.db.query(
            f"SELECT {_REVIEW_COLUMNS} FROM alerts"
            " WHERE user_id = ? AND status IN ('new', 'acknowledged')"
            " AND triggered_at < ? AND coalesce(is_test, FALSE) = FALSE"
            " ORDER BY CASE WHEN deferred_until IS NOT NULL AND deferred_until > ? THEN 1 ELSE 0 END,"
            " triggered_at ASC, id ASC LIMIT ?",
            [user_id, moment - OVERDUE_AFTER, moment, int(limit)],
        )
        return rows.to_dicts()

    def count_overdue(self, user_id: str, *, now: datetime) -> int:
        """`list_overdue` 同口径的全量数：清单只列 20 条，顶上那个数必须是全量——
        把 20 读成「就这些」是这一屏最难被发现的谎。"""
        moment = _moment(now)
        return int(self.db.scalar(
            "SELECT count(*) FROM alerts WHERE user_id = ?"
            " AND status IN ('new', 'acknowledged') AND triggered_at < ?"
            " AND coalesce(is_test, FALSE) = FALSE",
            [user_id, moment - OVERDUE_AFTER],
        ))

    def count_open(self, user_id: str) -> int:
        """导航徽标与 `/review` 顶部的「未处理总数」：同一个 `new + acknowledged`，
        同一条测试排除（D124/D131）。与 `latest_open` 的分工是「一个数 vs 最新一条」。"""
        return int(self.db.scalar(
            "SELECT count(*) FROM alerts WHERE user_id = ?"
            " AND status IN ('new', 'acknowledged')"
            " AND coalesce(is_test, FALSE) = FALSE",
            [user_id],
        ))
```

- [ ] **Step 4: 跑一遍确认通过**

Run: `uv run pytest -q tests/storage/test_alerts_review_queries.py`
Expected: PASS（6 条）

- [ ] **Step 5: `list_visible` 加 `since` 与服务的 `list_recent`**

`src/sams/storage/repos/annotations.py` 的 `list_visible` 参数表加一项（`tag` 之后、
`limit` 之前），并在 `if tag:` 那段之后加过滤：

```python
        since: datetime | None = None,
```

```python
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
```

`src/sams/services/annotations.py` 在 `list_target` 之后加：

```python
    def list_recent(
        self,
        viewer_id: str,
        *,
        since: datetime,
        team_sharing: bool = False,
        limit: int = 20,
    ) -> list[Annotation]:
        """`/review` 的「本周批注」：可见性闸与面板同一条（仓库的 `list_visible`），
        只是按时间切、不按目标切。复盘要的是「这周写了什么」，不问写在哪个对象上。"""
        return self.annotations.list_visible(
            viewer_id, since=since, team_sharing=team_sharing, limit=limit
        )
```

两条测试：`tests/storage/test_annotations_repo.py` 末尾加（该文件已有 `repo`/`clock`
夹具与 `add(repo, **kw)` helper，`timedelta` 需要补进它的 datetime import）：

```python
def test_list_visible_since_cuts_the_older_rows(repo, clock):
    add(repo, body_md="上周写的那条")
    clock.moment = NOW + timedelta(days=3)
    recent = add(repo, body_md="本周的")
    rows = repo.list_visible(OWNER, since=NOW + timedelta(days=1))
    assert [row.id for row in rows] == [recent.id]
```

`tests/services/test_annotations_service.py` 末尾加（`NOW`、`world` 夹具都在该文件里）：

```python
def test_list_recent_forwards_the_window(world):
    from datetime import timedelta

    world.service.create(
        world.user.id, target_type="symbol", target_ref="A/600519", body_md="上周的"
    )
    world.clock.moment = NOW + timedelta(days=3)
    recent = world.service.create(
        world.user.id, target_type="symbol", target_ref="A/600519", body_md="本周的"
    )
    got = world.service.list_recent(world.user.id, since=NOW + timedelta(days=1))
    assert [row.id for row in got] == [recent.id]
```

Run: `uv run pytest -q tests/storage/test_annotations_repo.py tests/services/test_annotations_service.py`
Expected: PASS（新增 2 条通过；`since` 参数已在本步实现，不必再分红绿两趟——它是
一个纯加的过滤键，红的那一趟只会红在 `TypeError` 上）

- [ ] **Step 6: 写失败的口径测试（新建 `tests/services/test_review_stats.py`）**

```python
"""§8.4 复盘口径：纯函数，输入是 `AlertRepo.list_window` 那种原始 dict。

分母为空是 None 不是 0（「没有可关的」与「一条没关上」是两句话）；旧版行不进分布；
撤销后的行按当前状态算——这些全在「读哪一列」的层面，不需要数据库也断言得动。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sams.services.review_stats import MIN_RULE_SAMPLES, summarize, top_rule_ratios

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def _row(**overrides):
    row = {
        "id": "a1",
        "rule_id": "r1",
        "rule_name": "止损线",
        "triggered_at": NOW,
        "status": "new",
        "severity": "mid",
        "acknowledged_at": None,
        "handled_at": None,
        "action_kind": None,
        "action_ref": None,
        "dismiss_code": None,
        "thesis_id": None,
        "deferred_until": None,
        "market": "A",
        "code": "600519",
    }
    row.update(overrides)
    return row


def test_empty_window_has_no_rates():
    stats = summarize([])
    assert stats.total == 0
    assert stats.close_rate is None and stats.ack_rate is None
    assert stats.act_rate is None and stats.dismiss_rate is None
    assert stats.median_close_hours is None


def test_rates_use_new_as_the_denominator():
    rows = [
        _row(id="a1", status="acknowledged", acknowledged_at=NOW),
        _row(
            id="a2",
            status="handled",
            acknowledged_at=NOW,
            handled_at=NOW + timedelta(hours=2),
            action_kind="watch",
        ),
        _row(id="a3", status="new"),
    ]
    stats = summarize(rows)
    assert stats.close_rate == 1 / 3
    assert stats.ack_rate == 2 / 3
    assert stats.act_rate == 1.0 and stats.dismiss_rate == 0.0
    assert stats.action_kinds == {"watch": 1}
    assert stats.median_close_hours == 2.0


def test_legacy_rows_stay_out_of_the_distributions():
    rows = [
        _row(id="a1", status="handled", handled_at=NOW + timedelta(hours=1)),
        _row(id="a2", status="dismissed", handled_at=NOW + timedelta(hours=3)),
    ]
    stats = summarize(rows)
    assert stats.closed == 2  # 状态口径不变：旧版行真的被关掉了
    assert stats.action_kinds == {} and stats.dismiss_codes == {}
    assert stats.legacy_handled == 1 and stats.legacy_dismiss == 1


def test_undo_flattens_back_to_open_by_status():
    """撤销把 status 改回 acknowledged：闭环率读的是当前状态，撤销过就不算关。"""
    rows = [_row(status="acknowledged", acknowledged_at=NOW, handled_at=None)]
    assert summarize(rows).closed == 0


def test_median_of_three_close_spans():
    rows = [
        _row(
            id=f"a{i}",
            status="dismissed",
            handled_at=NOW + timedelta(hours=h),
            dismiss_code="expected",
        )
        for i, h in enumerate((1, 2, 11))
    ]
    assert summarize(rows).median_close_hours == 2.0


def test_rule_ratio_ranks_and_thin_split():
    rows = []
    for rule, fired, missed in (("r1", 5, 4), ("r2", 3, 2), ("r3", 2, 2)):
        for index in range(fired):
            dismissed = index < missed
            rows.append(
                _row(
                    id=f"{rule}-{index}",
                    rule_id=rule,
                    rule_name=f"规则{rule}",
                    status="dismissed" if dismissed else "new",
                    dismiss_code="expected" if dismissed else None,
                    handled_at=NOW + timedelta(hours=1) if dismissed else None,
                )
            )
    ranked, thin = top_rule_ratios(rows)
    assert [x.rule_id for x in ranked] == ["r1", "r2"]
    assert [x.rule_id for x in thin] == ["r3"]
    assert ranked[0].ratio == 4 / 5 and not ranked[0].thin
    assert thin[0].triggered == MIN_RULE_SAMPLES - 1


def test_ranking_is_capped_at_five():
    rows = [
        _row(id=f"{rule}-{index}", rule_id=rule, status="dismissed", dismiss_code="expected")
        for rule in range(8)
        for index in range(MIN_RULE_SAMPLES)
    ]
    ranked, thin = top_rule_ratios(rows)
    assert len(ranked) == 5 and thin == []
```

- [ ] **Step 7: 跑一遍确认失败，然后实现 `src/sams/services/review_stats.py`**

Run: `uv run pytest -q tests/services/test_review_stats.py`
Expected: FAIL —`ModuleNotFoundError`/`ImportError`

```python
"""§8.4 复盘口径：纯函数，输入 `AlertRepo.list_window` 的原始 dict。

拆成两半的理由：SQL 只管「把窗口里的列读回来」，分子、分母、中位数、旧版单列这些
判断全在这里——它们不需要数据库就能逐条断言（`tests/services/test_review_stats.py`），
而页面只管渲染与「—」的措辞。撤销（undo）之后的行以当前 `status` 为准：这个函数
不认识 `alert_events`，「撤销过就不算闭环」是读状态列的自动推论，不是特判。
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 规则「忽略/触发」比的样本门槛：低于 3 条不排名（§6.4）。样本少时比率抖动大，
#: 把 1/2 排进前五是拿计数编造「规则质量」结论——那是 M5 跨期样本的事（§8.4）。
MIN_RULE_SAMPLES = 3

#: 排名取前几条（§6.4 的「最高的 5 条」）
TOP_RULES = 5

_CLOSED = ("handled", "dismissed")


@dataclass(frozen=True)
class RuleRatio:
    rule_id: str
    rule_name: str
    triggered: int
    dismissed: int
    ratio: float
    thin: bool


@dataclass(frozen=True)
class WindowStats:
    total: int
    acknowledged: int
    closed: int
    handled: int
    dismissed: int
    ack_rate: float | None
    close_rate: float | None
    act_rate: float | None
    dismiss_rate: float | None
    median_close_hours: float | None
    action_kinds: Mapping[str, int]
    dismiss_codes: Mapping[str, int]
    legacy_handled: int
    legacy_dismiss: int


def ratio(num: int, den: int) -> float | None:
    """空分母是 None，不是 0.0：「没有可关的」与「一条没关上」是两句话（§6.4）。"""
    return None if den == 0 else num / den


def summarize(rows: Sequence[Mapping[str, Any]]) -> WindowStats:
    """窗口行的分子分母。列名与 `_REVIEW_COLUMNS` 一致；时刻是带时区的 datetime。"""
    total = len(rows)
    acknowledged = sum(1 for row in rows if row["acknowledged_at"] is not None)
    handled_rows = [row for row in rows if row["status"] == "handled"]
    dismissed_rows = [row for row in rows if row["status"] == "dismissed"]
    closed = len(handled_rows) + len(dismissed_rows)
    # 旧版行：M4-B 之前关掉的没有 action_kind/dismiss_code。状态口径的比率照算
    # （它们真的被关了），两部分布的分子分母都不含它们——口径不可考，不伪造历史。
    legacy_handled = sum(1 for row in handled_rows if not row["action_kind"])
    legacy_dismiss = sum(1 for row in dismissed_rows if not row["dismiss_code"])
    action_kinds = Counter(str(row["action_kind"]) for row in handled_rows if row["action_kind"])
    dismiss_codes = Counter(str(row["dismiss_code"]) for row in dismissed_rows if row["dismiss_code"])
    spans = [
        (row["handled_at"] - row["triggered_at"]).total_seconds() / 3600.0
        for row in rows
        if row["handled_at"] is not None and row["status"] in _CLOSED
    ]
    return WindowStats(
        total=total,
        acknowledged=acknowledged,
        closed=closed,
        handled=len(handled_rows),
        dismissed=len(dismissed_rows),
        ack_rate=ratio(acknowledged, total),
        close_rate=ratio(closed, total),
        act_rate=ratio(len(handled_rows), closed),
        dismiss_rate=ratio(len(dismissed_rows), closed),
        median_close_hours=statistics.median(spans) if spans else None,
        action_kinds=dict(action_kinds),
        dismiss_codes=dict(dismiss_codes),
        legacy_handled=legacy_handled,
        legacy_dismiss=legacy_dismiss,
    )


def top_rule_ratios(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[RuleRatio], list[RuleRatio]]:
    """(按比率降序的前 `TOP_RULES` 条, 样本不足单列)。

    排名与门槛在同一个函数里算：页面各算各的会让一条规则同时进「前五」和
    「样本不足」——门槛是排名自己的事，不是渲染的事。并列时触发多者优先，
    再按 rule_id 定序（测试要能断言顺序，不能靠 dict 的插入序）。
    """
    by_rule: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_rule.setdefault(str(row["rule_id"]), []).append(row)
    ratios = []
    for rule_id, group in by_rule.items():
        triggered = len(group)
        dismissed = sum(1 for row in group if row["status"] == "dismissed")
        ratios.append(
            RuleRatio(
                rule_id=rule_id,
                rule_name=str(group[0]["rule_name"] or ""),
                triggered=triggered,
                dismissed=dismissed,
                ratio=dismissed / triggered,
                thin=triggered < MIN_RULE_SAMPLES,
            )
        )
    ranked = sorted(
        (item for item in ratios if not item.thin),
        key=lambda item: (-item.ratio, -item.triggered, item.rule_id),
    )
    thin = sorted(
        (item for item in ratios if item.thin),
        key=lambda item: (-item.triggered, item.rule_id),
    )
    return ranked[:TOP_RULES], thin
```

Run: `uv run pytest -q tests/services/test_review_stats.py`
Expected: PASS（7 条）

- [ ] **Step 8: 写失败的页面测试（新建 `tests/web/test_review_web.py`）**

```python
"""/review 那一屏（M4-B §6.4、D135）：窗口算术、清单顺序、比率与「—」的措辞。

口径不在这测——`tests/services/test_review_stats.py` 测的是函数；这里测的是
「窗口在 display_tz 里怎么切」「数是不是从对的那条查询来的」「旧版那两个字出没出现」。
数一律用 `id="…-total"` 的锚点读页面上的那个 `<span>`，不读测试自己写的常量。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from support import FrozenClock

from sams.storage.repos.alerts import HANDLED


def _seed(ctx, *, user_id, market="A", code="600519", **overrides):
    """与 `test_alerts_web.py` 同款（不建 tests/web/support.py，那会遮住根目录的 `support`）。"""
    kwargs = dict(
        rule_id=None,
        rule_name="止损线",
        account_id=None,
        account_name="",
        market=market,
        code=code,
        symbol_name="贵州茅台",
        severity="mid",
        stage="close",
        value_text="1500.00",
        threshold_text="1550.00",
        action_note="考虑减仓",
        triggered_at=datetime.now(UTC),
    )
    kwargs.update(overrides)
    return ctx.alerts_store.create(user_id, **kwargs)


def _alice(ctx):
    return ctx.users.get_by_username("alice")


def _number(page, anchor):
    found = re.search(rf'id="{anchor}"[^>]*>(\d+)<', page.text)
    assert found is not None, page.text
    return int(found.group(1))


def test_review_defaults_to_the_seven_day_window(logged):
    page = logged.client.get("/review")
    assert page.status_code == 200
    assert 'value="7d" selected' in page.text
    assert "超 24 小时未处理" in page.text


def test_unknown_window_is_answered_with_the_allowed_list(logged):
    page = logged.client.get("/review?window=14d")
    assert page.status_code == 400
    assert "未知窗口" in page.text


def test_week_window_starts_this_monday_by_display_tz(make_app):
    """冻结时钟装在「北京周日 09:00」：本周一 00:00 起算，上周六那条不该在。"""
    frozen = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    web = make_app(clock=FrozenClock(frozen))
    ctx = web.contexts.current()
    ctx.users.create("alice", "correct-horse-battery", is_admin=True)
    uid = ctx.users.get_by_username("alice").id
    _seed(ctx, user_id=uid, triggered_at=frozen - timedelta(days=7))  # 上周六
    _seed(ctx, user_id=uid, triggered_at=frozen - timedelta(days=6))  # 本周一 09:00
    with TestClient(web.app) as client:
        resp = client.post(
            "/login",
            data={"username": "alice", "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = client.get("/review?window=week")
        assert _number(page, "window-total") == 1


def test_seven_day_window_includes_the_sixth_midnight(logged):
    """「近 7 天」含今天：起点是 6 天前的零点，那一秒那一条要在；再早一分钟不在。"""
    ctx = logged.context
    tz = ZoneInfo(ctx.settings.server.display_tz)
    now_local = datetime.now(tz)
    sixth = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    _seed(ctx, user_id=_alice(ctx).id, triggered_at=sixth + timedelta(minutes=1))
    _seed(ctx, user_id=_alice(ctx).id, triggered_at=sixth - timedelta(minutes=1))
    page = logged.client.get("/review")
    assert _number(page, "window-total") == 1


def test_overdue_lists_old_not_fresh_and_the_top_number_is_full(logged):
    ctx = logged.context
    old = _seed(ctx, user_id=_alice(ctx).id, triggered_at=datetime.now(UTC) - timedelta(hours=30))
    fresh = _seed(ctx, user_id=_alice(ctx).id, triggered_at=datetime.now(UTC) - timedelta(hours=2))
    page = logged.client.get("/review")
    assert f"/alerts/{old.id}" in page.text
    assert fresh.id not in page.text  # 不到 24 小时的新告警不属于这份清单
    assert _number(page, "overdue-total") == 1


def test_deferred_not_yet_due_sorts_last(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    older = _seed(ctx, user_id=uid, triggered_at=datetime.now(UTC) - timedelta(hours=40))
    old = _seed(ctx, user_id=uid, triggered_at=datetime.now(UTC) - timedelta(hours=30))
    deferred = _seed(ctx, user_id=uid, triggered_at=datetime.now(UTC) - timedelta(hours=50))
    ctx.alerts_store.defer(
        deferred.id, user_id=uid, until=datetime.now(UTC) + timedelta(hours=3)
    )
    page = logged.client.get("/review")
    order = [page.text.index(f"/alerts/{row.id}") for row in (older, old, deferred)]
    assert order == sorted(order)  # 到点的最老在前，未到点的垫底


def test_quick_ack_button_closes_in_place(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id, triggered_at=datetime.now(UTC) - timedelta(hours=30))
    page = logged.client.get("/review")
    assert f'action="/alerts/{alert.id}/ack"' in page.text
    resp = logged.client.post(
        f"/alerts/{alert.id}/ack", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "已读" in logged.client.get("/review").text  # 已读仍算未处理，仍在这份清单里（D124）


def test_rule_ratio_top_rows_and_thin_single_column(logged):
    ctx = logged.context
    uid = _alice(ctx).id

    def _rule(rule_id, fired, missed):
        rows = []
        for index in range(fired):
            rows.append(
                _seed(
                    ctx, user_id=uid,
                    rule_id=rule_id, rule_name=f"规则{rule_id}",
                    triggered_at=datetime.now(UTC) - timedelta(hours=2),
                )
            )
        for row in rows[:missed]:
            ctx.alerts_store.dismiss(row.id, user_id=uid, code="expected")
        return rows

    _rule("r1", 5, 4)
    _rule("r2", 3, 2)
    _rule("r5", 2, 2)  # 样本不足：不排名
    page = logged.client.get("/review")
    assert "/rules/r1/edit" in page.text and "/rules/r2/edit" in page.text
    assert "样本不足" in page.text and "规则r5" in page.text


def test_handled_section_links_to_the_objects(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    watch = _seed(ctx, user_id=uid)
    ctx.alerts_store.act(watch.id, user_id=uid, action_kind="watch")
    legacy = _seed(ctx, user_id=uid, triggered_at=datetime.now(UTC) - timedelta(hours=2))
    # 旧版关法：不经过 `act`（没有 action_kind、也没有事件），只把状态列改了
    ctx.db.execute(
        "UPDATE alerts SET status = ?, handled_at = ? WHERE id = ?",
        [HANDLED, datetime.now(UTC), legacy.id],
    )
    page = logged.client.get("/review")
    assert "加入自选" in page.text and "/watchlist" in page.text
    assert "旧版" in page.text


def test_empty_denominators_are_dashes_not_zero(logged):
    """有一条 new 而无闭环：处置率/忽略率的分母是 0——「—」，不是 0%。"""
    ctx = logged.context
    _seed(ctx, user_id=_alice(ctx).id)
    page = logged.client.get("/review")
    assert page.text.count("<td>—</td>") >= 2


def test_empty_window_says_the_do_something_sentence(logged):
    page = logged.client.get("/review")
    assert "本周没有告警。可以去看看自选与持仓，或写一条复盘批注。" in page.text


def test_annotations_section_lists_this_week_only(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    ctx.annotations.create(uid, target_type="symbol", target_ref="A/600519", body_md="本周写的逻辑")
    old = ctx.annotations.create(uid, target_type="symbol", target_ref="A/600519", body_md="上个月写的")
    ctx.db.execute(
        "UPDATE annotations SET created_at = ? WHERE id = ?",
        [datetime.now(UTC) - timedelta(days=30), old.id],
    )
    page = logged.client.get("/review")
    assert "本周写的逻辑" in page.text
    assert "上个月写的" not in page.text


def test_open_count_partial_is_the_badge_number(logged):
    ctx = logged.context
    uid = _alice(ctx).id
    _seed(ctx, user_id=uid)
    _seed(ctx, user_id=uid, is_test=True)  # 徽标不该把测试告警数进去（D131）
    resp = logged.client.get("/api/partials/open-count")
    assert resp.status_code == 200
    assert resp.text == "1"


def test_nav_carries_the_review_link_and_the_badge_hook(logged):
    page = logged.client.get("/review")
    assert '<a href="/review">复盘' in page.text
    assert 'hx-get="/api/partials/open-count"' in page.text
```

- [ ] **Step 9: 跑一遍确认失败，然后实现页面与接线**

Run: `uv run pytest -q tests/web/test_review_web.py`
Expected: FAIL —404（`/review` 与 `/api/partials/open-count` 都还没有）

新建 `src/sams/web/pages/review.py`：

```python
"""/review 复盘工作台（M4-B §6.4、D135）：首屏是清单，不是仪表盘。

窗口按 display_tz 切：`7d`/`30d` 是含今天往前 n 天的零点起，`week` 是本周一 00:00 起；
上界一律次日零点、开区间（与 `AlertRepo.list_for` 的 `until` 同规）。口径不在这一层：
行从 `AlertRepo` 的窗口查询来（原始 dict），分子分母在 `services/review_stats.py`，
这一层只做窗口算术、拼行与那句「—」——比率与时刻都不带给模板算（`moment_label` 在
Python 里折，同收件箱）。仓库调用全在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from sams.services.review_stats import RuleRatio, summarize, top_rule_ratios
from sams.storage.repos.alerts import HANDLED
from sams.web.auth import require_user
from sams.web.display import action_label, alert_status, dismiss_label, moment_label

router = APIRouter()

#: 下拉与那道检查读的同一份：三个键就是 URL 里能出现的三个形状
WINDOWS = ("7d", "30d", "week")
_WINDOW_LABELS = {"7d": "近 7 天", "30d": "近 30 天", "week": "本周"}

#: 清单的截断（§6.4：超 24h 前 20 条）；批注取 10 条——第 11 条开始读的是兴趣不是义务
OVERDUE_LIMIT = 20
NOTE_LIMIT = 10

#: §6.4 逐字给的空状态话术：窗口不同也不改它——「本周」在这里是「这一段时间」的口语，
#: 换措辞要过产品评审，不在页面里即兴
EMPTY_NOTE = "本周没有告警。可以去看看自选与持仓，或写一条复盘批注。"


def _bounds(now_local: datetime, window: str) -> tuple[datetime, datetime]:
    """窗口的头尾：`since` 含、`until` 排（次日零点）。

    「近 7 天」含今天：起点是今天减 6 天的零点。减 7 会让每天零点那一秒在相邻窗口里
    各出现一次——与 `list_for` 开区间同一个理由，反着成立。
    """
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "week":
        since = midnight - timedelta(days=midnight.weekday())
    else:
        days = 7 if window == "7d" else 30
        since = midnight - timedelta(days=days - 1)
    return since, midnight + timedelta(days=1)


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _duration(hours: float | None) -> str:
    """中位关闭时长的口语：不到两天说小时，更久说天——`119.0 小时` 那种话没人读。"""
    if hours is None:
        return "—"
    return f"{hours:.1f} 小时" if hours < 48 else f"{hours / 24:.1f} 天"


def _ref_of(row: Mapping[str, Any]) -> tuple[str, str]:
    """处置那一行的 (说法, 链接)。`trade` 的引用是新建的成交 id，没有详情页——链到持仓；
    `watch` 链到自选。旧版行（没有 `action_kind`）两个都没有，那格由「旧版」占位。"""
    kind = str(row.get("action_kind") or "")
    ref = str(row.get("action_ref") or "")
    if kind == "thesis" and ref:
        return action_label(kind), f"/theses/{ref}"
    if kind == "rule" and ref:
        return action_label(kind), f"/rules/{ref}/edit"
    if kind == "trade":
        return action_label(kind), "/portfolio"
    if kind == "watch":
        return action_label(kind), "/watchlist"
    if kind:
        return action_label(kind), ""
    return "旧版", ""


def _rule_rows(items: list[RuleRatio]) -> list[dict]:
    return [
        {
            "id": item.rule_id,
            "name": item.rule_name or item.rule_id,
            "url": f"/rules/{item.rule_id}/edit",
            "dismissed": item.dismissed,
            "triggered": item.triggered,
            "ratio": _rate(item.ratio),
        }
        for item in items
    ]


@router.get("/review")
def review_page(request: Request, window: str = Query("7d")):
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    key = window.strip()
    if key not in WINDOWS:
        raise HTTPException(
            status_code=400, detail=f"未知窗口 {key!r}：只有 {'、'.join(WINDOWS)}"
        )
    tz = ctx.settings.server.display_tz
    now_utc = ctx.clock.now_utc()
    since, until = _bounds(now_utc.astimezone(ZoneInfo(tz)), key)
    rows = ctx.alerts_store.list_window(user.id, since=since, until=until)
    stats = summarize(rows)
    ranked, thin = top_rule_ratios(rows)
    overdue_rows = [
        {
            "id": row["id"],
            "at": moment_label(row["triggered_at"], tz),
            "rule_name": str(row["rule_name"] or ""),
            "symbol": f"{row['market']}/{row['code']}",
            "status": alert_status(str(row["status"])),
            # 「未到点」的那一格决定徽标措辞：到点前它仍是「你让它稍后提醒」，不是新的意外
            "deferred": row["deferred_until"] is not None and row["deferred_until"] > now_utc,
        }
        for row in ctx.alerts_store.list_overdue(user.id, now=now_utc, limit=OVERDUE_LIMIT)
    ]
    handled_rows = [
        {
            "id": row["id"],
            "at": moment_label(row["triggered_at"], tz),
            "rule_name": str(row["rule_name"] or ""),
            "label": _ref_of(row)[0],
            "url": _ref_of(row)[1],
        }
        for row in rows
        if row["status"] == HANDLED
    ]
    note_rows = [
        {
            "id": note.id,
            "at": moment_label(note.created_at, tz),
            "target": note.target_label or note.target_ref,
            "body": (note.body_md[:120] + "…") if len(note.body_md) > 120 else note.body_md,
            "tags": "、".join(note.tags),
        }
        for note in ctx.annotations.list_recent(
            user.id,
            since=since,
            team_sharing=ctx.app_settings.team_sharing(),
            limit=NOTE_LIMIT,
        )
    ]
    return web.render(
        request,
        "review.html",
        title="复盘",
        window=key,
        window_choices=[(value, _WINDOW_LABELS[value]) for value in WINDOWS],
        since_label=f"{since:%m-%d %H:%M} 至 {until:%m-%d %H:%M}（不含）",
        open_total=ctx.alerts_store.count_open(user.id),
        overdue_total=ctx.alerts_store.count_overdue(user.id, now=now_utc),
        window_total=stats.total,
        overdue=overdue_rows,
        handled=handled_rows,
        ranked=_rule_rows(ranked),
        thin=_rule_rows(thin),
        notes=note_rows,
        rate_rows=[
            ("闭环率", _rate(stats.close_rate), f"{stats.closed}/{stats.total}"),
            ("已读率", _rate(stats.ack_rate), f"{stats.acknowledged}/{stats.total}"),
            ("处置率", _rate(stats.act_rate), f"{stats.handled}/{stats.closed}"),
            ("忽略率", _rate(stats.dismiss_rate), f"{stats.dismissed}/{stats.closed}"),
            ("中位关闭时长", _duration(stats.median_close_hours), "仅终态；隔夜算进中位数"),
        ],
        action_rows=[
            (action_label(kind), count)
            for kind, count in sorted(stats.action_kinds.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        dismiss_rows=[
            (dismiss_label(code), count)
            for code, count in sorted(stats.dismiss_codes.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        legacy_handled=stats.legacy_handled,
        legacy_dismiss=stats.legacy_dismiss,
        empty=stats.total == 0 and not overdue_rows,
        empty_note=EMPTY_NOTE,
    )
```

> `_ref_of` 读的是原始 dict 的 `action_kind`/`action_ref` 两列——`Mapping` 的 import
> 已写进上面的 import 块，这里没有别的东西要补。

新建 `src/sams/web/templates/review.html`：

```html
{% extends "base.html" %}
{% block content %}
<h1>复盘</h1>
<form method="get" action="/review" class="inline-form">
  <select name="window" onchange="this.form.submit()">
    {% for value, label in window_choices %}
    <option value="{{ value }}"{% if value == window %} selected{% endif %}>{{ label }}</option>
    {% endfor %}
  </select>
  <button type="submit">切换</button>
</form>
<p class="muted">窗口 {{ since_label }}，按 display_tz 切。</p>

{% if empty %}
<p class="muted">{{ empty_note }}</p>
{% else %}
<div class="cards">
  <div class="card">未处理总数 <span class="num" id="open-total"><a href="/alerts">{{ open_total }}</a></span></div>
  <div class="card">超 24 小时 <span class="num" id="overdue-total"><a href="/alerts?status=new">{{ overdue_total }}</a></span></div>
  <div class="card">本窗口新增 <span class="num" id="window-total">{{ window_total }}</span></div>
</div>

<h2>超 24 小时未处理</h2>
{% if overdue %}
<table>
  <thead><tr><th>触发</th><th>标的</th><th>规则</th><th>状态</th><th>动作</th></tr></thead>
  <tbody>
  {% for row in overdue %}
  <tr>
    <td>{{ row.at }}</td>
    <td><a href="/alerts/{{ row.id }}">{{ row.symbol }}</a></td>
    <td>{{ row.rule_name }}</td>
    <td>{{ row.status }}{% if row.deferred %} <span class="badge">你让它稍后提醒</span>{% endif %}</td>
    <td>
      <form method="post" action="/alerts/{{ row.id }}/ack" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">已读</button>
      </form>
      <a href="/alerts/{{ row.id }}">处置 / 忽略 / 稍后</a>
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="muted">没有超过 24 小时的未处理告警。</p>
{% endif %}

<h2>忽略/触发比最高的规则</h2>
{% if ranked %}
<table>
  <thead><tr><th>规则</th><th>忽略/触发</th><th>比率</th></tr></thead>
  <tbody>
  {% for row in ranked %}
  <tr><td><a href="{{ row.url }}">{{ row.name }}</a></td><td>{{ row.dismissed }}/{{ row.triggered }}</td><td>{{ row.ratio }}</td></tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="muted">窗口里没有够样本的规则（触发 ≥ 3 条才排名）。</p>
{% endif %}
{% if thin %}
<h3>样本不足（不参与排名）</h3>
<ul>
  {% for row in thin %}
  <li><a href="{{ row.url }}">{{ row.name }}</a>：{{ row.dismissed }}/{{ row.triggered }}</li>
  {% endfor %}
</ul>
{% endif %}

<h2>本窗口已处置</h2>
{% for row in handled %}
<div class="row">
  <a href="/alerts/{{ row.id }}">{{ row.at }}</a>
  {{ row.rule_name }} —
  {% if row.url %}<a href="{{ row.url }}">{{ row.label }}</a>{% else %}{{ row.label }}{% endif %}
</div>
{% else %}
<p class="muted">这个窗口里还没有处置记录。</p>
{% endfor %}

<h2>本窗口批注</h2>
{% for note in notes %}
<div class="row">
  {{ note.at }} · {{ note.target }}{% if note.tags %} · {{ note.tags }}{% endif %}：{{ note.body }}
</div>
{% else %}
<p class="muted">这个窗口里还没有批注。</p>
{% endfor %}

<details>
  <summary>统计口径（分子/分母都标出来）</summary>
  <table>
    {% for label, value, note in rate_rows %}
    <tr><td>{{ label }}</td><td>{{ value }}</td><td class="muted">{{ note }}</td></tr>
    {% endfor %}
  </table>
  <h3>动作类型分布</h3>
  <ul>
    {% for label, count in action_rows %}<li>{{ label }}：{{ count }}</li>{% endfor %}
    {% if legacy_handled %}<li>旧版（无动作类型）：{{ legacy_handled }}</li>{% endif %}
    {% if not action_rows and not legacy_handled %}<li class="muted">窗口内没有处置。</li>{% endif %}
  </ul>
  <h3>忽略原因分布</h3>
  <ul>
    {% for label, count in dismiss_rows %}<li>{{ label }}：{{ count }}</li>{% endfor %}
    {% if legacy_dismiss %}<li>旧版（无原因码）：{{ legacy_dismiss }}</li>{% endif %}
    {% if not dismiss_rows and not legacy_dismiss %}<li class="muted">窗口内没有忽略。</li>{% endif %}
  </ul>
</details>
{% endif %}
{% endblock %}
```

（批注行是纯文本：本计划没有批注详情页路由，Task 1/2 建的是面板 fragment——
清单里链到一个不存在的页面是假链接，等计划 5 的 `/annotations` 搜索页再链。）

`src/sams/web/pages/alerts.py`：import 行把 `RedirectResponse` 扩成
`from fastapi.responses import PlainTextResponse, RedirectResponse`，文件末尾加：

```python
@router.get("/api/partials/open-count")
def open_count(request: Request):
    """导航徽标的那一个数：htmx 定期取，不整页刷新。只回一个数——这就是这张卡的最小形状。

    口径与 `/review` 顶部、`latest_open` 同一条（`new + acknowledged`、不含测试，
    D124/D131）：徽标和复盘说的必须是同一件事。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    return PlainTextResponse(str(ctx.alerts_store.count_open(user.id)))
```

`src/sams/web/app.py`：import 行与 `include_router` 各加一处 `review`（按字母序排在
`portfolio` 与 `rules` 之间）。

`src/sams/web/templates/base.html`：导航那一行改为——

```html
      <a href="/alerts">告警<span id="open-count" class="badge" hx-get="/api/partials/open-count" hx-trigger="load, every 60s"></span></a>
      <a href="/review">复盘</a>
```

（替换原来的 `<a href="/alerts">告警</a>` 一行，并在其后、`<a href="/settings">` 之前
插入复盘那行。）

- [ ] **Step 10: 跑一遍确认通过**

Run: `uv run pytest -q tests/web/test_review_web.py tests/storage/test_alerts_review_queries.py tests/services/test_review_stats.py`
Expected: PASS（14 + 6 + 7 条）。注意 `test_overdue_lists_old_not_fresh` 与
`test_seven_day_window_includes_the_sixth_midnight` 用真实时钟：如果恰好跨过本地
午夜，`sixth` 的算式取的是页面同一枚 `datetime.now(tz)`，两边同源不会漂。

- [ ] **Step 11: Commit**

```bash
git add src/sams/storage/repos/alerts.py src/sams/storage/repos/annotations.py src/sams/services/annotations.py src/sams/services/review_stats.py src/sams/web/pages/review.py src/sams/web/templates/review.html src/sams/web/app.py src/sams/web/pages/alerts.py src/sams/web/templates/base.html tests/storage/test_alerts_review_queries.py tests/services/test_review_stats.py tests/web/test_review_web.py tests/storage/test_annotations_repo.py tests/services/test_annotations_service.py
git commit -m "feat(web): /review 复盘工作台——窗口查询、口径纯函数与导航徽标（§6.4/D135）"
```

---

## Task 6: 拔除旧路——删除 `AlertRepo.set_status`、迁移测试调用点、README 备份一节

**Files:**
- Modify: `src/sams/storage/repos/alerts.py`（删 `set_status` 方法；修 `AlertRepo.get` 的 docstring）
- Modify: `tests/storage/test_alerts_repo.py`（约 16 处：迁移调用 7、整条删除 6、undo 旧版改写 2、docstring 1）
- Modify: `tests/cli/test_alert_cli.py`、`tests/test_adversarial_rules.py`、`tests/services/test_health.py`
- Modify: `README.md`（新增「备份与附件（M4-B）」一节）

Task 4 已经把 `/acted`/`/dismiss` 两个端点迁到 `ReviewService`，`set_status` 的**产品**
调用方归零——能拔的前提在这里。剩下的全是测试：一部分是当年为 `set_status` 写的行为
测试，那份行为今天由 `act`/`dismiss`/`undo` 各自测过（同文件里的 567-935 行区段），
这些删掉；另一部分只是「借它把状态推到 handled/dismissed」的装配步骤，这些改成调用
新命令。两处 undo 的旧版用例不删：它们伪造的「没有事件的终态」从今天起**没有任何
命令能造出来**，这正是测试要钉住的形状，改用直接 UPDATE 造。

`UserRepo.set_status`（用户封禁）同名不同物，一根手指都不要碰。

- [ ] **Step 1: 巡一遍调用方，确认 src 侧只剩定义本身**

```bash
grep -rn "\.set_status(" src --include='*.py' | grep -v __pycache__
```

Expected: 无输出（`src/sams/storage/repos/alerts.py` 里的 `def set_status` 是定义不是
调用；若 `web/pages/alerts.py` 还有漏网的调用，说明 Task 4 没迁干净——按 Task 4 的
形状补掉再往下走）。文档里「讲它」的叙述（历史注释）不在此列，随删除一起清理。

- [ ] **Step 2: 迁移 `tests/storage/test_alerts_repo.py`**

逐处替换（行号是迁移前的，仅作定位用）：

1. `test_list_for_filters_by_status`：

```python
    repo.act(alert.id, user_id=seeded_user, action_kind="other", note="已手动复核")
```

2. `test_set_status_rejects_an_unknown_one` —— **整条删除**。那句「不接受」的话随
   方法一起没了；未知状态的闸在 `list_for`（那里测过），`action_kind`/`dismiss_code`
   的闸由 `test_act_requires_an_action_kind` 与 `test_dismiss_requires_a_code` 测着。
3. `test_latest_open_...` 里 `set_status(first.id, HANDLED, ...)`：

```python
    repo.act(first.id, user_id=seeded_user, action_kind="watch")
```

4. `test_latest_open_returns_none_once_the_last_one_is_dismissed`：

```python
    repo.dismiss(got.id, user_id=seeded_user, code="expected")
```

（原句 `note="误报"` 交给原因码：`expected` 不强制写 reason，语义仍是「这条被关了」。）

5. `test_the_three_columns_a_human_writes_come_back_out` 整条改为（docstring 原样保留，
   只是把 `set_status` 换成 `act`——「三列读得回来」这件事仍归 `handled_at`/`handle_note`）：

```python
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.act(alert.id, user_id=seeded_user, action_kind="other", note="已挂条件单")
```

6. `test_a_dismissal_keeps_its_reason_in_its_own_column` —— **整条删除**：同一件事
   （忽略把说明留在自己那一列、不串进 handle_note）由 `test_dismiss_records_the_code_and_reason` 测。
7. `test_dismissing_without_a_reason_is_refused` 与 `test_a_blank_reason_is_not_a_reason` ——
   **整条删除**：旧 §5「dismissed 必须带 reason」已被 §8.5 的原因码取代——并非所有
   忽略都要写句子（`expected` 可以只选码），要写的那两种由
   `test_misreport_and_other_need_a_reason` 测。留着这两条等于把废弃口径钉进测试。
8. `test_someone_elses_alert_is_not_yours_to_move`：

```python
    with pytest.raises(StorageError, match="不属于"):
        repo.act(alert.id, user_id=other_user, action_kind="other", note="手滑")
```

9. `test_changing_your_mind_replaces_the_previous_sentence` —— **整条删除**：终态之间
   不再互相覆盖（`test_a_terminal_alert_refuses_a_second_action` 与
   `test_a_terminal_alert_refuses_a_dismiss`）；「改主意」的新路是 5 分钟内撤销，
   undo 区段测着。
10. `test_a_known_status_still_needs_a_known_alert`：

```python
    with pytest.raises(StorageError, match="不存在"):
        repo.act("ghost", user_id=seeded_user, action_kind="other", note="x")
```

11. `test_handling_does_not_stamp_a_moment_for_a_new_one` —— **整条删除**：
    「handled 之后退回 new 会把 handled_at 抹掉」今天由 undo 测
    （`test_undo_returns_to_the_previous_state_and_clears_the_conclusion`），
    而「set_status 回退 new」这条路本身没有了。
12. `test_get_reads_any_row_when_no_user_is_given` 的 docstring（约 459 行）里
    「`record`/`set_status` 内部那次读靠的就是它」改为
    「`create`/`act`/`dismiss` 内部那次读靠的就是它」。
13. `test_acknowledge_refuses_a_closed_alert`：

```python
    repo.act(alert.id, user_id=seeded_user, action_kind="watch")
```

14. 两处 undo 的旧版用例（`test_undo_refuses_when_a_legacy_close_is_newer_than_the_recorded_action`
    与 `test_undo_needs_a_recorded_transition`）：把

```python
    repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="旧入口")
```

各改为（两条签名都要补 `clock` 参数——`repo` 夹具已带 `clock`，测试自己加一个即可）：

```python
    repo.db.execute(
        "UPDATE alerts SET status = ?, handled_at = ?, handle_note = ? WHERE id = ?",
        [HANDLED, clock.now_utc(), "旧版关法", alert.id],
    )
```

    前一条的 docstring 里「然后旧入口关了它」改成「然后一条旧版关法把它关了」；
    后一条的 docstring 改为「旧版关法不写事件：那条告警没有可信的上一态，不许猜。」
    断言原样（`Conflict`，`match="动作记录"`）。

- [ ] **Step 3: 迁移其余三个测试文件**

`tests/cli/test_alert_cli.py` 三处：

```python
    container.alerts_store.act(first.id, user_id=uid(container), action_kind="other", note="已复核")
```

```python
    container.alerts_store.act(alert.id, user_id=uid(container), action_kind="watch")
```

（原 104 与 118 两处同款——本来就没写 note，换成 `watch` 正好是不带说明的动作。）

`tests/test_adversarial_rules.py` 两处：

```python
    h.alerts.act(open_row.id, user_id=USER, action_kind="other", note="已挂条件单")
```

```python
    h.alerts.dismiss(open_row.id, user_id=USER, code="misreport", reason="除权造成的假跌破")
```

（第二条的原断言 `kept.dismiss_reason == "除权造成的假跌破"` 与
`kept.handle_note is None` 原样保留——`dismiss` 写的正是 `dismiss_reason` 那一列。）

`tests/services/test_health.py`：`make_alerts` helper 里的

```python
        elif status != "new":
            container.alerts_store.set_status(
                alert.id, status, user_id=user.id, note="测试"
            )
```

改为分支到两条新命令（`handled` 要说明、`dismissed` 要原因码，各给各的）：

```python
        elif status == "handled":
            container.alerts_store.act(
                alert.id, user_id=user.id, action_kind="other", note="测试"
            )
        elif status == "dismissed":
            container.alerts_store.dismiss(
                alert.id, user_id=user.id, code="other", reason="测试"
            )
```

同文件 `test_a_dismissed_alert_needs_no_channel_to_leave_the_backlog` 里那处：

```python
    container.alerts_store.dismiss(
        made[0].id, user_id=made[0].user_id, code="not_applicable", reason="不需要通道也能销掉"
    )
```

- [ ] **Step 4: 删除方法并跑受影响文件**

`src/sams/storage/repos/alerts.py`：删掉整个 `def set_status(...)` 方法——从
`    def set_status(`（约 659 行）到 `_to_alert` 之前的 `        return moved`
（约 704 行），含其中间那条「已读请走 acknowledge()」的拒绝话。再把
`AlertRepo.get` docstring 里「因为 `record`/`set_status` 内部那次读」改成
「因为 `create`/`act`/`dismiss` 内部那次读」。

Run:

```bash
uv run pytest -q tests/storage/test_alerts_repo.py tests/cli/test_alert_cli.py tests/test_adversarial_rules.py tests/services/test_health.py
```

Expected: PASS（删除 6 条后 `test_alerts_repo.py` 的条数比迁移前少 6——这是预期的少）

- [ ] **Step 5: README 加「备份与附件」**

在 `README.md` 的「## 结构」之前插入：

```markdown
## 备份与附件（M4-B）

数据仍然是一个 DuckDB 文件，但批注的附件不在库里：它们落在 `attachments_root`——
`config.toml` 里 `[annotations] attachments_dir` 给的路径，没给就是库文件同级的
`attachments/` 子目录。**备份 = 把库文件与那个目录一起打包**：批注正文在库里、图片在
库外，只备库等于删图——行还在、缩略图全指到空文件上（面板会把这种行标成「文件缺失」，
但备份里没有第二次机会）。

| 键（`[annotations]`） | 默认 | 意思 |
|---|---|---|
| `attachments_dir` | 库文件旁的 `attachments/` | 附件根目录 |
| `max_attachments` | 5 | 每条批注最多几个附件 |
| `max_attachment_mb` | 5 | 单个附件上限（MB） |
| `max_user_total_mb` | 512 | 每用户附件总量上限（MB） |

`sams db prune` 顺带清理孤儿文件与软删过期的附件。恢复时把库与附件目录恢复到
**同一时刻**（先停服务再拷两边）：错开的那一侧要么库指着不存在的文件，要么文件在
库里没有行。
```

- [ ] **Step 6: 残留检查、lint 与 Commit**

```bash
grep -rn "\.set_status(" src tests --include='*.py' | grep -v __pycache__ | grep -v users
uv run ruff check --no-cache src tests
```

Expected: 第一条无输出（`UserRepo.set_status`/`users` 那几处是封禁路，不属于本任务）；
ruff 全绿。

```bash
git add src/sams/storage/repos/alerts.py tests/storage/test_alerts_repo.py tests/cli/test_alert_cli.py tests/test_adversarial_rules.py tests/services/test_health.py README.md
git commit -m "refactor(alerts): 拔除 set_status 旧路——测试调用点迁到 act/dismiss/undo，README 补备份一节"
```

---

## 收尾（控制器执行，不属于任何单个任务）

全部任务合完之后、推送之前，由控制器一趟做完：

- [ ] `uv run pytest -q`（全量）—— 全绿；任何红点回到对应任务修，不许跳过或改测试凑数。
- [ ] `uv run ruff check --no-cache src tests` —— 全绿。
- [ ] `git status` —— 未跟踪只允许 `qodercli_resume.sh` 与 `tmp/`；出现别的文件先弄清来源。
- [ ] 真实冒烟（尽力而为，缺环境不阻塞）：`uv run sams serve` 起 web，登录 → `/review` →
      建一条带附件批注 → 发一条测试告警 → 收件箱处置 → 5 分钟内撤销一次。
- [ ] `git push origin main`（只有控制器执行推送）。

## 与 spec 的对账单

| spec 条目 | 落点 | 偏差（写进代码注释/上面的任务正文） |
|---|---|---|
| §6.2 批注面板（读/写/预览/编辑/置顶/删除/限速/空状态） | Task 1、Task 2 | 行内编辑器 = 重渲染整块 fragment（无 JS 折叠） |
| §6.2 附件缩略图与下载（§4.3 授权） | Task 3 | 下载走 `AttachmentService.resolve`，越权与缺失同为 404 |
| §6.3 详情页上下文区/动作区/事件历史/撤销/稍后 | Task 4 | trade 的引用由服务端铸（`act_with_fill`），表单只收数字段 |
| §6.3 处置「同时写一条标的批注」预填 | Task 4 `_sync_text` | 正文在服务层之外组装，`sync_annotation` 收成品 |
| §8.2 忽略原因码进表单与统计 | Task 4、Task 5 | — |
| §6.4 `/review` 首屏清单、三个顶部数字、折叠区带分子分母 | Task 5 | 快捷闭环只内联「已读」；处置/忽略/稍后一行一个入口（详情页） |
| §8.4 复盘口径全部纯函数化 | Task 5 `review_stats` | 旧版 dismiss 进状态口径比率、不进分布（按原文「不并进 other」执行） |
| §6.1 导航「复盘」与未处理徽标 | Task 5 | 徽标 htmx 60 秒轮询，只取一个数 |
| §9/拔旧路：`AlertRepo.set_status` 删除 | Task 6 | — |
| 旅程 §2「闭环后回下一条未处理」 | 不做（计划头已声明） | 闭环仍回详情页 |
| `visibility=team` 写入口、`/admin/annotations`、`/annotations` 搜索、附件编辑增删 | 计划 5 | 面板只建私有批注（服务层恒私有，闸不在 web 层补） |

---

**计划完。** 执行方式：subagent-driven——每个任务一个全新 implementer（带全文任务文本
与本计划的设计注释），spec-compliance 评审过了再走 code-quality 评审，控制器跑全量并
提交推送；连续执行不中途停顿。
