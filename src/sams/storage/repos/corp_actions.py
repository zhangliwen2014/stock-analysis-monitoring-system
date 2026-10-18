"""公司行为仓库：除权除息、拆合股、送转配。

后复权因子与持仓成本换算从这里推导，而不是从 `bars_daily` 的某一列读——
0003 迁移删掉了那个恒等于 1 的 `adj_factor` 占位列：没有生产者的列会被下游当成可信口径。
M2 起因子由 `analysis/adjust.py` 现算，本仓库只负责「这一条记录自洽」：
字段与类型的搭配（D34）在这里挡住，因为下游拿着它乘进价格序列，错一次就漂一整段。

同一 (market, code, ex_date, kind) 重复入库视为同一条记录的修正，不产生第二行：
数据源回填改数是常态，留下两行等于让因子算两次。

**但「修正」只在数值对得上时成立。** `src` 不在主键里，所以两个源对同一天同一类型给出
两个不同的数时，覆盖写会静默丢掉一个——而丢掉的正是「一年派两次，其中一次是特别分红」
这类同日两件事。那种情况下这里抛 `StorageError`：宁可让 `sync actions` 失败、让区间收益
继续拒绝出数，也不出一个看起来正常的错数。同日两件事需要换主键（§18 U21），眼下先拒。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date

import polars as pl

from sams.core.actions import ACTION_KINDS
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database

#: 词汇表在 core：采集侧与仓库侧认同一套拼写（见 core/actions 的说明）
KINDS = ACTION_KINDS

#: 每种类型允许带哪些数（D34）。缺了这张表，`ratio` 就会一列装三种语义：
#: 「每股送 0.5 股」与「除权后是原来的 1.5 倍」是同一个事实的两种写法，
#: 混在一列里时因子公式无论怎么写都有一半的行是错的。
#: 派息只动钱，其余四种只动股数（配股两者都动，但那份钱记在 `price` 里，不是 `cash`）。
_CASH_ONLY = frozenset({"dividend"})
_SHARE_ONLY = frozenset({"split", "bonus", "transfer", "rights"})
#: 股数只会增加的两种；写成 <1 几乎一定是把「每股送几股」当成了乘数
_GROWING = frozenset({"bonus", "transfer"})

#: 两个源对同一条记录差多少以内算「说的是同一件事」（见模块开头的冲突规则）。
#: 取半分：源的舍入口径差不到一分（2.10 与 2.099），而「两笔派息被当成一条」那种
#: 错数差的是三成。定得越松，越多真冲突被静默覆盖；定得越紧，越多改数被判成冲突。
#: 与 `datasource/akshare_actions._TOLERANCE` 数字相同而量的是两件事：这个比两个源的意见，
#: 那个比同一条记录里文本与数值是否自洽（D45）。改一个不必动另一个。
_TOL = 0.005

#: 同一条记录上会比的三个数。`src` 与 `note` 不在内：换源本身就是常态，
#: 而 `unknown` 的两条只有原文不同，数值全空，读侧无论如何都拒绝（D39）。
_NUMERIC_FIELDS = ("cash_per_share", "ratio", "price")


def _agree(stored: float | None, incoming: float | None) -> bool:
    """两个数是不是在说同一件事。一边有一边没有，永远是两件事。"""
    if stored is None or incoming is None:
        return stored is None and incoming is None
    return abs(stored - incoming) <= _TOL


@dataclass(frozen=True, kw_only=True)
class CorpAction:
    market: str
    code: str
    ex_date: date
    kind: str
    src: str
    cash_per_share: float | None = None
    ratio: float | None = None
    #: 配股价：每股新股要付的现金。只有 rights 用得上，缺它推不出除权参考价
    price: float | None = None

    note: str | None = None


def _canonicalize(action: CorpAction) -> CorpAction:
    """键的形态在这一层落定（D46 的下沉）：主键 (市场, 代码, 除权日, 类型) 由这里定义。

    记录种在小写市场名下不会报错，只会让读取层按 `A` 查出 0 条行为：区间收益于是
    认为「首尾之间没有除权日」，照实用裸价出一个看起来完全正常的错数——
    U12 那个错数换一条路回来，而且 D37 抓不住它（一次 4% 的派息跳变在 15% 阈值之下）。
    """
    market = canonical_market(action.market)
    return replace(action, market=market, code=canonical_code(market, action.code))


def _as_view(action: CorpAction) -> dict:
    """把同批里那一行摆成与库里读出来的一样的形状，好让冲突比对口径只有一处。"""
    return {
        "cash_per_share": action.cash_per_share,
        "ratio": action.ratio,
        "price": action.price,
        "src": action.src,
    }


class CorpActionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, action: CorpAction) -> None:
        action = _canonicalize(action)
        self._check(action)
        self._check_no_conflict(action, self._existing(action), where="库里那条")
        self._write(action)

    def upsert_many(self, frame: pl.DataFrame) -> int:
        """整批写入，返回入库行数（空帧返回 0）。

        逐行走 `_check` 而不是批量 insert：D34 的字段搭配是每条记录的事实。
        但**先看完一整批再动手**——一条冲突的记录不该把同批其余的好数据半写进库：
        那会留下一个「这次查过、但记录不齐」的区间，而读取层只看除权记录，
        看不出采集断在半路，也就不会有第二次机会想起它。

        冲突要比两处：库里那一条，**以及同一批次里排在前面、键相同的那一行**。
        只查库等于给「一个源在一次应答里给同键两行」留了后门——那一刻库里是空的，
        两行都放行，后写的静默盖掉前写的，正是 D45 拦的那种「凭空少一笔」。
        """
        if frame.is_empty():
            return 0
        batch = [_canonicalize(CorpAction(**row)) for row in frame.iter_rows(named=True)]
        queued: dict[tuple[str, str, date, str], CorpAction] = {}
        for action in batch:
            self._check(action)
            self._check_no_conflict(action, self._existing(action), where="库里那条")
            prior = queued.get((action.market, action.code, action.ex_date, action.kind))
            if prior is not None:
                self._check_no_conflict(action, _as_view(prior), where="同批的前一行")
            queued[(action.market, action.code, action.ex_date, action.kind)] = action
        for action in batch:
            self._write(action)
        return len(batch)

    def _existing(self, action: CorpAction) -> dict | None:
        """库里这一条（同一主键）现在长什么样；没有则 None。"""
        rows = self.db.query(
            """
            SELECT cash_per_share, ratio, price, src FROM corp_actions
            WHERE market = ? AND code = ? AND ex_date = ? AND kind = ?
            """,
            [action.market, action.code, action.ex_date, action.kind],
        )
        return rows.to_dicts()[0] if not rows.is_empty() else None

    def _check_no_conflict(self, action: CorpAction, prior: dict | None, *, where: str) -> None:
        """同一条记录上两个对不上的数：写谁都是凭空丢掉一件事（见模块开头）。

        出错信息里点名两条记录与两个源，是因为这一步之后没有别的现场可查：
        调用方只知道 `sync actions` 失败了，而它要决定的是信哪个数。
        `where` 说清另一条在哪——库里那条还是同批的前一行：前者要重新采集才能改，
        后者是这一次应答自己就不自洽，重跑一遍还是这样。
        """
        if prior is None:
            return
        diffs = [
            f"{name} {prior[name]} → {getattr(action, name)}"
            for name in _NUMERIC_FIELDS
            if not _agree(prior[name], getattr(action, name))
        ]
        if not diffs:
            return
        raise StorageError(
            f"{action.market}/{action.code} {action.ex_date} {action.kind} 记录冲突："
            + "；".join(diffs)
            + f"（{where}来自 {prior['src']}，这次要写 {action.src}）。"
            "主键是 (市场, 代码, 除权日, 类型)：同日同类型的两件事装不下，"
            "覆盖写会丢掉一笔，留两条会把一笔算两次。"
            "这次同步不写覆盖确认，这只票的区间收益在查清两个数之前继续拒绝出数。"
        )

    def _write(self, action: CorpAction) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO corp_actions
                (market, code, ex_date, kind, cash_per_share, ratio, price, src, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())
            """,
            [
                action.market,
                action.code,
                action.ex_date,
                action.kind,
                action.cash_per_share,
                action.ratio,
                action.price,
                action.src,
                action.note,
            ],
        )

    def _check(self, action: CorpAction) -> None:
        """字段与类型是否互相自洽（D34）。

        这一层看着像洁癖：`dividend` 带 `ratio` 会被下游当乘数用，`rights` 缺 `price`
        会让配股的除权参考价偏高（白送和花钱买的股，除权后价格不一样）。两者在库里
        都长得像一条正常记录，出错时没人会怀疑到这一行。
        """
        if action.kind not in KINDS:
            raise ValidationError(f"未知公司行为类型 {action.kind!r}，可选 {sorted(KINDS)}")
        if action.kind == "unknown":
            # 折不出数才叫 unknown；能给出数值就说明知道它是哪一种，标 unknown 是逃避校验
            if action.cash_per_share is not None or action.ratio is not None or action.price:
                raise ValidationError(
                    "unknown 记录不该带数值：它的意思是「这天有件事但折算不出因子」，"
                    "能折出数就请标明是哪一种行为"
                )
            if not (action.note or "").strip():
                raise ValidationError(
                    "unknown 必须留下源给的原文：一句空白的「有件事发生」既无法复核也无法补数"
                )
        for name, value in (
            ("cash_per_share", action.cash_per_share),
            ("ratio", action.ratio),
            ("price", action.price),
        ):
            # NaN 也算「给了」吗？不算——0 与负的派息/比例同样无从推导因子，却能一路乘进后复权序列
            if value is None:
                continue
            if not math.isfinite(value) or value < 0:
                raise ValidationError(f"{name}={value} 非法：必须是有限的非负数")
        if action.kind == "dividend" and action.cash_per_share is None:
            raise ValidationError("dividend 必须给出 cash_per_share：没有金额的派息推不出因子")
        if action.kind in _CASH_ONLY and action.ratio is not None:
            raise ValidationError("dividend 只给现金，带 ratio 会让同一笔派息被乘两次")
        if action.kind in _SHARE_ONLY and action.ratio is None:
            raise ValidationError(f"{action.kind} 必须给出 ratio（除权后/除权前的股数乘数）")
        if action.kind in _GROWING and (action.ratio or 0) <= 1:
            raise ValidationError(
                f"{action.kind} 的 ratio={action.ratio} 要大于 1：送转只会增加股数，"
                "若你想写「每股送 0.5 股」，乘数是 1.5"
            )
        if action.kind == "split" and (action.ratio == 1):
            raise ValidationError("拆合股的 ratio=1 意味着股数没变，这条不该存在")
        if action.kind == "rights":
            if not action.price:
                raise ValidationError("配股必须给配股价：没有它推不出除权参考价")
            if (action.ratio or 0) <= 1:
                raise ValidationError(f"配股的 ratio={action.ratio} 要大于 1（每股有权买更多股）")
        if action.price is not None and action.kind != "rights":
            raise ValidationError(
                f"price 只属于配股（rights），{action.kind} 带 price 会被当成要付钱买的新股"
            )
        if action.cash_per_share is not None and action.kind not in _CASH_ONLY:
            raise ValidationError(f"{action.kind} 不该带 cash_per_share：现金只随派息出现")
        if action.cash_per_share is not None and not action.cash_per_share > 0:
            raise ValidationError("派息金额必须是正数，0 或负数不构成一次除权")

    def list_for(
        self, market: str, code: str, *, until: date | None = None
    ) -> pl.DataFrame:
        """按除权生效日升序返回。

        `until` 是 PIT 读法：只取到某一天为止已发生的行为。**不传就是全量，包含未来的
        除权日**——这不是漏洞而是这个方法的定义，把它当「当前有效」用会算出带未来信息的
        因子（§18 U4 记的就是这件事）。所以推导因子的调用方一律传 `until=clock.today()`，
        而这个仓库方法不做时钟：仓库层读的是事实，不看现在是几点。
        """
        sql = ["SELECT * FROM corp_actions WHERE market = ? AND code = ?"]
        params: list = [canonical_market(market), canonical_code(market, code)]
        if until is not None:
            sql.append("AND ex_date <= ?")
            params.append(until)
        return self.db.query(" ".join(sql) + " ORDER BY ex_date, kind", params)

    def count(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM corp_actions") or 0)
