"""执行清单规范化 + HMAC 签名/校验（设计 §5 gpu-agent 契约）。

- canonicalization 带版本号（spec_version），升级期序列化差异由版本隔离（防误拒）
- 密钥 = sidecar↔agent 共享密钥（per-server，存钥匙串；M1 默认与 agent token 同源）
- M1 手动触发场景默认不强制校验（明文跑通再收紧），但生成与校验函数已就位并被
  单元测试覆盖；agent 端 --require-hmac 打开强制校验
"""
from __future__ import annotations

import hashlib
import hmac
import json

SPEC_VERSION = "m1/1"


def canonicalize(manifest: dict) -> bytes:
    envelope = {"spec_version": SPEC_VERSION, **manifest}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_hash(manifest: dict) -> str:
    """attempts.exec_manifest_hash 的口径：规范化字节的 SHA256。"""
    return hashlib.sha256(canonicalize(manifest)).hexdigest()


def sign(manifest: dict, key: str) -> str:
    return hmac.new(key.encode("utf-8"), canonicalize(manifest), hashlib.sha256).hexdigest()


def verify(manifest: dict, signature: str, key: str) -> bool:
    return hmac.compare_digest(sign(manifest, key), signature)
