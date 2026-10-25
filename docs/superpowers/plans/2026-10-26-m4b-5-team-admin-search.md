# M4-B 计划 5：团队可见、批注搜索、附件编辑与 admin break-glass 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M4-B spec 里 B2 剩下的四件事做完——symbol 批注的团队可见写路径、`/annotations` 全局搜索页、编辑批注时增删附件、`/admin/annotations` 元数据页 + 策略开关 + D139 break-glass——然后同步总 spec §14，把系统真跑起来交给交易专家试用。

**Architecture:** 全部长在同一棵已经立好的树上：可见性写闸进 `AnnotationService`（策略读 `AppSettingsRepo`，5 秒缓存那条不动）；break-glass 是新仓库 `admin_access`（表由 0020 铺好）+ 新服务 `BreakGlassService`；搜索复用 `list_visible` 的可见性 SQL（抽成一份 clause 构造器，加 `team_only` 与 `count_visible`）；附件编辑给 `AttachmentService.remove` 与 revision 补写 meta；admin 页是新文件 `pages/admin_annotations.py`（字母序注册，`require_admin` 认人）。

**Tech Stack:** Python 3.13 / FastAPI + Jinja2 + htmx / DuckDB（单写者，D113）/ uv / pytest / ruff（E,F,I,UP,B,SIM，line-length 100，CJK 按 1 字符）。

**依据 spec:** `docs/superpowers/specs/2026-10-23-m4b-annotations-review-design.md`（§4.2 可见性与策略、§4.5 搜索、§5.4 附件删除、§6.1 路由表、D121/D127/D136/D139）。

## 全局约束（每个任务都适用）

- 只用 uv：`uv run pytest -q <files>`；永不 pip/venv。**永不 `ruff format`**；lint 只 `uv run ruff check --no-cache src tests`。
- git 用 plain Bash（工作目录=仓库根）；`git add <显式文件>`，绝不 `-A`/`.`；不 amend、不 push（推送是控制器收尾的事）；未跟踪 `qodercli_resume.sh`、`tmp/` 永远不入库。
- 仓库调用只在端点体内走 `web.contexts.current()`（D113）；layering 测试不许 web→monitor。
- 注释/docstring 中文、解释 WHY；模板不做算术与时刻换算（Python 折好再传）。
- 每个任务：先写红测试 → 实现转绿 → 只跑自己的文件 + `uv run pytest -q tests/web`（回归）→ ruff → 按计划给的 `git add`/`git commit` 提交。
- 计划里的代码块是逐字规格；与真实源码冲突时以源码为准做**最小**适配，并在自报里逐条列偏差。

## 已核实的地面真值（不用再查）

- `src/sams/storage/repos/annotations.py`：常量 `PRIVATE`/`TEAM`/`SYMBOL`/`TARGET_TYPES`/`_ANNOTATION_COLUMNS`/`_TAG_MATCH`/`_json_dict`；`Annotation` dataclass 字段 `(id, user_id, target_type, target_ref, target_label, body_md, tags, pinned, visibility, created_at, updated_at, deleted_at=None, deleted_by=None, purged_at=None)`；`list_visible(viewer_id, *, team_sharing=False, mine_only=False, target_type=None, target_ref=None, q=None, tag=None, since=None, limit=20, offset=0)`（clause 顺序：`mine_only or not team_sharing → user_id = ?`，否则 `user_id = ? OR (visibility='team' AND target_type='symbol')`；`ORDER BY pinned DESC, created_at DESC, id DESC`）；repo 的 `get(annotation_id, *, include_deleted=False)`（默认把软删与已清除行都折成 None——问墓碑必须 `include_deleted=True`）；`revisions(annotation_id)` 返回 `Revision(id, annotation_id, user_id, before_md, after_md, meta, edited_at)`；写侧 `_reject_team_on_non_symbol` 在 `create`/`update` 里都有。
- `src/sams/services/annotations.py`：`AnnotationService.__init__(annotations, users, symbols, theses, rules, alerts, accounts, orders, settings)`；`create` 现在硬写 `visibility=PRIVATE`（docstring 里写明「B2 接入策略后由同一个服务放宽写入口」——本计划就是 B2）；`_readable` 与 `list_visible` 读侧已支持 team。
- `src/sams/storage/repos/app_settings.py`：`TEAM_SHARING = "annotations.team_sharing"`、`ALLOWED_KEYS`、`team_sharing() -> bool`（5 秒缓存）、`set(key, value, *, updated_by=None)`。
- `src/sams/storage/repos/attachments.py`：`Attachment(id, annotation_id, user_id, filename, media_type, size_bytes, sha256, storage_path, position, created_at)`；`get`、`list_for(annotation_id)`、`count_for(annotation_id)`、`total_bytes(user_id)`、`delete(attachment_id) -> bool`、`delete_for`、`iter_all`。
- `src/sams/services/attachments.py`：`AttachmentService.add(user_id, annotation_id, *, filename, data)`（配额与张数闸都在里面）、`list_for(viewer_id, ..., team_sharing=)`、`resolve(...)`、`path_of(att)`、`load(att)`、`missing_count()`；`self.root`、`self.clock`。
- `src/sams/storage/migrations/0020_annotations_review.sql` 已铺表：
  ```sql
  CREATE TABLE IF NOT EXISTS admin_access_log (
      id VARCHAR PRIMARY KEY, admin_user_id VARCHAR NOT NULL,
      annotation_id VARCHAR NOT NULL, owner_user_id VARCHAR NOT NULL,
      action VARCHAR NOT NULL DEFAULT 'break_glass', reason VARCHAR NOT NULL,
      granted_until TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
  ```
  （没有任何 Python 代码读它——本计划建仓库与服务。）
- `src/sams/bootstrap.py`：`ServiceContext` 字段里 `annotations_store/annotations/attachments_store/attachments/annotation_pruner/app_settings/...`；构造段 `annotations_store = AnnotationRepo(...)` → `annotations = AnnotationService(annotations_store, users, symbols, theses_store, rules_store, alerts_store, sim_accounts, sim_orders, cfg)` → `app_settings = AppSettingsRepo(database, clock=clock)`（在 annotations 服务**之后**构造——Task 1 要把它挪前）。
- `src/sams/web/pages/annotations.py`：`_render_panel(request, *, user, target_type, target_ref, error="", draft=None, status_code=200)`（内部 `sharing = ctx.app_settings.team_sharing()`；rows 的 `visibility` 已是中文标签「团队/私有」；`PANEL_LIMIT`、`THUMB_LIMIT=3`）；`annotation_create` 用 `Form(...)` + `files: list[UploadFile | str] = _files_field`；`annotation_edit` 用 `(body_md, tags, updated_at)` 三个 Form；`_load_for_write` 认作者；`_limit_hit(web, user.id, uploads=len)` 走 `_limit_lock`；`audit(request, action=..., target=..., ok=..., detail=...)` 在 `web/auth.py`（日志表不建表）。
- `src/sams/web/auth.py`：`require_user` 设 `request.state.sams_user`；`require_admin(request)` 存在（admin.py 在用，返回 admin 用户；非管理员走它自己的拒绝路径——照 `tests/web/test_admin_web.py` 的断言风格测）。
- `src/sams/web/templates/partials/annotations.html`：创建表单里那行 `{# 计划 4 留下的占位话 #}` `<p class="muted">可见范围：仅自己（团队共享在计划 5 开放）。</p>` 就是 Task 1 要替换的位置；编辑表单在 `<details><summary>编辑</summary>` 内，当前只有 textarea + tags + hidden updated_at。
- `src/sams/web/templates/base.html` 导航段（Task 3 插「批注」）：
  ```html
  <a href="/review">复盘</a>
  <a href="/settings">设置</a>
  ```
- `src/sams/web/app.py`：`from sams.web.pages import (...)` 与 `app.include_router(...)` 都按字母序（`admin` 与 `alerts` 之间插 `admin_annotations`；Task 3 不加新 router）。
- `src/sams/storage/repos/users.py`：`list_users() -> list[User]`、`get(user_id)`、`get_by_username`。
- `src/sams/web/pages/review.py` 的 `_ref_of`/`_rule_rows`、`src/sams/web/display.py` 的 `moment_label`——搜索页与 admin 页复用同款折法。
- CLI：`sams user add <name> --password <pw> [--admin]`（`src/sams/cli/user.py:31`）。
- **纯删除补记（2026-10-26 已核实，覆盖下文所有「按真名改」的括号）**：
  - `AnnotationRepo` **没有** `purge()`——`purged_at` 只由 `services/annotation_prune.py:88` 的裸 SQL 写。测试里造墓碑用 `repo.db.execute("UPDATE annotations SET purged_at = ? WHERE id = ?", [clock.now_utc(), made.id])`。
  - `ServiceContext` 字段名确认：`settings/clock/users/sim_accounts(SimAccountRepo)/sim_ledger(SimOrderRepo)/theses_store/rules_store/alerts_store/annotations_store/attachments_store/app_settings`——Task 3/5 端点代码里这些名字就是最终名，不用再 grep。
  - 测试时钟 `FrozenClock` 在 `tests/support.py`（不是 `sams.core.clock`），import 走 `from support import FrozenClock`（与 `tests/datasource/*` 同款）；推进用 `clock.advance(minutes=…)`。
  - 附件测试文件真名：`tests/services/test_attachments_service.py`、`tests/storage/test_attachments_repo.py`（都在）。
  - `render_markdown` 在 `sams/services/markdown.py`；`form_data` 在 `sams/web/forms.py:19`（async，返回 `dict[str, list[str]]`）；`new_id` 在 `sams/storage/ids.py`；`User.display_name` 存在（users.py:43）。
- 配置：`config/settings.toml` 的 `db_path = "data/sams.duckdb"`；secret 走环境变量 `SAMS_SECRET_KEY`；`uv run sams serve --host 0.0.0.0 --port <p>`。

---

## Task 1: 团队可见写路径（服务闸 + 面板选择器 + bootstrap 接线）

**Files:**
- Modify: `src/sams/services/annotations.py`（构造参数 + `create`/`edit` 的 visibility + `_gate_team`）
- Modify: `src/sams/bootstrap.py`（`AppSettingsRepo` 构造挪到 `AnnotationService` 之前并传入）
- Modify: `src/sams/web/pages/annotations.py`（create/edit 端点收 `visibility`；`_render_panel` 传 `team_sharing`；rows 加 `visibility_code`）
- Modify: `src/sams/web/templates/partials/annotations.html`（可见性选择器 + 关闭提示 + 行徽标）
- Modify: `tests/services/test_annotations_service.py`（fixture 补 `app_settings` + 新用例）
- Modify: `tests/web/test_annotations_web.py`（新用例）

- [ ] **Step 1: 服务层红测试**

