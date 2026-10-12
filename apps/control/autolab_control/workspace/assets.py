"""资料供给:数据集 / 权重放进工作区的两条通道。

① 服务器上已有目录 → 符号链接到工作区目标路径(大数据集不搬家);
② zip 上传 → 解包到目标路径(小数据集 / 预训练权重)。
目标路径由 Baseline agent 的提问给出(ask_human 的 target_path)。
"""

from __future__ import annotations

from ..core.errors import UserError
from .fs import Workspace
from .host import q


def _target(ws: Workspace, target_rel: str) -> str:
    rel = (target_rel or "").strip().strip("/")
    if not rel or rel.startswith(".autolab"):
        raise UserError("目标路径必须是工作区内的相对路径")
    return ws.abs(rel)


def link_dir(ws: Workspace, target_rel: str, server_path: str) -> dict:
    target = _target(ws, target_rel)
    src = (server_path or "").strip()
    if not src.startswith("/"):
        raise UserError("来源必须是服务器上的绝对路径")
    host = ws.host
    if not host.run(f'test -d {q(src)} && test -n "$(ls -A {q(src)} | head -1)"').ok:
        raise UserError(f"来源路径不存在或为空目录:{src}")
    if host.exists(target) or host.run(f"test -L {q(target)}").ok:
        # 空目录可以替换;已有内容(如标注文件)不覆盖,改用 zip 合并
        if not host.run(f'test -d {q(target)} && test -z "$(ls -A {q(target)})"'
                        f" && rmdir {q(target)}").ok:
            raise UserError(f"目标已有内容,不覆盖:{target}(改用 zip 上传会合并进该目录)")
    result = host.run(f"mkdir -p $(dirname {q(target)}) && ln -s {q(src)} {q(target)}")
    if not result.ok:
        raise UserError(f"创建链接失败:{result.text[-200:]}")
    return {"target": target, "source": src, "kind": "symlink"}


def unzip_into(ws: Workspace, target_rel: str, zip_bytes: bytes, *, merge: bool = False) -> dict:
    target = _target(ws, target_rel)
    if not zip_bytes:
        raise UserError("上传内容为空")
    host = ws.host
    if host.exists(target) and not merge:
        raise UserError(f"目标已存在,不覆盖:{target}")
    staging = f"{ws.workdir}/.autolab/upload.zip"
    host.write(staging, zip_bytes)
    result = host.run(f"mkdir -p {q(target)} && {q(host.python)} -m zipfile -e"
                      f" {q(staging)} {q(target)}; code=$?; rm -f {q(staging)}; exit $code",
                      timeout=600)
    if not result.ok:
        if not merge:
            host.run(f"rm -rf {q(target)}")
        raise UserError(f"解包失败(仅支持标准 zip):{result.text[-300:]}")
    return {"target": target, "kind": "zip"}
