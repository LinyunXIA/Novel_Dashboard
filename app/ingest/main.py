"""Ingest CLI（DESIGN §3/§6）。

用法：
    python -m app.ingest.main --env dev            # 默认
    python -m app.ingest.main --env prod --full    # 全量重建
    python -m app.ingest.main ping                 # DB 连通自检

F-P0-01：骨架（config/db/CLI）已就绪；detect/parsers/normalize/conflict 后续里程碑填充。

设计要点（issue #3）：每个子命令的 session 由 \`--env\` 显式构造（make_sessionmaker），
不再走导入期绑定的模块 SessionLocal，杜绝「打印 prod 实际写入 dev」的脱节。
"""
from __future__ import annotations

import typer

from app.config import get_config
from app.db import check_connection_for, make_sessionmaker
from app.ingest.parse import run_ingest
from app.ingest import conflict, writer

app = typer.Typer(help="Novel Dashboard ingest")


def _session_for(env: str):
    """按 \`--env\` 构造 sessionmaker（with-block 兼容 SessionLocal 接口）。"""
    return make_sessionmaker(env)()


@app.command()
def ping(env: str = typer.Option("dev", "--env")):
    """数据库连通自检（按 \`--env\` 真正打到对应 DSN）。"""
    cfg = get_config(env)
    ok = check_connection_for(env)
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
    # issue #11：parser 警告（如 country 已设但未产出记录）
    warns = report.warnings
    if warns:
        typer.echo(f"⚠ 解析告警 {len(warns)} 条：")
        for f, w in warns:
            typer.echo(f"   ⚠ {f}: {w}")


@app.command()
def ingest(
    env: str = typer.Option("dev", "--env"),
):
    """F-P0-04 落库：character→entity；initial_asset→entity/account/initial_asset/现金余额。

    从 Design_Folder（source_dir）读取基础数据；commit 前整批事务，失败回滚。
    Session 严格按 \`--env\` 构造（issue #3）。
    """
    cfg = get_config(env)
    with _session_for(env) as s:
        rep = run_ingest(cfg.source_dir)
        ck = 0; ia = {"asset": 0, "cash": 0}; sec = 0; rent = 0; prop = 0; shop = 0; sal = 0; he = 0; rcur = 0; fx_total = 0; tl_n = 0; bank_n = 0; bank_seg_skip = 0; blocked_files = 0
        fx_files = []
        for r in rep.ok:
            if r.category == "character" and r.records:
                ck += writer.import_characters(s, r.records, r.file)["imported"]
            if r.category == "return_table" and r.records:
                rcur += writer.import_return_curves(s, r.records)["n"]
            if r.category == "fx" and r.records:
                fx_files.append(r)        # 收集，权威优先 + 冲突检测在下方统一处理
            if r.category == "timeline" and r.records:
                tl_n += writer.import_timeline(s, r.records)["n"]
            if r.category == "bank" and r.records:
                # issue #9/#14/#15：幂等 + 内容变更提示 + H4/H5 冲突检测
                src = r.file
                st_ = _file_import_state(s, r, cfg.source_dir)
                if st_["status"] == "changed":
                    typer.secho(f"   ⚠ {src}: 检测到内容变更，待版本决策流程处理（P2）；本次跳过",
                                fg=typer.colors.YELLOW)
                    continue
                if st_["status"] == "unchanged":
                    continue                        # 幂等：内容一致才静默跳过
                crep = conflict.check_bank_import_conflict(s, src, r.records)
                if crep.blocked:
                    blocked_files += 1
                    for p in crep.problems:
                        typer.echo(f"   ❌ {src}: [{p['rule']}] {p['line']}: {p['detail']}")
                    continue
                st = writer.import_bank(s, r.records, source_file=src)
                bank_n += st["ledger"]; bank_seg_skip += st["skipped"]
                _record_current_version(s, r, cfg.source_dir)
            if r.category == "initial_asset" and r.records:
                st = writer.import_initial_assets(s, r.records)
                ia["asset"] += st["asset"]; ia["cash"] += st["cash"]
            if r.category in ("income_security","income_rent","income_property","income_shop","salary") and r.records:
                # issue #14/#15：幂等（内容一致跳过）+ 内容变更提示 + 冲突明细（调 conflict.py 而非复制版）
                st_ = _file_import_state(s, r, cfg.source_dir)
                if st_["status"] == "changed":
                    typer.secho(f"   ⚠ {r.file}: 检测到内容变更，待版本决策流程处理（P2）；本次跳过",
                                fg=typer.colors.YELLOW)
                    continue
                if st_["status"] == "unchanged":
                    continue                        # 幂等：内容一致才静默跳过
                crep = conflict.check_income_stream_conflict(
                    s, r.file, _normalize_conflict_recs(r.category, r.records))
                if crep.blocked:
                    blocked_files += 1
                    for p in crep.problems:
                        typer.echo(f"   ❌ {r.file}: [{p['rule']}] {p['line']}: {p['detail']}")
                    continue
                for rec in r.records:
                    rec.setdefault("source_file", r.file)
                if r.category == "income_security": sec += writer.import_income_security(s, r.records)["stream"]
                if r.category == "income_rent": rent += writer.import_income_rent(s, r.records)["stream"]
                if r.category == "income_property": prop += writer.import_income_property(s, r.records)["stream"]
                if r.category == "income_shop": shop += writer.import_income_shop(s, r.records)["stream"]
                if r.category == "salary": sal += writer.import_salary(s, r.records)["stream"]
                _record_current_version(s, r, cfg.source_dir)
            if r.category == "household_expense" and r.records:
                he += writer.import_household_expense(s, r.records)["n"]
        # —— 汇率两轮：权威文件(W全量)先入库为基准；其它 fx 文件检测与权威冲突，冲突则拦 ——
        authority = [r for r in fx_files if conflict.is_authority_fx(r.file)]
        others = [r for r in fx_files if not conflict.is_authority_fx(r.file)]
        for r in authority:
            fx_total += writer.import_fx(s, r.records)["n"]
        for r in others:
            crep = conflict.check_fx_authority_conflict(s, r.file, r.records)
            if crep.blocked:
                typer.echo(f"   ⚠ fx冲突拦截 {r.file}: {len(crep.problems)} 处（以权威表为准）")
                blocked_files += 1
                continue
            fx_total += writer.import_fx(s, r.records)["n"]
        cc = writer.close_2002_currency(s)
        closed = cc["closed"]
        # —— DESIGN §9 摄入因果链尾巴：增量重算 + 重建快照 + recompute-done 通知（issue #13）——
        if not blocked_files:
            from app.core.recompute import recompute_all, record_recompute_done
            from app.core.snapshot import rebuild_snapshots as _rebuild
            recompute_all(s, 1947)
            _rebuild(s, range(1947, 2026), from_year=1947)
            job = record_recompute_done(s, 1947, reason="ingest")
        s.flush()
        s.commit()
        notif_part = f"；recompute job#{job['job_id']} 通知#{job['notification_id']}" if not blocked_files else ""
        typer.echo(f"[{cfg.env}] 落库完成：人物 {ck}、初始资产 {ia['asset']}、现金 {ia['cash']}、票息 {sec}、租房 {rent}、经营房 {prop}、开店 {shop}、薪资 {sal}、家庭支出 {he}、收益曲线 {rcur}、汇率 {fx_total}、时间线 {tl_n}、银行流水 {bank_n}（seg 跳过 {bank_seg_skip}）、2002关池 {closed}（EUR承接 {cc['migrated']} / 零结转跳过 {cc['skipped_zero']}）、冲突拦截 {blocked_files}{notif_part}")


