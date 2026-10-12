"""项目工作区:Host 之上、以工作区根为基准的相对路径读写与执行。

agent 工具与各阶段都经由这里碰工作区文件;写源码时先备份原件到
.autolab/patches/<时间>/orig/,.py 写入后做语法检查、不过即回滚。
"""

from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager

from ..core.credentials import CredentialStore
from ..core.errors import UserError
from .host import Host, RunResult, open_host, q

PATCHES_REL = ".autolab/patches"


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
        rel = (rel or "").strip().strip("/")
        if not rel or rel == ".":
            return self.workdir
        if ".." in rel.split("/"):
            raise UserError(f"路径不能包含 ..:{rel}")
        return f"{self.workdir}/{rel}"

    # ---- 基础操作 ----

    def sh(self, command: str, *, timeout: float = 60) -> RunResult:
        """在工作区目录下执行。"""
        return self.host.run(f"cd {q(self.workdir)} && {command}", timeout=timeout)

    def read_bytes(self, rel: str) -> bytes:
        try:
            return self.host.read(self.abs(rel))
        except OSError as exc:
            raise UserError(f"读取失败:{rel}") from exc

    def write_bytes(self, rel: str, data: bytes) -> None:
        self.host.write(self.abs(rel), data)

    def exists(self, rel: str) -> bool:
        return self.host.exists(self.abs(rel))

    def listdir(self, rel: str) -> list[str]:
        result = self.host.run(f"ls -1A {q(self.abs(rel))} 2>/dev/null")
        return [line for line in result.out.splitlines() if line.strip()] if result.ok else []

    # ---- 给 agent 的操作 ----

    def read_file(self, rel: str, offset: int = 1, limit: int = 400) -> str:
        start = max(1, int(offset))
        end = start + max(1, int(limit)) - 1
        result = self.host.run(f"sed -n '{start},{end}p' {q(self.abs(rel))}")
        if not result.ok:
            raise UserError(f"读取失败:{rel}(不存在或无权限)")
        numbered = "\n".join(f"{start + i}\t{line}"
                             for i, line in enumerate(result.out.splitlines()))
        return numbered[:30000] or "(空文件)"

    def list_files(self, rel: str = ".", max_entries: int = 200) -> str:
        result = self.host.run(
            f"find {q(self.abs(rel))} -maxdepth 2 -not -path '*/.git*'"
            " -not -path '*/.autolab*' -not -path '*/__pycache__*'"
            f" | head -{min(max(1, max_entries), 300)}", timeout=30)
        if not result.ok:
            raise UserError(f"列目录失败:{rel}")
        return result.out[:20000] or "(空)"

    def grep(self, pattern: str, rel: str = "", max_hits: int = 80) -> str:
        result = self.host.run(
            "grep -rn --include='*.py' --include='*.yaml' --include='*.yml'"
            " --include='*.json' --include='*.md' --include='*.sh' --include='*.cfg' --"
            f" {q(pattern)} {q(self.abs(rel))} 2>/dev/null | head -{min(max(1, max_hits), 200)}",
            timeout=60)
        if result.code not in (0, 1):
            raise UserError(f"grep 失败(pattern={pattern})")
        return result.out[:20000] or "(无匹配)"

    def write_file(self, rel: str, content: str, reason: str = "") -> str:
        """写源码:首次改动前备份原件;.py 语法检查不过则回滚本次写入。"""
        path = self.abs(rel)
        if rel.strip("/").startswith(".autolab"):
            raise UserError(".autolab/ 由系统维护,不能直接写")
        backup = f"{self.workdir}/{PATCHES_REL}/{self._stamp}/orig/{rel.strip('/')}"
        existed = self.host.exists(path)
        if rel not in self._backed_up:
            if existed:
                self.host.write(backup, self.host.read(path))
            self._backed_up.add(rel)
        self.host.write(path, content.encode("utf-8"))
        if rel.endswith(".py"):
            check = self.host.run(f"{q(self.host.python)} -m py_compile {q(path)} 2>&1")
            if not check.ok:
                if self.host.exists(backup):
                    self.host.write(path, self.host.read(backup))
                else:
                    self.host.run(f"rm -f {q(path)}")
                return f"语法检查失败,已回滚本次写入:\n{check.text.strip()[:600]}"
        return f"已写入 {rel}({'修改' if existed else '新建'}){':' + reason if reason else ''}"


@contextmanager
def open_workspace(repo, credentials: CredentialStore | None, project: dict):
    """with open_workspace(...) as ws:按项目所在服务器打开工作区,用完自动断开。"""
    host = open_host(repo, credentials, project["server_id"])
    try:
        yield Workspace(host, project.get("remote_workdir") or "")
    finally:
        host.close()
