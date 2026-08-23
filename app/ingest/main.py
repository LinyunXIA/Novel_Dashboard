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
from app.db import SessionLocal, check_connection
from app.ingest.parse import run_ingest
from app.ingest import writer

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
    """扫描输入目录 → detect → parse → 输出报告（F-P0-02；落库在后续里程碑）。"""
    cfg = get_config(env)
    typer.echo(f"[{cfg.env}] 输入目录={cfg.input_dir}")
    report = run_ingest(cfg.input_dir)
    typer.echo(f"识别 {len(report.ok)} 个可解析文件 · {len(report.failed)} 需人工 · {len(report.skipped)} Phase2 跳过")
    for r in report.ok:
        typer.echo(f"  ✅ {r.category:12s} {r.file} ({len(r.records)} 条)")
    for r in report.failed:
        typer.echo(f"  ❌ {r.category:12s} {r.file} — {r.error}")


@app.command()
def ingest(
    env: str = typer.Option("dev", "--env"),
):
    """F-P0-04 落库：character→entity；initial_asset→entity/account/initial_asset/现金余额。

    从 Design_Folder（source_dir）读取基础数据；commit 前整批事务，失败回滚。
    """
    cfg = get_config(env)
    from sqlalchemy import func
    with SessionLocal() as s:
        # character
        rep = run_ingest(cfg.source_dir)
        ck = 0; ia = {"asset": 0, "cash": 0}
        for r in rep.ok:
            if r.category == "character" and r.records:
                ck += writer.import_characters(s, r.records, r.file)["imported"]
            if r.category == "initial_asset" and r.records:
                st = writer.import_initial_assets(s, r.records)
                ia["asset"] += st["asset"]; ia["cash"] += st["cash"]
        s.flush()
        s.commit()
        typer.echo(f"[{cfg.env}] 落库完成：人物 {ck}、初始资产 {ia['asset']} 项、现金入账 {ia['cash']} 笔")


if __name__ == "__main__":
    app()