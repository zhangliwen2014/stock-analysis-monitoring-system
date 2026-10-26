"""表单字段的读法。这一层只认表单，不认参数校验。

参数的默认值、上下界、枚举与分档串全归 `core.rules.validate_params`：CLI 与页面念的都是
它那几句话，这里再排一遍数字与边界只会得到两套会分叉的措辞。所以这层只把
`param_*` 那几个输入框的**原文**交出去——填了的那几项进校验，没填的一项都不出现：
出现就等于替用户显式承认了那个值。编辑那一路怎么拿这份去盖库里那一份，
写在 `pages/rules.py` 的合并处。

字段名与契约对齐：`param_<name>`、`filter_<key>`、`channel_ids`（多选）。
"""

from __future__ import annotations

from fastapi import Request

from sams.core.rules import FILTER_KEYS, Template


def _latin1_defaulted(request: Request) -> bool:
    """starlette 对 urlencoded 体的字段值是写死的 `decode("latin-1")`
    （`formparsers.FormParser.parse`，HTTP 的历史默认），Content-Type 里的 charset
    它根本不看。这里只认「urlencoded 且没声明 charset」的请求去猜裸字节：
    声明了 charset 的客户端我们不改口——那不是我们能替它翻译的东西。"""
    content_type = (request.headers.get("content-type") or "").lower()
    return (
        content_type.startswith("application/x-www-form-urlencoded")
        and "charset" not in content_type
    )


def _repair_utf8(value: str) -> str:
    """把 latin-1 读出来的裸 UTF-8 字节折回正确字符：整串能编回 latin-1（每字符
    < U+0100）且那些字节是**合法** UTF-8 才折，严格失败就原样保留。
    浏览器一律 percent-encode，解出来是 > U+00FF 的字符会撞 UnicodeEncodeError
    原样返回；真 latin-1 的裸字节（如 lone 0xE9）过不了 UTF-8 校验，也原样返回。
    """
    if value.isascii():
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


async def form_data(request: Request) -> dict[str, list[str]]:
    """表单原文，同名多值都留着（复选框组与多选 select）。

    Starlette 缓存 `request.form()`：`csrf_protect` 先解析一遍，这里拿到的是同一份，
    不会解析两次，也不会把 body 读空。
    """
    form = await request.form()
    repair = _latin1_defaulted(request)
    data: dict[str, list[str]] = {}
    for key, value in form.multi_items():
        text = value if isinstance(value, str) else ""
        # 只折值不折键：键是代码里的字段名，永远该是 ASCII，出现乱码是请求错不是编码问题
        if repair:
            text = _repair_utf8(text)
        data.setdefault(key, []).append(text)
    return data


def one(data: dict[str, list[str]], key: str, default: str = "") -> str:
    values = data.get(key)
    return values[0] if values else default


def many(data: dict[str, list[str]], key: str) -> list[str]:
    return [value for value in data.get(key, []) if value]


def checked(data: dict[str, list[str]], key: str) -> bool:
    """复选框的合同只有一条：勾了这个键就在，没勾就不在。值是什么不看——
    浏览器交的是 `on`，而把 `"0"`/`"false"` 读成「没勾」是给一种不存在的写法留位子，
    顺带还会把一个真叫 `false` 的值读成关。"""
    return key in data


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