`tests/services/test_annotations_service.py`：`world` 夹具（该文件 30-65 行，构造 `AnnotationService(...)` 用位置参数、末位 `settings`）补两处——构造前一行加 `app_settings = AppSettingsRepo(db, clock=clock)`（import `from sams.storage.repos.app_settings import AppSettingsRepo`），`AnnotationService(...)` 末尾加 `app_settings`，`SimpleNamespace` 里加 `app_settings=app_settings`、`annotations=service`（`service` 已在，加个别名让新用例读起来像 API；旧用例不动）。模块常量已核实：用户 id 用 `USER`/`OTHER`，thesis 用文件里的 `make_thesis(world)` 助手。新用例：

```python
def test_team_write_needs_the_policy_on(world):
    """D127：off 时新写 team 被拒——话要说清是谁关的开关管不着这条草稿能存。"""
    world.app_settings.set("annotations.team_sharing", "off")
    with pytest.raises(ValidationError, match="团队共享当前已关闭"):
        world.annotations.create(
            USER, target_type="symbol", target_ref="A:600519",
            body_md="共享草稿", visibility="team",
        )


def test_team_write_on_symbol_flips_visibility(world):
    made = world.annotations.create(
        USER, target_type="symbol", target_ref="A:600519",
        body_md="给团队看", visibility="team",
    )
    assert made.visibility == "team"


def test_team_on_non_symbol_still_refused(world):
    ref = make_thesis(world)
    with pytest.raises(ValidationError, match="只对 symbol 生效"):
        world.annotations.create(
            USER, target_type="thesis", target_ref=ref,
            body_md="绕过 UI 也写不进去", visibility="team",
        )


def test_edit_flip_to_team_refused_when_off(world):
    made = world.annotations.create(
        USER, target_type="symbol", target_ref="A:600519", body_md="先私有"
    )
    world.app_settings.set("annotations.team_sharing", "off")
    with pytest.raises(ValidationError, match="团队共享当前已关闭"):
        world.annotations.edit(USER, made.id, body_md="先私有", visibility="team")


def test_edit_may_keep_existing_team_row_when_off(world):
    """关策略不拦「保持原样」：§4.2 说既有 team 行只是对非作者隐藏，不是不能碰。"""
    made = world.annotations.create(
        USER, target_type="symbol", target_ref="A:600519",
        body_md="先共享", visibility="team",
    )
    world.app_settings.set("annotations.team_sharing", "off")
    kept = world.annotations.edit(
        USER, made.id, body_md="改一个字", visibility="team"
    )
    assert kept.visibility == "team"
```

（`make_thesis(world)` 返回值若不是纯 id 串而是对象，按该文件其它 thesis 用例的取 ref 写法照抄——**「只对 symbol 生效」这句断言的是仓库 `_reject_team_on_non_symbol` 的话**，服务层不重复造这句话，让仓库的 ValidationError 冒泡即可；若仓库原话不是这几个字，match 改仓库原话，写进自报。）

Run: `uv run pytest -q tests/services/test_annotations_service.py -k team` → FAIL（create 没有 visibility 参数 / 没有闸）。

- [ ] **Step 2: 实现服务层**

`src/sams/services/annotations.py`：

1. 构造器加参数（放最后一个，带默认=None 反而危险：**必填**，让漏接线在启动就炸）：

```python
    def __init__(
        self,
        annotations: AnnotationRepo,
        users: UserRepo,
        symbols: SymbolRepo,
        theses: ThesisRepo,
        rules: RuleRepo,
        alerts: AlertRepo,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        settings: Settings,
        app_settings: AppSettingsRepo,
    ) -> None:
        ...
        self.app_settings = app_settings
```

import `from sams.storage.repos.app_settings import AppSettingsRepo`；`PRIVATE`/`TEAM` 从 repos.annotations 一并 import（`TEAM` 新加）。文件头 docstring 里那句「B1 只开私有批注……B2 接入策略后由同一个服务放宽写入口」改为陈述现在的事实：写入口已放宽，闸在 `_gate_team`（策略关时新写 team 被拒；保持既有 team 不变不拦）。

2. `create` 加 `visibility: str = PRIVATE` 形参（keyword-only，`tags` 之后），传进 `self.annotations.create(..., visibility=...)`；调用前 `self._gate_team(visibility, target_type)`。

3. `edit` 加 `visibility: str | None = None`：

```python
        wanted = made.visibility if visibility is None else (visibility or made.visibility)
        # 闸只管「从私有翻成团队」这一跳：关策略后保持原样的 team 行还能编辑，
        # 否则人不改一个字都保存不了——§4.2 说的「不删、只是隐藏」就落空了
        if wanted == TEAM and made.visibility != TEAM:
            self._gate_team(wanted, made.target_type)
```

并把 `update(..., visibility=wanted)`（原来是 `made.visibility`）。

4. 私有闸：

```python
    def _gate_team(self, visibility: str, target_type: str) -> None:
        """策略关着就不许**新写** team（D127）；非标的 team 由仓库那同一条闸拒（D121）。"""
        if visibility == TEAM and target_type == SYMBOL and not self.app_settings.team_sharing():
            raise ValidationError(
                "团队共享当前已关闭：这条只能先写成私有，开关在管理后台随时能再打开"
            )
```

5. `bootstrap.py:515`：`app_settings = AppSettingsRepo(database, clock=clock)` 整行挪到 `annotations_store = AnnotationRepo(...)` 之前；`AnnotationService(...)` 调用末尾加 `app_settings`。**直接构造点已全部查明（5 处，逐个补参）**：`src/sams/bootstrap.py:515`、`tests/services/test_annotations_service.py:43`（`world` 夹具，顺手把 `app_settings` 挂进返回的 SimpleNamespace）、`tests/services/test_attachments_service.py:59`、`tests/services/test_annotation_prune.py:50`、`tests/services/test_review_service.py:110`——后三处传 `AppSettingsRepo(db, clock=clock)` 即可（它们不测策略闸）。

Run: `uv run pytest -q tests/services/test_annotations_service.py` → 全绿。

- [ ] **Step 3: web 层红测试**

`tests/web/test_annotations_web.py` 加（`logged` 夹具同文件既有用法）：

```python
def test_symbol_panel_offers_visibility(logged):
    page = logged.client.get(
        "/api/partials/annotations",
        params={"target_type": "symbol", "target_ref": "A:600519"},
    )
    assert 'name="visibility"' in page.text
    assert "团队可见" in page.text


def test_thesis_panel_hides_visibility(logged, ...):
    # 非 symbol 目标：D121——不给选择器，那句「仅自己」的说明替代它
    assert 'name="visibility"' not in page.text
    assert "仅自己" in page.text


def test_create_team_flips_badge(logged):
    resp = logged.client.post("/annotations", data={
        "csrf": logged.csrf, "target_type": "symbol", "target_ref": "A:600519",
        "body_md": "给团队", "visibility": "team",
    })
    assert resp.status_code == 200
    assert '<span class="badge">团队</span>' in resp.text


def test_create_team_refused_when_policy_off(logged):
    logged.context.app_settings.set("annotations.team_sharing", "off")
    resp = logged.client.post("/annotations", data={
        "csrf": logged.csrf, "target_type": "symbol", "target_ref": "A:600519",
        "body_md": "给团队", "visibility": "team",
    })
    assert resp.status_code == 400
    assert "团队共享当前已关闭" in resp.text


def test_team_row_sharing_closed_says_so(logged):
    made = logged.context.annotations.create(
        logged.context.users.get_by_username("alice").id,
        target_type="symbol", target_ref="A:600519",
        body_md="共享着", visibility="team",
    )
    logged.context.app_settings.set("annotations.team_sharing", "off")
    page = logged.client.get(
        "/api/partials/annotations",
        params={"target_type": "symbol", "target_ref": "A:600519"},
    )
    assert "团队共享当前已关闭" in page.text
    assert made.id in page.text  # §4.2：作者仍可见自己那条
```

（`test_thesis_panel_hides_visibility` 的 thesis 构造：用 `logged.context.theses` 或直接向服务 `create(target_type="rule", ...)`——挑该文件已有的非 symbol 目标用例写法照抄。）

Run: `uv run pytest -q tests/web/test_annotations_web.py -k "visibility or team"` → FAIL。

- [ ] **Step 4: 实现 web 层**

`src/sams/web/pages/annotations.py`：

- `annotation_create` 形参加 `visibility: str = Form("private")`；`draft` 加 `"visibility": visibility`；`ctx.annotations.create(..., visibility=visibility.strip() or "private")`。
- `annotation_edit` 形参加 `visibility: str = Form("")`；`ctx.annotations.edit(..., visibility=visibility.strip() or None)`。
- `_render_panel`：rows 里加 `"visibility_code": made.visibility`；`web.render(..., team_sharing=sharing)` 加这一个 kwarg。

`src/sams/web/templates/partials/annotations.html`：

创建表单里替换那行 `<p class="muted">可见范围：仅自己（团队共享在计划 5 开放）。</p>` 为：

```html
    {% if target_type == "symbol" %}
    <div class="ann-editor-row">
      <label>可见范围
        <select name="visibility">
          <option value="private">仅自己</option>
          <option value="team">团队可见</option>
        </select>
      </label>
      {% if not team_sharing %}<span class="muted">团队共享当前已关闭，选「团队可见」会被退回</span>{% endif %}
    </div>
    {% else %}
    <p class="muted">可见范围：仅自己——团队共享只对标的批注生效（观点、规则、告警、成交都是个人决策数据）。</p>
    {% endif %}
```

行徽标处：`<span class="badge">{{ row.visibility }}</span>` 之后加

```html
      {% if row.visibility_code == "team" and not team_sharing %}<span class="badge">团队共享当前已关闭</span>{% endif %}
```

编辑表单 `<input name="tags" ...>` 之后加（只有 symbol 给选择器）：

```html
          {% if target_type == "symbol" %}
          <select name="visibility">
            <option value="private"{% if row.visibility_code == "private" %} selected{% endif %}>仅自己</option>
            <option value="team"{% if row.visibility_code == "team" %} selected{% endif %}>团队可见</option>
          </select>
          {% else %}
          <input type="hidden" name="visibility" value="{{ row.visibility_code }}">
          {% endif %}
```

- [ ] **Step 5: 回归 + ruff + Commit**

```bash
uv run pytest -q tests/services/test_annotations_service.py tests/web/test_annotations_web.py tests/web
uv run pytest -q tests/services/test_annotation_context.py  # provider 也拿 app_settings，确认没被挪前的构造顺序波及
uv run ruff check --no-cache src tests
git add src/sams/services/annotations.py src/sams/bootstrap.py src/sams/web/pages/annotations.py src/sams/web/templates/partials/annotations.html tests/services/test_annotations_service.py tests/web/test_annotations_web.py
git commit -m "feat(annotations): 团队可见写路径——策略闸、面板选择器与关闭提示（§4.2/D121/D127）"
```

（若 Step 4 动了其它直接构造 `AnnotationService` 的文件，一并 `git add` 并写进自报。）

---

## Task 2: break-glass 底座（`AdminAccessRepo` + `BreakGlassService` + 接线）

