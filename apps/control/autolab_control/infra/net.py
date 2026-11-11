"""网络传输偏好:仅对需要 IPv4 的客户端显式构造 HTTPTransport。

真实环境诊断(2026-09-24 验收):open.bigmodel.cn 为阿里云 GA 双栈,
本机 IPv6 到该地址段**间歇性半通**(SSL 握手中途 UNEXPECTED_EOF);
httpx 的地址选择偶尔命中 IPv6 即失败,而 IPv4 恒稳。

处置:通过 ipv4_transport() 仅为指定 LLM 客户端挂载 local_address="0.0.0.0" +
retries,不再全局 monkeypatch httpx.HTTPTransport.__init__,避免污染
RunnerTransport 与本地/IPv6 连接。
"""

from __future__ import annotations

import httpx


def ipv4_transport(retries: int = 2) -> httpx.HTTPTransport:
    """返回绑定 IPv4 (0.0.0.0) 并带连接重试的 httpx.HTTPTransport。"""
    return httpx.HTTPTransport(local_address="0.0.0.0", retries=retries)


def force_ipv4_for_httpx() -> None:
    """兼容保留的安全空操作:不再全局修改 httpx.HTTPTransport.__init__。"""
    return None
