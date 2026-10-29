"""写面动过谁，版本闸就得响谁：盘中缓存整表失效的输入就是这几次 bump。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.version import VersionGate
from sams.services.ledger import LedgerService
from sams.services.rules.derive import RuleDeriver
from sams.services.rules.maintenance import RuleService
from sams.services.scheduler.types import JobDeps
from sams.storage.crypto import cipher_for
from sams.storage.repos.channels import ChannelRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

T0 = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)
NAME, PWD = "u-bump", "long-enough-to-hash"


class NoTargets:
    def expand(self, rule):
        return []


def make_service(db, settings, gate) -> RuleService:
    return RuleService(
        rules=RuleRepo(db),
        theses=ThesisRepo(db),
        accounts=SimAccountRepo(db),
        channels=ChannelRepo(db, cipher=cipher_for(settings)),
        users=UserRepo(db),
        data=NoTargets(),
        db=db,
        settings=settings,
        versions=gate,
    )


def test_rule_writes_bump_the_rules_version(db, settings):
    gate = VersionGate()
    service = make_service(db, settings, gate)
    uid = service.users.create(NAME, PWD).id
    base = gate.current("rules")
    added = service.add(
        uid,
        kind="stop.fixed",
        name="跌破 10 元",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
    )
    assert gate.current("rules") == base + 1
    service.set_enabled(uid, added.rule.id, False)
    assert gate.current("rules") == base + 2
    service.remove(uid, added.rule.id)
    assert gate.current("rules") == base + 3


def test_edit_bumps_too(db, settings):
    gate = VersionGate()
    service = make_service(db, settings, gate)
    uid = service.users.create(NAME, PWD).id
    added = service.add(
        uid,
        kind="stop.fixed",
        name="线",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
    )
    base = gate.current("rules")
    service.edit(uid, added.rule.id, params={"price": 11.0})
    assert gate.current("rules") == base + 1


def test_edit_writing_back_to_a_thesis_bumps_theses_too(db, settings):
    """改一条派生规则会回写论上的价位（`stop_price`），那一头也得响。

    `changed_since` 是按种类各问各的（`core/version.py`）：只 bump `rules` 的话，
    读「论的那几列」的使用者拿到的仍是旧快照，而屏幕上那条止损线已经动了。
    """
    gate = VersionGate()
    service = make_service(db, settings, gate)
    user = service.users.create(NAME, PWD)
    thesis = service.theses.create(
        user_id=user.id,
        market="A",
        code="601088",
        style="value",
        actor_id=user.id,
        now=T0,
        status="proposed",
        stop_price=28.0,
    )
    added = service.add(
        user.id,
        kind="stop.fixed",
        name="跌破 10 元",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
        thesis_id=thesis.id,
    )
    before = gate.snapshot()
    report = service.edit(user.id, added.rule.id, params={"price": 11.0})
    assert report.written_back == {"stop_price": 11.0}
    assert gate.current("rules") == before["rules"] + 1
    assert gate.current("theses") == before["theses"] + 1


def test_deriver_bumps_only_when_it_created_something(db, settings):
    gate = VersionGate()
    theses = ThesisRepo(db)
    user = UserRepo(db).create(NAME, PWD)
    thesis = theses.create(
        user_id=user.id,
        market="A",
        code="601088",
        style="value",
        actor_id=user.id,
        now=T0,
        status="proposed",
        entry_low=32.0,
        entry_high=36.0,
        stop_price=28.0,
        target_price=52.0,
    )
    deriver = RuleDeriver(RuleRepo(db), theses, versions=gate)
    first = deriver.derive(user.id, thesis.id)
    assert first.created and gate.current("rules") == 1
    deriver.derive(user.id, thesis.id)
    assert gate.current("rules") == 1  # 第二趟全是 already：库没变，不该把缓存全清


def test_ledger_writes_bump_the_ledger_version(db, settings):
    gate = VersionGate()
    users = UserRepo(db)
    user = users.create(NAME, PWD)
    service = LedgerService(
        SimAccountRepo(db),
        SimOrderRepo(db),
        ThesisRepo(db),
        users,
        settings,
        versions=gate,
    )
    base = gate.current("ledger")
    account = service.open_account(NAME, "SIM-1")
    assert gate.current("ledger") == base + 1
    service.deposit(
        account.id, "CNY", 10000.0, username=NAME, reason="初始入金", actor_id=user.id
    )
    assert gate.current("ledger") == base + 2
    order = service.buy(account.id, NAME, "A", "601088", qty=10, price=10.0)
    assert gate.current("ledger") == base + 3
    service.undo_fill(NAME, account.id, order.id)
    assert gate.current("ledger") == base + 4
    service.convert(NAME, account.id, "CNY", "HKD", 100.0, 1.08)
    assert gate.current("ledger") == base + 5


def test_jobdeps_carries_a_gate_by_default():
    deps = JobDeps(
        settings=None,
        registry=None,
        sync=None,
        corp_actions=None,
        fundamentals=None,
        fx=None,
        calendar=None,
        rules=None,
        recheck=None,
        users=None,
    )
    assert isinstance(deps.versions, VersionGate)