**Files:**
- Create: `src/sams/storage/repos/admin_access.py`
- Create: `src/sams/services/admin_access.py`
- Modify: `src/sams/bootstrap.py`（ServiceContext 两枚字段 + 构造）
- Test: `tests/storage/test_admin_access_repo.py`（新）、`tests/services/test_break_glass.py`（新）

- [ ] **Step 1: 仓库红测试**

新建 `tests/storage/test_admin_access_repo.py`：

```python
"""`admin_access_log` 仓库（D139）：授权行只进不出，到期靠比时间。"""

from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.storage.repos.admin_access import AdminAccessRepo

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return FrozenClock(NOW)


@pytest.fixture
def repo(db, clock):
    return AdminAccessRepo(db, clock=clock)


def test_grant_writes_an_active_row(repo):
    log = repo.grant(
        admin_user_id="adm", annotation_id="ann-1", owner_user_id="own",
        reason="核对除权口径", granted_until=NOW + timedelta(minutes=15),
    )
    assert log.action == "break_glass"
    got = repo.active("adm", "ann-1", now=NOW)
    assert got is not None and got.id == log.id


def test_naive_until_is_refused(repo):
    with pytest.raises(Exception, match="带时区"):
        repo.grant(
            admin_user_id="adm", annotation_id="ann-2", owner_user_id="own",
            reason="裸时间", granted_until=datetime(2026, 10, 26, 9, 15),
        )


def test_expired_grant_is_not_active(repo):
    repo.grant(
        admin_user_id="adm", annotation_id="ann-3", owner_user_id="own",
        reason="核对", granted_until=NOW + timedelta(minutes=15),
    )
    assert repo.active("adm", "ann-3", now=NOW + timedelta(minutes=20)) is None


def test_active_is_per_admin(repo):
    repo.grant(
        admin_user_id="adm-1", annotation_id="ann-4", owner_user_id="own",
        reason="核对", granted_until=NOW + timedelta(minutes=15),
    )
    assert repo.active("adm-2", "ann-4", now=NOW) is None


def test_count_since_window(repo):
    for n in range(3):
        repo.grant(
            admin_user_id="adm", annotation_id=f"ann-{100 + n}", owner_user_id="own",
            reason="核对", granted_until=NOW + timedelta(minutes=15),
        )
    assert repo.count_since("adm", since=NOW - timedelta(hours=1)) == 3
    assert repo.count_since("adm", since=NOW + timedelta(minutes=1)) == 0


def test_list_recent_newest_first(repo, clock):
    for n in range(3):
        repo.grant(
            admin_user_id="adm", annotation_id=f"ann-{200 + n}", owner_user_id="own",
            reason="核对",
            granted_until=NOW + timedelta(minutes=15),
        )
        clock.advance(minutes=1)
    rows = repo.list_recent(limit=2)
    assert [r.annotation_id for r in rows] == ["ann-202", "ann-201"]
```

（`db` 是 `tests/conftest.py:21` 的全局内存库夹具（建表后交给用例）；`FrozenClock` 在 `tests/support.py`，推进用 `clock.advance(minutes=…)`——`grant` 的 `created_at` 取 `clock.now_utc()`，所以每 grant 一次推一分钟就能造出严格递增的三条。）

Run: `uv run pytest -q tests/storage/test_admin_access_repo.py` → FAIL（模块不存在）。

- [ ] **Step 2: 实现仓库**

新建 `src/sams/storage/repos/admin_access.py`：

```python
"""`admin_access_log` 的仓库（D139）：break-glass 授权的唯一事实来源。

只有 INSERT 与 SELECT：审计行能改就等于授权能被抹掉。「限时 15 分钟」不做定时
任务——读的时候比 `granted_until`，过期行留在表里当历史，重启也不丢。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

_COLUMNS = (
    "id, admin_user_id, annotation_id, owner_user_id, action, reason,"
    " granted_until, created_at"
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class AdminAccess:
    id: str
    admin_user_id: str
    annotation_id: str
    owner_user_id: str
    action: str
    reason: str
    granted_until: datetime
    created_at: datetime


class AdminAccessRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def grant(
        self,
        *,
        admin_user_id: str,
        annotation_id: str,
        owner_user_id: str,
        reason: str,
        granted_until: datetime,
    ) -> AdminAccess:
        """写一行授权。「至少 4 个字」是使用规则，归服务层；仓库只挡空串与裸时间。"""
        text = (reason or "").strip()
        if not text:
            raise ValidationError("break-glass 必须写理由：审计里没有「想看看」")
        if granted_until.tzinfo is None:
            raise ValidationError("granted_until 必须带时区：到没到期不能取决于服务器 locale")
        moment = self.clock.now_utc()
        self.db.execute(
            "INSERT INTO admin_access_log"
            " (id, admin_user_id, annotation_id, owner_user_id, action, reason,"
            "  granted_until, created_at)"
            " VALUES (?, ?, ?, ?, 'break_glass', ?, ?, ?)",
            [
                rid := new_id(),
                admin_user_id,
                annotation_id,
                owner_user_id,
                text,
                _utc(granted_until),
                moment,
            ],
        )
        return AdminAccess(
            id=rid, admin_user_id=admin_user_id, annotation_id=annotation_id,
            owner_user_id=owner_user_id, action="break_glass", reason=text,
            granted_until=_utc(granted_until), created_at=moment,
        )

    def _to_row(self, row: dict) -> AdminAccess:
        return AdminAccess(
            id=str(row["id"]), admin_user_id=str(row["admin_user_id"]),
            annotation_id=str(row["annotation_id"]),
            owner_user_id=str(row["owner_user_id"]), action=str(row["action"]),
            reason=str(row["reason"]),
            granted_until=_utc(row["granted_until"]), created_at=_utc(row["created_at"]),
        )

    def active(
        self, admin_user_id: str, annotation_id: str, *, now: datetime
    ) -> AdminAccess | None:
        """此刻仍然有效的那一条授权（最新的一个），没有就返回 None。"""
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM admin_access_log"
            " WHERE admin_user_id = ? AND annotation_id = ? AND granted_until > ?"
            " ORDER BY granted_until DESC, created_at DESC, id DESC LIMIT 1",
            [admin_user_id, annotation_id, _utc(now)],
        ).to_dicts()
        return self._to_row(rows[0]) if rows else None

    def count_since(self, admin_user_id: str, *, since: datetime) -> int:
        """限次闸用：这个管理员在 `since` 之后发起过几次授权。"""
        return int(
            self.db.scalar(
                "SELECT count(*) FROM admin_access_log WHERE admin_user_id = ?"
                " AND created_at >= ?",
                [admin_user_id, _utc(since)],
            )
            or 0
        )

    def list_recent(self, *, limit: int = 20) -> list[AdminAccess]:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM admin_access_log"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            [int(limit)],
        ).to_dicts()
        return [self._to_row(row) for row in rows]
```

（`rid := new_id()` 的 walrus 若惹 ruff/pylint 洁癖可先赋值再传——保持 line-length 100 内。`Database.execute` 返回值与 `scalar` 用法照抄 `repos/attachments.py`。）

Run: 6 passed。

- [ ] **Step 3: 服务红测试**

新建 `tests/services/test_break_glass.py`：

```python
"""break-glass 用例（D139）：单条、限时、限次、留痕——四条都在服务层钉。"""

from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import Conflict, NotFound, ValidationError
from sams.services.admin_access import GRANT_MINUTES, MAX_PER_HOUR, BreakGlassService
from sams.storage.repos.admin_access import AdminAccessRepo
from sams.storage.repos.annotations import AnnotationRepo

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return FrozenClock(NOW)


@pytest.fixture
def annotations_store(db, clock):
    return AnnotationRepo(db, clock=clock)


@pytest.fixture
def svc(db, clock, annotations_store):
    logs = AdminAccessRepo(db, clock=clock)
    return BreakGlassService(logs, annotations_store, clock=clock)
```

（`db` 全局夹具已在头部核实；批注行直接由仓库 `create` 铺——break-glass 问的是仓库行的归属，不需要服务层的策略闸。）

继续：

```python
def _own_annotation(annotations_store, user_id="u-1"):
    return annotations_store.create(
        user_id, target_type="symbol", target_ref="A:600519",
        target_label="贵州茅台", body_md="私有正文", tags=[], visibility="private",
    )


def test_reason_needs_four_chars(svc, annotations_store):
    made = _own_annotation(annotations_store)
    with pytest.raises(ValidationError, match="至少 4 个字"):
        svc.request_access("adm", made.id, "看看")


def test_unknown_annotation_has_no_body_to_read(svc):
    with pytest.raises(NotFound):
        svc.request_access("adm", "ghost", "核对除权口径")


def test_purged_tombstone_is_refused(svc, annotations_store, clock):
    made = _own_annotation(annotations_store)
    # 仓库没有 purge()：purged_at 只由 services/annotation_prune.py 的裸 SQL 写，
    # 测试照它的样子补一行（见头部「纯删除补记」）
    annotations_store.db.execute(
        "UPDATE annotations SET purged_at = ? WHERE id = ?",
        [clock.now_utc(), made.id],
    )
    with pytest.raises(Conflict, match="已被清除"):
        svc.request_access("adm", made.id, "核对除权口径")


def test_grant_lasting_the_window(svc, annotations_store):
    made = _own_annotation(annotations_store)
    log = svc.request_access("adm", made.id, "核对除权口径")
    assert log.granted_until == NOW + timedelta(minutes=GRANT_MINUTES)
    assert svc.can_read("adm", made.id, now=NOW)
    assert not svc.can_read("adm", made.id, now=NOW + timedelta(minutes=GRANT_MINUTES + 1))


def test_hourly_cap_per_admin(svc, annotations_store):
    ids = [_own_annotation(annotations_store, user_id=f"u-{n}").id for n in range(MAX_PER_HOUR + 1)]
    for annotation_id in ids[:MAX_PER_HOUR]:
        svc.request_access("adm", annotation_id, "核对除权口径")
    with pytest.raises(Conflict, match="每小时"):
        svc.request_access("adm", ids[MAX_PER_HOUR], "核对除权口径")
    # 限次是按管理员算的：换个人照样能批
    assert svc.request_access("adm-2", ids[MAX_PER_HOUR], "核对除权口径") is not None
```

（`purge` 的真实方法名：`grep -n "def purge" src/sams/storage/repos/annotations.py`——prune 那条路走的那个；按真名改。）

Run: `uv run pytest -q tests/services/test_break_glass.py` → FAIL（模块不存在）。

- [ ] **Step 4: 实现服务 + 接线**

新建 `src/sams/services/admin_access.py`：

