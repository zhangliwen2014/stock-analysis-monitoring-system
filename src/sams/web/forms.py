"""表单字段的读法。这一层只认表单，不认参数校验。

参数的默认值、上下界、枚举与分档串全归 `core.rules.validate_params`：CLI 与仓库念的都是
它那几句话，页面在这里再排一遍数字与边界只会得到两套会分叉的措辞。所以这层只把
`param_*` 那几个输入框的**原文**交出去——填了的那几项进校验，没填的一项都不出现：
出现就等于替用户显式承认了那个值（编辑那条路径要拿它盖在旧值上，见 `pages/rules.py`）。

字段名与契约对齐：`param_<name>`、`filter_<key>`、`channel_ids`（多选）。
"""

from __future__ import annotations

from fastapi import Request

from sams.core.rules import FILTER_KEYS, Template


async def form_data(request: Request) -> dict[str, list[str]]:
    """表单原文，同名多值都留着（复选框组与多选 select）。

    Starlette 缓存 `request.form()`：`csrf_protect` 先解析一遍，这里拿到的是同一份，
    不会解析两次，也不会把 body 读空。
    """
    form = await request.form()
    data: dict[str, list[str]] = {}
    for key, value in form.multi_items():
        data.setdefault(key, []).append(value if isinstance(value, str) else "")
    return data


def one(data: dict[str, list[str]], key: str, default: str = "") -> str:
    values = data.get(key)
    return values[0] if values else default


def many(data: dict[str, list[str]], key: str) -> list[str]:
    return [value for value in data.get(key, []) if value]


def checked(data: dict[str, list[str]], key: str) -> bool:
    return key in data and data[key][0] not in ("", "0", "false")


def raw_params(template: Template, data: dict[str, list[str]]) -> dict[str, str]:
    """这个模板要的、用户真填了的那几项：键是参数名，值还是表单里的原文。"""
    return {
        spec.name: value.strip()
        for spec in template.params
        if (value := one(data, f"param_{spec.name}")).strip()
    }


def param_fields(data: dict[str, list[str]]) -> dict[str, str]:
    """所有 `param_*` 字段的原文（含空的）：被拒后重新渲染时用它把人家打的字回填。"""
    return {key: values[0] for key, values in data.items() if key.startswith("param_") and values}


def filter_flags(data: dict[str, list[str]]) -> dict[str, bool]:
    """四个开关全给出去，勾没勾都算一个答案。

    只交勾了的那几个不行：`validate_filters` 会把没收到的键补成 `False`，于是「改个名字」
    顺手把用户三年前打开的降噪开关关了——那正是这条规则从此不响的原因。
    """
    return {key: checked(data, f"filter_{key}") for key in FILTER_KEYS}
