"""issue #107 回归：CLI --env 缺省必须尊重 APP_ENV，不再硬编码 dev。

- 全部子命令的 --env Option 默认值应为 None（经 get_config 回退链解析）；
- 未传 --env 且未设 APP_ENV 时才落 dev；
- 回显用 _resolved_env，防打印 None。
"""
from __future__ import annotations

import inspect

import typer

import app.ingest.main as cli
from app.config import get_config


def _commands() -> list:
    return [cmd for cmd in cli.app.registered_commands]


def test_all_cli_env_options_default_to_none():
    for cmd in _commands():
        fn = cmd.callback
        sig = inspect.signature(fn)
        if "env" in sig.parameters:
            default = sig.parameters["env"].default
            assert isinstance(default, typer.models.OptionInfo), cmd.name or fn.__name__
            assert default.default is None, (
                f"{cmd.name or fn.__name__} 的 --env 仍硬编码默认值 {default.default!r}"
            )


def test_get_config_none_falls_back_to_app_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    assert get_config(None).env == "test"
    assert get_config(None).dsn.endswith("novel_test")


def test_resolved_env_never_prints_none(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    assert cli._resolved_env(None) == "prod"
    assert cli._resolved_env("dev") == "dev"