```python
"""break-glass 用例（D139）：单条、15 分钟、每人每小时 5 次、理由 ≥4 字。

规则全在这一层，仓库只存行：审计的「能不能批」得能被没有 DB 的单测问出来。
不做批量：一屏一条的授权，才是「确需查看」四个字的形状。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import Conflict, NotFound
from sams.storage.repos.admin_access import AdminAccess, AdminAccessRepo
from sams.storage.repos.annotations import AnnotationRepo

MIN_REASON_CHARS = 4
GRANT_MINUTES = 15
MAX_PER_HOUR = 5


class BreakGlassService:
    def __init__(
        self,
        logs: AdminAccessRepo,
        annotations: AnnotationRepo,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.logs = logs
        self.annotations = annotations
        self.clock = clock

    def request_access(
        self, admin_user_id: str, annotation_id: str, reason: str
    ) -> AdminAccess:
        text = (reason or "").strip()
        if len(text) < MIN_REASON_CHARS:
            raise ValidationError(
                f"理由至少 {MIN_REASON_CHARS} 个字：写下为什么要看这条私有批注——它会进审计"
            )
        # 墓碑也要能问得到：默认 get 把 purged/deleted 行折成 None，那条「只剩墓碑」的
        # Conflict 就永远走不到——include_deleted 是这道判断的前提
        row = self.annotations.get(annotation_id, include_deleted=True)
        if row is None:
            raise NotFound("批注不存在：break-glass 只对真实存在的那一条生效")
        if row.purged_at is not None:
            raise Conflict("正文已被清除：这条批注只剩墓碑，没有可读的东西")
        now = self.clock.now_utc()
        used = self.logs.count_since(admin_user_id, since=now - timedelta(hours=1))
        if used >= MAX_PER_HOUR:
            raise Conflict(
                f"本小时已授权 {used} 次：每人每小时最多 {MAX_PER_HOUR} 条——"
                "批量排查请把问题写成一条批注让作者自己贴出来"
            )
        return self.logs.grant(
            admin_user_id=admin_user_id,
            annotation_id=annotation_id,
            owner_user_id=row.user_id,
            reason=text,
            granted_until=now + timedelta(minutes=GRANT_MINUTES),
        )

    def can_read(
        self, admin_user_id: str, annotation_id: str, *, now: datetime | None = None
    ) -> bool:
        return self.logs.active(admin_user_id, annotation_id, now=now or self.clock.now_utc()) is not None
```

`bootstrap.py`：import 两样；ServiceContext 在 `app_settings` 字段之后插

```python
    #: break-glass 授权仓库（D139）：表 0020 就铺好了，这里是它唯一的读者
    admin_access_store: AdminAccessRepo
    #: 单条限时授权的用例：admin 页的「能不能读这条正文」问的就是它
    break_glass: BreakGlassService
```

构造段在 `app_settings = ...` 之后加：

```python
    admin_access_store = AdminAccessRepo(database, clock=clock)
    break_glass = BreakGlassService(admin_access_store, annotations_store, clock=clock)
```

并把两枚传进 ServiceContext 的构造（按字段名）。

- [ ] **Step 5: 全量自己的文件 + ruff + Commit**

```bash
uv run pytest -q tests/storage/test_admin_access_repo.py tests/services/test_break_glass.py tests/web
uv run ruff check --no-cache src tests
git add src/sams/storage/repos/admin_access.py src/sams/services/admin_access.py src/sams/bootstrap.py tests/storage/test_admin_access_repo.py tests/services/test_break_glass.py
git commit -m "feat(admin): break-glass 底座——授权仓库与单条限时限次用例（D139）"
```

---

## Task 3: `/annotations` 全局搜索页（clause 抽取 + team_only + 分页 + 导航）

**Files:**
- Modify: `src/sams/storage/repos/annotations.py`（`_visibility_clauses` 抽取、`team_only`、`count_visible`）
- Modify: `src/sams/services/annotations.py`（`search`）
- Modify: `src/sams/web/pages/annotations.py`（`GET /annotations`）
- Create: `src/sams/web/templates/annotations.html`
- Modify: `src/sams/web/templates/base.html`（导航）
- Test: `tests/storage/test_annotations_repo.py`、`tests/services/test_annotations_service.py`、`tests/web/test_annotations_web.py`

- [ ] **Step 1: 仓库红测试**

`tests/storage/test_annotations_repo.py` 追加（夹具同款 `OWNER`/`OTHER` 与 `add(...)` helper）：

```python
def test_team_only_scope_is_teams_only(repo):
    mine_team = add(repo, OWNER, visibility="team")
    mine_private = add(repo, OWNER, body_md="只给自己")
    theirs_team = add(repo, OTHER, visibility="team")
    add(repo, OTHER, body_md="只给作者")
    rows = repo.list_visible(OWNER, team_sharing=True, team_only=True)
    assert {r.id for r in rows} == {mine_team.id, theirs_team.id}
    off = repo.list_visible(OWNER, team_sharing=False, team_only=True)
    assert {r.id for r in off} == {mine_team.id}
    # 两条 scope 旗子不能同真：同给等于没说
    with pytest.raises(Exception, match="只能选一"):
        repo.list_visible(OWNER, mine_only=True, team_only=True)
    assert mine_private.id and theirs_team.id  # 防 IDE 清 import


def test_count_visible_matches_page(repo):
    for n in range(25):
        add(repo, OWNER, body_md=f"第 {n} 条")
    rows = repo.list_visible(OWNER, limit=20, offset=0)
    total = repo.count_visible(OWNER)
    assert len(rows) == 20 and total == 25
    rows2 = repo.list_visible(OWNER, limit=20, offset=20)
    assert len(rows2) == 5
```

（`add()` 的真实签名按该文件现有 helper——若没有 `visibility=` 参数就现场内联 `repo.create(OWNER, target_type="symbol", target_ref=f"A:600{n}", ...)`；照抄文件里其它可见性用例的构造方式。）

Run: FAIL（没有 `team_only`/`count_visible`）。

- [ ] **Step 2: 实现仓库**

`repos/annotations.py`：把 `list_visible` 的 clause 组装抽成模块内私有构造器（**一份规则一个名字**，列表与计数共用）：

```python
    def _visibility_clauses(
        self,
        viewer_id: str,
        *,
        team_sharing: bool,
        mine_only: bool,
        team_only: bool,
        target_type: str | None,
        target_ref: str | None,
        q: str | None,
        tag: str | None,
        since: datetime | None,
    ) -> tuple[list[str], list[object]]:
        if mine_only and team_only:
            raise ValidationError("mine_only 与 team_only 只能选一：两个范围同时给等于没筛")
        clauses = ["deleted_at IS NULL", "purged_at IS NULL"]
        params: list[object] = []
        if mine_only:
            clauses.append("user_id = ?")
            params.append(viewer_id)
        elif team_only:
            # team 只对 symbol 生效是写侧闸（D121）；读侧不重复——写得进来的形态已经过了那道闸
            if team_sharing:
                clauses.append("visibility = 'team'")
            else:
                clauses.append("(visibility = 'team' AND user_id = ?)")
                params.append(viewer_id)
        elif team_sharing:
            clauses.append("(user_id = ? OR (visibility = 'team' AND target_type = 'symbol'))")
            params.append(viewer_id)
        else:
            clauses.append("user_id = ?")
            params.append(viewer_id)
        if target_type is not None:
            clauses.append("target_type = ?")
            params.append(target_type)
        if target_ref is not None:
            clauses.append("target_ref = ?")
            params.append(target_ref)
        if q:
            clauses.append("contains(lower(body_md), lower(?))")
            params.append(q)
        if tag:
            clauses.append(_TAG_MATCH)
            params.append(tag)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        return clauses, params
```

`list_visible(..., team_only: bool = False)` 改为调用它 + 原有 ORDER/LIMIT；参数校验（target_type 白名单、limit 为正）保留在 `list_visible` 开头，`count_visible` 同样先过这两道再共用构造器：

```python
    def count_visible(self, viewer_id: str, **filters: object) -> int:
        """与 `list_visible` 同一份 clause 构造器的总数：分页的分母必须与分子同源。"""
        clauses, params = self._visibility_clauses(viewer_id, **filters)  # 展开时类型已由 list_visible 的调用方保证
        return int(
            self.db.scalar(
                f"SELECT count(*) FROM annotations WHERE {' AND '.join(clauses)}",
                params,
            )
            or 0
        )
```

（`**filters` 的键就是 `_visibility_clauses` 的 keyword-only 名；`list_visible` 保留显式签名不动——外部调用面不变。若 `count_visible` 想挡 `limit/offset` 混入，直接按具名参数逐个传给构造器，别用 `**filters`——**以调用处展开显式参数为准则**，`list_visible` 内部同样调构造器时逐个传。）

- [ ] **Step 3: 服务 `search` + web 页红测试**

服务（`services/annotations.py`）：

```python
    def search(
        self,
        viewer_id: str,
        *,
        q: str | None = None,
        tag: str | None = None,
        target_type: str | None = None,
        scope: str = "mine",
        team_sharing: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Annotation], int]:
        """`/annotations` 搜索页的读用例：`scope` 折成仓库的两面旗子，返回（本页，总数）。"""
        if scope not in ("mine", "team"):
            raise ValidationError(f"scope 只有 mine/team（收到 {scope!r}）")
        filters = dict(
            team_sharing=team_sharing,
            mine_only=scope == "mine",
            team_only=scope == "team",
            target_type=target_type,
            target_ref=None,
            q=q,
            tag=tag,
            since=None,
        )
        rows = self.annotations.list_visible(viewer_id, limit=limit, offset=offset, **filters)
        total = self.annotations.count_visible(viewer_id, **filters)
        return rows, total
```

（`count_visible` 的实现按 Step 2 的具名参数版本，`filters` 字典在 `list_visible` 与 `count_visible` 两处展开为 `**filters`——若 ruff/typing 有意见就在 `**filters: object` 上收敛；语义不变。）

`tests/web/test_annotations_web.py` 红测试（要点列名，代码按文件既有夹具风格写全）：

```python
def test_search_requires_login(web_app): → 303 到 /login
def test_default_scope_is_mine_and_hides_teammates(logged): alice 写 private + bob 写 team（第二人构造照 `test_annotations_web.py` 里已有的双用户方式；没有就 make_app 后 create user + login）→ 默认页只含 alice 的
def test_team_scope_shows_teammates_when_shared(logged): scope=team → 含 bob 的 team 行
def test_team_scope_hides_teammate_when_policy_off(logged): policy off → scope=team 只含自己的 team
def test_query_does_not_leak_private(logged): bob 写 private「茅台提价」；alice 搜 q=提价 → 响应不含 bob 那条正文串
def test_type_filter_bad_value_says_so(logged): type=planet → 页面上有「未知批注目标」的话且没有崩
def test_pagination_second_page(logged): alice 25 条自己批注 → page=2 显示 5 条、「共 25 条」出现
def test_tag_filter(logged): tag 精确匹配大小写不敏感（`LOGIC` 搜到 `logic`）
```

Run: FAIL（GET /annotations 404 或旧路由语义）。

- [ ] **Step 4: 实现 GET /annotations 与模板**

`src/sams/web/pages/annotations.py`（端点体全走 `contexts.current()`）：

