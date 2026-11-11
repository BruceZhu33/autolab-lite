"""资料供给:数据集 / 权重放进工作区的两条通道。

① 服务器上已有目录 → 符号链接到工作区目标路径(大数据集不搬家);
② zip 上传 → 解包到目标路径(小数据集 / 预训练权重)。
目标路径由 Baseline agent 的提问给出(ask_human 的 target_path)。
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path

from ..core.errors import UserError
from .fs import Workspace, is_autolab_internal, normalize_rel_path, safe_extract_zip
from .host import LocalHost, q


def _target(ws: Workspace, target_rel: str) -> str:
    norm = normalize_rel_path(target_rel)
    if not norm or is_autolab_internal(norm):
        raise UserError("目标路径必须是工作区内的相对路径")
    target = f"{ws.workdir}/{norm}"
    parent = posixpath.dirname(target)
    real_root, real_parent = ws.ensure_within_workdir(parent)
    parent_rel = posixpath.relpath(real_parent, real_root)
    if is_autolab_internal(parent_rel):
        raise UserError("目标路径必须是工作区内的相对路径")
    return target


def _validate_server_asset_source(ws: Workspace, src: str) -> str:
    clean = (src or "").strip()
    if not clean.startswith("/") or "\x00" in clean:
        raise UserError("来源必须是服务器上的绝对路径")
    norm = posixpath.normpath(clean)
    home = posixpath.normpath(ws.host.home().rstrip("/") or "/")
    check_paths = [norm]
    if isinstance(ws.host, LocalHost):
        check_paths.append(os.path.realpath(clean))
    protected = tuple(
        p for p in (
            f"{home}/.autolab" if home != "/" else "/.autolab",
            f"{home}/.autolab-runner" if home != "/" else "/.autolab-runner",
            f"{home}/.ssh" if home != "/" else "/.ssh",
            "/etc",
            "/private/etc",
            "/root",
        )
        if p
    )
    for candidate in check_paths:
        if candidate in ("/", home):
            raise UserError(f"来源路径不能直接指向根目录或家目录根路径:{src}")
        for sens in protected:
            if candidate == sens or candidate.startswith(sens + "/"):
                raise UserError(f"来源路径不允许指向受保护系统目录:{src}")
    return clean


def link_dir(ws: Workspace, target_rel: str, server_path: str) -> dict:
    target = _target(ws, target_rel)
    src = _validate_server_asset_source(ws, server_path)
    host = ws.host
    if not host.run(f'test -d {q(src)} && test -n "$(ls -A {q(src)} | head -1)"').ok:
        raise UserError(f"来源路径不存在或为空目录:{src}")
    if host.exists(target) or host.run(f"test -L {q(target)}").ok:
        # 空目录可以替换;已有内容(如标注文件)不覆盖,改用 zip 合并
        if not host.run(f'test -d {q(target)} && test -z "$(ls -A {q(target)})"'
                        f" && rmdir {q(target)}").ok:
            raise UserError(f"目标已有内容,不覆盖:{target}(改用 zip 上传会合并进该目录)")
    result = host.run(f'mkdir -p "$(dirname {q(target)})" && ln -s {q(src)} {q(target)}')
    if not result.ok:
        raise UserError(f"创建链接失败:{result.text[-200:]}")
    return {"target": target, "source": src, "kind": "symlink"}


def unzip_into(ws: Workspace, target_rel: str, zip_bytes: bytes | Path, *, merge: bool = False) -> dict:
    target = _target(ws, target_rel)
    if isinstance(zip_bytes, Path):
        if not zip_bytes.is_file() or zip_bytes.stat().st_size == 0:
            raise UserError("上传内容为空")
    elif not zip_bytes:
        raise UserError("上传内容为空")
    host = ws.host
    existed = host.exists(target) or host.run(f"test -L {q(target)}").ok
    if existed and not merge:
        raise UserError(f"目标已存在,不覆盖:{target}")
    real_root, real_target = ws.ensure_within_workdir(target)
    if is_autolab_internal(posixpath.relpath(real_target, real_root)):
        raise UserError("目标路径必须是工作区内的相对路径")
    host.run(f"mkdir -p {q(ws.workdir)}/.autolab {q(target)}")
    staging = f"{ws.workdir}/.autolab/upload.zip"
    try:
        safe_extract_zip(host, zip_bytes, target, staging_path=staging)
    except Exception:
        if not merge and not existed:
            host.run(f"rm -rf {q(target)}")
        raise
    return {"target": target, "kind": "zip"}
