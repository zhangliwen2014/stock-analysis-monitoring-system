"""缺失四级：屏幕上的「—」后面必须挂一个可区分的理由（D241）。

四个词不是同义词：

* **不适用**：口径本身不成立（PE 为负时的 PE 分位）；
* **未披露**：源/公司没给这个数（没有已披露年报 ROE）；
* **无法计算**：数有，但样本/口径不够（分位不足 250 天、中位不足 3 家）；
* **未覆盖**：这个市场/数据种类还没接（港美同业）。

合并成一句「暂无数据」就分不清「该补数」与「本来就不可算」。模板只通过
`missing_label()` 出中文，不在各模板里写第二份映射。
"""

from __future__ import annotations

NOT_APPLICABLE = "not_applicable"
NOT_DISCLOSED = "not_disclosed"
UNCOMPUTABLE = "uncomputable"
NOT_COVERED = "not_covered"

MISSING_LABELS = {
    NOT_APPLICABLE: "不适用",
    NOT_DISCLOSED: "未披露",
    UNCOMPUTABLE: "无法计算",
    NOT_COVERED: "未覆盖",
}


def missing_label(kind: str) -> str:
    if not kind:
        return ""
    known = MISSING_LABELS.get(kind)
    if known is None:
        return f"未知缺失类型（{kind}）"
    return known
