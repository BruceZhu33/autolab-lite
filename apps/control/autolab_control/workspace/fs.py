"""项目工作区:Host 之上、以工作区根为基准的相对路径读写与执行。

agent 工具与各阶段都经由这里碰工作区文件;写源码时先备份原件到
.autolab/patches/<时间>/orig/,.py 写入后做语法检查、不过即回滚。
"""

from __future__ import annotations

import datetime as _dt
import os
import posixpath
from contextlib import contextmanager
from pathlib import Path

from ..core.credentials import CredentialStore
from ..core.errors import UserError
from .host import Host, LocalHost, RunResult, open_host, q

PATCHES_REL = ".autolab/patches"
ZIP_MAX_ENTRIES = 50_000
ZIP_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

_SAFE_UNZIP_PY = """
import os, posixpath, re, sys, zipfile

zip_path, dest_dir, max_entries, max_bytes = (
    sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
)
try:
    zf = zipfile.ZipFile(zip_path, "r")
except zipfile.BadZipFile as exc:
    sys.stderr.write(f"无效的 zip 文件: {exc}\\n")
    sys.exit(2)

with zf:
    infos = zf.infolist()
    if len(infos) > max_entries:
        sys.stderr.write(f"zip 条目数({len(infos)})超过安全上限({max_entries})\\n")
        sys.exit(3)
    declared_total = 0
    for info in infos:
        raw = info.filename.replace("\\\\", "/")
        if "\\x00" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            sys.stderr.write(f"zip 含绝对路径或非法条目: {info.filename}\\n")
            sys.exit(4)
        if ".." in raw.split("/"):
            sys.stderr.write(f"zip 含路径穿越条目: {info.filename}\\n")
            sys.exit(5)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            sys.stderr.write(f"zip 含符号链接条目: {info.filename}\\n")
            sys.exit(6)
        if mode not in (0, 0o100000, 0o040000):
            sys.stderr.write(f"zip 含非常规文件条目: {info.filename}\\n")
            sys.exit(7)
        if info.file_size < 0:
            sys.stderr.write(f"zip 条目大小异常: {info.filename}\\n")
            sys.exit(8)
        declared_total += info.file_size
        if declared_total > max_bytes:
            sys.stderr.write(f"zip 解压总体积超过 {max_bytes} 字节上限\\n")
            sys.exit(9)

    os.makedirs(dest_dir, exist_ok=True)
    real_dest = os.path.realpath(dest_dir).rstrip("/")
    actual_total = 0
    for info in infos:
        raw = info.filename.replace("\\\\", "/")
        norm = posixpath.normpath(raw.lstrip("/"))
        if norm in ("", "."):
            continue
        out_path = os.path.join(real_dest or "/", *norm.split("/"))
        real_out = os.path.realpath(out_path)
        prefix = (real_dest + "/") if real_dest else "/"
        if real_out == (real_dest or "/") or not real_out.startswith(prefix):
            sys.stderr.write(f"zip 目标路径越出解包目录: {info.filename}\\n")
            sys.exit(10)
        if os.path.lexists(out_path) and os.path.islink(out_path):
            sys.stderr.write(f"zip 目标路径存在符号链接: {info.filename}\\n")
            sys.exit(11)
        if info.is_dir() or raw.endswith("/"):
            os.makedirs(out_path, exist_ok=True)
        else:
            parent = os.path.dirname(out_path)
            os.makedirs(parent, exist_ok=True)
            real_parent = os.path.realpath(parent)
            if real_parent != (real_dest or "/") and not real_parent.startswith(prefix):
                sys.stderr.write(f"zip 父目录越出解包目录: {info.filename}\\n")
                sys.exit(12)
            with zf.open(info, "r") as src, open(out_path, "wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    actual_total += len(chunk)
                    if actual_total > max_bytes:
                        sys.stderr.write(f"zip 实际解压字节数超过 {max_bytes} 字节上限\\n")
                        sys.exit(13)
                    dst.write(chunk)
""".strip()


def normalize_rel_path(rel: str) -> str:
    """将工作区相对路径规范化为不含前导 ./ 或尾随 / 的 POSIX 相对路径;根返回空串。"""
    raw = (rel or "").strip()
    if "\x00" in raw:
        raise UserError("路径包含非法字符")
    if raw.startswith("/"):
        raise UserError(f"路径必须是工作区内的相对路径:{rel}")
    raw = raw.rstrip("/")
    if not raw or raw == ".":
        return ""
    norm = posixpath.normpath(raw)
    if norm in ("", "."):
        return ""
    if norm == ".." or norm.startswith("../"):
        raise UserError(f"路径不能包含 ..:{rel}")
    return norm


