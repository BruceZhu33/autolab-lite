"""立项:把代码放到服务器上,得到项目工作区。

三种来源:服务器上已有目录 / git clone / zip 上传解包;另有「从本机目录或
GitHub 重新同步代码」。本机与 SSH 服务器共用同一套逻辑(经由 Host)。
"""

from __future__ import annotations

import datetime
import json
import os
import posixpath
import re
import tarfile
import tempfile
from pathlib import Path

from ..core.errors import UserError
from .fs import safe_extract_zip, validate_safe_local_path
from .host import Host, q

PROJECTS_DIR = "autolab-projects"
SITONHOLY_PERSIST_ROOT = "/root/data1"
_PERSIST_DATA1_MARKER = "__AUTOLAB_DATA1__"
SYNC_EXCLUDES = {
    ".git", ".venv", "venv", "__pycache__", ".autolab", ".autolab-runner", ".ssh",
    ".gnupg", "node_modules",
    "output", "outputs", "runs", "wandb", "data", "dataset", "datasets",
}
SYNC_EXCLUDE_SUFFIXES = (".ckpt", ".pt", ".pth")
SYNC_MAX_BYTES = 800 * 1024 * 1024  # 代码同步上限;数据集刻意不在同步范围
_GIT_URL_RE = re.compile(r"^(?:https?://[^\s]+|git@[A-Za-z0-9._-]+:[A-Za-z0-9._/-]+)$")


def validate_project_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        raise UserError("项目名称不能为空")
    if (
        clean in {".", ".."}
        or "/" in clean
        or "\\" in clean
        or ".." in clean
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in clean)
    ):
        raise UserError(f"项目名称包含非法路径字符:{name}")
    return clean


def validate_git_url(url: str) -> str:
    clean = (url or "").strip()
    if not clean or clean.startswith("-") or "\x00" in clean:
        raise UserError(f"非法的 Git 仓库地址:{url}")
    if not _GIT_URL_RE.fullmatch(clean):
        raise UserError("Git 仓库地址仅支持 http(s):// 或 git@host:path 格式")
    return clean


def default_projects_dir(host: Host) -> str:
    """容器/服务器挂载了可写的持久化卷 `/root/data1` 时优先用 `/root/data1/autolab-projects`,否则回落 `~/autolab-projects`。"""
    probe = host.run(
        f"test -d {SITONHOLY_PERSIST_ROOT} && test -w {SITONHOLY_PERSIST_ROOT} && echo {_PERSIST_DATA1_MARKER}",
        timeout=10,
    )
    if probe.ok and _PERSIST_DATA1_MARKER in (probe.out or ""):
        return posixpath.normpath(f"{SITONHOLY_PERSIST_ROOT}/{PROJECTS_DIR}")
    return posixpath.normpath(f"{host.home().rstrip('/')}/{PROJECTS_DIR}")


def project_target(host: Host, name: str) -> str:
    """解析并校验新项目目标路径(`<default_projects_dir>/<clean_name>`),不创建目录。"""
    clean = validate_project_name(name)
    base = default_projects_dir(host)
    target = posixpath.normpath(f"{base}/{clean}")
    if posixpath.dirname(target) != base:
        raise UserError(f"项目目标路径越界:{name}")
    return target


def _new_target(host: Host, name: str) -> str:
    target = project_target(host, name)
    host.run(f"mkdir -p {q(posixpath.dirname(target))}")
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


