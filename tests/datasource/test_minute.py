"""分钟粒度：两条能力的探针、列集与那张意愿表（计划 B）。

`answers` 必须与适配器自己的拒答共用一次判定，否则「拒绝的适配器」与「链自己挑的源」
是两套说法——屏幕会说「这一档点不动」，日志却记着「腾讯正常应答、只是没数据」。
"""

from __future__ import annotations

from sams.datasource.minute import (
    MINUTE_BARS_COLUMNS,
    MINUTE_SERIES_COLUMNS,
    answers,
    empty_minute_bars,
    empty_minute_series,
    has_minute_bars,
    has_minute_series,
)


class _OnlyBars:
    name = "only_bars"
    markets = frozenset({"A"})

    def minute_bars(self, market, code, start, end, *, scale, count):
        return None


def test_the_two_probes_only_ask_whether_the_method_is_there():
    """探针不碰 `markets`：市场维在 `answers` 那张表里问，两处各管各的。"""
    assert has_minute_bars(_OnlyBars())
    assert not has_minute_series(_OnlyBars())


def test_the_minute_column_sets_share_the_daily_shape_where_it_counts():
    """分时与分钟 K 的列集只有一份出处：适配器、服务、测试三处各拼一遍必漂移。"""
    assert MINUTE_SERIES_COLUMNS[:4] == ["market", "code", "date", "time"]
    assert MINUTE_BARS_COLUMNS[:6] == ["market", "code", "date", "clock", "open", "high"]


def test_the_empty_minute_answer_has_the_same_shape_as_a_real_one():
    """调用方不该为「这个源一条都没给」写分支：空帧的列集与真帧一致。"""
    assert empty_minute_series().columns == MINUTE_SERIES_COLUMNS
    assert empty_minute_bars().columns == MINUTE_BARS_COLUMNS


def test_answers_defaults_to_yes_when_the_source_declares_no_table():
    """没声明 `<方法>_markets` 的源按「都答」处理：漏声明不是拒绝的理由。"""
    assert answers(_OnlyBars(), "minute_bars", "US")


def test_answers_reads_the_per_method_willingness_table():
    """一张表两档各自可关：腾讯答得了分时、答不了美股，也答不了任何市场的分钟 K。"""

    class Fussy(_OnlyBars):
        minute_bars_markets = frozenset({"A"})

    assert answers(Fussy(), "minute_bars", "A")
    assert not answers(Fussy(), "minute_bars", "HK")
    assert answers(Fussy(), "minute_series", "US")  # 没声明 = 不关这一档
