from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from app.config import get_config
from app.db import Base

# ORM 模型导入以注册到 metadata（all 表由此注册，供 autogenerate 检测）。
from app import model as app_model  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    """按应用配置取 DSN。

    环境取 APP_ENV（与 config.get_config 同口径）。亦支持 alembic 的
    `-x env=test` 命令行参数（存于 config.cmd_opts.x）覆盖。
    """
    import os as _os
    env = _os.environ.get("APP_ENV", "dev")
    cmd = getattr(config, "cmd_opts", None)
    if cmd is not None and getattr(cmd, "x", None):
        for kv in cmd.x:
            if kv.startswith("env="):
                env = kv.split("=", 1)[1]
    return get_config(env).dsn


def run_migrations_offline() -> None:
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    conf = config.get_section(config.config_ini_section, {})
    conf["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(
        conf,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()