@app.command()
def health(env: str = typer.Option("dev", "--env")):
    """运行全库健康校验（H1-H5）并输出问题清单。"""
    from app.core.health import run_report, summarize
    with _session_for(env) as s:
        summ = summarize(s)
        typer.echo(f"[{env}] 健康校验汇总（H1-H5）：")
        for rule in ("H1", "H2", "H3", "H4", "H5"):
            x = summ.get(rule, {"total": 0})
            typer.echo(f"  {rule}: {x['total']} 项"
                       + (f"（warn {x.get('warn',0)} / crit {x.get('crit',0)}）" if x["total"] else " ✓"))
        for f in run_report(s):
            typer.echo(f"  [{f['rule']}/{f['level']}] {f['location']}: {f['detail']}")


@app.command()
def recompute(env: str = typer.Option("dev", "--env"), from_year: int = typer.Option(1947, "--from")):
    """全库增量重算：从受影响起点年向后滚动账户余额（F-P0-12）。

    完成后写 recompute_job + recompute-done 通知（DESIGN §9.2 步骤3-4；issue #13）。
    """
    from app.core.recompute import recompute_all, record_recompute_done
    from app.core.snapshot import rebuild_snapshots
    with _session_for(env) as s:
        res = recompute_all(s, from_year)
        # §9.2c 重算后重建受影响起点起的快照（account/entity/family 三层，增量）
        rebuild_snapshots(s, range(from_year, 2026), from_year=from_year)
        # §9.2 步骤3-4：写 job + 通知
        job = record_recompute_done(s, from_year, reason="recompute")
        s.commit()
        total_updated = sum(r["updated"] for r in res)
        typer.echo(f"[{env}] 重算 {len(res)} 个账户，更新 {total_updated} 行余额（自 {from_year} 起）"
                   f"；job#{job['job_id']} 通知#{job['notification_id']}")


