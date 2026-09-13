"""运行期共享状态（挂在 app.state 上）。"""
from __future__ import annotations

from .config import Config
from .db import Database
from .events import Hub
from .ssh.keychain import CredentialStore
from .ssh.transport import Transport, make_transport


class AppState:
    def __init__(self, cfg: Config, db: Database, hub: Hub, cred_store: CredentialStore):
        self.cfg = cfg
        self.db = db
        self.hub = hub
        self.creds = cred_store
        self.transports: dict[str, Transport] = {}
        self.loop = None  # 主事件循环（worker 线程回调用）

    def transport_for(self, server_row) -> Transport:
        sid = server_row["id"]
        if sid not in self.transports:
            cred = self.creds.get(server_row["credential_ref"])
            self.transports[sid] = make_transport(server_row, cred)
        return self.transports[sid]

    def agent_credential(self, server_row) -> dict:
        """agent 通信凭据：{agent_token, hmac_key}（与 SSH 凭据同存一条钥匙串记录）。"""
        data = self.creds.get(server_row["credential_ref"])
        return {"agent_token": data.get("agent_token", ""),
                "hmac_key": data.get("hmac_key") or data.get("agent_token", "")}

    def budget_payload(self, conn, pid: str) -> dict:
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
        if acc is None:
            return {}
        return {"project_id": pid, "limit_s": acc["limit_s"], "consumed_s": acc["consumed_s"],
                "reserved_s": acc["reserved_s"], "overage_s": acc["overage_s"],
                "status": acc["status"], "revision": acc["revision"]}
