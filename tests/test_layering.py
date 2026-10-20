"""分层守卫：依赖只能指向更下的层，越界 import 直接失败。

「模块化、边界清晰」这种要求只有被机器检查，才不会被下一个人顺手破坏，
所以规则写在这里而不是文档里。

  core  ←  storage / datasource  ←  services  ←  cli
  bootstrap（组装根）是唯一可以同时认识所有实现的地方。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src"
SRC = ROOT / "sams"

#: 每个层允许 import 的其它层。缺的方向一律禁止，包括同层横串（storage ↔ datasource）。
#: 后四行是 spec §3.1 声明但尚未落地的层：先把依赖规则写全，M2 建目录时才不必开后门
#: ——守卫一旦为了临时方便被放宽，就再也不会被收紧。
#: services → analysis 是单向的：读用例负责取数与定口径，分析层不反向认识任何服务，
#: 否则指标引擎将来被 monitor/trading 复用时会出现第二条绕过守卫的取数路径。
#: analysis 因此也不 import storage：它拿到的三份事实是参数（K 线、公司行为、覆盖区间），
#: 「能 import storage」与「会自己去看一眼库」只差一个顺手，而后者的结果没有过闸。
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "storage": {"core"},
    "datasource": {"core"},
    "services": {"core", "storage", "datasource", "analysis"},
    "analysis": {"core"},
    # monitor 只许认识 core 与 storage：路由住在这里（`monitor/router.py`），而 services
    # 不许 import monitor——放宽这三项等于把 monitor 与 services 长成一块
    "monitor": {"core", "storage"},
    "trading": {"core", "storage", "services", "analysis"},
    "cli": {"core", "services"},
}

#: 组装根不受限（它的工作就是认识所有实现），但只有 cli 可以引用它取依赖
ASSEMBLY = "bootstrap"
ASSEMBLY_CONSUMERS = {"cli"}


def _resolved(name: str, level: int, module_parts: tuple[str, ...]) -> str:
    """把 `from ..storage.db import X` 这类相对导入还原成绝对 dotted name。"""
    if level == 0:
        return name
    package = list(module_parts[:-1])  # 去掉文件名，留包路径
    base = package[: len(package) - (level - 1)] if level > 1 else package
    return ".".join([*base, *name.split(".")] if name else base)


def imported_layers(path: Path) -> set[str]:
    """该模块 import 到的 sams 顶层子包集合。"""
    return {seg[1] for seg in _imports(path) if len(seg) > 1 and seg[0] == "sams"}


def imported_third_party(path: Path) -> set[str]:
    """该模块 import 到的第三方包根名（`import a.b` 取 `a`）。"""
    return {seg[0] for seg in _imports(path) if seg[0] != "sams"}


def _imports(path: Path) -> list[tuple[str, ...]]:
    """所有 import 的 dotted 段序列，相对导入先还原成绝对。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parts = path.relative_to(ROOT).with_suffix("").parts
    found: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "", node.level)]
        else:
            continue
        for name, level in names:
            found.append(tuple(_resolved(name, level, parts).split(".")))
    return found


#: 第三方库的落点。「换存储只动一层」（spec §3.1 约束 2）能不能成立，取决于
#: duckdb/requests 有没有在方便的地方被顺手 import——所以这条也由机器看着。
#: 值是按前缀匹配的相对路径（`storage/db.py` 精确到文件，`datasource` 放行整层）。
GATED: dict[str, tuple[str, ...]] = {
    "duckdb": ("storage/db.py",),
    "bcrypt": ("storage/repos/users.py",),
    # 通道实现也要发 HTTP：但只许在通道文件里，router/CLI 碰不到 requests
    "requests": ("datasource", "monitor/notifiers"),
    "akshare": ("datasource",),
    "yfinance": ("datasource",),
    "exchange_calendars": ("datasource",),
    # M1 没有任何模块需要 pandas：跨层传 DataFrame 一律 Polars（D13），
    # 边界处转 pandas 是 M5 与 backtrader 对接时才开的小口
    "pandas": (),
    # Fernet 只许住在 storage/crypto.py：通道凭据的加解密是存储层的细节，
    # 谁都能 import cryptography 就等于谁都能自己决定怎么存别人的密钥
    "cryptography": ("storage/crypto.py",),
    # 桌面与语音通道只能起本机进程；`env` 探测件不许起进程（它只读环境变量）
    "subprocess": ("monitor/notifiers",),
    "smtplib": ("monitor/notifiers/email.py",),
}


def _allowed_here(rel: str, prefix: str) -> bool:
    """`storage/db.py` 精确到文件，`datasource` 放行整层（含子包）。"""
    return rel == prefix or rel.startswith(prefix + "/")


@pytest.mark.parametrize(
    "path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC))
)
def test_third_party_imports_are_gated(path: Path):
    rel = str(path.relative_to(SRC))
    for name in imported_third_party(path):
        allowed = GATED.get(name)
        if allowed is None:
            continue  # 非受管库（polars 等）不受本用例约束
        assert any(_allowed_here(rel, prefix) for prefix in allowed), (
            f"{rel} import 了 {name}，但它只允许出现在 {list(allowed) or '任何地方都不许'}"
        )