@app.command()
def calendar(env: str = typer.Option("dev", "--env"), as_of: str = typer.Option("2001-12-30", "--as-of")):
    """全局日历游标：按截至日期读取快照。"""
    from datetime import date
    from app.core.calendar import snapshot_as_of
    d = date.fromisoformat(as_of)
    with _session_for(env) as s:
        snaps = snapshot_as_of(s, d)
        typer.echo(f"[{env}] 截至 {d} 快照 {len(snaps)} 条：")
        for x in snaps:
            typer.echo(f"  {x['scope']}: {x['value']:,.0f} ({x['currency']})")


@app.command()
def wealth(env: str = typer.Option("dev", "--env"), year: int = typer.Option(2001, "--year")):
    """财富曲线视图：某年家族合计(USD) + 各币种分项。

    汇率缺失币种不计入合计，并在底部显式告警（issue #2 修复：杜绝 1.0 静默 fallback）。
    """
    from app.core.wealth import wealth_series
    with _session_for(env) as s:
        w = wealth_series(s, year, year)
        d = w.get(year, {})
        typer.echo(f"[{env}] {year} 家族合计(展示USD) = {d.get('family_total_usd', 0):,.0f}")
        for cur, val in d.get("currencies", {}).items():
            typer.echo(f"   {cur}: {val:,.0f}")
        missing = d.get("missing_rates", [])
        if missing:
            typer.secho(
                f"   ⚠ 缺汇率未折算币种: {', '.join(missing)}（合计不含此部分）",
                fg=typer.colors.YELLOW,
            )


@app.command()
def snapshot(env: str = typer.Option("dev", "--env"),
             from_year: int = typer.Option(1947, "--from",
                                            help="仅重建 from_year 起的快照（旧段保留）")):
    """重建逐年 as-of 快照（account/entity/family 三层；F-P0-08 + issue #12）。"""
    from app.core.snapshot import rebuild_snapshots
    with _session_for(env) as s:
        r = rebuild_snapshots(s, range(from_year, 2026))
        s.commit()
        typer.echo(f"[{env}] 快照重建完成：{r['snapshots']} 条 / {r['accounts']} 账户 / {r['entities']} 实体聚合 / {r['family_years']} 家族合计年（自 {from_year} 起）")


# ---- 收益/银行文件的幂等 + 内容变更提示（issue #14） ----
_CAT_STREAM = {"income_security": "security", "income_rent": "rent",
               "income_property": "property", "income_shop": "shop", "salary": "salary"}


def _content_fingerprint(content: str) -> str:
    import hashlib
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()


def _file_import_state(s, r, source_dir) -> dict:
    """文件导入状态：已导入(source_file 命中) → unchanged/changed；未导入 → new。

    用 source_file_version 记录首次导入时的内容哈希快照；再次导入对比当前文件内容。
    判断 content 仅对比首行尾字符与行长，回答"改动了吗"而不写库。
    返回 {"status": "new"|"unchanged"|"changed"}。
    """
    from sqlalchemy import select as _sel
    from app.model import IncomeStream as _IS
    already = s.execute(
        _sel(_IS.id).where(_IS.source_file == r.file).limit(1)
    ).scalar_one_or_none()
    if already is None:
        return {"status": "new"}
    # 已导入：对比内容哈希快照
    from app.model import SourceFileVersion
    row = s.execute(
        _sel(SourceFileVersion).where(
            SourceFileVersion.file_path == r.file, SourceFileVersion.is_current.is_(True))
        .order_by(SourceFileVersion.version.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return {"status": "unchanged"}            # 首次导入先于版本记录建立（旧库无快照）→ 保守跳过
    path = source_dir / r.file
    try:
        cur_content = path.read_text(encoding="utf-8") if path.exists() else None
    except (OSError, UnicodeDecodeError):
        cur_content = None
    if cur_content is None:
        return {"status": "unchanged"}
    prev_hash = _content_fingerprint(row.content)
    if _content_fingerprint(cur_content) != prev_hash:
        return {"status": "changed"}
    return {"status": "unchanged"}


def _record_current_version(s, r, source_dir):
    """导入成功后记录当前内容版本（source_file_version v1，is_current=True）。"""
    from datetime import datetime
    from app.model import SourceFileVersion
    path = source_dir / r.file
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    s.add(SourceFileVersion(file_path=r.file, version=1, content=content,
                            captured_at=datetime.now(), is_current=True))


def _normalize_conflict_recs(category: str, records: list[dict]) -> list[dict]:
    """把各收益类别记录归一成冲突检测所需的 {entity_name, stream_type, currency, year, amount}。"""
    out = []
    for rec in records:
        amt = rec.get("amount") if rec.get("amount") is not None else rec.get("after_tax")
        ent = rec.get("entity_name") or rec.get("holder")
        st = rec.get("stream_type") or _CAT_STREAM.get(category)
        out.append({
            "entity_name": ent, "stream_type": st,
            "currency": rec.get("currency"),
            "year": rec.get("year", rec.get("y0")), "amount": amt,
        })
    return out


if __name__ == "__main__":
    app()