```python
@router.get("/annotations")
def annotations_search(
    request: Request,
    q: str = Query(""),
    tag: str = Query(""),
    type: str = Query(""),
    scope: str = Query("mine"),
    page: str = Query("1"),
):
    """全局搜索（§4.5）：`q` 是正文子串、`tag` 精确、`type` 白名单、`scope` 两档。

    地址栏里的手滑不判 400：`page=abc` 回到第 1 页、`scope` 野值回 `mine`——
    这两样的「正确答案」对用户不可见，报错只是把人挡在门外；`type` 野值**要**报错，
    因为下拉之外的值说明有人在改 URL，悄悄吞掉会教人以为筛到了。
    """
    user = require_user(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    try:
        page_no = max(1, int(page))
    except ValueError:
        page_no = 1
    scope_key = scope.strip() if scope.strip() in ("mine", "team") else "mine"
    error = ""
    rows_read: list = []
    total = 0
    type_key = type.strip()
    if type_key and type_key not in TARGET_TYPES:
        error = f"未知批注目标 {type_key!r}：只有 {', '.join(sorted(TARGET_TYPES))}"
        type_key = ""
    if not error:
        try:
            rows_read, total = ctx.annotations.search(
                user.id,
                q=q.strip() or None,
                tag=tag.strip() or None,
                target_type=type_key or None,
                scope=scope_key,
                team_sharing=ctx.app_settings.team_sharing(),
                limit=PAGE_SIZE,
                offset=(page_no - 1) * PAGE_SIZE,
            )
        except DatabaseLocked:
            raise
        except SamsError as exc:
            error, rows_read, total = str(exc), [], 0
    tz = ctx.settings.server.display_tz
    accounts = None  # 成交目标的存在性检查要账户列表；按页取一次，N+1 到此为止
    rows = []
    for made in rows_read:
        if made.target_type == ORDER and accounts is None:
            accounts = ctx.sim_accounts.list_for(user.id)
        rows.append({
            "id": made.id,
            "at": moment_label(made.created_at, tz),
            "author": _author_label(ctx, made, user.id),
            "visibility": "团队" if made.visibility == "team" else "私有",
            "tags": made.tags,
            "target_label": made.target_label,
            "target_url": _target_url(made),
            "target_gone": _target_gone(ctx, user.id, made, accounts=accounts),
            "body_html": render_markdown(made.body_md),
        })
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return web.render(
        request, "annotations.html", title="批注搜索",
        rows=rows, total=total, page=page_no, pages=pages,
        q=q, tag=tag, type=type_key, scope=scope_key, error=error,
        type_choices=[("", "全部目标"), *[(t, TARGET_TYPE_LABELS[t]) for t in TARGET_TYPES]],
        prev_url=_search_url(q, tag, type_key, scope_key, page_no - 1) if page_no > 1 else "",
        next_url=_search_url(q, tag, type_key, scope_key, page_no + 1) if page_no < pages else "",
        empty_note="没有命中：换个关键词，或把范围切到「团队」。",
    )
```

模块级新增（放该文件常量区）：

```python
PAGE_SIZE = 20

#: 搜索页下拉的中文标签：与告警详情页的引用文案同一批词
TARGET_TYPE_LABELS = {
    "symbol": "标的", "thesis": "观点", "rule": "规则", "alert": "告警", "order": "成交",
}


def _target_url(made) -> str:
    """把批注链回宿主面板那一页：目标页上有全文与附件，搜索页只给正文。"""
    if made.target_type == "symbol":
        market, _, code = made.target_ref.partition(":")
        return f"/symbol/{market}/{code}"
    if made.target_type == "thesis":
        return f"/theses/{made.target_ref}"
    if made.target_type == "rule":
        return f"/rules/{made.target_ref}/edit"
    if made.target_type == "alert":
        return f"/alerts/{made.target_ref}"
    return "/portfolio"


def _target_gone(ctx, user_id: str, made, *, accounts) -> bool:
    """§4.5「目标已删但批注可见」：拿写侧同一个仓库 get 问一句还在不在。

    标的永不「已删」（主档只增）；成交挂在账户下，`accounts` 由调用方按页取一次。
    """
    if made.target_type == "symbol":
        return False
    if made.target_type == "thesis":
        return ctx.theses_store.get(user_id, made.target_ref) is None
    if made.target_type == "rule":
        return ctx.rules_store.get(made.target_ref, user_id=user_id) is None
    if made.target_type == "alert":
        return ctx.alerts_store.get(made.target_ref, user_id=user_id) is None
    if made.target_type == "order":
        return all(
            ctx.sim_ledger.order(account.id, made.target_ref) is None
            for account in (accounts or [])
        )
    return False


def _search_url(q: str, tag: str, type_: str, scope: str, page: int) -> str:
    pairs = [("scope", scope)]
    if q:
        pairs.append(("q", q))
    if tag:
        pairs.append(("tag", tag))
    if type_:
        pairs.append(("type", type_))
    if page != 1:
        pairs.append(("page", str(page)))
    return "/annotations?" + "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs
    )
```

（`ctx.theses_store` 的真实字段名：`grep -n "theses_store\|theses:" src/sams/bootstrap.py` 确认后统一——观点仓库若是 `theses_store` 就用它；rule 仓库 `rules_store`、告警 `alerts_store`、成交 `sim_ledger` 已在计划头核实。**`from urllib.parse import quote` 若该文件没 import 就补。`Query` 从 fastapi import。**）

新建 `src/sams/web/templates/annotations.html`：

```html
{% extends "base.html" %}
{% block content %}
<h1>批注搜索</h1>
{% if error %}<p class="flash-error">{{ error }}</p>{% endif %}
<form method="get" action="/annotations" class="inline-form">
  <input name="q" value="{{ q }}" placeholder="正文关键词">
  <input name="tag" value="{{ tag }}" placeholder="标签">
  <select name="type">
    {% for value, label in type_choices %}
    <option value="{{ value }}"{% if value == type %} selected{% endif %}>{{ label }}</option>
    {% endfor %}
  </select>
  <select name="scope">
    <option value="mine"{% if scope == "mine" %} selected{% endif %}>我的</option>
    <option value="team"{% if scope == "team" %} selected{% endif %}>团队</option>
  </select>
  <button type="submit">搜索</button>
</form>
<p class="muted">共 {{ total }} 条 · 第 {{ page }}/{{ pages }} 页（一页 {{ 20 }} 条）</p>
{% for row in rows %}
<article class="ann-row">
  <header>
    <span class="ann-avatar">{{ row.author[0]|upper }}</span>
    <strong>{{ row.author }}</strong>
    <span class="muted">{{ row.at }}</span>
    <span class="badge">{{ row.visibility }}</span>
    {% for tag in row.tags %}<span class="tag">{{ tag }}</span>{% endfor %}
    {% if row.target_gone %}<span class="badge">目标已删除</span>{% endif %}
  </header>
  <p class="muted">挂在：<a href="{{ row.target_url }}">{{ row.target_label }}</a></p>
  <div class="md-body">{{ row.body_html | safe }}</div>
</article>
{% else %}
<p class="muted">{{ empty_note }}</p>
{% endfor %}
<nav class="ann-pager">
  {% if prev_url %}<a href="{{ prev_url }}">上一页</a>{% endif %}
  {% if next_url %}<a href="{{ next_url }}">下一页</a>{% endif %}
</nav>
{% endblock %}
```

（`{{ 20 }}` 若显得傻就删那半句，只留「第 x/y 页 · 共 N 条」。）

`base.html`：`<a href="/review">复盘</a>` 与 `<a href="/settings">设置</a>` 之间插 `<a href="/annotations">批注</a>`。

- [ ] **Step 5: 回归 + ruff + Commit**

```bash
uv run pytest -q tests/storage/test_annotations_repo.py tests/services/test_annotations_service.py tests/web/test_annotations_web.py tests/web
uv run ruff check --no-cache src tests
git add src/sams/storage/repos/annotations.py src/sams/services/annotations.py src/sams/web/pages/annotations.py src/sams/web/templates/annotations.html src/sams/web/templates/base.html tests/storage/test_annotations_repo.py tests/services/test_annotations_service.py tests/web/test_annotations_web.py
git commit -m "feat(annotations): /annotations 全局搜索——team_only 范围、总数同源与导航入口（§4.5）"
```

---

## Task 4: 编辑批注时增删附件（multipart 化 + `remove` + revision meta 补写）

**Files:**
- Modify: `src/sams/services/attachments.py`（`remove`）
- Modify: `src/sams/storage/repos/annotations.py`（`stamp_last_revision_attachments`）
- Modify: `src/sams/web/pages/annotations.py`（`annotation_edit` 重排 + 附件流）
- Modify: `src/sams/web/templates/partials/annotations.html`（编辑表单 multipart + 文件输入 + 删除勾选；`images` 行加 `id`）
- Test: `tests/services/test_attachments_service.py`（或该主题现有文件真名）、`tests/storage/test_annotations_repo.py`、`tests/web/test_annotations_web.py`

- [ ] **Step 1: 服务与仓库红测试**

附件服务测试（文件真名已核实 `tests/services/test_attachments_service.py`；夹具 `world(db, settings, tmp_path)` 已挂 `world.service`（AttachmentService）、`world.annotations`（AnnotationService）、`world.note`、`world.store`、`world.root`，图片 helper 是该文件的 `image_bytes()`）：

```python
def test_remove_takes_the_row_and_the_file(world):
    """作者删附件：行先没、文件后删——顺序反过来就留下一行永远 404 的引用。"""
    row = add(world)
    path = world.service.path_of(row)
    assert path.is_file()
    removed = world.service.remove(world.user.id, world.note.id, row.id)
    assert removed.id == row.id
    assert world.store.get(row.id) is None
    assert not path.exists()


def test_remove_is_author_only(world):
    row = add(world)
    with pytest.raises(NotFound):
        world.service.remove(world.other.id, world.note.id, row.id)


def test_remove_rejects_foreign_annotation(world):
    row = add(world)
    other_note = world.annotations.create(
        world.user.id, target_type="symbol", target_ref="A:000001",
        body_md="另一条", tags=())
    with pytest.raises(NotFound):
        world.service.remove(world.user.id, other_note.id, row.id)


def test_removed_bytes_free_the_quota(world):
    # 把 world.settings.annotations.max_user_total_mb 调小（dataclass 可直改；
    # add() 每次现读 limits，见 services/attachments.py:139）：
    # 传两张占满配额 → 删一张 → 同配额还能再传一张
    ...
```

（`add(world, data=...)` 是该文件既有的「往 world.note 挂一张图」助手，直接用。`remove` 里「行删了但 unlink 失败」的路径只记 warning 不抛——测试不强求造失败，代码评审覆盖即可。）

repo 补写 revision meta 测试（`tests/storage/test_annotations_repo.py`）：

```python
def test_stamp_last_revision_records_attachments(repo, seeded_user):
    made = add(repo, seeded_user, body_md="带图")
    repo.update(made.id, user_id=seeded_user, body_md="改一下", tags=[], visibility="private")
    repo.stamp_last_revision_attachments(made.id, ["att-a", "att-b"])
    meta = repo.revisions(made.id)[-1].meta
    assert meta["attachments"] == ["att-a", "att-b"]


def test_stamp_without_revision_is_a_no_op(repo, seeded_user):
    made = add(repo, seeded_user)
    repo.stamp_last_revision_attachments(made.id, ["att-a"])  # 不炸
    assert repo.revisions(made.id) == []
```