def is_autolab_internal(norm_rel: str) -> bool:
    """精确匹配 .autolab 系统目录及其子路径,放行 .autolab_helper.py 等合法文件名。"""
    return norm_rel == ".autolab" or norm_rel.startswith(".autolab/")


def validate_safe_local_path(local_path: str, *, label: str = "本机目录") -> Path:
    """拒绝将控制端敏感配置/密钥目录(~/.autolab、~/.ssh、/etc 等)暴露给项目或参考库。"""
    raw = (local_path or "").strip()
    if not raw or "\x00" in raw:
        raise UserError(f"{label}路径不能为空或包含非法字符")
    resolved = Path(os.path.expanduser(raw)).resolve()
    home = Path(os.path.expanduser("~")).resolve()
    if resolved == Path("/") or resolved == home:
        raise UserError(f"{label}不能直接指向根目录或家目录根路径:{local_path}")
    sensitive_roots = (
        home / ".autolab",
        home / ".autolab-runner",
        home / ".ssh",
        home / ".gnupg",
        home / ".aws",
        Path("/etc").resolve(),
        Path("/root").resolve(),
    )
    for sens in sensitive_roots:
        if resolved == sens or sens in resolved.parents:
            raise UserError(f"{label}不允许指向受保护敏感目录({sens}):{local_path}")
    return resolved


def safe_extract_zip(
    host: Host,
    zip_source: bytes | Path,
    target_dir: str,
    *,
    staging_path: str,
    max_entries: int = ZIP_MAX_ENTRIES,
    max_bytes: int = ZIP_MAX_UNCOMPRESSED_BYTES,
) -> None:
    """在目标宿主机上安全解包 zip:拒绝 ../、绝对路径、符号链接条目、超多文件与 Zip 炸弹。"""
    local_direct = False
    if isinstance(zip_source, Path):
        if not zip_source.is_file() or zip_source.stat().st_size == 0:
            raise UserError("上传内容为空")
        if isinstance(host, LocalHost):
            local_direct = True
            archive_arg = str(zip_source)
        else:
            host.write(staging_path, zip_source.read_bytes())
            archive_arg = staging_path
    else:
        if not zip_source:
            raise UserError("上传内容为空")
        host.write(staging_path, bytes(zip_source))
        archive_arg = staging_path

    try:
        cmd = (
            f"{q(host.python)} -c {q(_SAFE_UNZIP_PY)} "
            f"{q(archive_arg)} {q(target_dir)} {int(max_entries)} {int(max_bytes)}"
        )
        result = host.run(cmd, timeout=600)
    finally:
        if not local_direct:
            host.run(f"rm -f {q(staging_path)}")
    if not result.ok:
        raise UserError(f"解包失败(仅支持安全标准 zip):{result.text.strip()[-300:]}")


