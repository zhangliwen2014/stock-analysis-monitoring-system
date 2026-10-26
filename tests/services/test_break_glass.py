"""break-glass 用例（D139）：单条、限时、限次、留痕——四条都在服务层钉。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import Conflict, NotFound, ValidationError
from sams.services.admin_access import BreakGlassService
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
def svc(db, clock, annotations_store, settings):
    logs = AdminAccessRepo(db, clock=clock)
    # settings 走全局夹具：默认 15/5，正是原来的写死值——断言语义不变，只是数字换出处
    return BreakGlassService(logs, annotations_store, settings, clock=clock)


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
    # 测试照它的样子补一行（见计划头部「纯删除补记」）
    annotations_store.db.execute(
        "UPDATE annotations SET purged_at = ? WHERE id = ?",
        [clock.now_utc(), made.id],
    )
    with pytest.raises(Conflict, match="已被清除"):
        svc.request_access("adm", made.id, "核对除权口径")


def test_grant_lasting_the_window(svc, annotations_store):
    made = _own_annotation(annotations_store)
    minutes = svc.settings.annotations.break_glass_minutes
    log = svc.request_access("adm", made.id, "核对除权口径")
    assert log.granted_until == NOW + timedelta(minutes=minutes)
    assert svc.can_read("adm", made.id, now=NOW)
    assert not svc.can_read("adm", made.id, now=NOW + timedelta(minutes=minutes + 1))


def test_hourly_cap_per_admin(svc, annotations_store):
    cap = svc.settings.annotations.break_glass_max_per_hour
    ids = [_own_annotation(annotations_store, user_id=f"u-{n}").id for n in range(cap + 1)]
    for annotation_id in ids[:cap]:
        svc.request_access("adm", annotation_id, "核对除权口径")
    with pytest.raises(Conflict, match="每小时"):
        svc.request_access("adm", ids[cap], "核对除权口径")
    # 限次是按管理员算的：换个人照样能批
    assert svc.request_access("adm-2", ids[cap], "核对除权口径") is not None


def test_grant_window_follows_settings(db, clock, annotations_store, settings):
    # 治理参数只有一个出处：把时长调到 1 分钟，授权窗口就得跟着缩——
    # 还写死 15 的话这条会红
    tuned = replace(settings, annotations=replace(settings.annotations, break_glass_minutes=1))
    logs = AdminAccessRepo(db, clock=clock)
    svc = BreakGlassService(logs, annotations_store, tuned, clock=clock)
    made = _own_annotation(annotations_store)
    log = svc.request_access("adm", made.id, "核对除权口径")
    assert log.granted_until == NOW + timedelta(minutes=1)
    assert not svc.can_read("adm", made.id, now=NOW + timedelta(minutes=1))


def test_audit_row_pins_owner_admin_and_trimmed_reason(svc, annotations_store):
    made = _own_annotation(annotations_store, user_id="u-1")
    # 理由带首尾空白：落库的必须是裁好的那一串（审计要答得出「为什么看」）
    svc.request_access("adm", made.id, "  核对除权口径  ")
    row = svc.logs.active("adm", made.id, now=NOW)
    assert row is not None
    # owner 与 admin 各钉各的值：把审批人当作者写反，这条当场红
    assert row.owner_user_id == made.user_id
    assert row.admin_user_id == "adm"
    assert row.reason == "核对除权口径"
