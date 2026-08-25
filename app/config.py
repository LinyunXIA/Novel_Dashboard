"""Novel Dashboard 配置（DESIGN §4）。

按 APP_ENV ∈ {dev, test, prod} 返回 DSN 与数据目录；三环境相互独立。
本地推理（搜索 LLM）三环境共用同一 omlx-server，见 DESIGN §18.5。
连接口令优先读环境变量 POSTGRES_PASSKEY；未设则按本机 trust 直连。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_str(key: str, default: str) -> str:
    """LLM 配置环境变量覆盖（DESIGN §18.5）；未设时用缺省值。"""
    return os.environ.get(key, default)


def _env_int(key: str, default: int | None) -> int | None:
    """文本覆盖键解析为 int；非法值静默回退缺省。"""
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


@dataclass(frozen=True)
class EnvConfig:
    env: str
    dsn: str
    source_dir: Path  # Novel 设计源库（只读，绝不回写）
    input_dir: Path   # 数据调整员放置待导入文件目录（各环境独立）
    overlay_dir: Path # 用户数据 md 覆盖层（编年史）

    # 本地 LLM（搜索，DESIGN §18.5；三环境共用同一 omlx-server）
    # 六项均支持环境变量覆盖（LLM_URL/LLM_MODEL/EMBED_URL/EMBED_MODEL/
    # LLM_MODEL_CONTEXT/EMBED_DIM），未设用缺省；键名与 §18.5 对齐。
    llm_url: str = field(default_factory=lambda: _env_str("LLM_URL", "http://127.0.0.1:8000"))
    llm_model: str = field(default_factory=lambda: _env_str("LLM_MODEL", "Qwen3.8-27B-MLX-4bit"))
    embed_url: str = field(default_factory=lambda: _env_str("EMBED_URL", "http://127.0.0.1:8000"))
    embed_model: str = field(default_factory=lambda: _env_str("EMBED_MODEL", "Qwen3-Embedding-8B-4bit-DWQ"))
    llm_model_context: int | None = field(default_factory=lambda: _env_int("LLM_MODEL_CONTEXT", None))
    # 向量维（search_index.embedding 固定列宽；须与 EMBED_MODEL 输出一致——实测 Qwen3-Embedding-8B=4096）。
    embed_dim: int = field(default_factory=lambda: _env_int("EMBED_DIM", 4096) or 4096)

    # 外部系统 API①（公司基础信息，DESIGN §13/§13.3 · F-P1-05）：URL 指向其 `/api/v1` 根。
    # 优先级：环境变量 EXTERNAL_API_BASE_URL > 该 per-env 默认。凭据（用户名/密码）不入 config，
    # 走 secrets.local.yaml + 环境变量（见 app/ingest/importers/_client.py）。
    external_api_url: str = field(
        default_factory=lambda: _env_str("EXTERNAL_API_BASE_URL", "http://127.0.0.1:7273"))


def _dsn(db_name: str) -> str:
    host = os.environ.get("PGHOST", "127.0.0.1")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("POSTGRES_PASSKEY")
    # issue #132：密码 URL 编码——含 @ : / # 等特殊字符时不再破坏 DSN 解析
    from urllib.parse import quote_plus
    auth = (f"{quote_plus(user)}:{quote_plus(password)}@" if password
            else f"{quote_plus(user)}@")
    return f"postgresql+psycopg://{auth}{host}:{port}/{db_name}"


def get_config(env: str | None = None) -> EnvConfig:
    """读取配置。env 缺省取 APP_ENV，再缺省 dev。"""
    env = env or os.environ.get("APP_ENV", "dev")
    env = env.lower()
    if env not in ("dev", "test", "prod"):
        raise ValueError(f"APP_ENV 必须为 dev/test/prod，得到 {env!r}")

    source_dir = PROJECT_ROOT / "Design_Folder"
    inputs = {
        "dev":  "data/input-dev",
        "test": "data/input-test",
        "prod": "data/input",
    }
    overlays = {
        "dev":  "data/overlay-dev",
        "test": "data/overlay-test",
        "prod": "data/overlay",
    }
    # 外部系统 API① 公司基础信息（DESIGN §13）；dev/test 连 7273，prod 连 7274。
    # 环境变量 EXTERNAL_API_BASE_URL 可覆盖（default_factory 已读；这里仅在未设时给 per-env 默认）。
    external_urls = {
        "dev":  "http://127.0.0.1:7273",
        "test": "http://127.0.0.1:7273",
        "prod": "http://127.0.0.1:7274",
    }
    external_api_url = os.environ.get("EXTERNAL_API_BASE_URL") or external_urls[env]

    return EnvConfig(
        env=env,
        dsn=_dsn(f"novel_{env}"),
        source_dir=source_dir,
        input_dir=PROJECT_ROOT / inputs[env],
        overlay_dir=PROJECT_ROOT / overlays[env],
        external_api_url=external_api_url,
    )


CONFIG = get_config()  # 模块级：随 APP_ENV 解析，导入即得当前环境配置