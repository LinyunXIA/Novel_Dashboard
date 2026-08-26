"""omlx / LLM 客户端（F-P1-08 · DESIGN §18.5）。

连本地 omlx-server（http://127.0.0.1:8000，无鉴权，三环境共用）：
- embed(texts) -> list[list[float]]   POST /v1/embeddings {input, model:EMBED_MODEL}
- chat(system, user) -> str          POST /v1/chat/completions {model:LLM_MODEL, messages}

统一抛 LlmUnavailable（连接失败/非2xx/结构错），上层据此优雅降级（搜索返回 503/提示）。
客户端可注入 httpx.Client(transport=MockTransport) 供测试免网。
"""
from __future__ import annotations

import httpx

from app.config import CONFIG


class LlmUnavailable(Exception):
    """omlx 不可用或返回异常；msg 供 503 detail。"""


def _client() -> httpx.Client:
    return httpx.Client(timeout=60)


def embed(texts: list[str], *, client: httpx.Client | None = None) -> list[list[float]]:
    """文本列表 → 向量列表。注入 client 可离线测试；返回维 == EMBED_DIM。"""
    base, model = CONFIG.embed_url, CONFIG.embed_model
    owned = client is None
    client = client or httpx.Client(timeout=60)
    try:
        r = client.post(f"{base}/v1/embeddings",
                        json={"input": texts, "model": model})
        if r.status_code != 200:
            raise LlmUnavailable(f"embedding 失败（HTTP {r.status_code}）")
        data = r.json()
        out = [item["embedding"] for item in data.get("data", [])]
        if not out:
            # 四轮审计 #169：空 data 原样返回会让 search.py `[0]` 抛 IndexError 裸 500
            raise LlmUnavailable("embedding 返回空 data（模型未加载？）")
        dim = CONFIG.embed_dim
        if dim and any(len(v) != dim for v in out):
            raise LlmUnavailable(f"embedding 维度 {len(out[0])} ≠ EMBED_DIM {dim}（请设 EMBED_DIM）")
        return out
    except httpx.RequestError as e:
        raise LlmUnavailable(f"无法连接本地 omlx（{base}）") from e
    finally:
        if owned:
            client.close()


def chat(system: str, user: str, *, client: httpx.Client | None = None) -> str:
    """装配 LLM 调用，返回最终文本内容。"""
    base, model = CONFIG.llm_url, CONFIG.llm_model
    owned = client is None
    client = client or httpx.Client(timeout=90)
    try:
        r = client.post(f"{base}/v1/chat/completions",
                        json={"model": model,
                              "messages": [{"role": "system", "content": system},
                                           {"role": "user", "content": user}]})
        if r.status_code != 200:
            raise LlmUnavailable(f"chat 失败（HTTP {r.status_code}）")
        data = r.json()
        # 四轮审计 #169：响应结构错（缺 choices/message）按 docstring 统一降级，不裸 500
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LlmUnavailable(f"chat 响应结构异常：{e}") from e
    except httpx.RequestError as e:
        raise LlmUnavailable(f"无法连接本地 omlx（{base}）") from e
    finally:
        if owned:
            client.close()