_FORK_EXCLUDES = frozenset({
    ".git", ".autolab", ".autolab-runner", ".venv", "venv",
    "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".DS_Store",
})
FORK_SHARED_PATHS_REL = ".autolab/fork_shared_paths"
_NON_SOURCE_UNTRACKED_TOPS = frozenset({"data", "dataset", "datasets", "checkpoints"})
_NON_SOURCE_UNTRACKED_SUFFIXES = (
    ".ckpt", ".pt", ".pth", ".safetensors", ".bin", ".h5", ".onnx",
)
_STATIC_SOURCE_EXCLUDE_SPECS = (
    ":(exclude).autolab",
    ":(exclude).autolab-runner",
    ":(exclude)output*",
    ":(exclude)outputs",
    ":(exclude)runs",
    ":(exclude)wandb",
    ":(exclude).venv",
    ":(exclude)venv",
    ":(exclude)__pycache__",
    ":(exclude,glob)**/__pycache__",
    ":(exclude)node_modules",
    ":(exclude).pytest_cache",
    ":(exclude).mypy_cache",
    ":(exclude).ruff_cache",
    ":(exclude)*.egg-info",
    ":(exclude).DS_Store",
    ":(exclude)._*",
)


def _is_fork_excluded_part(name: str, *, top_level: bool = False) -> bool:
    return (
        not name
        or name in _FORK_EXCLUDES
        or name.startswith("._")
        or name.endswith(".egg-info")
        or (top_level and name.startswith("output"))
    )


def _share_fork_entry(
    host: Host,
    src: str,
    dst: str,
    *,
    rel: str,
    shared_rels: list[str],
    top_level: bool,
    depth: int = 0,
) -> None:
    """将源工作区未跟踪/被忽略的条目共享到新 worktree:
    - 目标不存在:目录/软链/子层文件做符号链接(ln -sn),顶层普通小文件复制,并记入 shared_rels;
    - 目标已存在且为真实目录(如 git 跟踪了 data/.gitkeep):递归补齐缺失子项,避免嵌套 data/data。"""
    if depth > 4:
        return
    if not host.run(f"test -e {q(dst)} -o -L {q(dst)}").ok:
        parent = posixpath.dirname(dst)
        if parent:
            host.run(f"mkdir -p {q(parent)}")
        if (not top_level) or host.run(f"test -d {q(src)} -o -L {q(src)}").ok:
            host.run(f"ln -sn {q(src)} {q(dst)}")
        else:
            host.run(f"cp -a {q(src)} {q(dst)}")
        shared_rels.append(rel)
        return
    if host.run(f"test -d {q(dst)} && test ! -L {q(dst)} && test -d {q(src)}").ok:
        children = [
            line.strip()
            for line in (host.run(f"ls -1A {q(src)}").out or "").splitlines()
            if line.strip()
        ]
        for child in children:
            if _is_fork_excluded_part(child, top_level=False):
                continue
            _share_fork_entry(
                host, f"{src}/{child}", f"{dst}/{child}",
                rel=f"{rel}/{child}", shared_rels=shared_rels,
                top_level=False, depth=depth + 1,
            )


def source_git_pathspecs(host: Host, workdir: str) -> list[str]:
    """构造 git status / add / diff 仅针对「项目源码」的 pathspec 列表:
    排除 .autolab、output*、虚拟环境/缓存、fork_workspace 共享链接以及未跟踪的数据集/权重目录。"""
    workdir = workdir.rstrip("/")
    specs: list[str] = [".", *_STATIC_SOURCE_EXCLUDE_SPECS]
    seen = set(specs)

    def _add_exclude(rel_path: str) -> None:
        clean = rel_path.strip().strip("/")
        if not clean or ".." in clean.split("/"):
            return
        spec = f":(exclude){clean}"
        if spec not in seen:
            seen.add(spec)
            specs.append(spec)

    marker = f"{workdir}/{FORK_SHARED_PATHS_REL}"
    if host.run(f"test -f {q(marker)}").ok:
        try:
            raw_marker = host.read(marker).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw_marker = ""
        for line in raw_marker.splitlines():
            _add_exclude(line)

    untracked = host.run(
        f"git -C {q(workdir)} ls-files --others --exclude-standard --directory 2>/dev/null"
    )
    if untracked.ok:
        for raw in (untracked.out or "").splitlines():
            rel = raw.strip().rstrip("/")
            if not rel:
                continue
            top = rel.split("/", 1)[0]
            if (
                top in _NON_SOURCE_UNTRACKED_TOPS
                or rel.endswith(_NON_SOURCE_UNTRACKED_SUFFIXES)
                or host.run(f"test -L {q(workdir + '/' + rel)}").ok
            ):
                _add_exclude(rel)
    return specs


