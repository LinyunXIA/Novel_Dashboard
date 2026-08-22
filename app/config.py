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


@dataclass(frozen=True)
class EnvConfig:
    env: str
    dsn: str
    source_dir: Path  # Novel 设计源库（只读，绝不回写）
    input_dir: Path   # 数据调整员放置待导入文件目录（各环境独立）
    overlay_dir: Path # 用户数据 md 覆盖层（编年史）

    # 本地 LLM（搜索，DESIGN §18.5；三环境共用同一 omlx-server）
    llm_url: str = "http://127.0.0.1:8000"
    llm_model: str = "Qwen3.8-27B-MLX-4bit"
    embed_url: str = "http://127.0.0.1:8000"
    embed_model: str = "Qwen3-Embedding-8B-4bit-DWQ"


def _dsn(db_name: str) -> str:
    host = os.environ.get("PGHOST", "127.0.0.1")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("POSTGRES_PASSKEY")
    auth = f"{user}:{password}@" if password else f"{user}@"
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

    return EnvConfig(
        env=env,
        dsn=_dsn(f"novel_{env}"),
        source_dir=source_dir,
        input_dir=PROJECT_ROOT / inputs[env],
        overlay_dir=PROJECT_ROOT / overlays[env],
    )


CONFIG = get_config()  # 模块级：随 APP_ENV 解析，导入即得当前环境配置