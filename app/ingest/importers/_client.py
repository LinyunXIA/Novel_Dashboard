"""外部系统 API① 连接与凭据加载（DESIGN §13.3 · F-P1-05）。

凭据铁律（DESIGN §13.3）：外部 API 凭据放本地 `secrets.local.yaml`（.gitignore 排除，
不入 git、不入 DB）；可被环境变量覆盖；在本模块读取，不落入日志/notification。

URL 优先级：环境变量 EXTERNAL_API_BASE_URL > secrets.local.yaml external_api.base_url
            > config.CONFIG.external_api_url（per-env 默认 7273/7274）。
用户名/密码优先级：环境变量 EXTERNAL_API_USER / EXTERNAL_API_PASSWORD
            > secrets.local.yaml external_api.username / .password。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from app.config import PROJECT_ROOT, CONFIG

_SECRETS_PATH = PROJECT_ROOT / "secrets.local.yaml"


def _load_secrets() -> dict:
    """读本地 secrets.local.yaml；文件缺失/非法 → 空 dict（不抛）。
    yaml.safe_load，字段 keys 为 external_api: {base_url, username, password}。"""
    if not _SECRETS_PATH.exists():
        return {}
    try:
        with _SECRETS_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (yaml.YAMLError, OSError):
        return {}
    return data.get("external_api") or {}


def load(base_url: str | None = None) -> tuple[str, str, str]:
    """解析 (外部 base_url, username, password)。

    base_url 形如 `http://127.0.0.1:7273`（外部系统 `/api/v1` 根；具体端点路径后续拼接）。
    """
    sec = _load_secrets()
    url = (
        base_url
        or os.environ.get("EXTERNAL_API_BASE_URL")
        or sec.get("base_url")
        or CONFIG.external_api_url
    )
    username = (
        os.environ.get("EXTERNAL_API_USER")
        or sec.get("username")
        or ""
    )
    password = (
        os.environ.get("EXTERNAL_API_PASSWORD")
        or sec.get("password")
        or ""
    )
    return url, username, password