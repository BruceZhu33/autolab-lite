"""立项:把代码放到服务器上,得到项目工作区。

三种来源:服务器上已有目录 / git clone / zip 上传解包;另有「从本机目录或
GitHub 重新同步代码」。本机与 SSH 服务器共用同一套逻辑(经由 Host)。
"""

from __future__ import annotations

import os
import tarfile
import tempfile
from pathlib import Path

from ..core.errors import UserError
from .host import Host, q

PROJECTS_DIR = "autolab-projects"
SYNC_EXCLUDES = {
    ".git", ".venv", "venv", "__pycache__", ".autolab", "node_modules",
    "output", "outputs", "runs", "wandb", "data", "dataset", "datasets",
}
SYNC_EXCLUDE_SUFFIXES = (".ckpt", ".pt", ".pth")
SYNC_MAX_BYTES = 800 * 1024 * 1024  # 代码同步上限;数据集刻意不在同步范围


def _new_target(host: Host, name: str) -> str:
    base = f"{host.home()}/{PROJECTS_DIR}"
    host.run(f"mkdir -p {q(base)}")
    target = f"{base}/{name}"
    if host.exists(target):
        raise UserError(f"目标目录已存在:{target}")
    return target


def _unwrap_single_dir(host: Host, target: str) -> str:
    """zip 常见 <repo>-main/ 单层前缀:唯一条目且是目录时以它为工作区。"""
    entries = [e for e in host.run(f"ls -1Ap {q(target)}").out.splitlines() if e.strip()]
    if len(entries) == 1 and entries[0].endswith("/"):
        return f"{target}/{entries[0].rstrip('/')}"
    return target


def use_existing_dir(host: Host, workdir: str) -> str:
    if not workdir.startswith("/"):
        raise UserError("工作目录必须是绝对路径")
    if not host.run(f"test -d {q(workdir)}").ok:
        raise UserError(f"目录不存在:{workdir}")
    if not host.run(f"test -w {q(workdir)}").ok:
        raise UserError(f"目录不可写:{workdir}")
    return workdir.rstrip("/")


def clone_repo(host: Host, *, url: str, name: str) -> str:
    target = _new_target(host, name)
    result = host.run(f"git clone --depth 1 {q(url)} {q(target)}", timeout=300)
    if not result.ok:
        host.run(f"rm -rf {q(target)}")
        detail = result.text[-300:]
        hint = ("服务器无法访问该地址(网络受限):改用上传 zip"
                if "timed out" in detail or "Failed to connect" in detail
                else "私有仓库需先在服务器上配置 Git 凭据")
        raise UserError(f"git clone 失败:{detail}({hint})")
    return target


def extract_zip(host: Host, zip_bytes: bytes, *, name: str) -> str:
    if not zip_bytes:
        raise UserError("上传内容为空")
    target = _new_target(host, name)
    staging = f"{target}.zip"
    host.write(staging, zip_bytes)
    result = host.run(f"{q(host.python)} -m zipfile -e {q(staging)} {q(target)}"
                      f" && rm -f {q(staging)}", timeout=600)
    if not result.ok:
        host.run(f"rm -rf {q(target)} {q(staging)}")
        raise UserError(f"zip 解包失败(仅支持标准 zip):{result.text[-300:]}")
    return _unwrap_single_dir(host, target)


def prepare_workspace(host: Host, workdir: str) -> None:
    """工作区内建 .autolab/(实验记录、论文、研究日志都放这里)。"""
    host.run(f"mkdir -p {q(workdir)}/.autolab")


def code_presence(host: Host, workdir: str) -> dict:
    if not host.run(f"test -d {q(workdir)}").ok:
        return {"present": False, "reason": "目录不存在", "files": []}
    files = [line for line in host.run(
        f"ls -1A {q(workdir)} | grep -v '^\\.autolab$' | head -30").out.splitlines() if line.strip()]
    return {"present": bool(files), "reason": "" if files else "目录为空", "files": files}


def pull_github(host: Host, workdir: str) -> str:
    result = host.run(f"git -C {q(workdir)} pull --ff-only", timeout=120)
    if not result.ok:
        raise UserError(f"git pull 失败:{result.text[-200:]}")
    return "已执行 git pull --ff-only"


def _tar_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """丢弃 macOS AppleDouble(._*)与元数据——它们会毒害远端的 glob 扫描。"""
    name = member.name.rsplit("/", 1)[-1]
    if name.startswith("._") or name in (".DS_Store", "__MACOSX"):
        return None
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def push_local_dir(host: Host, local_path: str, workdir: str) -> str:
    """本机目录 → 服务器工作区:tar(排除数据/产物/.git)→ 上传 → 解包;
    远端 .autolab/ 保留。"""
    source = Path(local_path).expanduser()
    if not source.is_dir():
        raise UserError(f"本地目录不存在:{source}")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        archive = Path(tmp.name)
    previous = os.environ.get("COPYFILE_DISABLE")
    os.environ["COPYFILE_DISABLE"] = "1"  # 不把 macOS 扩展属性打成 ._ 条目
    try:
        with tarfile.open(archive, "w:gz") as tar:
            for item in source.iterdir():
                if item.name in SYNC_EXCLUDES or item.name.startswith("._") \
                        or item.name.endswith(SYNC_EXCLUDE_SUFFIXES):
                    continue
                tar.add(item, arcname=item.name, filter=_tar_filter)
        size = archive.stat().st_size
        if size > SYNC_MAX_BYTES:
            raise UserError(f"打包后 {size // (1024 * 1024)}MB 超过上限:"
                            "请检查是否包含大文件(数据集放服务器上,不需要同步)")
        remote = f"{workdir}/.autolab/.sync.tar.gz"
        host.write(remote, archive.read_bytes())
        result = host.run(f"tar -xzf {q(remote)} -C {q(workdir)} && rm -f {q(remote)}"
                          f" && find {q(workdir)} -name '._*' -type f -delete", timeout=300)
        if not result.ok:
            raise UserError(f"远端解包失败:{result.text[-200:]}")
        return f"已同步本机目录({size // 1024}KB,已排除数据/产物/.git)"
    finally:
        if previous is None:
            os.environ.pop("COPYFILE_DISABLE", None)
        else:
            os.environ["COPYFILE_DISABLE"] = previous
        archive.unlink(missing_ok=True)