Run 两组 → FAIL。

- [ ] **Step 2: 实现服务 `remove` 与仓库 `stamp_last_revision_attachments`**

`services/attachments.py` 写区加：

```python
    def remove(self, viewer_id: str, annotation_id: str, attachment_id: str) -> Attachment:
        """作者删附件：先过批注归属（与写正文同一条闸），再删行、再删文件。

        顺序与新增相反——行没了文件才动：中间崩了最坏留一个孤儿文件给 prune，
        反过来就是「库里指着一张不存在的图」。
        """
        made = self.annotations.get(viewer_id, annotation_id)
        if made.user_id != viewer_id:
            raise NotFound(f"批注 {annotation_id} 不是你的：删不了别人批注上的附件")
        row = self.attachments.get(attachment_id)
        if row is None or row.annotation_id != annotation_id:
            raise NotFound(f"附件 {attachment_id} 不存在或不属于这条批注")
        self.attachments.delete(attachment_id)
        try:
            self.path_of(row).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("附件文件删除失败（行已删，交给 prune）：%s", exc)
        return row
```

`repos/annotations.py` 加：

```python
    def stamp_last_revision_attachments(
        self, annotation_id: str, attachment_ids: list[str]
    ) -> None:
        """把「这次编辑之后有哪些附件」补进最新 revision 的 meta_json（§4.3）。

        附件增删发生在正文 revision 落库**之后**（新文件的 id 要传完才知道），
        所以是补写而不是随 update 一起写；没有 revision（人不带版本改了个寂寞）就静默跳过。
        """
        rows = self.db.query(
            "SELECT id, meta_json FROM annotation_revisions"
            " WHERE annotation_id = ? ORDER BY edited_at DESC, id DESC LIMIT 1",
            [annotation_id],
        ).to_dicts()
        if not rows:
            return
        meta = _json_dict(rows[0]["meta_json"])
        meta["attachments"] = [str(item) for item in attachment_ids]
        self.db.execute(
            "UPDATE annotation_revisions SET meta_json = ? WHERE id = ?",
            [_json_text(meta), str(rows[0]["id"])],
        )
```

（`_json_text`：`grep -n "json.dumps\|_json_text" src/sams/storage/repos/annotations.py`——该文件写 meta_json 用的那份序列化，缺 helper 就新增 `_json_text(value) -> str: return _json.dumps(value, ensure_ascii=False, default=str)`，命名对齐文件既有私有函数风格。）

- [ ] **Step 3: web 红测试**

`tests/web/test_annotations_web.py`（`_make_annotation_with_png` helper 已在）：

```python
def test_edit_can_add_an_attachment(logged, _rgba_png_bytes):
    uid = logged.context.users.get_by_username("alice").id
    made = logged.context.annotations.create(uid, target_type="symbol",
        target_ref="A:600519", body_md="先没图", tags=())
    panel = logged.client.post(f"/annotations/{made.id}/edit",
        data={"csrf": logged.csrf, "body_md": "先没图", "tags": "",
              "updated_at": made.updated_at.isoformat()},
        files={"files": ("shot.png", _rgba_png_bytes(), "image/png")})
    assert panel.status_code == 200
    assert "ann-imgs" in panel.text
    assert len(logged.context.attachments_store.list_for(made.id)) == 1


def test_edit_can_remove_a_checked_attachment(logged, _rgba_png_bytes):
    # 建带图批注 → edit 带 remove_attachments=<att_id>（不带新文件）→ 面板回「还没有批注」式空图区、
    # list_for 为空、磁盘文件没了、最新 revision 的 meta["attachments"] == []
    ...


def test_edit_oversize_keeps_everything(logged, ...):
    """尺寸闸在改动之前：图超了正文一个字都不动——和 create 同一条纪律（§10）。"""
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (6 * 1024 * 1024)
    made = ...  # body_md="原样"
    resp = logged.client.post(f"/annotations/{made.id}/edit",
        data={...,"body_md": "改了也不该存", "files": ("b.png", big, "image/png")})
    assert resp.status_code == 400 and "超了" in resp.text
    assert logged.context.annotations_store.get(made.id).body_md == "原样"


def test_edit_conflict_does_not_touch_attachments(logged, _rgba_png_bytes):
    # updated_at 给旧值 + 带一张文件 → 409；文件没被加进去（list_for 计数不变）
    ...


def test_revision_meta_records_final_attachments(logged, _rgba_png_bytes):
    # edit 传一张图后 repo.revisions(made.id)[-1].meta["attachments"] == [att.id]
    ...
```

Run → FAIL（edit 端点收不到 files）。

- [ ] **Step 4: 实现 `annotation_edit` 重排与模板**

`annotation_edit` 签名改为：

```python
@router.post("/annotations/{annotation_id}/edit")
def annotation_edit(
    request: Request,
    annotation_id: str,
    _csrf: None = Depends(csrf_protect),
    body_md: str = Form(""),
    tags: str = Form(""),
    updated_at: str = Form(""),
    visibility: str = Form(""),
    remove_attachments: list[str] = Form(default=[]),
    files: list[UploadFile | str] = _files_field,
):
```

（若 FastAPI 对非嵌套的多值字段要求 `Form(default=[])` 的 list 写法有版本差异，用 `Annotated[list[str], Form()]`；以能同时收 0 个与多个勾选为准，测试里钉。）

流程按注释顺序：

```python
    web, ctx, user, made = _load_for_write(request, annotation_id)
    clean = _clean_files(files)
    removed_ids = [item.strip() for item in remove_attachments if item.strip()]

    def fail(error: str, status_code: int):  # 同现在，draft 带上 body_md/tags
        ...

    wait = _limit_hit(web, user.id, uploads=len(clean))
    if wait:
        return fail(f"写得太快：请 {wait} 秒后再来（每分钟 30 条批注、10 次附件上传）", 429)
    limits = ctx.settings.annotations
    if len(removed_ids) + len(clean) > limits.max_attachments:
        return fail(f"一次最多动 {limits.max_attachments} 张附件的增删：分批来", 400)
    oversize = [...]  # 与 create 同款
    if oversize:
        return fail(f"单张附件最多 {limits.max_attachment_mb} MB，{'、'.join(oversize)} 超了：这条批注一个字都没改", 400)
    stamp: datetime | None = None
    if updated_at:
        ...  # 现状同款
    before_ids = [att.id for att in ctx.attachments_store.list_for(made.id)]
    try:
        updated = ctx.annotations.edit(
            user.id, made.id, body_md=body_md, tags=tags,
            expected_updated_at=stamp,
            visibility=visibility.strip() or None,
        )
    except DatabaseLocked:
        raise
    except Conflict as exc:
        return fail(str(exc), 409)
    except SamsError as exc:
        return fail(str(exc), 400)
    # 正文成立之后才动附件：删一张算一张，失败的点名（与 create 的 rejected 同一话术）
    rejected: list[str] = []
    for att_id in removed_ids:
        try:
            ctx.attachments.remove(user.id, made.id, att_id)
        except DatabaseLocked:
            raise
        except SamsError as exc:
            rejected.append(f"删除 {att_id}：{exc}")
    for upload in clean:
        try:
            ctx.attachments.add(user.id, made.id, filename=upload.filename, data=upload.file.read())
        except DatabaseLocked:
            raise
        except SamsError as exc:
            rejected.append(f"{upload.filename}：{exc}")
    after_ids = [att.id for att in ctx.attachments_store.list_for(made.id)]
    if after_ids != before_ids:
        ctx.annotations_store.stamp_last_revision_attachments(made.id, after_ids)
    audit(request, action="annotation.edit", target=made.id, ok=not rejected,
          detail=f"att-{len(removed_ids) - len([r for r in rejected if r.startswith('删除')])}+{len(clean) - len([r for r in rejected if not r.startswith('删除')])}")
    error = f"批注已保存，但附件没全成：{'；'.join(rejected)}" if rejected else ""
    return _render_panel(request, user=user, target_type=updated.target_type,
                         target_ref=updated.target_ref, error=error)
```

（audit 的 detail 折得傻就把两个计数各存变量再拼，保持一行能说清；NotFound 属于 SamsError，`remove` 的 NotFound 会落进 rejected——对勾选了已消失附件的重复提交，这就是那句话。）

`partials/annotations.html`：编辑表单 `<form ...>` 标签加 `hx-encoding="multipart/form-data"`；`<input name="tags" ...>` 之后加：

```html
          <input type="file" name="files" accept="image/png,image/jpeg,image/webp" multiple>
          {% if row.images %}
          <div class="ann-remove-imgs">
            {% for img in row.images %}
            <label><input type="checkbox" name="remove_attachments" value="{{ img.id }}"> 删 {{ img.filename }}</label>
            {% endfor %}
          </div>
          {% endif %}
```

`_render_panel` 的 thumbs 字典加 `"id": att.id`。THUMB_LIMIT=3 的旧注释处补一句：勾选只覆盖前 3 张——更多附件的删除走「编辑前先删批注重发」不如把缩略图上限抬到 `max_attachments`？ **决策：把 `THUMB_LIMIT` 改成读配置** `ctx.settings.annotations.max_attachments`（配置默认 5，一屏装得下；3 张封顶的旧注释相应改写——「张数上限与配额同源，编辑器要能勾选到每一张」）。这条改动波及 Task 3（计划 4）的断言「还有 N 张」语义：`tests/web/test_annotations_web.py` 中构造 >3 张附件的用例按新上限调断言（fewer than 5 张时全部是 thumbs、`extra == 0`）。

- [ ] **Step 5: 回归 + ruff + Commit**

```bash
uv run pytest -q tests/services tests/storage/test_annotations_repo.py tests/web/test_annotations_web.py tests/web
uv run ruff check --no-cache src tests
git add src/sams/services/attachments.py src/sams/storage/repos/annotations.py src/sams/web/pages/annotations.py src/sams/web/templates/partials/annotations.html tests/services/test_attachments_service.py tests/storage/test_annotations_repo.py tests/web/test_annotations_web.py
git commit -m "feat(annotations): 编辑批注可增删附件——remove 用例、尺寸闸前置与 revision 补记（§4.3/§5.4）"
```

---

## Task 5: `/admin/annotations` 页 + 策略开关 + break-glass 路由

**Files:**
- Modify: `src/sams/storage/repos/annotations.py`（`list_metadata`/`count_metadata`）
- Modify: `src/sams/storage/repos/attachments.py`（`usage_by_user`、`count_by_annotation_map` 可选）
- Create: `src/sams/web/pages/admin_annotations.py`
- Create: `src/sams/web/templates/admin_annotations.html`、`admin_annotation_detail.html`
- Modify: `src/sams/web/app.py`（import + include_router，字母序 `admin` 之后）
- Modify: `src/sams/web/templates/admin_users.html`（入口链接）
- Test: `tests/storage/test_annotations_repo.py`（metadata）、`tests/web/test_admin_annotations_web.py`（新）

