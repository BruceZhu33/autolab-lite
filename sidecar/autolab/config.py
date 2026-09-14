"""运行配置：数据目录（D48 平台适配器 M1 版）、端口、capability token。

sidecar 仅绑 127.0.0.1 随机端口（B2）；运行期事实（port/token/pid）落 runtime.json，
供 Tauri 壳与 UI 代理发现。AUTOLAB_HOME 环境变量优先（开发/测试用）。
"""
from __future__ import annotations

import os
import platform
from pathlib import Path


def default_data_dir() -> Path:
    override = os.environ.get("AUTOLAB_HOME")
    if override:
        return Path(override).expanduser().resolve()
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "AutoLab"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "AutoLab"
    return home / ".local" / "share" / "autolab"


class Config:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = (data_dir or default_data_dir()).resolve()
        self.db_path = self.data_dir / "autolab.db"
        self.migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
        self.assets_dir = Path(__file__).resolve().parent.parent / "assets"
        # gpu-agent 源码（自动部署用；dev = 仓库同级目录，打包时随应用分发）
        _repo_root = Path(__file__).resolve().parents[2]
        self.agent_src_dir = Path(os.environ.get(
            "AUTOLAB_AGENT_SRC") or (_repo_root / "gpu-agent"))
        self.projects_dir = self.data_dir / "projects"   # BASE 快照 / 项目产物（产品管理的项目库）
        self.artifacts_dir = self.data_dir / "artifacts"
        self.token_path = self.data_dir / "capability_token"
        self.runtime_path = self.data_dir / "runtime.json"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.projects_dir, self.artifacts_dir):
            p.mkdir(parents=True, exist_ok=True)
