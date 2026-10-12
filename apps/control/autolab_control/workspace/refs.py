"""参考代码库:登记在控制端本机(~/.autolab/refs/),研究 agent 只读访问。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..core.errors import UserError


def _refs_root() -> Path:
    root = Path.home() / ".autolab" / "refs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def register(source: str) -> dict:
    """git URL → clone --depth 1(已存在则 pull);本机路径 → 直接索引。
    返回 {name, source, path}。"""
    source = source.strip()
    if source.startswith(("http://", "https://", "git@", "file://")):
        name = source.rstrip("/").split("/")[-1].removesuffix(".git") or "ref"
        target = _refs_root() / name
        if target.exists():
            subprocess.run(["git", "-C", str(target), "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=120)
        else:
            cloned = subprocess.run(["git", "clone", "--depth", "1", source, str(target)],
                                    capture_output=True, text=True, timeout=300)
            if cloned.returncode != 0:
                raise UserError(f"参考库克隆失败:{cloned.stderr[-250:]}")
        return {"name": name, "source": source, "path": str(target)}
    local = Path(source).expanduser()
    if not local.is_dir():
        raise UserError(f"本机参考路径不存在:{local}")
    return {"name": local.name, "source": source, "path": str(local)}


def read(refs: list[dict], name: str, path: str = ".", offset: int = 1, limit: int = 400) -> str:
    """目录 → 两层列表;文件 → 带行号分片。路径不得越出参考库根。"""
    by_name = {r["name"]: r for r in refs}
    if name not in by_name:
        return "可用参考库:" + (", ".join(by_name) or "无")
    root = Path(by_name[name]["path"]).resolve()
    target = (root / (path or ".")).resolve()
    if target != root and root not in target.parents:
        raise UserError("路径越界")
    if target.is_dir():
        entries = sorted(p.relative_to(root).as_posix() + ("/" if p.is_dir() else "")
                         for p in target.rglob("*")
                         if ".git" not in p.parts and len(p.relative_to(target).parts) <= 2)
        return "\n".join(entries[:300]) or "(空目录)"
    if not target.is_file():
        raise UserError(f"不存在:{path}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, offset)
    chunk = lines[start - 1:start - 1 + max(1, limit)]
    return "\n".join(f"{start + i}\t{line}" for i, line in enumerate(chunk))[:30000]
