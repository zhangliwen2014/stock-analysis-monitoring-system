"""`app_settings`：常驻期间改完即生效的那几个策略（D127、§4.2）。

白名单在仓库层——spec 把「未知键读写都拒绝」写在 §9，而「读」的第一个调用方
就是推送 provider：闸放在调用方等于每个调用方自己记得放。
5 秒缓存的坏消息要说清：「改完即生效」在跨进程时最长滞后 5 秒，那是缓存的代价，
写路径会立刻废掉本进程的缓存条目。
"""

from __future__ import annotations

import time

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.app_settings import TEAM_SHARING, AppSettingsRepo


def test_the_migration_row_says_team_sharing_is_on(db):
    repo = AppSettingsRepo(db)
    assert repo.get(TEAM_SHARING, default="on") == "on"
    assert repo.team_sharing() is True


def test_an_unknown_key_is_refused_on_both_sides(db):
    repo = AppSettingsRepo(db)
    with pytest.raises(ValidationError, match="白名单"):
        repo.get("alerts.everything", default="")
    with pytest.raises(ValidationError, match="白名单"):
        repo.set("alerts.everything", "on")


def test_a_bad_value_for_the_known_key_is_refused(db):
    repo = AppSettingsRepo(db)
    with pytest.raises(ValidationError, match="on/off"):
        repo.set(TEAM_SHARING, "maybe")


def test_set_flips_the_value_and_records_the_operator(db):
    repo = AppSettingsRepo(db)
    repo.set(TEAM_SHARING, "off", updated_by="admin-1")
    assert repo.team_sharing() is False
    row = db.query("SELECT updated_by FROM app_settings WHERE key = ?", [TEAM_SHARING]).to_dicts()
    assert row[0]["updated_by"] == "admin-1"


def test_a_write_from_another_repo_instance_is_seen_after_the_cache_expires(db, monkeypatch):
    """缓存必须存在（否则每次批注读取都问一遍库）也必须过期（否则「改完即生效」是谎）。

    把 `time.monotonic` 拨快 6 秒而不是真等：钉的是「按单调钟过期」这个实现选择。
    """
    first = AppSettingsRepo(db)
    assert first.team_sharing() is True
    AppSettingsRepo(db).set(TEAM_SHARING, "off")
    assert first.team_sharing() is True  # 5 秒内的旧答案
    real = time.monotonic
    monkeypatch.setattr(
        "sams.storage.repos.app_settings.time.monotonic", lambda: real() + 6.0
    )
    assert first.team_sharing() is False
