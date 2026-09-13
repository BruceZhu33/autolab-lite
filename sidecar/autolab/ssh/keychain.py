"""凭据存储适配器（D30/D48：凭据本体不入库，库内只存引用）。

- macOS：Security 钥匙串（security CLI，generic-password）
- 其他平台 / 强制覆盖：文件存储（0600，数据目录内）——deviation-log #3（D48 适配器 M1 覆盖面）
存储内容为 JSON：{"kind":"password"|"key", "secret":…, "agent_token":…, "hmac_key":…}
引用格式：keychain://AutoLab/server/{id} 或 file://credentials/{id}.json
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

SERVICE = "AutoLab"


class CredentialStore:
    def put(self, server_id: str, data: dict) -> str: ...
    def get(self, ref: str) -> dict: ...
    def delete(self, ref: str) -> None: ...


class MacKeychainStore(CredentialStore):
    def _account(self, server_id: str) -> str:
        return f"server:{server_id}"

    def put(self, server_id: str, data: dict) -> str:
        account = self._account(server_id)
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account,
             "-w", json.dumps(data)],
            check=True, capture_output=True)
        return f"keychain://{SERVICE}/{account}"

    def get(self, ref: str) -> dict:
        account = ref.rsplit("/", 1)[-1]
        r = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", SERVICE, "-a", account],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise KeyError(f"keychain entry not found: {ref}")
        return json.loads(r.stdout.strip())

    def delete(self, ref: str) -> None:
        account = ref.rsplit("/", 1)[-1]
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                       capture_output=True)


class FileStore(CredentialStore):
    """非 macOS / 测试兜底：数据目录下 0600 JSON 文件。"""

    def __init__(self, data_dir: Path):
        self.dir = data_dir / "credentials"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, server_id: str) -> Path:
        return self.dir / f"{server_id}.json"

    def put(self, server_id: str, data: dict) -> str:
        p = self._path(server_id)
        p.write_text(json.dumps(data), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
        return f"file://credentials/{server_id}.json"

    def get(self, ref: str) -> dict:
        name = ref.rsplit("/", 1)[-1].removesuffix(".json")
        p = self.dir / f"{name}.json"
        if not p.exists():
            raise KeyError(f"credential file not found: {p}")
        return json.loads(p.read_text(encoding="utf-8"))

    def delete(self, ref: str) -> None:
        name = ref.rsplit("/", 1)[-1].removesuffix(".json")
        self._path(name).unlink(missing_ok=True)


def make_store(data_dir: Path) -> CredentialStore:
    if platform.system() == "Darwin" and os.environ.get("AUTOLAB_CRED_STORE", "keychain") == "keychain":
        return MacKeychainStore()
    return FileStore(data_dir)
