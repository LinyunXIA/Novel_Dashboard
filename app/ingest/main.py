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
from app.ingest import conflict, writer

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
    with SessionLocal() as s:
        rep = run_ingest(cfg.source_dir)
        ck = 0; ia = {"asset": 0, "cash": 0}; sec = 0; rent = 0; prop = 0; shop = 0; sal = 0; he = 0; blocked_files = 0
        for r in rep.ok:
            if r.category == "character" and r.records:
                ck += writer.import_characters(s, r.records, r.file)["imported"]
            if r.category == "initial_asset" and r.records:
                st = writer.import_initial_assets(s, r.records)
                ia["asset"] += st["asset"]; ia["cash"] += st["cash"]
            if r.category in ("income_security","income_rent","income_property","income_shop","salary") and r.records:
                # 导入前冲突检测(H2/H5)：同 key 金额冲突或引用断链 → 整文件不入库
                gate = _conflict_gate(s, r)
                if gate["blocked"]:
                    blocked_files += 1
                    continue
                if gate["exists"]:
                    continue                       # 幂等：同 source_file 已导入，跳过
                for rec in r.records:
                    rec.setdefault("source_file", r.file)
                if r.category == "income_security": sec += writer.import_income_security(s, r.records)["stream"]
                if r.category == "income_rent": rent += writer.import_income_rent(s, r.records)["stream"]
                if r.category == "income_property": prop += writer.import_income_property(s, r.records)["stream"]
                if r.category == "income_shop": shop += writer.import_income_shop(s, r.records)["stream"]
                if r.category == "salary": sal += writer.import_salary(s, r.records)["stream"]
            if r.category == "household_expense" and r.records:
                he += writer.import_household_expense(s, r.records)["n"]
        closed = writer.close_2002_currency(s)["closed"]
        s.flush()
        s.commit()
        typer.echo(f"[{cfg.env}] 落库完成：人物 {ck}、初始资产 {ia['asset']}、现金 {ia['cash']}、票息 {sec}、租房 {rent}、经营房 {prop}、开店 {shop}、薪资 {sal}、家庭支出 {he}、2002关池 {closed}、冲突拦截文件 {blocked_files}")


@app.command()
def health(env: str = typer.Option("dev", "--env")):
    """运行全库健康校验（H1-H5）并输出问题清单。"""
    from app.core.health import run_report, summarize
    with SessionLocal() as s:
        summ = summarize(s)
        typer.echo(f"[{env}] 健康校验汇总（H1-H5）：")
        for rule in ("H1", "H2", "H3", "H4", "H5"):
            x = summ.get(rule, {"total": 0})
            typer.echo(f"  {rule}: {x['total']} 项"
                       + (f"（warn {x.get('warn',0)} / crit {x.get('crit',0)}）" if x["total"] else " ✓"))
        for f in run_report(s):
            typer.echo(f"  [{f['rule']}/{f['level']}] {f['location']}: {f['detail']}")


def _conflict_gate(s, r) -> dict:
    """对收益类文件做导入前冲突检测：H2 金额 / H5 引用。命中冲突 → 该文件不入库。"""
    from sqlalchemy import select as _sel
    from app.model import Entity, IncomeStream
    src = r.file
    # 幂等判重：同 source_file 已导入 → 整文件跳过（金额冲突检测只需跨文件时）
    from sqlalchemy import exists as _exists
    from app.model import IncomeStream as _IS
    already = s.execute(
        _sel(_IS.id).where(_IS.source_file == src).limit(1)
    ).scalar_one_or_none()
    blocked = False
    problems = []
    exists = False
    if already is not None:
        exists = True                          # 该文件已导入（source_file 命中）→ 幂等跳过
        return {"blocked": False, "exists": True, "problems": []}
    # H2 金额冲突：与其它来源文件同 (entity, stream_type, currency, year) 金额不一致 → 拦
    for rec in r.records:
        ent_name = rec.get("entity_name") or rec.get("holder")
        st_key = rec.get("stream_type") or {"income_security": "security",
                                             "income_rent": "rent",
                                             "income_property": "property",
                                             "income_shop": "shop",
                                             "salary": "salary"}.get(r.category)
        cur = rec.get("currency")
        year = rec.get("year", rec.get("y0"))
        amt = rec.get("amount", rec.get("after_tax"))
        if not (ent_name and st_key and year is not None and amt is not None):
            continue
        ent_id = s.execute(_sel(Entity.id).where(Entity.name == ent_name)).scalar_one_or_none()
        if ent_id is None:
            problems.append(f"H5-引用 {ent_name} 不存在")
            continue
        existing = s.execute(
            _sel(_IS.amount).where(
                _IS.entity_id == ent_id, _IS.stream_type == st_key,
                _IS.currency == cur, _IS.year == year)
        ).scalar_one_or_none()
        if existing is not None and existing != amt:
            blocked = True
            problems.append(f"H2-金额 {st_key} {cur} {year} 既有{existing}≠新{amt}")
    return {"blocked": blocked, "exists": exists, "problems": problems}


if __name__ == "__main__":
    app()