class Workspace:
    def __init__(self, host: Host, workdir: str):
        if not workdir:
            raise UserError("项目还没有工作区,先完成立项")
        self.host = host
        self.workdir = workdir.rstrip("/")
        self._stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._backed_up: set[str] = set()

    # ---- 路径 ----

    def abs(self, rel: str) -> str:
        norm = normalize_rel_path(rel)
        if not norm:
            return self.workdir
        return f"{self.workdir}/{norm}"

    def ensure_within_workdir(self, abs_path: str) -> tuple[str, str]:
        """在宿主机解析 realpath,拒绝通过符号链接逃逸到 workdir 之外。"""
        if isinstance(self.host, LocalHost):
            real_root = os.path.realpath(self.workdir).rstrip("/")
            real_target = os.path.realpath(abs_path)
        else:
            res = self.host.run(
                f"{q(self.host.python)} -c "
                "'import os,sys; print(os.path.realpath(sys.argv[1])); print(os.path.realpath(sys.argv[2]))' "
                f"{q(self.workdir)} {q(abs_path)}"
            )
            if not res.ok:
                raise UserError(f"无法校验工作区真实路径:{abs_path}")
            lines = res.out.splitlines()
            if len(lines) < 2:
                raise UserError(f"无法校验工作区真实路径:{abs_path}")
            real_root = lines[0].strip().rstrip("/")
            real_target = lines[1].strip()
        root_cmp = real_root or "/"
        prefix = f"{real_root}/" if real_root else "/"
        if real_target != root_cmp and not real_target.startswith(prefix):
            raise UserError(f"路径软链越出工作区边界:{abs_path}")
        return root_cmp, real_target

    # ---- 基础操作 ----

    def sh(self, command: str, *, timeout: float = 60) -> RunResult:
        """在工作区目录下执行。"""
        return self.host.run(f"cd {q(self.workdir)} && {command}", timeout=timeout)

    def read_bytes(self, rel: str) -> bytes:
        path = self.abs(rel)
        self.ensure_within_workdir(path)
        try:
            return self.host.read(path)
        except OSError as exc:
            raise UserError(f"读取失败:{rel}") from exc

    def write_bytes(self, rel: str, data: bytes) -> None:
        path = self.abs(rel)
        self.ensure_within_workdir(path)
        self.host.write(path, data)

    def exists(self, rel: str) -> bool:
        return self.host.exists(self.abs(rel))

    def listdir(self, rel: str) -> list[str]:
        result = self.host.run(f"ls -1A {q(self.abs(rel))} 2>/dev/null")
        return [line for line in result.out.splitlines() if line.strip()] if result.ok else []

    # ---- 给 agent 的操作 ----

    def read_file(self, rel: str, offset: int = 1, limit: int = 400) -> str:
        path = self.abs(rel)
        self.ensure_within_workdir(path)
        start = max(1, int(offset))
        end = start + max(1, int(limit)) - 1
        result = self.host.run(f"sed -n '{start},{end}p' {q(path)}")
        if not result.ok:
            raise UserError(f"读取失败:{rel}(不存在或无权限)")
        numbered = "\n".join(f"{start + i}\t{line}"
                             for i, line in enumerate(result.out.splitlines()))
        return numbered[:30000] or "(空文件)"

    def list_files(self, rel: str = ".", max_entries: int = 200) -> str:
        path = self.abs(rel)
        self.ensure_within_workdir(path)
        result = self.host.run(
            f"find {q(path)} -maxdepth 2 -not -path '*/.git*'"
            " -not -path '*/.autolab*' -not -path '*/__pycache__*'"
            f" | head -{min(max(1, max_entries), 300)}", timeout=30)
        if not result.ok:
            raise UserError(f"列目录失败:{rel}")
        return result.out[:20000] or "(空)"

    def grep(self, pattern: str, rel: str = "", max_hits: int = 80) -> str:
        path = self.abs(rel)
        self.ensure_within_workdir(path)
        result = self.host.run(
            "grep -rn --include='*.py' --include='*.yaml' --include='*.yml'"
            " --include='*.json' --include='*.md' --include='*.sh' --include='*.cfg' --"
            f" {q(pattern)} {q(path)} 2>/dev/null | head -{min(max(1, max_hits), 200)}",
            timeout=60)
        if result.code not in (0, 1):
            raise UserError(f"grep 失败(pattern={pattern})")
        return result.out[:20000] or "(无匹配)"

    def write_file(self, rel: str, content: str, reason: str = "") -> str:
        """写源码:首次改动前备份原件;.py 语法检查不过则回滚本次写入。"""
        norm = normalize_rel_path(rel)
        if not norm:
            raise UserError("目标文件路径不能为空")
        if is_autolab_internal(norm):
            raise UserError(".autolab/ 由系统维护,不能直接写")
        path = f"{self.workdir}/{norm}"
        real_root, real_target = self.ensure_within_workdir(path)
        real_rel = posixpath.relpath(real_target, real_root)
        if is_autolab_internal(real_rel):
            raise UserError(".autolab/ 由系统维护,不能直接写")
        backup = f"{self.workdir}/{PATCHES_REL}/{self._stamp}/orig/{norm}"
        existed = self.host.exists(path)
        if norm not in self._backed_up:
            if existed:
                self.host.write(backup, self.host.read(path))
            self._backed_up.add(norm)
        self.host.write(path, content.encode("utf-8"))
        if norm.endswith(".py"):
            check = self.host.run(f"{q(self.host.python)} -m py_compile {q(path)} 2>&1")
            if not check.ok:
                if self.host.exists(backup):
                    self.host.write(path, self.host.read(backup))
                else:
                    self.host.run(f"rm -f {q(path)}")
                return f"语法检查失败,已回滚本次写入:\n{check.text.strip()[:600]}"
        return f"已写入 {norm}({'修改' if existed else '新建'}){':' + reason if reason else ''}"


@contextmanager
def open_workspace(repo, credentials: CredentialStore | None, project: dict):
    """with open_workspace(...) as ws:按项目所在服务器打开工作区,用完自动断开。"""
    host = open_host(repo, credentials, project["server_id"])
    try:
        yield Workspace(host, project.get("remote_workdir") or "")
    finally:
        host.close()
