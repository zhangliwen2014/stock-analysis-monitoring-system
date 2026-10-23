from __future__ import annotations

import threading

import pytest

from sams.core.version import DEFAULT_KINDS, VersionGate


def test_a_new_gate_has_a_zero_for_every_declared_kind():
    gate = VersionGate()
    assert gate.kinds == DEFAULT_KINDS
    assert gate.snapshot() == {kind: 0 for kind in DEFAULT_KINDS}


def test_bump_only_moves_its_own_kind():
    gate = VersionGate()
    gate.bump("rules")
    assert gate.current("rules") == 1
    assert gate.current("bars") == 0


def test_a_snapshot_is_a_copy_not_a_live_view():
    gate = VersionGate()
    before = gate.snapshot()
    gate.bump("theses")
    assert before["theses"] == 0 and gate.snapshot()["theses"] == 1


def test_changed_since_answers_the_question_the_cache_asks():
    gate = VersionGate()
    seen = gate.snapshot()
    assert not gate.changed_since(seen)
    gate.bump("ledger")
    assert gate.changed_since(seen)


def test_an_unknown_kind_is_a_programming_error_with_the_known_list():
    gate = VersionGate()
    with pytest.raises(KeyError, match="rules"):
        gate.bump("rulse")
    with pytest.raises(KeyError, match="rules"):
        gate.current("rulse")
    with pytest.raises(KeyError, match="rules"):
        gate.changed_since({"rulse": 0})


def test_concurrent_bumps_do_not_lose_counts():
    gate = VersionGate(kinds=("rules",))
    threads = [
        threading.Thread(target=lambda: [gate.bump("rules") for _ in range(100)]) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert gate.current("rules") == 800