def fork_workspace(host: Host, source_workdir: str, new_workdir: str, commit: str, *,
                   branch: str | None = None) -> str:
    """研究分叉的工作区侧:git worktree 检出目标基线版本的代码(秒级,零拷贝),
    源工作区里未被 git 跟踪或被 .gitignore 忽略的大目录(数据集/预训练权重)以符号链接共享;
    .autolab(实验记录/研究日志)与 output*(训练产物)不共享,各自独立。
    branch 显式给定时用它作新分支名(campaign 并行 fork 同秒不撞默认时间戳);
    返回新分支名。"""
    source_workdir = source_workdir.rstrip("/")
    new_workdir = new_workdir.rstrip("/")
    git = f"git -C {q(source_workdir)}"
    if not host.run(f"{git} rev-parse --is-inside-work-tree").ok:
        raise UserError("源项目工作区不是 git 仓库,无法分叉;只有 git 工作区支持分叉")
    if host.run(f"test -e {q(new_workdir)} -o -L {q(new_workdir)}").ok:
        raise UserError(f"目标目录已存在:{new_workdir}")
    parent_dir = posixpath.dirname(new_workdir)
    if parent_dir:
        host.run(f"mkdir -p {q(parent_dir)}")
    branch = branch or ("autolab/fork-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    out = host.run(f"{git} worktree add -b {q(branch)} {q(new_workdir)} {q(commit)}", timeout=120)
    if not out.ok:
        raise UserError(f"git worktree 检出失败:{(out.err or out.out).strip()[:200]}")
    # 同时扫描未跟踪与 .gitignore 忽略的条目(不加 --exclude-standard),避免漏掉被忽略的 data/checkpoints;
    # 排除 .git/.autolab/output*/虚拟环境/缓存;若目标目录已由 git worktree 建出(如 data/.gitkeep),
    # 仅链接缺失子项,绝不制造 <new_workdir>/data/data 嵌套死链。
    listing = host.run(f"{git} ls-files --others --directory")
    rels: list[str] = []
    for raw in (listing.out or "").splitlines():
        rel = raw.strip().rstrip("/")
        if not rel:
            continue
        parts = [p for p in rel.split("/") if p]
        if not parts:
            continue
        if any(_is_fork_excluded_part(p, top_level=(idx == 0)) for idx, p in enumerate(parts)):
            continue
        rels.append("/".join(parts))
    handled_prefixes: list[str] = []
    shared_rels: list[str] = []
    for rel in sorted(set(rels)):
        if any(rel == p or rel.startswith(p + "/") for p in handled_prefixes):
            continue
        handled_prefixes.append(rel)
        _share_fork_entry(
            host, f"{source_workdir}/{rel}", f"{new_workdir}/{rel}",
            rel=rel, shared_rels=shared_rels,
            top_level=("/" not in rel),
        )
    host.run(f"mkdir -p {q(new_workdir)}/.autolab")
    if shared_rels:
        host.write(
            f"{new_workdir}/{FORK_SHARED_PATHS_REL}",
            ("\n".join(shared_rels) + "\n").encode("utf-8"),
        )
    return branch


def _within(path: str, roots: list[str]) -> bool:
    if not path or not path.startswith("/"):
        return False
    norm_path = posixpath.normpath(path)
    for root in roots:
        if not root or not str(root).startswith("/"):
            continue
        norm_root = posixpath.normpath(str(root))
        if norm_root == "/":
            return True
        if norm_path == norm_root or norm_path.startswith(norm_root + "/"):
            return True
    return False


def ensure_within_roots(server: dict, workdir: str, output_root: str) -> None:
    """立项早失败:工作区/输出目录必须在该服务器 Runner 的白名单根内。
    否则要到 Job 派发那一刻才被 Runner 拒(workdir_outside_roots),owner
    和 agent 都难定位——真机:项目立到 ~/autolab-projects,沙盒 Runner 只
    信 /tmp,agent 拷整个工程自救也无效。roots 未登记(空)时跳过,
    Runner 派发时仍会兜底拒绝。"""
    work_roots = json.loads(server.get("allowed_work_roots_json") or "[]")
    out_roots = json.loads(server.get("allowed_output_roots_json") or "[]")
    problems: list[str] = []
    if work_roots and workdir and not _within(workdir, work_roots):
        problems.append(f"工作区 {workdir} 不在 Runner 允许根目录 {work_roots} 内")
    if out_roots and output_root and not _within(output_root, out_roots):
        problems.append(f"输出目录 {output_root} 不在 Runner 允许输出根目录 {out_roots} 内")
    if problems:
        raise UserError(
            "；".join(problems)
            + "——Job 派发时会被该服务器的 Runner 直接拒绝。两选一:"
              "①把项目放到白名单内的路径重新立项;"
              "②由 owner 重新部署该服务器的 Runner,把目标路径的父目录"
              "加进 allowed_work_roots/allowed_output_roots(GPU Servers 页操作)")


def clone_repo(host: Host, *, url: str, name: str) -> str:
    clean_url = validate_git_url(url)
    target = _new_target(host, name)
    result = host.run(f"git clone --depth 1 -- {q(clean_url)} {q(target)}", timeout=300)
    if not result.ok:
        host.run(f"rm -rf {q(target)}")
        detail = result.text[-300:]
        hint = ("服务器无法访问该地址(网络受限):改用上传 zip"
                if "timed out" in detail or "Failed to connect" in detail
                else "私有仓库需先在服务器上配置 Git 凭据")
        raise UserError(f"git clone 失败:{detail}({hint})")
    return target


def extract_zip(host: Host, zip_bytes: bytes | Path, *, name: str) -> str:
    if isinstance(zip_bytes, Path):
        if not zip_bytes.is_file() or zip_bytes.stat().st_size == 0:
            raise UserError("上传内容为空")
    elif not zip_bytes:
        raise UserError("上传内容为空")
    target = _new_target(host, name)
    staging = f"{target}.zip"
    try:
        safe_extract_zip(host, zip_bytes, target, staging_path=staging)
    except Exception:
        host.run(f"rm -rf {q(target)} {q(staging)}")
        raise
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
    source = validate_safe_local_path(local_path, label="本机代码目录")
    if not source.is_dir():
        raise UserError(f"本地目录不存在:{source}")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        archive = Path(tmp.name)
    previous = os.environ.get("COPYFILE_DISABLE")
    os.environ["COPYFILE_DISABLE"] = "1"  # 不把 macOS 扩展属性打成 ._ 条目

    def safe_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        filtered = _tar_filter(member)
        if filtered is None:
            return None
        parts = [p for p in filtered.name.split("/") if p]
        if any(p in SYNC_EXCLUDES or p == ".." for p in parts):
            return None
        if filtered.issym() or filtered.islnk():
            candidate = (source / filtered.name).parent / filtered.linkname
            resolved = candidate.resolve()
            if resolved != source and source not in resolved.parents:
                return None
        return filtered

    try:
        with tarfile.open(archive, "w:gz") as tar:
            for item in source.iterdir():
                if item.name in SYNC_EXCLUDES or item.name.startswith("._") \
                        or item.name.endswith(SYNC_EXCLUDE_SUFFIXES):
                    continue
                if item.is_symlink():
                    target_resolved = item.resolve()
                    if target_resolved != source and source not in target_resolved.parents:
                        continue
                tar.add(item, arcname=item.name, filter=safe_filter)
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
