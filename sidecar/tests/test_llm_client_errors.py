"""LLM 客户端上游错误体解析（429 ≠ 限流：bigmodel 1113 余额不足也走 429）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sidecar"))

from autolab.llm.client import upstream_error_detail  # noqa: E402


def test_openai_style_code_and_message():
    body = '{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}'
    assert upstream_error_detail(body) == "[1113] 余额不足或无可用资源包,请充值。"


def test_message_only():
    assert upstream_error_detail('{"error":{"message":"boom"}}') == "boom"


def test_plain_text_falls_back():
    assert upstream_error_detail("Bad Gateway") == "Bad Gateway"
    assert upstream_error_detail("") == "(无响应体)"
