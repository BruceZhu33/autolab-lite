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


def _private_segments(module: str) -> list[str]:
    return [seg for seg in module.split(".") if seg.startswith("_") and not seg.startswith("__")]


def _imports_from_source(source: str, rel_parts: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    """解析源码中所有对 autolab_control 内部模块的导入(同时覆盖 ast.ImportFrom 与 ast.Import)。"""
    here = list(rel_parts[:-1])
    out: list[tuple[str, list[str]]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level:
            keep = max(0, len(here) - (node.level - 1))
            base = here[:keep]
            mod_parts = node.module.split(".") if node.module else []
            prefix_parts = [*base, *mod_parts]
            if prefix_parts:
                target = ".".join(prefix_parts)
                out.append((target, [*_private_segments(target), *(a.name for a in node.names)]))
            else:
                for alias in node.names:
                    out.append((alias.name, [alias.name]))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "autolab_control":
                for alias in node.names:
                    out.append((alias.name, [alias.name]))
            elif mod.startswith("autolab_control."):
                sub = mod.removeprefix("autolab_control.")
                out.append((sub, [*_private_segments(sub), *(a.name for a in node.names)]))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("autolab_control."):
                    sub = alias.name.removeprefix("autolab_control.")
                    out.append((sub, _private_segments(sub)))
    return out


def _imports(path: Path) -> list[tuple[str, list[str]]]:
    """(被导入的 autolab_control 内模块, 导入的名字) 列表。"""
    rel = path.relative_to(PACKAGE).with_suffix("")
    return _imports_from_source(path.read_text(encoding="utf-8"), rel.parts)


def find_layer_violations(package_root: Path = PACKAGE) -> list[str]:
    violations: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        rel = path.relative_to(package_root).with_suffix("")
        own = _layer(".".join(rel.parts))
        if own not in LAYERS:
            continue
        for target, _ in _imports_from_source(path.read_text(encoding="utf-8"), rel.parts):
            other = _layer(target)
            if other in LAYERS and LAYERS.index(other) > LAYERS.index(own):
                violations.append(f"{'/'.join(rel.parts)} ({own}) → {target} ({other})")
    return violations


def test_dependencies_only_point_down():
    violations = find_layer_violations(PACKAGE)
    assert not violations, "依赖方向违规:\n" + "\n".join(violations)


def test_no_private_imports_across_modules():
    violations = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for target, names in _imports(path):
            private = [n for n in names if n.startswith("_") and not n.startswith("__")]
            if private:
                violations.append(f"{path.relative_to(PACKAGE)} 导入 {target}.{private}")
    assert not violations, "\n".join(violations)
