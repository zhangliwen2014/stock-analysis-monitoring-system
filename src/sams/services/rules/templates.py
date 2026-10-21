"""每个 kind 一个纯函数：给一份 `Snapshot` 与那几条参数，判一次（spec §10.3）。

`Outcome` 而不是 `Verdict | None`：**「没触发」与「判不了」是两句话**。前者不该有任何
输出，后者必须给出那句话——否则一条因为缺数据而从没响过的规则，在屏幕上与一条
工作正常的规则长得一模一样（§1 那张表的最后一行）。

所有阈值都在**现价口径 + 标的币种**里比（§5.4 规则 3）：折回因子是 T-6 乘好的，
这里再乘一次就是两件外套，除权日会只有一侧折回。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sams.analysis.valuation.scoring import item_label
from sams.core.rules import HIT, PRE, URGENT, Verdict, tier_of
from sams.services.rules.snapshot import Snapshot
from sams.storage.repos.theses import HIT_STOP, INVALIDATED, Thesis

#: 「快到了」那一档有多近：现价离止损线不到 3% 就报 `pre`（§10.2 点名的那个刻度）。
#: 写成一处而不是每个模板的参数：九种止损各配一个 `pre_band`，用户要回答一个他自己
#: 也说不清的数，而配出九个值就是没有值。
PRE_BAND = 0.03


@dataclass(frozen=True)
class Ask:
    """一次求值的输入：谁的哪个对象、此刻的数、这条规则的参数。"""

    snapshot: Snapshot
    params: Mapping[str, object]
    #: 这条规则挂着的那条论（若有）。模板读它的价位与状态时**只**从这里读，不自己去查库：
    #: `Snapshot` 已经查过一次，两次之间规则还能被改
    thesis: Thesis | None = None


@dataclass(frozen=True)
class Outcome:
    """一个模板的一次输出。`hit` 为 None 时 `why_not` 是那句话，或者那句空。"""

    hit: Verdict | None = None
    why_not: str = ""


def money(value: float | None, currency: str) -> str:
    """阈值与现价的措辞：数后面永远跟着币种（§5.4 规则 3）。

    金额类阈值不允许不带币种，而 A 股是元、美股是美元——屏幕上写「止损 100」的那条规则，
    用户在手机上会按另一种货币去理解它。
    """
    if value is None:
        return "未知"
    return f"{value:.2f} {currency or '元'}"


def stop_stage(
    snapshot: Snapshot, line: float, *, pre_band: float = PRE_BAND
) -> str | None:
    """三档判定。顺序不能调：先看跳空，再看是否已破，最后才看「快到了」。

    `urgent` 要求**前收盘在线上方**而**开盘在线下方**——只有这种形状才是「一开盘就没机会」。
    只看开盘的话，一条早就破了的线每趟都会升格成 urgent，那一档的意义被用光。
    """
    price = snapshot.price
    if price is None:
        return None
    if (
        snapshot.day_open is not None
        and snapshot.prev_close is not None
        and snapshot.day_open < line
        and snapshot.prev_close >= line
    ):
        return URGENT
    if price < line:
        return HIT
    if price < line * (1.0 + pre_band):
        return PRE
    return None


def _no_price(snapshot: Snapshot) -> str:
    return (
        f"没有 {snapshot.market}/{snapshot.code} 过闸的现价：这一趟不判任何价格类条件"
        "（§10.1 的新鲜度过滤器拦掉的就是这种读数）"
    )


def _gap_note(snapshot: Snapshot, line: float, stage: str) -> str:
    """`urgent` 那句话要把「开盘 vs 前收盘」两个数一起说出来：只报现价的话，
    读的人看不出这是跳空而不是慢慢跌过去的。"""
    if stage != URGENT:
        return ""
    return (
        f"；开盘 {money(snapshot.day_open, snapshot.currency)} 直接在线 "
        f"{money(line, snapshot.currency)} 下方（前收盘 "
        f"{money(snapshot.prev_close, snapshot.currency)}），这一跳不是盘中跌过去的"
    )


def _hit(snapshot: Snapshot, line: float, stage: str, why: str) -> Outcome:
    return Outcome(
        Verdict(
            stage=stage,
            value=snapshot.price,
            threshold=line,
            value_text=money(snapshot.price, snapshot.currency),
            threshold_text=money(line, snapshot.currency),
            why=why + _gap_note(snapshot, line, stage),
        )
    )


def stop_fixed(ask: Ask) -> Outcome:
    """`stop.fixed`：跌破绝对价即报。线就是用户填的那个数（现价口径、标的币种）。"""
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    line = float(ask.params["price"])
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot, line, stage, f"绝对价止损线 {money(line, snapshot.currency)}"
    )


def stop_pct(ask: Ask) -> Outcome:
    """`stop.pct`：成本下浮 x%。没有持仓就没有成本，这一条不判而不是判成 0 成本。"""
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.avg_cost is None or snapshot.avg_cost <= 0:
        return Outcome(
            why_not=f"这一笔在账户 {snapshot.account_id} 里没有成本：`stop.pct` 的线"
            "由成本算出，缺了它就没有线"
        )
    pct = float(ask.params["pct"])
    line = snapshot.avg_cost * (1.0 - pct)
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot,
        line,
        stage,
        f"成本 {money(snapshot.avg_cost, snapshot.currency)} 下浮 {pct:.1%}"
        f" ⇒ 线 {money(line, snapshot.currency)}",
    )


def stop_trailing(ask: Ask) -> Outcome:
    """`stop.trailing`：持仓期最高价回撤 x%。最高价来自 T-6，已是现价口径。"""
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.holding_high is None:
        return Outcome(
            why_not="没有可用的持仓期最高价（持仓不足三根日 K 时它没有意义）："
            "拿现价凑会让回撤恒为 0，那条线贴在现价上方，第一次呼吸就报止损"
        )
    pct = float(ask.params["pct"])
    line = snapshot.holding_high * (1.0 - pct)
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot,
        line,
        stage,
        f"持仓期最高价 {money(snapshot.holding_high, snapshot.currency)}"
        f" 回撤 {pct:.1%} ⇒ 线 {money(line, snapshot.currency)}",
    )


def stop_atr(ask: Ask) -> Outcome:
    """`stop.atr`：成本 − 倍数×ATR14。ATR 由 T-6 折回现价口径，这里只做算术。"""
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.avg_cost is None or snapshot.avg_cost <= 0:
        return Outcome(why_not="没有成本就没有这条线：`stop.atr` 是从成本往下量倍数")
    if snapshot.atr is None:
        return Outcome(
            why_not="这一路没有 ATR14（日 K 不足 14 根，或那几根没过数据闸）："
            "把它当 0 会让线正好贴在成本上，现价低一分都报止损"
        )
    multiple = float(ask.params["multiple"])
    line = snapshot.avg_cost - multiple * snapshot.atr
    if line <= 0:
        return Outcome(
            why_not=f"倍数 {multiple:g} 把线算到了 {money(line, snapshot.currency)}："
            "一条在 0 或以下的线永远不会触发，这条规则等于没写"
        )
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot,
        line,
        stage,
        f"成本 {money(snapshot.avg_cost, snapshot.currency)} − {multiple:g}×ATR14"
        f"（{money(snapshot.atr, snapshot.currency)}，已从后复权折回现价口径）"
        f" ⇒ 线 {money(line, snapshot.currency)}",
    )


#: `basis` 那两个取值 → (取 `Snapshot` 的哪个字段, 那句话里的名字)
#: 名字写在这里而不是拼出来：「日 MA60」是给人读的词，字段名是给代码读的，
#: 把 `daily_ma60` 直接上屏等于让用户去猜那是什么意思。
_MA_LINES = {
    "weekly_ma20": ("weekly_ma20", "周 MA20"),
    "daily_ma60": ("daily_ma60", "日 MA60"),
}


def stop_ma(ask: Ask) -> Outcome:
    """`stop.ma`：跌破均线。两条线都在 T-6 折回过，用哪一条由参数说。"""
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    field, label = _MA_LINES[str(ask.params["basis"])]
    line = getattr(snapshot, field)
    if line is None:
        return Outcome(
            why_not=f"没有 {label}：这一路的窗口不够长，或那几根日 K 没过数据闸"
        )
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot, line, stage, f"{label} 线 {money(line, snapshot.currency)}"
    )


def stop_breakeven(ask: Ask) -> Outcome:
    """`stop.breakeven`：浮盈曾经到过 x% 之后，把线抬到成本。

    触发判据是**持仓期最高价**而不是今天的现价：这一条的意义就是
    「已经赚过，别把利润全还回去」，而赚过的那一刻是唯一可查的事实。
    线抬到成本 ⇒ 现价只要低于成本就 `hit`，正是字面意思。
    """
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.avg_cost is None or snapshot.avg_cost <= 0:
        return Outcome(why_not="保本止损要把线抬到成本，而这一笔没有成本可读")
    if snapshot.holding_high is None:
        return Outcome(
            why_not="没有持仓期最高价：判不出「曾经到过 x%」，宁可不报也不猜"
        )
    trigger = float(ask.params["trigger_pct"])
    reached = snapshot.holding_high / snapshot.avg_cost - 1.0
    if reached < trigger - 1e-9:
        return Outcome()
    line = snapshot.avg_cost
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot,
        line,
        stage,
        f"持仓期最高 {money(snapshot.holding_high, snapshot.currency)}"
        f"（成本上方 {reached:.1%}，已过 {trigger:.1%}）⇒ 线已抬到成本 "
        f"{money(line, snapshot.currency)}",
    )


def stop_time(ask: Ask) -> Outcome:
    """`stop.time`：持仓 N 天没到预期涨幅 ⇒ 提醒复核。

    天数是**自然日**（`holding_days` = as_of − 建仓日）而不是交易日：判据来自持仓的
    `open_ts`，换成交易日要引 `CalendarService`，而 T-6 刻意没让取数依赖日历。
    保守方向也在这里：自然日永远不少于交易日，所以这一条只会**早**响不会晚响。

    它是 §10.2 说的「阴跌不触发价格线时的兜底」——价格类模板全沉默时唯一会说话的一条。
    """
    snapshot = ask.snapshot
    if snapshot.holding_days is None:
        return Outcome(
            why_not="没有建仓时间：`holding_days` 来自持仓的 open_ts，"
            "缺了它「拿了多久」无从判起（当 0 会让这条规则永远不响）"
        )
    days = int(ask.params["days"])
    if snapshot.holding_days < days:
        return Outcome()
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.avg_cost is None or snapshot.avg_cost <= 0:
        return Outcome(why_not="时间止损要比「现在的涨幅」，而这一笔没有成本可读")
    ceiling = float(ask.params["max_gain_pct"])
    gain = snapshot.price / snapshot.avg_cost - 1.0
    if gain >= ceiling - 1e-9:
        return Outcome()
    opened = f"，{snapshot.opened_on} 建仓" if snapshot.opened_on else ""
    return Outcome(
        Verdict(
            stage=HIT,
            value=gain,
            threshold=ceiling,
            value_text=f"{gain:.1%}",
            threshold_text=f"{ceiling:.1%}",
            why=f"持有 {snapshot.holding_days} 天{opened}（自然日口径），涨幅 {gain:.1%} "
            f"没到 {ceiling:.1%}：这一条只看时间与涨幅，不替代价格线"
            " ⇒ 请复核理由还在不在（系统只提示，不替你平仓，§9.3）",
        )
    )


#: 「理由已经没了」的两个状态。名字从 `storage` 引，不在这里抄一遍字符串：
#: 词表在 0001 与 `theses.py` 里各有一份的时候，漂移就是这么来的。
_DEAD_STATUSES = (INVALIDATED, HIT_STOP)


def stop_thesis_drift(ask: Ask) -> Outcome:
    """`stop.thesis_drift`：打分掉档 / 赔率崩塌 / 论已被判死——三条里任意一条成立即报。

    这一条与 `thesis.drift`（T-11，周复检掉档）读同一份基线，但**级别不同、时机不同**：
    那一头的意义是「每周有一次人来看一眼」，所以只有掉档；这一头是止损位的替代判断，
    所以论一旦被判死（状态）也立刻算数——价格还没跌，而记账的理由已经没了。

    三个数都读不到时不是「没触发」而是 `why_not` 有话：`Outcome` 的两句话在这里最值钱。
    """
    thesis = ask.thesis
    if thesis is None:
        return Outcome(
            why_not="这条规则要求挂一条论（`needs_thesis`），而这一趟没读到它："
            "没有「理由」在手，就不存在「理由失效」——这是取数出了问题，不是判过没事"
        )
    snapshot = ask.snapshot
    findings: list[str] = []
    unknowns: list[str] = []
    value: float | None = None
    threshold: float | None = None
    value_text = "—"
    threshold_text = "—"

    if thesis.status in _DEAD_STATUSES:
        findings.append(f"这条论已被记为 {thesis.status}：账上写着的理由已经没了")

    base_tier = tier_of(thesis.baseline_score)
    new_tier = tier_of(snapshot.score)
    if base_tier is None:
        unknowns.append("这条论没有建仓时的基线分：掉档与否判不了")
    elif new_tier is None:
        unknowns.append("有建仓时的基线分，这一趟却没合成出新分数（掉档判不了）")
    elif new_tier < base_tier:
        findings.append(
            f"打分从 {thesis.baseline_score:g} 掉到 {snapshot.score:g}"
            f"（{base_tier} 档 → {new_tier} 档）"
        )
        value, threshold = snapshot.score, thesis.baseline_score
        value_text = f"{snapshot.score:g} 分"
        threshold_text = f"{thesis.baseline_score:g} 分"

    if thesis.reward_risk is None:
        unknowns.append("这条论没有记下赔率：崩塌与否判不了")
    elif snapshot.odds is None:
        unknowns.append("论上记着赔率，这一趟没有合成出来（崩塌与否判不了）")
    elif snapshot.odds < thesis.reward_risk:
        findings.append(
            f"赔率从论上记着的 {thesis.reward_risk:g} 倍掉到 {snapshot.odds:g} 倍："
            "同一个止损位下，这笔仓的期望已经支撑不住"
        )
        if value is None:
            value, threshold = snapshot.odds, thesis.reward_risk
            value_text = f"{snapshot.odds:g} 倍"
            threshold_text = f"{thesis.reward_risk:g} 倍"

    if findings:
        return Outcome(
            Verdict(
                stage=HIT,
                value=value,
                threshold=threshold,
                value_text=value_text,
                threshold_text=threshold_text,
                why="；".join(findings) + " ⇒ 请复核这条论（系统只提示，不替你平仓，§9.3）",
            )
        )
    if unknowns:
        return Outcome(why_not="；".join(unknowns))
    return Outcome()


def tp_tiered(ask: Ask) -> Outcome:
    """`tp.tiered`：把「成本 → 目标价」这一段切成几档，走到哪一档报哪一档。

    刻度是**那一段涨幅**而不是目标价本身：0.5 的意思是「目标涨幅走了一半」，
    与 §10.2 那句「解决涨一半不敢拿」对上。只报**已达到的最高一档**（不是每一档各报一次），
    因为一趟求值发两条同键告警会被冷却吃掉第二条，屏幕上看到的是「只报了一条」——
    与其让人猜，不如一次只说一格，下一格由下一趟来说。

    档位用 `pre`/`hit` 两个阶段而不是一律 `hit`：这是 D94 唯一的非止损用法——
    中间档报 `hit` 的话，真到目标价那一条会被半小时前的中间档吞掉。
    中间档之间仍共用一个去重键，30 分钟内第二档会被压——那是 §10.1
    的节流语义（键里没有档位），而两档之间通常隔着几天，不是半小时。
    """
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.avg_cost is None or snapshot.avg_cost <= 0:
        return Outcome(why_not="分批止盈的刻度从成本起算，而这一笔没有成本可读")
    if snapshot.target_price is None:
        return Outcome(
            why_not="没有目标价：分档量的是「成本 → 目标价」那一段，缺终点就没有刻度"
            "（拿现价当终点会让进度恒为 1，每趟都报到了）"
        )
    span = snapshot.target_price - snapshot.avg_cost
    if span <= 0:
        return Outcome(
            why_not=f"目标价 {money(snapshot.target_price, snapshot.currency)} 不高于成本 "
            f"{money(snapshot.avg_cost, snapshot.currency)}：那一段是 0 或负的，"
            "百分比刻度在这里没有意义（要盯跌到某个价请用 stop.fixed）"
        )
    tiers = ask.params["tiers"]  # `validate_params` 已保证递增且每档 > 0
    progress = (snapshot.price - snapshot.avg_cost) / span
    reached = [tier for tier in tiers if progress >= tier]
    if not reached:
        return Outcome()
    top = reached[-1]
    final = progress >= tiers[-1]
    remaining = len(tiers) - len(reached)
    tail = "已到最后一档 ⇒ 目标价" if final else f"还剩 {remaining} 档"
    # 进度可以超过 100%（现价已在目标价上方），那句「走完 250.0%」没人读得懂。
    over = (
        f"（现价 {money(snapshot.price, snapshot.currency)} 已越过目标价）"
        if progress > 1.0
        else ""
    )
    return Outcome(
        Verdict(
            stage=HIT if final else PRE,
            value=progress,
            threshold=top,
            value_text=f"{progress:.1%}",
            threshold_text=f"{top:.0%}",
            why=f"成本 {money(snapshot.avg_cost, snapshot.currency)} 到目标价 "
            f"{money(snapshot.target_price, snapshot.currency)} 这一段走完 {progress:.1%}"
            f"{over}（已达 {top:.0%} 那一档，{tail}）⇒ 请复核是否分批止盈"
            "（系统只提示，不替你平仓，§9.3）",
        )
    )


def _cross(ask: Ask, *, upward: bool) -> Outcome:
    """两个 `price.cross_*` 的共用体：穿过去 = 前一根还在另一侧，这一根到了这一侧。

    `>=` / `<=` 里带等号：前收盘**正好**压在线上也算在另一侧起跳——那是最典型的
    回踩确认，把它算成「已经穿过了」会让这一天不报。
    """
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.prev_close is None:
        return Outcome(
            why_not="没有前收盘：「穿」说的是从今天上一根到今天的这一跳，"
            "只用现价判的话，一条早就在下面的线每个交易日都会重报一次"
        )
    line = float(ask.params["price"])
    if upward:
        crossed = snapshot.prev_close <= line < snapshot.price
    else:
        crossed = snapshot.prev_close >= line > snapshot.price
    if not crossed:
        return Outcome()
    verb = "上穿" if upward else "下穿"
    return Outcome(
        Verdict(
            stage=HIT,
            value=snapshot.price,
            threshold=line,
            value_text=money(snapshot.price, snapshot.currency),
            threshold_text=money(line, snapshot.currency),
            why=f"前收盘 {money(snapshot.prev_close, snapshot.currency)} 在线 "
            f"{money(line, snapshot.currency)} 的另侧，现价 "
            f"{money(snapshot.price, snapshot.currency)} 已{verb}",
        )
    )


def price_cross_below(ask: Ask) -> Outcome:
    """`price.cross_below`：跌穿指定价（§9.2 派生的「进入买入区间」用它）。"""
    return _cross(ask, upward=False)


def price_cross_above(ask: Ask) -> Outcome:
    """`price.cross_above`：涨穿指定价（§9.2 派生的「达到目标价」用它）。"""
    return _cross(ask, upward=True)


def thesis_odds_below(ask: Ask) -> Outcome:
    """`thesis.odds_below`：合成出来的赔率跌破阈值（§9.2 派生默认 1.5 倍）。

    阈值来自参数而不是 `thesis.reward_risk`：后者是**建仓时**的赔率（那是历史事实），
    这一条要判的是「现在还算不算得过来」，两者相减的部分归 `stop.thesis_drift`。
    """
    snapshot = ask.snapshot
    if snapshot.odds is None:
        return Outcome(
            why_not="这一趟没合成出赔率（止损位或目标价缺一个就算不出）："
            "把它当 0 会让所有赔率规则在数据缺的那天全体触发"
        )
    threshold = float(ask.params["threshold"])
    # 阈值处带 ±1e-9 容差：赔率是除出来的，「真值恰好等于阈值」会读成 1.4999999999…
    if snapshot.odds >= threshold - 1e-9:
        return Outcome()
    return Outcome(
        Verdict(
            stage=HIT,
            value=snapshot.odds,
            threshold=threshold,
            value_text=f"{snapshot.odds:g} 倍",
            threshold_text=f"{threshold:g} 倍",
            why=f"现价口径下赔率 {snapshot.odds:g} 倍，低于阈值 {threshold:g} 倍："
            "往上到目标价的空间已经换不起往下到止损位的空间了",
        )
    )


def thesis_drift(ask: Ask) -> Outcome:
    """`thesis.drift`：§9.3 周复检那一半的判据——**分项**掉过一档就算漂移。

    读的是 `baseline_items`（T-4 那三列之一）而不是总分：spec 的原话是「分项打分下滑
    超过一档」。总分只用来进 `Verdict.value`（`--json` 那边要一个数），因为总分掉一档
    可能是一个大权重项掉了 3 分，而分项那句话说的是「哪一项变了」。

    与 `stop.thesis_drift` 的分工写在那一头的 docstring 里：同一份基线，两个时机两个级别。
    """
    thesis = ask.thesis
    if thesis is None:
        return Outcome(
            why_not="这一条要比的是这条论的基线，而这一趟没读到论本身："
            "没有基线在手就没有「漂移」可判"
        )
    baseline = dict(thesis.baseline_items)
    if not baseline and thesis.baseline_score is None:
        return Outcome(
            why_not="这条论还没有基线（周复检第一次跑时建立，T-16）："
            "从没比过不等于没漂移，在这里报「观点未漂移」是一句没人验证过的谎（D96）"
        )
    snapshot = ask.snapshot
    now = dict(snapshot.items)
    dropped: list[str] = []
    unseen: list[str] = []
    for key, base in sorted(baseline.items()):
        label = item_label(key)
        if key not in now:
            unseen.append(label)
            continue
        was, new = tier_of(base), tier_of(now[key])
        if was is None or new is None:
            unseen.append(label)
            continue
        if new < was:
            dropped.append(f"{label} {base:g} → {now[key]:g}（{was} 档 → {new} 档）")
    if dropped:
        return Outcome(
            Verdict(
                stage=HIT,
                value=snapshot.score,
                threshold=thesis.baseline_score,
                value_text=(
                    f"{snapshot.score:g} 分" if snapshot.score is not None else "—"
                ),
                threshold_text=(
                    f"{thesis.baseline_score:g} 分"
                    if thesis.baseline_score is not None
                    else "—"
                ),
                why="；".join(dropped)
                + " ⇒ 观点漂移，请复核（系统只提示，不替你平仓，§9.3）",
            )
        )
    if unseen:
        return Outcome(
            why_not="这些分项这一趟没合成出来，比不了：" + "、".join(unseen)
        )
    return Outcome()


def scheduled_reevaluate(ask: Ask) -> Outcome:
    """`scheduled.reevaluate`：最近 `within_days` 天内出了新财报 ⇒ 提醒复核这条论。

    「这一趟是哪一天」用 `Snapshot.price_on`：引擎按 `as_of` 取数，而那一天最后一个过闸
    读数的日子就是它——再引一个日期字段会让两个「天」有机会不一致。

    窗口内每个交易日都会各判中一次，这不是漏：§10.1 的 `cooldown_min` 管的是刷屏，
    不是「提醒到用户看过为止」。T-15 派生这一条时默认带 `no_open_alert`（T-12 的过滤器：
    同一只票已有未处理告警就不重复推），于是这一段窗口实际只响第一次，之后由用户的
    「已处理」或新告警来清。
    """
    snapshot = ask.snapshot
    if snapshot.disclosed_on is None:
        return Outcome(
            why_not="没有财报披露日（这一路基本面没读到 published_on）："
            "事件驱动的那一端没有事件"
        )
    if snapshot.price_on is None:
        return Outcome(why_not="这一趟没有基准日：比较披露日需要「今天是哪天」")
    within = int(ask.params["within_days"])
    age = (snapshot.price_on - snapshot.disclosed_on).days
    if age < 0:
        return Outcome(
            why_not=f"披露日 {snapshot.disclosed_on} 晚于这一趟的基准日 "
            f"{snapshot.price_on}：宁可不报，也不报一条来自未来的提醒"
        )
    if age > within:
        return Outcome()
    return Outcome(
        Verdict(
            stage=HIT,
            value=float(age),
            threshold=float(within),
            value_text=f"{age} 天前",
            threshold_text=f"{within} 天窗口",
            why=f"{snapshot.disclosed_on} 披露了新财报（{age} 天前，在 {within} 天窗口内）"
            " ⇒ 该用新数据复核这条论：ROE、增速、PR 的输入都换了",
        )
    )


TABLE: dict[str, Callable[[Ask], Outcome]] = {
    "stop.fixed": stop_fixed,
    "stop.pct": stop_pct,
    "stop.trailing": stop_trailing,
    "stop.atr": stop_atr,
    "stop.ma": stop_ma,
    "stop.breakeven": stop_breakeven,
    "stop.time": stop_time,
    "stop.thesis_drift": stop_thesis_drift,
    "tp.tiered": tp_tiered,
    "price.cross_below": price_cross_below,
    "price.cross_above": price_cross_above,
    "thesis.odds_below": thesis_odds_below,
    "thesis.drift": thesis_drift,
    "scheduled.reevaluate": scheduled_reevaluate,
}