- [ ] **Step 1: 仓库红测试**

`tests/storage/test_annotations_repo.py`：

```python
def test_list_metadata_includes_tombstones_and_others(repo, seeded_user, other_user):
    """admin 的元数据列表（§6.1/D139）：不按可见性滤，带软删墓碑，排已硬清除。"""
    a = add(repo, seeded_user, body_md="别人的也列")
    b = add(repo, other_user, body_md="将被删")
    c = add(repo, seeded_user, body_md="将被清除")
    repo.soft_delete(b.id, user_id=other_user)
    repo.db.execute(
        "UPDATE annotations SET purged_at = ? WHERE id = ?", [NOW, c.id]
    )  # 仓库无 purge()——照 services/annotation_prune.py 的裸 SQL 补一行
    ids = {r.id for r in repo.list_metadata(limit=50)}
    assert {a.id, b.id} <= ids and c.id not in ids
    assert repo.count_metadata() == len(ids)
```

`tests/storage/test_attachments_repo.py`（或真名）：

```python
def test_usage_by_user_groups_counts_and_bytes(repo, seeded_user, other_user):
    # 各建批注+附件行（repo.create 直接铺行），usage_by_user() 返回 {user: (张数, 字节)} 的列表，按字节降序
```

Run → FAIL。

- [ ] **Step 2: 实现仓库**

`repos/annotations.py`：

```python
    def list_metadata(self, *, limit: int = 50, offset: int = 0) -> list[Annotation]:
        """admin 页的列表：所有人、含墓碑、排已硬清除。正文列照带——**遮不遮是页面层的事**，
        仓库不做权限（权限只有一处才数得清）。"""
        rows = self.db.query(
            f"SELECT {_ANNOTATION_COLUMNS} FROM annotations"
            " WHERE purged_at IS NULL ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [int(limit), int(offset)],
        ).to_dicts()
        return [self._to_annotation(row) for row in rows]

    def count_metadata(self) -> int:
        return int(
            self.db.scalar("SELECT count(*) FROM annotations WHERE purged_at IS NULL", [])
            or 0
        )
```

`repos/attachments.py`：

```python
    def usage_by_user(self) -> list[tuple[str, int, int]]:
        """每人 (附件数, 字节)：admin 用量表（D136 的「计数与占用」），按字节降序。"""
        rows = self.db.query(
            "SELECT user_id, count(*) AS cnt, COALESCE(sum(size_bytes), 0) AS bytes"
            " FROM attachments GROUP BY user_id ORDER BY bytes DESC",
            [],
        ).to_dicts()
        return [(str(r["user_id"]), int(r["cnt"]), int(r["bytes"])) for r in rows]
```

- [ ] **Step 3: 页面红测试**

新建 `tests/web/test_admin_annotations_web.py`（admin 登录夹具照抄 `tests/web/test_admin_web.py` 的装法；普通用户 `logged`）：

```python
def test_page_requires_admin(logged):
    resp = logged.client.get("/admin/annotations", follow_redirects=False)
    assert resp.status_code in (303, 403)


def test_policy_toggle_roundtrip(admin_app):
    page = admin_app.client.get("/admin/annotations")
    assert "团队共享" in page.text
    resp = admin_app.client.post("/admin/annotations/policy",
        data={"csrf": admin_app.csrf, "value": "off"}, follow_redirects=False)
    assert resp.status_code == 303
    assert admin_app.context.app_settings.team_sharing() is False


def test_policy_rejects_a_stray_value(admin_app):
    resp = admin_app.client.post("/admin/annotations/policy",
        data={"csrf": admin_app.csrf, "value": "maybe"})
    assert resp.status_code == 400


def test_metadata_lists_teammate_without_body(admin_app):
    # 建 alice 的 private 批注 → 列表行有「私有」徽标与该条 id，但页面不含正文串
    assert "只给自己看" not in body_page.text


def test_break_glass_unlocks_body_then_expires(admin_app):
    # private 批注 → detail 页无正文有 reason 表单 → POST break_glass（理由 ≥4 字）303 →
    # detail 页出现正文与「附件同权」的链接 → FrozenClock 推进 15 分钟后（admin_app 用 make_app(clock=)）又没了
    ...


def test_short_reason_is_refused(admin_app): → 400 且审计 deny 无所谓，断言页面有「至少 4 个字」
def test_hourly_cap_surfaces(admin_app): 直接 SQL/服务铺 5 条本小时授权 → 第 6 次 POST 得 409 页
def test_team_body_readable_without_grant(admin_app): team+policy on → detail 有正文
def test_team_hidden_when_policy_off(admin_app): team+policy off → detail 无正文（走 break-glass 才能读）
def test_attachment_download_by_admin_grant(admin_app):
    # 有授权时 GET /admin/annotations/{id}/attachments/{att} 200 且 Content-Type 是图；
    # 无授权 403；被 purge 的 404
def test_recent_break_glass_table_shows_reason(admin_app): 授权一次 → 列表页表格含理由文本
def test_purged_tombstone_says_body_gone(admin_app): purged 行 detail 页出现「已被清除」的话
```

Run → FAIL（404 全片）。

- [ ] **Step 4: 实现页面**

新建 `src/sams/web/pages/admin_annotations.py`：

```python
"""`/admin/annotations`（§6.1/D139）：元数据、策略、用量与 break-glass。

admin 的默认视野是「有哪些行」而不是「写了什么」：列表与详情头部只有元数据，
正文、revision 与附件要满足两者之一——team 且策略开，或对**这一条**有未过期的授权。
附件走本文件多出来那条 `/admin/.../attachments/...`：D139 说「附件与 revision 同权」，
用普通下载路由会先被「不是作者」拦死，授权就白批了。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from sams.core.errors import Conflict, DatabaseLocked, NotFound, SamsError
from sams.services.admin_access import GRANT_MINUTES, BreakGlassService
from sams.storage.repos.app_settings import TEAM_SHARING
from sams.web.auth import audit, csrf_protect, require_admin
from sams.web.display import moment_label
from sams.web.forms import form_data
from sams.services.markdown import render_markdown

router = APIRouter()

_LIST_LIMIT = 50
_RECENT_LOGS = 10
_form_body = Depends(form_data)


def _readable(ctx, admin, made, *, sharing: bool) -> bool:
    """两者其一：team（且策略开、且是 symbol——写侧闸保证的形状，读侧再认一遍不亏），或有效授权。"""
    if made.visibility == "team" and sharing and made.target_type == "symbol":
        return True
    return ctx.break_glass.can_read(admin.id, made.id)


def _meta_rows(ctx, made_by: dict[str, str]) -> list[dict]:
    tz = ctx.settings.server.display_tz
    rows = []
    for made in ctx.annotations_store.list_metadata(limit=_LIST_LIMIT):
        atts = ctx.attachments_store.count_for(made.id)
        rows.append({
            "id": made.id,
            "owner": made_by.get(made.user_id, made.user_id[:8]),
            "at": moment_label(made.created_at, tz),
            "updated": moment_label(made.updated_at, tz),
            "target": f"{made.target_type} · {made.target_label}",
            "visibility": "团队" if made.visibility == "team" else "私有",
            "attachments": atts,
            "deleted": made.deleted_at is not None,
            "purged": made.purged_at is not None,
        })
    return rows


@router.get("/admin/annotations")
def admin_annotations_page(request: Request):
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    names = {user.id: (user.display_name or user.username) for user in ctx.users.list_users()}
    usage = [
        {
            "user": names.get(user_id, user_id[:8]),
            "count": count,
            "mb": f"{size_bytes / (1024 * 1024):.1f}",
        }
        for user_id, count, size_bytes in ctx.attachments_store.usage_by_user()
    ]
    logs = [
        {
            "admin": names.get(row.admin_user_id, row.admin_user_id[:8]),
            "annotation": row.annotation_id,
            "reason": row.reason,
            "until": moment_label(row.granted_until, ctx.settings.server.display_tz),
        }
        for row in ctx.admin_access_store.list_recent(limit=_RECENT_LOGS)
    ]
    return web.render(
        request, "admin_annotations.html", title="批注与附件",
        sharing=ctx.app_settings.team_sharing(),
        total=ctx.annotations_store.count_metadata(),
        missing=ctx.attachments.missing_count(),
        rows=_meta_rows(ctx, names), usage=usage, logs=logs,
        notice=request.query_params.get("notice", ""),
    )


@router.post("/admin/annotations/policy")
def policy_set(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    value: str = Form(""),
):
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    wanted = value.strip()
    if wanted not in ("on", "off"):
        raise HTTPException(status_code=400, detail=f"策略只有 on/off（收到 {value!r}）")
    old = ctx.app_settings.get(TEAM_SHARING, default="on")
    try:
        ctx.app_settings.set(TEAM_SHARING, wanted, updated_by=admin.id)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(request, action="annotation.policy", target=TEAM_SHARING, ok=True,
          detail=f"{old}->{wanted}")
    return RedirectResponse(f"/admin/annotations?notice=策略已切到 {'开' if wanted == 'on' else '关'}", status_code=303)
```

（`Form` import 补进 fastapi 那行；`notice` 走 query param 与 `_feedback` 同款——admin.py 的 `_feedback(request)` 若更好复用就复用它，自报说明。）

详情 + break-glass + 附件下载：

