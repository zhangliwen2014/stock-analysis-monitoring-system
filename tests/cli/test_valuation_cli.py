"""`sams analysis valuation` 的出口契约（M2-D，spec §8.3）。

这里验的不是算式（在 `tests/analysis`）也不是取数（在 `tests/services`），而是**屏幕上
那几行有没有替合成说假话**：

* 合成分要能被当场拆开：每一项的分、它在模板里的权重、以及它凭什么。只印一个 72 分的
  命令与黑盒没有区别，而 spec §8.3 明写「可解释、非黑盒」。
* 被拒的分项照旧出现在表里，值是 `–` 而不是 0：0 分是「这项很差」，`–` 才是「这项没数据」。
* 五个价位各带自己的式子；拒了的那一行要说为什么拒、下一步跑什么（D43）。
* `--json` 在每个出口都给可解析 JSON 且带 `exit`，参数错也是（D57）。
* 现价的日期与「离今天多远」要印出来：拿 20 天前那根收盘当现价，屏幕上原来看不出来。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
import support
from support import StubAdapter, sample_bars
from support_fundamentals import StubFundamentalsAdapter
from support_valuation import CLOSES, CODE, DAYS, PE, PR_NOW, TODAY, seed

import sams.cli as cli
from sams.analysis.valuation.styles import get_style
from sams.bootstrap import build_container

HEAD = ["analysis", "valuation", "--market", "A", "--code", CODE]
#: 样本库铺 300 个日历日，`--as-of` 那一组用例取中段的一天
BACK = date(2024, 6, 30)


def make_container(db, settings, *, today: date = TODAY, **seeded) -> object:
    """一个铺好样本库的容器：时钟冻在 `today`，命令不传 `--as-of` 时默认就是那天。"""
    adapter = StubFundamentalsAdapter(["A"])
    container = build_container(
        settings,
        db=db,
        adapters={
            "akshare": adapter,
            "eastmoney": adapter,
            "yfinance": StubAdapter("yfinance", ["HK", "US"], sample_bars(source="yfinance")),
        },
        clock=support.FrozenClock(datetime(today.year, today.month, today.day, 15, tzinfo=UTC)),
    )
    container.registry.sleep = lambda _seconds: None
    seed(db, **seeded)
    return container


def invoke(container, *extra: str) -> int:
    args = cli.build_parser().parse_args([*HEAD, *extra])
    return args.handler(container, args)


def out(container, capsys, *extra: str) -> tuple[int, str]:
    """跑一次，把退出码与那屏字一起交回来（读命令，跑两次是同一份库）。"""
    capsys.readouterr()
    code = invoke(container, *extra)
    return code, capsys.readouterr().out


def payload(container, capsys, *extra: str) -> tuple[int, dict]:
    code, text = out(container, capsys, "--json", *extra)
    return code, json.loads(text)


# --- 那张分项表 ----------------------------------------------------------------------------


def test_the_score_is_printed_with_every_item_and_its_weight(db, settings, capsys):
    """一屏里要能同时看到分项的名字、它的分、以及模板给它的权重（spec §8.3）。"""
    container = make_container(db, settings)
    code, body = out(container, capsys)
    assert code == 0
    template = get_style("value")
    assert "合成分" in body
    for key in template.weights:
        assert key in body, f"{key} 没出现在屏幕上：它被静默丢了"


def test_the_json_lets_the_caller_recompute_the_score(db, settings, capsys):
    """给了分不给权重，下游只能信黑盒。这里按 D70 那条式子自己重算一遍。"""
    container = make_container(db, settings)
    code, data = payload(container, capsys)
    assert (code, data["exit"], data["status"]) == (0, 0, "ok")
    template = get_style("value")
    rows = data["composite"]["items"]
    assert [row["key"] for row in rows] == list(template.asked())
    for row in rows:
        assert row["weight"] == pytest.approx(template.weights[row["key"]])
        assert row["evidence"] or row["reason"], "既无依据也无原因的分项是一行装饰"
    got = [row for row in rows if row["value"] is not None]
    expected = sum(r["value"] * r["weight"] for r in got) / sum(r["weight"] for r in got)
    assert data["composite"]["score"] == pytest.approx(expected)


def test_a_refused_item_shows_a_dash_not_zero(db, settings, capsys):
    """库里没有资金流行 ⇒ 那一项是 `–`。印成 0 分就是说「主力在出货」。"""
    container = make_container(db, settings)
    code, data = payload(container, capsys)
    refused = {row["key"]: row for row in data["composite"]["items"] if not row["ok"]}
    assert set(refused) == {"main_inflow", "north_bound"}
    for row in refused.values():
        assert row["value"] is None
        assert "sams sync flow" in row["reason"]
    assert set(data["composite"]["missing"]) == set(refused)
    _, body = out(container, capsys)
    for key in refused:
        line = next(line for line in body.splitlines() if line.strip().startswith(key))
        assert "–" in line, f"{key} 那行没把「没有数」打成 –"


def test_the_composite_line_does_not_read_out_every_missing_item(db, settings, capsys):
    """缺项的原因在它自己那一行已经说完了；合成分那行再整段抄一遍，屏幕上就只剩原因、
    看不见分数（真跑一遍那一行 1500 字）。`--json` 里仍带完整 evidence：那是给机器读的，
    不占人的行数。"""
    container = make_container(db, settings)
    _, body = out(container, capsys)
    assert body.count("sams sync flow") == 2, "那句下一步命令应当在两行分项里各出现一次"
    line = next(row for row in body.splitlines() if "合成分" in row)
    assert len(line) < 160, f"合成分那一行 {len(line)} 字，把整张缺项表抄了一遍"
    assert "缺 2 项" in line, "少了那行要指到哪几项没数"
    _, data = payload(container, capsys)
    assert "缺项：" in data["composite"]["evidence"], "屏上省的那段必须在 JSON 里原样还在"


def test_a_composite_below_the_floor_refuses_the_score_but_keeps_the_rows(db, settings, capsys):
    """覆盖不到地板时不给合成分（也不补一个中性分），但有数的那几项仍旧逐行印出来；
    那一行说的是「哪些权重的数没到」，不是「这只票 0 分」。"""
    container = make_container(db, settings, with_fundamentals=False)
    code, body = out(container, capsys)
    assert code == 1
    line = next(row for row in body.splitlines() if "合成分" in row)
    assert "–" in line and "不合成" in line
    assert len(line) < 160, f"拒分那行把缺项逐条抄了一遍：{len(line)} 字"
    assert "ma_position" in body, "有数的分项不能跟着一起消失"


def test_the_percentile_lines_carry_their_own_sample_count(db, settings, capsys):
    """分位那句要带着样本数与窗口：一个没有天数的百分位说不出它凭什么。"""
    container = make_container(db, settings)
    _, data = payload(container, capsys)
    assert [row["metric"] for row in data["percentiles"]] == ["pr", "pe_ttm", "pb"]
    pr = data["percentiles"][0]
    assert pr["value"] == pytest.approx(0.0)
    assert pr["current"] == pytest.approx(PR_NOW)
    assert pr["window_to"] == str(TODAY)
    assert pr["samples"] == len(DAYS) - 1, "截至日自己那一票不投：样本是窗口里的其余天"
    _, body = out(container, capsys)
    assert f"{pr['samples']} 天" in body


# --- 五个价位 -------------------------------------------------------------------------------


def test_the_five_levels_carry_their_own_formula(db, settings, capsys):
    """合理区间、买入区、止损、目标、赔率：每一行都要说得出自己那条式子。"""
    container = make_container(db, settings)
    code, body = out(container, capsys)
    assert code == 0
    for word in ("合理区间", "买入区", "止损", "目标", "赔率"):
        assert word in body
    _, data = payload(container, capsys)
    plan = data["plan"]
    for leg in ("fair", "entry", "stop", "target"):
        assert plan[leg]["evidence"], f"{leg} 那一行没带式子"
    price = data["price"]
    assert plan["odds"]["value"] == pytest.approx(
        (plan["target"]["low"] - price) / (price - plan["stop"]["low"])
    )


def test_the_entry_gate_is_printed_as_passed_or_blocked(db, settings, capsys):
    """「分位过没过闸门」与「现价到没到那一段」是两件事，屏幕上要分开说。"""
    container = make_container(db, settings)
    _, data = payload(container, capsys)
    assert data["plan"]["entry"]["ready"] is False
    assert "通过闸门" in data["plan"]["entry"]["evidence"]
    assert "还在买入区上限" in data["plan"]["entry"]["reason"]


# --- 现价与它的日期 ------------------------------------------------------------------------


def test_a_price_older_than_the_threshold_is_called_out(db, settings, capsys):
    """库里只铺到 20 天前 ⇒ 屏幕上那个「现价」其实是 20 天前那根收盘，得说出来。

    这与估值行陈旧是两件事：那一句说的是 PE 是旧的，这一句说的是合成左边那个数本身。
    """
    container = make_container(db, settings, days=DAYS[:-20], closes=CLOSES[:-20])
    _, body = out(container, capsys)
    assert "距今 20 天" in body
    assert "⚠" in body


def test_as_of_back_in_time_moves_every_window_with_it(db, settings, capsys):
    """回看的每一天都要回到那一天：现价、估值行、分位窗口共用同一个 `as_of`。"""
    container = make_container(db, settings)
    _, data = payload(container, capsys, "--as-of", str(BACK))
    index = DAYS.index(BACK)
    assert (data["as_of"], data["price_on"]) == (str(BACK), str(BACK))
    assert data["price"] == pytest.approx(CLOSES[index])
    assert data["fundamental"]["pe_ttm"] == pytest.approx(PE[index])
    assert data["percentiles"][0]["window_to"] == str(BACK)


# --- 拒绝的几种形状 ------------------------------------------------------------------------


def test_no_bars_refuses_the_whole_read(db, settings, capsys):
    """没有日线就没有现价：不给一个「只有分、没有价位」的半条结论，退出码 1。"""
    container = make_container(db, settings, with_bars=False)
    code, text = out(container, capsys)
    assert code == 1
    assert "拒绝出数" in text
    _, data = payload(container, capsys)
    assert (code, data["exit"], data["status"]) == (1, 1, "refused")


def test_a_market_without_fundamental_rows_names_the_next_command(db, settings, capsys):
    """港美股在库里没有基本面行：屏幕上要有一句能跑的下一步（D43）。"""
    container = make_container(db, settings, market="HK", code="00700", with_fundamentals=False)
    capsys.readouterr()
    args = cli.build_parser().parse_args(
        ["analysis", "valuation", "--market", "HK", "--code", "700"]
    )
    code = args.handler(container, args)
    body = capsys.readouterr().out
    assert code == 1
    assert "sams sync financials" in body
    assert "没有已披露的财务数据" in body


def test_the_style_flag_switches_the_template(db, settings, capsys):
    """两套模板给两个分是正常的事：屏幕上要点名用的是哪一套。"""
    container = make_container(db, settings)
    _, trend = payload(container, capsys, "--style", "trend")
    assert trend["style"] == "trend"
    assert [row["metric"] for row in trend["percentiles"]] == ["pr"]
    assert [row["key"] for row in trend["composite"]["items"]] == list(get_style("trend").asked())


# --- 参数错也是 JSON（D57） ----------------------------------------------------------------


def test_an_unknown_style_is_a_json_error_with_exit_2(db, settings, capsys):
    container = make_container(db, settings)
    code, data = payload(container, capsys, "--style", "growth")
    assert code == 2 == data["exit"]
    assert data["status"] == "refused"
    assert "不认识的打分模板" in data["reason"]
    assert "value" in data["reason"] and "trend" in data["reason"]


def test_a_percent_caliber_roe_estimate_is_refused(db, settings, capsys):
    """`--roe-estimate 15` 会让 PR 静默小两百倍（D58）：拒收，且拒得看得见。"""
    container = make_container(db, settings)
    code, data = payload(container, capsys, "--roe-estimate", "15")
    assert code == 2 == data["exit"]
    assert "0.15" in data["reason"]


def test_a_malformed_as_of_is_refused(db, settings, capsys):
    container = make_container(db, settings)
    code, data = payload(container, capsys, "--as-of", "2024-6-30")
    assert code == 2 == data["exit"]
    assert data["status"] == "refused"
