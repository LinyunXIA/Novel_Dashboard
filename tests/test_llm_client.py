"""omlx/LLM 客户端单测（F-P1-08 §18.5）：embed/chat 用 MockTransport 免网；维校验。"""
from __future__ import annotations

import httpx
import pytest

from app.core import llm
from app.config import CONFIG


def _client(json_body):
    return lambda request: httpx.Response(200, json=json_body)


def test_embed_returns_vectors():
    c = httpx.Client(transport=httpx.MockTransport(
        _client({"data": [{"embedding": [0.1] * CONFIG.embed_dim},
                          {"embedding": [0.2] * CONFIG.embed_dim}]})))
    out = llm.embed(["a", "b"], client=c)
    assert len(out) == 2 and len(out[0]) == CONFIG.embed_dim


def test_embed_dim_mismatch_raises():
    # MockTransport 返回 2 维 → 与 EMBED_DIM 不符 → LlmUnavailable
    c = httpx.Client(transport=httpx.MockTransport(
        _client({"data": [{"embedding": [0.5, 0.5]}]})))
    with pytest.raises(llm.LlmUnavailable):
        llm.embed(["x"], client=c)


def test_embed_connection_error_raises():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    with pytest.raises(llm.LlmUnavailable):
        llm.embed(["x"], client=c)


def test_chat_returns_content():
    c = httpx.Client(transport=httpx.MockTransport(
        _client({"choices": [{"message": {"content": "1947年"}}]})))
    assert llm.chat("sys", "user问", client=c) == "1947年"


def test_chat_error_raises():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})))
    with pytest.raises(llm.LlmUnavailable):
        llm.chat("sys", "u", client=c)