"""公司行为仓库：除权除息、拆合股、送转配。

后复权因子与持仓成本换算从这里推导，而不是从 `bars_daily` 的某一列读——
0003 迁移删掉了那个恒等于 1 的 `adj_factor` 占位列：没有生产者的列会被下游当成可信口径。
M2 起因子由 `analysis/adjust.py` 现算，本仓库只负责「这一条记录自洽」：
字段与类型的搭配（D34）在这里挡住，因为下游拿着它乘进价格序列，错一次就漂一整段。

同一 (market, code, ex_date, kind) 重复入库视为同一条记录的修正，不产生第二行：
数据源回填改数是常态，留下两行等于让因子算两次。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import polars as pl

from sams.core.actions import ACTION_KINDS
from sams.core.errors import ValidationError
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


class CorpActionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, action: CorpAction) -> None:
        self._check(action)
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

    def upsert_many(self, frame: pl.DataFrame) -> int:
        """整批写入，返回入库行数（空帧返回 0）。

        逐行走 `_check` 而不是批量 insert：D34 的字段搭配是每条记录的事实，
        一条不自洽的记录不该把同批其余的好数据一起挡在库外，也不该静默进去。
        """
        if frame.is_empty():
            return 0
        for row in frame.iter_rows(named=True):
            self.upsert(CorpAction(**row))
        return frame.height

    def _check(self, action: CorpAction) -> None:
        """字段与类型是否互相自洽（D34）。

        这一层看着像洁癖：`dividend` 带 `ratio` 会被下游当乘数用，`rights` 缺 `price`
        会让配股的除权参考价偏高（白送和花钱买的股，除权后价格不一样）。两者在库里
        都长得像一条正常记录，出错时没人会怀疑到这一行。
        """
        if action.kind not in KINDS:
            raise ValidationError(f"未知公司行为类型 {action.kind!r}，可选 {sorted(KINDS)}")
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
        params: list = [market, code]
        if until is not None:
            sql.append("AND ex_date <= ?")
            params.append(until)
        return self.db.query(" ".join(sql) + " ORDER BY ex_date, kind", params)

    def count(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM corp_actions") or 0)
