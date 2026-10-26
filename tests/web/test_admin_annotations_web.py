"""`/admin/annotations`（§6.1/D139）：元数据列表、策略开关、break-glass 读正文、附件同权。

这一屏被钉住的几件事：
* 五个入口全部走 `require_admin`——普通登录用户带着真 CSRF 也是 403，挡人的是角色那道闸；
* 默认视野是「有哪些行」而不是「写了什么」：列表行有「私有」徽标与 id，但没有正文串；
  正文、revision 与附件只在 team+策略开、或对**这一条**有未过期授权时才出现；
  被遮蔽的行连 `target_label` 与标签文字都不上屏（spec:340/549——元数据本身会泄密）；
* 被拒的 break-glass 不许吞状态码：短理由 400、超滑动窗口 429（带等待秒数）、
  墓碑 409，且都回详情页重渲染（人要看见「为什么不能读」的下一句是可用的表单，
  不是错误页一座）；
* 附件走 admin 自己那条下载路由并有 `Cache-Control: no-store`：私人正文不许进中间缓存；
  无有效授权时按不存在返回 404（spec:343——403 会把「有这一条」泄露出去）。

过期一类的用例把 app 装在 `FrozenClock` 上并冻在**将来**：`auth._active_ok` 拿心跳与
真钟比（`pages/admin.py` 同一口径），时钟回到过去会让所有会话当场过期。
"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from support import FrozenClock

#: 页面层此刻唯一能断言的入口拒绝：`require_admin` 对普通用户一律 403
#: （`test_admin_web.py` 的同一批入口钉的是同一条规则）
ALL_ROUTES = [
    ("get", "/admin/annotations"),
    ("post", "/admin/annotations/policy"),
    ("get", "/admin/annotations/{aid}"),
    ("post", "/admin/annotations/{aid}/break-glass"),
    ("get", "/admin/annotations/{aid}/attachments/{att}"),
]


def _login(client, username, password):
    resp = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    return client.cookies.get("sams_csrf").rpartition(".")[0]


def stored_policy(ctx) -> str:
    """绕过 `AppSettingsRepo` 的 5 秒缓存直接问库：策略写入落没落地只能这么钉
    （夹具在另一个线程上下文里，拿的是另一份缓存）。"""
    row = ctx.app_settings.db.query(
        "SELECT value FROM app_settings WHERE key = ?", ["annotations.team_sharing"]
    ).to_dicts()
    return str(row[0]["value"]) if row else ""


@pytest.fixture
def admin_app(make_app):
    """alice 是 admin（`web_app` 夹具里她本来就是），这一份额外把时钟冻在将来：
    过期/限次的用例要推进时钟，而会话校验用的是真钟。
    """
    frozen = datetime.now(UTC) + timedelta(days=400)
    web = make_app(clock=FrozenClock(frozen))
    ctx = web.contexts.current()
    ctx.users.create("alice", "correct-horse-battery", is_admin=True)
    with TestClient(web.app) as client:
        csrf = _login(client, "alice", "correct-horse-battery")
        yield SimpleNamespace(client=client, ctx=ctx, csrf=csrf, clock=frozen)


def _seed_symbol(ctx):
    ctx.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["600519"],
                "name": ["贵州茅台"],
                "currency": ["CNY"],
            }
        )
    )


def _bob(ctx):
    return ctx.users.create("bob", "correct-horse-battery")


def _private_of_bob(admin_app, body="bob 的私有正文"):
    """bob 名下的一条私有批注：admin 默认读不到正文，正是 break-glass 的对象。"""
    ctx = admin_app.ctx
    _seed_symbol(ctx)
    return ctx.annotations.create(
        _bob(ctx).id, target_type="symbol", target_ref="A:600519", body_md=body
    )


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    return buf.getvalue()


# --- 入口闸 --------------------------------------------------------------------------------


def test_page_requires_admin(make_app):
    """五个入口全部挡普通用户，且 403 落在「目标存不存在」之前（403 只留给角色那道闸）。"""
    web = make_app()
    ctx = web.contexts.current()
    ctx.users.create("alice", "correct-horse-battery", is_admin=True)
    bob = _bob(ctx)
    made = ctx.annotations.create(
        bob.id, target_type="symbol", target_ref="A:600519", body_md="私有"
    )
    with TestClient(web.app) as client:
        csrf = _login(client, "bob", "correct-horse-battery")
        for method, path in ALL_ROUTES:
            target = path.format(aid=made.id, att="ghost")
            if method == "get":
                resp = client.get(target)
            else:
                data = {"csrf": csrf, "value": "on", "reason": "核对除权口径"}
                resp = client.post(target, data=data)
            assert resp.status_code == 403, target


# --- 策略卡 --------------------------------------------------------------------------------


def test_policy_toggle_roundtrip(admin_app):
    page = admin_app.client.get("/admin/annotations")
    assert page.status_code == 200
    assert "团队共享" in page.text
    resp = admin_app.client.post(
        "/admin/annotations/policy",
        data={"csrf": admin_app.csrf, "value": "off"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # 5 秒缓存是仓库那道读闸的本意（`AppSettingsRepo.team_sharing`）：夹具在**另一个线程
    # 上下文**里拿着另一份缓存，断言「写入落库」要直接问库，不能问缓存
    assert stored_policy(admin_app.ctx) == "off"
    # 跟进跳转：那句话要真的落在屏幕上（只看 Location 时，没实现的反馈也是绿的）
    after = admin_app.client.post(
        "/admin/annotations/policy",
        data={"csrf": admin_app.csrf, "value": "on"},
        follow_redirects=True,
    )
    assert "策略已切到 开" in after.text
    assert stored_policy(admin_app.ctx) == "on"


def test_policy_rejects_a_stray_value(admin_app):
    resp = admin_app.client.post(
        "/admin/annotations/policy", data={"csrf": admin_app.csrf, "value": "maybe"}
    )
    assert resp.status_code == 400
    assert stored_policy(admin_app.ctx) != "maybe", "被拒的一次不许留下半个写入"


# --- 元数据列表 ----------------------------------------------------------------------------


def test_metadata_lists_teammate_without_body(admin_app):
    """列表行的默认视野：有「私有」徽标与 id，没有正文串（遮不遮是页面层的事）。"""
    made = _private_of_bob(admin_app, body="只给自己看的那句话")
    page = admin_app.client.get("/admin/annotations")
    assert page.status_code == 200
    assert "私有" in page.text
    assert made.id in page.text
    assert "只给自己看的那句话" not in page.text


def test_recent_break_glass_table_shows_reason(admin_app):
    made = _private_of_bob(admin_app)
    resp = admin_app.client.post(
        f"/admin/annotations/{made.id}/break-glass",
        data={"csrf": admin_app.csrf, "reason": "核对除权口径与历史成交"},
        follow_redirects=True,
    )
    assert "已授权 15 分钟" in resp.text
    page = admin_app.client.get("/admin/annotations")
    assert "核对除权口径与历史成交" in page.text


# --- 详情 + break-glass --------------------------------------------------------------------


def test_break_glass_unlocks_body_then_expires(admin_app):
    made = _private_of_bob(admin_app, body="私有正文在这里")
    detail = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert detail.status_code == 200
    assert "私有正文在这里" not in detail.text
    assert "reason" in detail.text, "不可读时表单要在场：下一句是可用的表单，不是死路"

    resp = admin_app.client.post(
        f"/admin/annotations/{made.id}/break-glass",
        data={"csrf": admin_app.csrf, "reason": "核对除权口径"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # httpx 会把 Location 里的中文写成百分号串：比之前先解回来
    assert "已授权 15 分钟" in unquote(resp.headers["location"])

    shown = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert "私有正文在这里" in shown.text

    # 推进注入的时钟到窗口之外：正文、revision 与附件一起收回去
    admin_app.ctx.clock.advance(minutes=16)
    gone = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert "私有正文在这里" not in gone.text
    assert "reason" in gone.text


def test_short_reason_is_refused(admin_app):
    """被拒也回详情页重渲染：400 + 服务那句话原样上屏（两处措辞迟早分叉）。"""
    made = _private_of_bob(admin_app)
    resp = admin_app.client.post(
        f"/admin/annotations/{made.id}/break-glass",
        data={"csrf": admin_app.csrf, "reason": "看看"},
    )
    assert resp.status_code == 400
    assert "至少 4 个字" in resp.text


def test_long_reason_is_refused(admin_app):
    """200 字上限（spec §6.3/D139）从这条路由也要能撞见：消息点名上限。"""
    made = _private_of_bob(admin_app)
    resp = admin_app.client.post(
        f"/admin/annotations/{made.id}/break-glass",
        data={"csrf": admin_app.csrf, "reason": "核" * 201},
    )
    assert resp.status_code == 400
    assert "200" in resp.text


def test_hourly_cap_surfaces(admin_app):
    """第 6 次是 429 页（spec:342/530）：带「等待」与秒数——「等多久」是这一屏要答的话。"""
    ctx = admin_app.ctx
    _seed_symbol(ctx)
    bob = _bob(ctx)
    ids = [
        ctx.annotations.create(
            bob.id, target_type="symbol", target_ref="A:600519", body_md=f"第 {n} 条"
        ).id
        for n in range(6)
    ]
    for annotation_id in ids[:5]:
        ok = ctx.break_glass.request_access(
            ctx.users.get_by_username("alice").id, annotation_id, "核对除权口径"
        )
        assert ok is not None
    resp = admin_app.client.post(
        f"/admin/annotations/{ids[5]}/break-glass",
        data={"csrf": admin_app.csrf, "reason": "核对除权口径"},
    )
    assert resp.status_code == 429
    assert "每小时" in resp.text
    assert "等待" in resp.text, "超限那句必须给出等待时间（spec:342）"


def test_team_body_readable_without_grant(admin_app):
    """team + 策略开：正文直接可读，不用批授权（D121 的形状——挂的是 symbol）。"""
    ctx = admin_app.ctx
    _seed_symbol(ctx)
    made = ctx.annotations.create(
        _bob(ctx).id,
        target_type="symbol",
        target_ref="A:600519",
        body_md="团队的公开判断",
        visibility="team",
    )
    page = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert "团队的公开判断" in page.text


def test_team_hidden_when_policy_off(admin_app):
    """策略关掉后 team 行也读不到正文——但 break-glass 仍然批得动（这正是它的用途）。

    先写成 team 再关策略：关着的时候**新写** team 会被服务拒（D127），
    而既有行不消失，只是对非作者（包括 admin）隐藏。
    """
    ctx = admin_app.ctx
    _seed_symbol(ctx)
    made = ctx.annotations.create(
        _bob(ctx).id,
        target_type="symbol",
        target_ref="A:600519",
        body_md="策略关着的团队正文",
        visibility="team",
    )
    ctx.app_settings.set("annotations.team_sharing", "off", updated_by=None)
    page = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert "策略关着的团队正文" not in page.text

    admin_app.client.post(
        f"/admin/annotations/{made.id}/break-glass",
        data={"csrf": admin_app.csrf, "reason": "核对除权口径"},
        follow_redirects=True,
    )
    after = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert "策略关着的团队正文" in after.text


def test_purged_tombstone_says_body_gone(admin_app):
    """purged 行 detail 不 404：墓碑要能问得到（`include_deleted=True`），
    而屏幕上说的是「正文已被清除」——即便此刻有一条未过期的授权。"""
    ctx = admin_app.ctx
    made = _private_of_bob(admin_app)
    ctx.break_glass.request_access(
        ctx.users.get_by_username("alice").id, made.id, "核对除权口径"
    )
    # 仓库无 purge()：照 services/annotation_prune.py 的裸 SQL 写 purged_at
    ctx.annotations_store.db.execute(
        "UPDATE annotations SET purged_at = ? WHERE id = ?",
        [ctx.clock.now_utc(), made.id],
    )
    page = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert page.status_code == 200
    assert "正文已被清除" in page.text


# --- 附件同权 ------------------------------------------------------------------------------


def test_attachment_download_by_admin_grant(admin_app):
    """有授权 200 + no-store；无授权 404（spec:343：与「不存在」同一句，不泄露存在性）；
    库里有行、文件没了 404（那是授权之后的事）；墓碑 404。"""
    ctx = admin_app.ctx
    alice = ctx.users.get_by_username("alice")
    made = _private_of_bob(admin_app)
    # 附件按构造是**作者**传上来的：服务层认归属，admin 不会替别人传图
    att = ctx.attachments.add(made.user_id, made.id, filename="截图.png", data=_png_bytes())
    # 不是作者、也没有授权：按不存在回答——403 会说漏「这条私有批注真的有附件」
    denied = admin_app.client.get(f"/admin/annotations/{made.id}/attachments/{att.id}")
    assert denied.status_code == 404
    assert "附件不存在" in denied.text

    ctx.break_glass.request_access(alice.id, made.id, "核对除权口径")
    ok = admin_app.client.get(f"/admin/annotations/{made.id}/attachments/{att.id}")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/")
    # no-store 是有意的：私人正文不许进中间缓存
    assert ok.headers["cache-control"] == "no-store"

    # 授权过期后同一串地址读不到任何东西：与无授权同一个 404
    ctx.clock.advance(minutes=16)
    expired = admin_app.client.get(f"/admin/annotations/{made.id}/attachments/{att.id}")
    assert expired.status_code == 404

    # 库里还有行、文件不在了：404 要说清是「文件缺失」而不是「没这一条」
    ctx.break_glass.request_access(alice.id, made.id, "核对除权口径")
    ctx.attachments.path_of(att).unlink()
    missing = admin_app.client.get(f"/admin/annotations/{made.id}/attachments/{att.id}")
    assert missing.status_code == 404
    assert "附件文件缺失" in missing.text

    # 墓碑没什么可下载：与「不存在」同一个回答（purged 行连列表都不出现）
    ctx.annotations_store.db.execute(
        "UPDATE annotations SET purged_at = ? WHERE id = ?", [ctx.clock.now_utc(), made.id]
    )
    purged = admin_app.client.get(f"/admin/annotations/{made.id}/attachments/{att.id}")
    assert purged.status_code == 404
    # 三种 404 必须同一个身子：不存在 / 已被清除 / 没授权在屏幕上不许读出任何区别
    #（403→404 改的就是「不泄露存在性」，多一句文案也算泄露）
    ghost = admin_app.client.get("/admin/annotations/ghost/attachments/ghost")
    assert ghost.status_code == 404
    assert purged.text == denied.text == ghost.text


def test_detail_lists_attachment_only_when_readable(admin_app):
    """附件与正文同权：不可读时详情页连图片链接都不出现。"""
    ctx = admin_app.ctx
    alice = ctx.users.get_by_username("alice")
    made = _private_of_bob(admin_app)
    att = ctx.attachments.add(made.user_id, made.id, filename="图.png", data=_png_bytes())
    page = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert f"/admin/annotations/{made.id}/attachments/{att.id}" not in page.text

    ctx.break_glass.request_access(alice.id, made.id, "核对除权口径")
    shown = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert f"/admin/annotations/{made.id}/attachments/{att.id}" in shown.text


# --- 遮蔽 target_label 与标签（spec:340/549）------------------------------------------------


def test_masked_rows_hide_target_label_and_tags(admin_app):
    """不可读的行：列表与详情头部都不许出现 label 与标签文字；symbol 只出代码，
    非 symbol 出字面量「私有 <type>」——元数据本身泄密就不算遮蔽（D139）。"""
    ctx = admin_app.ctx
    _seed_symbol(ctx)
    bob = _bob(ctx)
    sym = ctx.annotations.create(
        bob.id,
        target_type="symbol",
        target_ref="A:600519",
        body_md="私有正文",
        tags=["渠道内幕"],
    )
    thesis = ctx.theses.add("bob", "A", "600519").thesis.id
    th = ctx.annotations.create(
        bob.id, target_type="thesis", target_ref=thesis, body_md="观点私有", tags=["加仓思路"]
    )
    page = admin_app.client.get("/admin/annotations")
    assert "贵州茅台" not in page.text
    assert "渠道内幕" not in page.text
    assert "加仓思路" not in page.text
    assert "A:600519 观点" not in page.text
    assert "600519" in page.text, "symbol 遮蔽后仍要给代码：admin 得知道挂的是谁"
    assert "私有 thesis" in page.text
    detail = admin_app.client.get(f"/admin/annotations/{sym.id}")
    assert "贵州茅台" not in detail.text and "渠道内幕" not in detail.text
    assert "600519" in detail.text
    th_detail = admin_app.client.get(f"/admin/annotations/{th.id}")
    assert "私有 thesis" in th_detail.text and "A:600519 观点" not in th_detail.text


def test_readable_rows_show_label_and_tags(admin_app):
    """遮蔽不许顺手关掉可读行：team + 策略开时 label 与标签照上屏（spec:340 前半句）。"""
    ctx = admin_app.ctx
    _seed_symbol(ctx)
    made = ctx.annotations.create(
        _bob(ctx).id,
        target_type="symbol",
        target_ref="A:600519",
        body_md="团队正文",
        tags=["渠道"],
        visibility="team",
    )
    page = admin_app.client.get("/admin/annotations")
    assert "贵州茅台" in page.text and "渠道" in page.text
    detail = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert "贵州茅台" in detail.text and "渠道" in detail.text


# --- 授权横幅（spec:341/549）---------------------------------------------------------------


def test_grant_banner_persists_on_detail(admin_app):
    """有效期内详情页顶部常驻「临时授权至 HH:MM，理由：…」：授权到点和理由都要在场，
    人要随时看得见自己此刻为什么在读别人的私有正文。"""
    made = _private_of_bob(admin_app)
    resp = admin_app.client.post(
        f"/admin/annotations/{made.id}/break-glass",
        data={"csrf": admin_app.csrf, "reason": "核对除权口径"},
        follow_redirects=True,
    )
    assert "临时授权至" in resp.text
    assert "理由：核对除权口径" in resp.text
    page = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert "临时授权至" in page.text and "理由：核对除权口径" in page.text

    admin_app.ctx.clock.advance(minutes=16)
    gone = admin_app.client.get(f"/admin/annotations/{made.id}")
    assert "临时授权至" not in gone.text, "过期后横幅随正文一起收回"


# --- 列表筛选与配额列（spec:340）------------------------------------------------------------


def test_list_filters_by_user_type_visibility_and_window(admin_app):
    """地址栏四个筛选各管各的；`until` 含当天（+1d 开区间）；野值静默忽略（搜索页读法）。"""
    ctx = admin_app.ctx
    _seed_symbol(ctx)
    bob = _bob(ctx)
    carol = ctx.users.create("carol", "correct-horse-battery")
    sym = ctx.annotations.create(
        bob.id, target_type="symbol", target_ref="A:600519", body_md="A 私有"
    )
    thesis = ctx.theses.add("carol", "A", "600519").thesis.id
    th = ctx.annotations.create(carol.id, target_type="thesis", target_ref=thesis, body_md="观点")
    filtered = admin_app.client.get(
        "/admin/annotations",
        params={"user": "bob", "type": "symbol", "visibility": "private"},
    )
    assert sym.id in filtered.text and th.id not in filtered.text
    assert "共 1 条批注" in filtered.text, "总数必须按同一份筛选算：分母与分子同源"
    tz = ZoneInfo(ctx.settings.server.display_tz)
    today = ctx.clock.now_utc().astimezone(tz).date()
    empty = admin_app.client.get(
        "/admin/annotations", params={"until": (today - timedelta(days=1)).isoformat()}
    )
    assert sym.id not in empty.text and "还没有一条批注" in empty.text
    both = admin_app.client.get(
        "/admin/annotations", params={"since": today.isoformat(), "until": today.isoformat()}
    )
    assert sym.id in both.text and th.id in both.text
    bogus = admin_app.client.get(
        "/admin/annotations",
        params={"user": "ghost", "type": "stock", "visibility": "public", "since": "不是日期"},
    )
    assert bogus.status_code == 200
    assert sym.id in bogus.text, "认不出的筛选值等于没筛：不许报错，也不许筛成空"


def test_list_shows_revision_count_and_usage_annotation_count(admin_app):
    """列表有「revision 数」列、用量表有「批注数」列（spec:340）：数字各只有一个出处
    （count_revisions / count_by_user），这里钉的是它们真的上了屏。"""
    ctx = admin_app.ctx
    made = _private_of_bob(admin_app, body="第一版")
    ctx.attachments.add(made.user_id, made.id, filename="图.png", data=_png_bytes())
    for body in ("第二版", "第三版"):
        ctx.annotations_store.update(
            made.id, user_id=made.user_id, body_md=body, tags=[], visibility="private"
        )
    page = admin_app.client.get("/admin/annotations")
    assert "revision" in page.text and "批注数" in page.text
    assert "<td>2</td>" in page.text, "两次编辑 = 两条 revision，要能在行里数出来"


def test_usage_table_shows_users_without_attachments(admin_app):
    """spec:339：用量表行集以批注拥有者驱动——零附件用户的批注数不许隐身。
    `usage_by_user()` 设计上就不列没附件的人；只由附件驱动，「只有批注」的人
    在配额表里永远查无此人。"""
    _private_of_bob(admin_app)  # bob 名下一条批注、零附件
    page = admin_app.client.get("/admin/annotations")
    row = re.search(r"<td>bob</td><td>1</td>\s*<td>0</td><td>0\.0</td>", page.text)
    assert row, "用量表要有 bob 一行：批注 1 / 张数 0 / 0.0 MB"


# --- 审计表扩到 50 条 + 所有者列（spec:340）--------------------------------------------------


def test_recent_logs_show_owner_and_fifty_rows(admin_app):
    """最近授权：一屏 50 条而不是 10 条，且每行带「所有者」——「谁看过谁的什么」
    是这一表要答的完整问题。种 51 条而不是 12 条：只种到上限以下，
    「限 50」和「限 10」都能过，等于没钉住上限。"""
    ctx = admin_app.ctx
    made = _private_of_bob(admin_app)
    alice = ctx.users.get_by_username("alice")
    for n in range(51):
        ctx.admin_access_store.grant(
            admin_user_id=alice.id,
            annotation_id=made.id,
            owner_user_id=made.user_id,
            reason=f"核对第{n}次授权",
            granted_until=ctx.clock.now_utc() + timedelta(minutes=15),
        )
    page = admin_app.client.get("/admin/annotations")
    # 冻钟下 created_at 全等，被挤出去的那一条由 id 决定：数数量，不点名
    shown = [n for n in range(51) if f"核对第{n}次授权" in page.text]
    assert len(shown) == 50, "51 条授权要正好截成最近 50 条"
    assert page.text.count("<th>所有者</th>") >= 2, "审计表也要有所有者列"
