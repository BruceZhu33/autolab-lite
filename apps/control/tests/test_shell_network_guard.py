"""shell 工具的网络不可达升级:输出累计撞到阈值,强制 Pause 进人工
(真机教训:HF 组件即使有缓存也默认联网校验,网络部分不通时训练进程
挂在重试里,agent 跟着空转烧步数——应该尽早把 owner 拉进来)。"""

from __future__ import annotations

from types import SimpleNamespace

from autolab_control.agent import tools
from autolab_control.agent.runtime import Pause


class FakeWorkspace:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)

    def sh(self, command: str, timeout: int = 120):
        text = self.outputs.pop(0) if self.outputs else ""
        return SimpleNamespace(text=text, code=0)


def make_env(run_id: str = "arun-test", db_path: str = "/tmp/x/control.db",
             cred_path: str = "/tmp/x/credentials.json"):
    workspace = FakeWorkspace([])
    env = SimpleNamespace(run={"id": run_id}, workspace=workspace,
                          repo=SimpleNamespace(db=SimpleNamespace(path=db_path)),
                          credentials=SimpleNamespace(path=cred_path))
    return env, workspace


def call_shell(env, command="python train.py"):
    tool = tools.shell_tool()
    return tool.handler(env, {"command": command})


def test_three_network_failures_pause_to_human():
    env, ws = make_env("arun-net-1")
    ws.outputs = [
        "ok start\nRetrying in 8s [Retry 1/5] Network is unreachable",
        "ok start\nMax retries exceeded with url: https://huggingface.co/x",
        "ok start\nHTTPSConnection... Failed to establish a new connection:"
        " [Errno 101] Network is unreachable",
    ]
    first = call_shell(env)
    assert isinstance(first, str) and "exit=0" in first
    second = call_shell(env)
    assert isinstance(second, str)
    third = call_shell(env)
    assert isinstance(third, Pause) and third.kind == "human"
    assert "3 次" in third.payload["question"]
    assert "HF_HUB_OFFLINE=1" in third.payload["question"]
    # 升级后计数清零:再来两轮才可能再次触发
    ws.outputs = ["clean run no errors", "clean again"]
    assert isinstance(call_shell(env), str)
    assert isinstance(call_shell(env), str)


def test_clean_output_never_pauses():
    env, ws = make_env("arun-net-2")
    ws.outputs = ["epoch 1 done dice=0.5", "epoch 2 done dice=0.6"] * 4
    for _ in range(8):
        result = call_shell(env)
        assert isinstance(result, str)


# ------------------------------------------------------------------ 治理

def test_control_db_path_is_denied():
    """真机前科:agent 用 shell 直改控制端 DB 的 projects 表自救。"""
    import pytest
    from autolab_control.agent.runtime import ToolError

    env, _ = make_env(db_path="/tmp/autolab-web-X/control.db",
                      cred_path="/tmp/autolab-web-X/credentials.json")
    with pytest.raises(ToolError, match="产品边界"):
        call_shell(env, command='python -c "import sqlite3;'
                                ' c=sqlite3.connect(\'/tmp/autolab-web-X/control.db\')"')
    with pytest.raises(ToolError, match="产品边界"):
        call_shell(env, command="cat /tmp/autolab-web-X/credentials.json")


def test_platform_process_kill_is_denied_but_own_training_allowed():
    import pytest
    from autolab_control.agent.runtime import ToolError

    env, ws = make_env()
    with pytest.raises(ToolError, match="平台自身进程"):
        call_shell(env, command='pkill -f "autolab_control serve"')
    with pytest.raises(ToolError, match="平台自身进程"):
        call_shell(env, command="killall next-server")
    ws.outputs = ["killed"]
    result = call_shell(env, command="pkill -f train.py || true")
    assert isinstance(result, str) and "exit=0" in result
