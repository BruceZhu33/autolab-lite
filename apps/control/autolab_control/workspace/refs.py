"""参考代码库:登记在控制端本机(~/.autolab/refs/),研究 agent 只读访问。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..core.errors import UserError
from .fs import validate_safe_local_path

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_GIT_URL_RE = re.compile(r"^(?:https?://[^\s]+|git@[A-Za-z0-9._-]+:[A-Za-z0-9._/-]+)$")


def _refs_root() -> Path:
    root = Path.home() / ".autolab" / "refs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_slug(slug: str) -> str:
    if not slug or slug in {".", ".."} or not _SAFE_SLUG_RE.fullmatch(slug):
        raise UserError(f"参考库名称不合法:{slug}")
    return slug


def register(source: str) -> dict:
    """git URL → clone --depth 1(已存在则 pull);本机路径 → 直接索引。
    返回 {name, source, path}。"""
    source = (source or "").strip()
    if not source or "\x00" in source or source.startswith("-"):
        raise UserError(f"非法的参考库来源:{source}")
    if source.startswith("file://"):
        raise UserError("不允许使用 file:// 协议登记参考库")
    if source.startswith(("http://", "https://", "git@")) or "://" in source:
        if not _GIT_URL_RE.fullmatch(source):
            raise UserError(f"参考库 Git 地址仅支持 http(s):// 或 git@host:path:{source}")
        tail = source.rstrip("/")
        if tail.startswith("git@") and ":" in tail:
            tail = tail.split(":", 1)[1]
        raw_name = tail.split("/")[-1].removesuffix(".git")
        name = _validate_slug(raw_name)
        root = _refs_root().resolve()
        target = (root / name).resolve()
        if target.parent != root:
            raise UserError(f"参考库目标路径越界:{name}")
        if target.exists():
            subprocess.run(["git", "-C", str(target), "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=120)
        else:
            cloned = subprocess.run(["git", "clone", "--depth", "1", "--", source, str(target)],
                                    capture_output=True, text=True, timeout=300)
            if cloned.returncode != 0:
                raise UserError(f"参考库克隆失败:{cloned.stderr[-250:]}")
        return {"name": name, "source": source, "path": str(target)}
    local = validate_safe_local_path(source, label="本机参考路径")
    if not local.is_dir():
        raise UserError(f"本机参考路径不存在:{local}")
    name = _validate_slug(local.name)
    return {"name": name, "source": source, "path": str(local)}


def read(refs: list[dict], name: str, path: str = ".", offset: int = 1, limit: int = 400) -> str:
    """目录 → 两层列表;文件 → 带行号分片。路径不得越出参考库根。"""
    by_name = {r["name"]: r for r in refs}
    if name not in by_name:
        return "可用参考库:" + (", ".join(by_name) or "无")
    raw_root = str(by_name[name]["path"] or "")
    root = Path(raw_root).expanduser().resolve()
    refs_home = _refs_root().resolve()
    if root != refs_home and refs_home not in root.parents:
        root = validate_safe_local_path(raw_root, label="本机参考路径")
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