def _layer(path: Path) -> str:
    """模块属于哪一层：目录名，src/sams 根下的文件则用文件名（bootstrap）。"""
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def test_only_known_layer_directories_exist():
    layers = set(ALLOWED) | {ASSEMBLY, "__pycache__"}
    extra = {p.name for p in SRC.iterdir() if p.is_dir() and p.name not in layers}
    assert extra == set(), f"出现未知层目录 {extra}：加层要同时改本文件的规则"


def test_bootstrap_is_the_only_assembly_root():
    """只有 bootstrap 可以同时握齐 storage、datasource 与 services 来接线。

    services 用两层是允许的（它的职责就是编排），但把三层凑齐就是第二个组装根。
    """
    all_three = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if {"storage", "datasource", "services"} <= imported_layers(path)
        and _layer(path) != ASSEMBLY
    ]
    assert all_three == [], f"{all_three} 自行组装依赖，应改为从 bootstrap 注入"


#: 值得单独点名的是一组**不过闸**的裸算原语（`hfq_frame` 与它能拼出因子的那几个零件：
#: 不查覆盖、不听未标记跳变）。分层规则管得住「谁能 import 哪一层」，管不住「同一层里谁挑了
#: 哪个函数」，而绕过 `adjusted_series` 拿它们出数，症状恰恰是一条看起来完全正常的错曲线——
#: U12 那个数只是换个形状出门。`day_factors` 单独就够拼出 hfq 序列，所以只钉 `hfq_frame`
#: 等于留了一扇写着「请勿使用」的门。消费者必须只有一个，且由机器守着。
SOLE_CONSUMER: dict[str, str] = {
    "hfq_frame": "analysis/adjust.py",
    "day_factors": "analysis/adjust.py",
    "ex_reference": "analysis/adjust.py",
    "group_by_ex_date": "analysis/adjust.py",
}


def _used_names(path: Path) -> set[str]:
    """模块里出现过的标识符：调用、属性、`from x import y` 都算「用了这个名字」。

    字符串形式的动态取用也算：`getattr(adjust, "hfq_frame")` 里那个名字不是 Name 也不是
    Attribute 节点，只扫标识符的话，绕过守卫的那条路连一行可疑代码都不留下——而守卫的意义
    正是「这条链上没有第二个出数口」，它不该只挡写得直白的那一种。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.split(".")[-1])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value.rsplit(".", 1)[-1])
    return names


@pytest.mark.parametrize(
    "path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC))
)
def test_unguarded_primitives_have_only_their_declared_consumer(path: Path):
    rel = str(path.relative_to(SRC))
    used = _used_names(path)
    for symbol, owner in SOLE_CONSUMER.items():
        if rel == owner:
            continue
        assert symbol not in used, (
            f"{rel} 用了不过闸的裸原语 {symbol}：出数只能走 analysis.adjust.adjusted_series，"
            "它后面那三道闸（覆盖 / 未标记跳变 / 收盘价）才会生效"
        )


def test_the_guard_notices_a_reach_that_is_not_written_as_a_name(tmp_path):
    """守卫自己也得能验：一行 `getattr` 就绕过它，等于没有守卫。

    起别名那条路（`import hfq_frame as f`）之所以挡得住，是因为 `ast.alias` 里记的还是
    原名；字符串那条路不是——名字在 AST 里根本不作为标识符出现。
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from sams.analysis import adjust\n"
        "g = getattr(adjust, 'hfq_frame')\n"
        "from sams.analysis.adjust import day_factors as d\n",
        encoding="utf-8",
    )
    used = _used_names(probe)
    assert {"hfq_frame", "day_factors"} <= used


@pytest.mark.parametrize(
    "path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC))
)
def test_imports_point_downward(path: Path):
    own = _layer(path)
    if own not in ALLOWED:
        return
    may_use_assembly = own in ASSEMBLY_CONSUMERS
    for target in imported_layers(path):
        if target == own:
            continue
        if target == ASSEMBLY:
            # 各层回头 import 组装根会构成环（storage → bootstrap → storage），
            # 而环恰好是这个测试要防的那类东西
            assert may_use_assembly, f"{path.relative_to(SRC)}：{own} 层不得 import 组装根"
            continue
        assert target in ALLOWED[own], (
            f"{path.relative_to(SRC)}：{own} 层不得 import {target} 层，"
            f"允许的是 {sorted(ALLOWED[own])}"
        )


def test_monitor_may_only_reach_core_and_storage():
    """`monitor` 的许可是 `{core, storage}`，不是声明里那个更宽的集合。

    收紧的理由只有一条：路由（`monitor/router.py`）住在 monitor，而 `services` 不许
    import `monitor`。如果 monitor 又能 import services，两边就会长成一个大模块——
    「谁认识谁」这件事不再有一个可检查的答案。
    """
    assert ALLOWED["monitor"] == {"core", "storage"}


def test_notify_transports_have_a_home_and_nowhere_else():
    assert _allowed_here("monitor/notifiers/wecom.py", GATED["requests"][1]), (
        "通道实现要能落在 monitor/notifiers，但不能顺手扩到整个 monitor 层：发送侧的"
        "HTTP 只许出现在通道文件里，router 与 CLI 碰不到 requests"
    )
    assert not _allowed_here("monitor/router.py", GATED["requests"][1])
    assert _allowed_here("monitor/notifiers/email.py", GATED["smtplib"][0])
    assert _allowed_here("monitor/notifiers/desktop.py", GATED["subprocess"][0])
    assert not _allowed_here("monitor/env.py", GATED["subprocess"][0])
    assert GATED["cryptography"] == ("storage/crypto.py",)
