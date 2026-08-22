"""Ingest CLI（DESIGN §3/§6）。

用法：
    python -m app.ingest.main --env dev            # 默认
    python -m app.ingest.main --env prod --full    # 全量重建
    python -m app.ingest.main ping                 # DB 连通自检

F-P0-01：骨架（config/db/CLI）已就绪；detect/parsers/normalize/conflict 后续里程碑填充。
"""
from __future__ import annotations

import typer

from app.config import get_config
from app.db import check_connection

app = typer.Typer(help="Novel Dashboard ingest")


@app.command()
def ping(env: str = typer.Option("dev", "--env")):
    """数据库连通自检。"""
    cfg = get_config(env)
    ok = check_connection()  # db.py 用模块级 CONFIG；此处按 env 打印，连接仍走默认
    typer.echo(f"[{cfg.env}] dsn={cfg.dsn}")
    typer.secho("连接成功" if ok else "连接失败", fg=typer.colors.GREEN if ok else typer.colors.RED)


@app.command()
def run(
    env: str = typer.Option("dev", "--env"),
    full: bool = typer.Option(False, "--full", help="全量重建"),
):
    """执行导入（骨架：仅打印计划；后续通过 detect→parse→normalize→import）。"""
    cfg = get_config(env)
    typer.echo(f"[{cfg.env}] 源目录={cfg.source_dir}")
    typer.echo(f"[{cfg.env}] 输入目录={cfg.input_dir}")
    typer.echo(f"[{cfg.env}] 覆盖层={cfg.overlay_dir}")
    typer.echo("计划：detect → parse → normalize → conflict → import（骨架占位）")


if __name__ == "__main__":
    app()