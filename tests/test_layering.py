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
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "storage": {"core"},
    "datasource": {"core"},
    "services": {"core", "storage", "datasource"},
    "analysis": {"core", "storage", "services"},
    "monitor": {"core", "storage", "datasource", "services", "analysis"},
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
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parts = path.relative_to(ROOT).with_suffix("").parts
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "", node.level)]
        else:
            continue
        for name, level in names:
            segments = _resolved(name, level, parts).split(".")
            if len(segments) > 1 and segments[0] == "sams":
                found.add(segments[1])
    return found


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