```python
@router.get("/admin/annotations/{annotation_id}")
def annotation_detail(request: Request, annotation_id: str):
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    made = ctx.annotations_store.get(annotation_id, include_deleted=True)
    if made is None:
        raise HTTPException(status_code=404, detail="批注不存在")
    sharing = ctx.app_settings.team_sharing()
    readable = _readable(ctx, admin, made, sharing=sharing)
    tz = ctx.settings.server.display_tz
    grants = ctx.admin_access_store.active(admin.id, made.id, now=ctx.clock.now_utc())
    body = None
    revisions = []
    images = []
    if readable and made.purged_at is None:
        body = render_markdown(made.body_md)
        revisions = ctx.annotations_store.revisions(made.id)
        for att in ctx.attachments_store.list_for(made.id):
            if ctx.attachments.path_of(att).is_file():
                images.append({
                    "url": f"/admin/annotations/{made.id}/attachments/{att.id}",
                    "filename": att.filename,
                })
    return web.render(
        request, "admin_annotation_detail.html", title="批注详情",
        meta={
            "id": made.id,
            "owner": made.user_id,
            "at": moment_label(made.created_at, tz),
            "target": f"{made.target_type} · {made.target_label}",
            "visibility": "团队" if made.visibility == "team" else "私有",
            "deleted": made.deleted_at is not None,
            "purged": made.purged_at is not None,
        },
        readable=readable, body=body, revisions=revisions, images=images,
        granted_until=moment_label(grants.granted_until, tz) if grants else "",
        notice=request.query_params.get("notice", ""),
    )


@router.post("/admin/annotations/{annotation_id}/break-glass")
def break_glass(
    request: Request,
    annotation_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    reason = (data.get("reason") or [""])[0]
    try:
        ctx.break_glass.request_access(admin.id, annotation_id, reason)
    except DatabaseLocked:
        raise
    except SamsError as exc:
        status = 409 if isinstance(exc, Conflict) else 400
        audit(request, action="admin.break_glass", target=annotation_id, ok=False,
              detail=str(exc)[:80])
        # 被拒也回详情页：人要看清「为什么不能读」的下一句是可用的表单，不是错误页一座
        # （渲染 helper 带 error 形参，成功分支传 ""，被拒分支传 str(exc) 并回 status——见下方括注）
        return annotation_detail(request, annotation_id)
    audit(request, action="admin.break_glass", target=annotation_id, ok=True,
          detail=f"+{GRANT_MINUTES}min")
    return RedirectResponse(
        f"/admin/annotations/{annotation_id}?notice=已授权 {GRANT_MINUTES} 分钟",
        status_code=303,
    )


@router.get("/admin/annotations/{annotation_id}/attachments/{attachment_id}")
def admin_attachment(request: Request, annotation_id: str, attachment_id: str):
    admin = require_admin(request)
    web = request.app.state.web
    ctx = web.contexts.current()
    made = ctx.annotations_store.get(annotation_id, include_deleted=True)
    # 墓碑没什么可下载：与「不存在」同一个回答，403 只留给「存在但没授权」
    if made is None or made.purged_at is not None:
        raise HTTPException(status_code=404, detail="批注不存在或只剩墓碑")
    sharing = ctx.app_settings.team_sharing()
    if not _readable(ctx, admin, made, sharing=sharing):
        raise HTTPException(status_code=403, detail="正文不可读：先在详情页为这条批注申请 break-glass")
    row = ctx.attachments_store.get(attachment_id)
    if row is None or row.annotation_id != annotation_id:
        raise HTTPException(status_code=404, detail="附件不存在")
    data = ctx.attachments.load(row)
    if data is None:
        raise HTTPException(status_code=404, detail="附件文件缺失：库里还有这一行")
    from urllib.parse import quote
    return Response(
        content=data, media_type=row.media_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(row.filename, safe='')}",
            "Cache-Control": "no-store",
        },
    )
```

（`Response` 从 fastapi import、`quote` 挪到文件头 import——上面写在函数体里只是示意，实现时按 `pages/annotations.py` 的下载端点同款整理；`Cache-Control: no-store` 是有意的：私人正文不许进中间缓存。`annotation_detail(request, annotation_id)` 直接函数调用复用渲染在 FastAPI 里可行（它就是普通 Python 函数）；状态码若要 409，加 `error: str = ""` 参数进 `_render`，被拒分支传进去——**采用带 error 渲染 + 原状态码**，别吞状态码。写自报时说明最终选的哪条。）

模板 `admin_annotations.html`（extends base）：`notice` 红条、策略卡（当前「开/关」+ on/off 两个 POST 小表单，csrf hidden）、批注列表表（时间/所有者/目标/可见性/附件数/删除徽标/`detail` 链接 `/admin/annotations/{{ row.id }}`）、用量表、最近 break-glass 表、`<a href="/admin/users">返回用户</a>`。
模板 `admin_annotation_detail.html`：meta 表 + 可读时正文（md-body safe）+ 附件图（`<a><img loading=lazy></a>`）+ revisions 简表（时间 + before/after 长度 + meta.attachments 若在有）+ 「授权到 {granted_until}」；不可读时理由 textarea + submit（`name="reason"`）+ 说明「单条、15 分钟、每小时最多 5 次，理由进审计（D139）」；purged 时「正文已被清除，只剩墓碑」；`notice`/`error` 红条。
`admin_users.html`：`<h1>用户</h1>` 下一行加 `<p class="muted"><a href="/admin/annotations">批注与附件</a></p>`。
`app.py`：import 与 `include_router(admin_annotations.router)` 插在 `admin` 与 `alerts` 之间。

- [ ] **Step 5: 回归 + ruff + Commit**

```bash
uv run pytest -q tests/storage tests/services/test_break_glass.py tests/web/test_admin_annotations_web.py tests/web
uv run ruff check --no-cache src tests
git add src/sams/storage/repos/annotations.py src/sams/storage/repos/attachments.py src/sams/web/pages/admin_annotations.py src/sams/web/templates/admin_annotations.html src/sams/web/templates/admin_annotation_detail.html src/sams/web/templates/admin_users.html src/sams/web/app.py tests/storage/test_annotations_repo.py tests/storage/test_attachments_repo.py tests/web/test_admin_annotations_web.py
git commit -m "feat(admin): /admin/annotations——元数据列表、策略开关、用量与 break-glass 读正文（§6.1/D139）"
```

---

## Task 6: 总 spec §14 同步与收尾文档

**Files:**
- Modify: `docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md`（§14 的 M4 小节追加「M4-B 验收状态」一段）
- Modify: `README.md`（若有功能/路由清单则补两行；没有就不动）

- [ ] **Step 1: 写验收状态**

`grep -n "### M4" docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md` 定位 M4 小节末尾，按 M1/M2 小节的同款风格追加（数字留空由控制器收尾时按当次全量填）：

```markdown
**M4-B 验收状态（2026-10-26）**：批注（B1 私有 + B2 团队可见、搜索、附件、软删 30 天硬清除、
revision 留痕）、推送上下文（接收人可见 3 条、`notify_log.context_json` 记实带入的 id）、
告警闭环 UI（五类动作、原因码、稍后/撤销、事件历史、`/review` 复盘工作台）、
admin 侧（`/admin/annotations` 元数据与用量、团队共享开关、D139 break-glass：单条 15 分钟、
每人每小时 5 次、理由进审计）全部落地。
关键偏差都有出处：`handled` 保留为 acted 不迁旧数据（D124）、trade 引用由服务端铸（D125）、
附件缩略图与张数同源、admin 附件走新加的 `/admin/.../attachments/...`（「同权」的可执行形状）。
离线 `uv run pytest -q` <全量数> passed，全绿。
```

- [ ] **Step 2: README**

`grep -n "路由\|页面\|/review\|/alerts" README.md`——若存在页面/路由清单，补 `/annotations 批注搜索` 与 `/admin/annotations 批注管理（admin）` 两行；若无此类清单，README 不动（备份一节计划 4 已写）。

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md README.md
git commit -m "docs(specs): 总 spec §14 记 M4-B 验收状态；README 页面清单补搜索与管理页"
```

（若 Step 2 没改 README，从 `git add` 去掉它。）

---

## 收尾（控制器执行，不属于任何单个任务）

- [ ] `uv run pytest -q` 全量——全绿；任何红点回对应任务修，不许改测试凑数。
- [ ] `uv run ruff check --no-cache src tests` 全绿。
- [ ] `git status`——未跟踪只允许 `qodercli_resume.sh` 与 `tmp/`。
- [ ] 把全量条数回填 Task 6 那段「<全量数>」（单独一次 `docs` 提交或 amend 前与用户确认过再动）。
- [ ] `git push origin main`（只有控制器推送）。

## 试运行部署（用户指令：开发完把系统启动起来，交易专家从界面测试验证）

推送完成后控制器按序执行：

- [ ] 建试用账号（不给 admin 权）：
  ```bash
  SAMS_SECRET_KEY=<与 serve 同一个> uv run sams user add trader1 --password '<生成>'
  SAMS_SECRET_KEY=<同> uv run sams user add trader2 --password '<生成>'
  ```
- [ ] 起服务（真实配置、默认端口）：`SAMS_SECRET_KEY=<同> nohup uv run sams serve > tmp/serve.log 2>&1 &`（`tmp/` 不入库）。
- [ ] 把 URL、两份账号口令与「专家验证单」交给用户：单子上按旅程排——自选加票→看板报价→建规则→测试告警→收件箱已读/处置（含录一笔模拟成交）/忽略/稍后→5 分钟撤销→标的批注（带截图附件、团队可见选项）→推送上下文可见→`/annotations` 搜索→`/review` 三窗口→（若用户想再看 admin）管理页策略开关与 break-glass。
- [ ] 同名单派 2-3 个「股票交易专家」子代理各自独立跑一遍（沿用 M2 第三道闸的做法：专家从界面提问、核对屏幕上的话与库里的数是否同一件事），回收缺陷清单；每条缺陷由控制器复现后立 fix 或记入下期。

## 与 spec 的对账单

| spec 条目 | 落点 | 偏差 |
|---|---|---|
| §4.2 团队写路径与策略闸 | Task 1 | 闸只拦「私有翻团队」这一跳；保持既有 team 行不受策略开关影响（否则 off 时作者连改个错别字都不行，§4.2「不删、只是隐藏」落空） |
| §4.2 off 时作者仍见「已关闭」提示 | Task 1 行徽标 | 徽标用中文「团队共享当前已关闭」，`visibility` 徽标本身不变色 |
| §4.5 搜索（q/tag/type/scope、分页、不泄漏） | Task 3 | clause 抽成 `_visibility_clauses` 一份规则，`count_visible` 与列表同源；`page/scope` 野值回默认、`type` 野值报错（见 Task 3 端点 docstring） |
| §4.5 目标已删除显示徽标 | Task 3 `_target_gone` | 标的永不算已删（主档只增） |
| §4.3 编辑可改可见性 | Task 1 edit 端点 | revision meta 的可见性 before/after 仓库早就写 |
| §4.3 revision meta 记附件 id | Task 4 `stamp_last_revision_attachments` | 附件增删在 revision 落库之后发生（新 id 传完才知道），故**补写**最新 revision 的 meta 而不是随 update 一起写 |
| §5.4 附件删除 | Task 4 `AttachmentService.remove` | 行先删、文件后 unlink，崩中间留孤儿给 prune（与新增同一对偶） |
| §6.2 缩略图 3 张封顶 | Task 4 改 `THUMB_LIMIT` 为配置的 `max_attachments` | 编辑器要能勾选到每一张，封顶与配额同源 |
| D139 break-glass：单条/15min/5 次/hour/≥4 字/留痕 | Task 2 + Task 5 | 规则在服务层（无 DB 可测）；小时窗按 `created_at` 数行，不做滑动桶 |
| D139「附件与 revision 同权」 | Task 5 新路由 `/admin/annotations/{id}/attachments/{att_id}` | spec 路由表没有这条 GET——不加它「同权」只能看缩略图断链；`Cache-Control: no-store` |
| §6.1 `/admin/annotations` 四路由 | Task 5 | policy 的 `notice` 走 query param（admin 区无 flash 机制） |
| 总 spec §14 同步 | Task 6 | 全量条数由控制器收尾回填 |
| 交易专家界面试用 | 收尾「试运行部署」 | 沿用 M2 第三道闸的专家子代理做法 + 真人账号双轨 |

---

**计划完。** 执行方式：subagent-driven——每任务一个全新 implementer（带本文件行号范围），spec-compliance 评审过了再走 code-quality 评审，控制器跑全量并推送；连续执行不中途停顿。
