"""WS 契约协议模型校验（复审 A4/S1：文档级协议要素 + 与 OpenAPI 一致）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import CONTRACTS_DIR  # noqa: E402

WS = (CONTRACTS_DIR / "ws-events.md").read_text(encoding="utf-8")


def test_subscription_id_persists_across_reconnect():
    """A4：重连 resume 结构必须含 subscription_id（r3：文档 resume 定义同步）。"""
    assert WS.count("subscription_id") >= 3, "subscription_id must appear in envelope/reconnect/subscribe"
    resume_zone = WS[WS.find("resume"):WS.find("resume") + 600]
    assert "subscription_id" in resume_zone, "resume frame must carry subscription_id (A4/r3)"
    assert "TTL" in WS or "ttl" in WS, "subscription reconnect TTL must be defined (A4/r3)"


def test_heartbeat_watermark_semantics():
    """A4：心跳携带 last_emitted_sub_seq（已发 watermark）；客户端落后即 resync——无丢事件窗口。"""
    assert "last_emitted_sub_seq" in WS
    assert "resync" in WS


def test_snapshot_watermark_on_authoritative_commit_order():
    """A4：快照 watermark 在权威提交序上取得；客户端按 state_revision 丢弃回退事件。"""
    assert "snapshot_at_sub_seq" in WS
    assert "state_revision" in WS
    assert "提交序" in WS or "权威" in WS


def test_test_scope_never_streamed():
    """U3 硬约束：test 数值永不经 WS 下发。"""
    assert "scope" in WS and "只允许 `val`" in WS


def test_epoch_bump_forces_snapshot():
    assert "epoch" in WS and "全量快照" in WS
