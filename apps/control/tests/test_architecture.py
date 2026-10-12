"""分层守卫:依赖只能往下走,也不准跨模块用别人的 _私有 名字。

core < infra < workspace < execution < agent < stages < api < cli
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "autolab_control"
LAYERS = ["core", "infra", "workspace", "execution", "agent", "stages", "api", "cli"]


def _layer(module: str) -> str:
    return module.split(".")[0]


def _imports(path: Path) -> list[tuple[str, list[str]]]:
    """(被导入的 autolab_control 内模块, 导入的名字) 列表。"""
    rel = path.relative_to(PACKAGE).with_suffix("")
    here = list(rel.parts[:-1])
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = here[: len(here) - (node.level - 1)] if node.level > 1 else here
            target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            out.append((target, [a.name for a in node.names]))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("autolab_control."):
            out.append((node.module.removeprefix("autolab_control."), [a.name for a in node.names]))
    return out


def test_dependencies_only_point_down():
    violations = []
    for path in PACKAGE.rglob("*.py"):
        rel = path.relative_to(PACKAGE).with_suffix("")
        own = _layer(".".join(rel.parts))
        if own not in LAYERS:
            continue
        for target, _ in _imports(path):
            other = _layer(target)
            if other in LAYERS and LAYERS.index(other) > LAYERS.index(own):
                violations.append(f"{'/'.join(rel.parts)} ({own}) → {target} ({other})")
    assert not violations, "依赖方向违规:\n" + "\n".join(violations)


def test_no_private_imports_across_modules():
    violations = []
    for path in PACKAGE.rglob("*.py"):
        for target, names in _imports(path):
            private = [n for n in names if n.startswith("_") and not n.startswith("__")]
            if private:
                violations.append(f"{path.relative_to(PACKAGE)} 导入 {target}.{private}")
    assert not violations, "\n".join(violations)
