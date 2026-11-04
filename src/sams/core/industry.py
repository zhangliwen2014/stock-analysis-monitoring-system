"""行业路径归一：把「食品饮料／饮料／白酒」与「食品饮料-饮料-白酒」认成同一层（D236）。

这里只做**同一分类体系内的结构归一**，不做跨体系翻译：申万、证监会、GICS 的行业名
各说各的，把「白酒」当成另一套体系里的「饮料制造」需要一张别名表，而那张表没人维护
就会静默把同业算错。所以：

* 分隔符统一（全角与 ASCII 的破折号、斜杠、大于号）；
* 去空段、去首尾空白；
* 超过三级取末三级（更具体的层级在后）；
* 空答复返回 `None`——「没有行业」与「行业叫空字符串」是两件事。

`match_keys()` 是同业放宽的唯一入口：三级 → 二级 → 一级，顺序固定，屏幕上的
「已放宽到二级」就来自这里；放宽丢掉的是最后（最细）那一级。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

#: 比这更细的层级不保留：同业按三级行业分组，再细就只剩自己
MAX_LEVELS = 3

#: NFKC 会把全角 `－`/`／`/`＞` 折成 ASCII，但 `—`（EM DASH）不会
_SEPARATORS = ("—", "/", ">", "-")


@dataclass(frozen=True)
class IndustryPath:
    levels: tuple[str, ...]

    @property
    def path(self) -> str:
        return "-".join(self.levels)

    def key(self, count: int) -> str:
        """取前 `count` 级（越靠前越宽）；要得比有的多，给的还是全部。

        层级是「宽 → 窄」排的（`食品饮料-饮料-白酒`），所以放宽要丢掉**最后**那一级：
        二级是「食品饮料-饮料」，不是「饮料-白酒」——后者把中间层与叶子拼在一起，
        永远配不上同一中间层下的其它叶子。
        """
        if count <= 0:
            return ""
        return "-".join(self.levels[:count])

    def match_keys(self) -> tuple[str, ...]:
        """从最具体到最宽：三级路径 → 二级 → 一级。"""
        return tuple(self.key(n) for n in range(len(self.levels), 0, -1))


def parse_industry(raw: str | None) -> IndustryPath | None:
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw))
    for separator in _SEPARATORS:
        text = text.replace(separator, "\x00")
    levels = tuple(part.strip() for part in text.split("\x00") if part.strip())
    if not levels:
        return None
    return IndustryPath(levels=levels[-MAX_LEVELS:])


def normalize_industry(raw: str | None) -> str:
    path = parse_industry(raw)
    return path.path if path is not None else ""
