"""§5.1 的九个盘中 kind：名单钉在模板声明上，测试只认名单。

五条日线专属（stop.time 等）留在 False：盘中一趟不该去读打分与赔率——
那一头的输入（合成、复盘）只有收盘后才更新，盘中判它等于用昨天的分说今天的话。
"""

from __future__ import annotations

import pytest

from sams.core.rules import TEMPLATES, is_intraday

INTRADAY = {
    "stop.fixed",
    "stop.pct",
    "stop.trailing",
    "stop.atr",
    "stop.ma",
    "stop.breakeven",
    "tp.tiered",
    "price.cross_below",
    "price.cross_above",
}
DAILY = {
    "stop.time",
    "stop.thesis_drift",
    "thesis.odds_below",
    "thesis.drift",
    "scheduled.reevaluate",
}


def test_nine_kinds_are_intraday():
    assert {k for k, t in TEMPLATES.items() if t.intraday} == INTRADAY


def test_the_other_five_stay_daily_only():
    # 两枚测试合起来钉死整张表：多出第 15 个 kind 而没标，这里就会红
    assert {k for k, t in TEMPLATES.items() if not t.intraday} == DAILY


def test_is_intraday_answers_the_same_question():
    assert is_intraday("stop.fixed") is True
    assert is_intraday("stop.time") is False


def test_an_unknown_kind_is_a_keyerror_not_a_false():
    # 野 kind 在 `template_for` 也是炸：这里让它同样炸，False 会把「名字打错了」
    # 伪装成「这条不该盘中判」
    with pytest.raises(KeyError):
        is_intraday("stop.madeup")
