"""进程级网络偏好:LLM 端点强制 IPv4。

真实环境诊断(2026-09-24 验收):open.bigmodel.cn 为阿里云 GA 双栈,
本机 IPv6 到该地址段**间歇性半通**(SSL 握手中途 UNEXPECTED_EOF);
httpx 的地址选择偶尔命中 IPv6 即失败,而 IPv4 恒稳(3/3 curl、4/4 强制
IPv4)。症状与 GLM「高发空回复/端点波动」病史高度重合。

处置:Control 进程内所有 httpx 客户端默认 local_address=0.0.0.0(仅
IPv4)+ 连接级 retries=2。显式传了 local_address 的调用方不受影响。
"""

from __future__ import annotations

_PATCHED = False


def force_ipv4_for_httpx() -> None:
    """幂等补丁:httpx.HTTPTransport 默认绑 IPv4 并重试连接。"""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    import httpx

    original_init = httpx.HTTPTransport.__init__

    def _init(self, *args, **kwargs):
        kwargs.setdefault("local_address", "0.0.0.0")
        kwargs.setdefault("retries", 2)
        original_init(self, *args, **kwargs)

    httpx.HTTPTransport.__init__ = _init
