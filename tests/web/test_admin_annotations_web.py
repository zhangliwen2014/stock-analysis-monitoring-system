"""`/admin/annotations`（§6.1/D139）：元数据列表、策略开关、break-glass 读正文、附件同权。

这一屏被钉住的几件事：
* 五个入口全部走 `require_admin`——普通登录用户带着真 CSRF 也是 403，挡人的是角色那道闸；
* 默认视野是「有哪些行」而不是「写了什么」：列表行有「私有」徽标与 id，但没有正文串；
  正文、revision 与附件只在 team+策略开、或对**这一条**有未过期授权时才出现；
* 被拒的 break-glass 不许吞状态码：短理由 400、超次 409，且都回详情页重渲染
  （人要看见「为什么不能读」的下一句是可用的表单，不是错误页一座）；
* 附件走 admin 自己那条下载路由并有 `Cache-Control: no-store`：私人正文不许进中间缓存。

过期一类的用例把 app 装在 `FrozenClock` 上并冻在**将来**：`auth._active_ok` 拿心跳与
真钟比（`pages/admin.py` 同一口径），时钟回到过去会让所有会话当场过期。
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote

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
    """第 6 次是 409 页：Conflict 不许被折成 400——「稍后再来」与「你写错了」是两件事。"""
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
    assert resp.status_code == 409
    assert "每小时" in resp.text


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
    """有授权 200 + no-store；无授权 403；库里有行、文件没了 404；墓碑 404。"""
    ctx = admin_app.ctx
    alice = ctx.users.get_by_username("alice")
    made = _private_of_bob(admin_app)
    # 附件按构造是**作者**传上来的：服务层认归属，admin 不会替别人传图
    att = ctx.attachments.add(made.user_id, made.id, filename="截图.png", data=_png_bytes())
    # 不是作者、也没有授权：普通下载路由会先被「不是作者」拦死，这里 403
    denied = admin_app.client.get(f"/admin/annotations/{made.id}/attachments/{att.id}")
    assert denied.status_code == 403

    ctx.break_glass.request_access(alice.id, made.id, "核对除权口径")
    ok = admin_app.client.get(f"/admin/annotations/{made.id}/attachments/{att.id}")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/")
    # no-store 是有意的：私人正文不许进中间缓存
    assert ok.headers["cache-control"] == "no-store"

    # 授权过期后同一串地址读不到任何东西
    ctx.clock.advance(minutes=16)
    expired = admin_app.client.get(f"/admin/annotations/{made.id}/attachments/{att.id}")
    assert expired.status_code == 403

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
