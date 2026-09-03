"""Ingest CLI（DESIGN §3/§6）。

用法：
    python -m app.ingest.main --env dev ingest     # 落库主链路
    python -m app.ingest.main ping                 # DB 连通自检

F-P0-01：骨架（config/db/CLI）；F-P0-02..06 落库链路。
（issue #144：删除无实现的 run --full 死参数与旧用法示例。）

设计要点（issue #3）：每个子命令的 session 由 `--env` 显式构造（make_sessionmaker），
不再走导入期绑定的模块 SessionLocal，杜绝「打印 prod 实际写入 dev」的脱节。

issue #68：幂等统一为 source_file_version(is_current) 内容哈希判定；
initial_asset / household_expense 分支接入守卫；_record_current_version 版本递增；
核心落库循环抽为 import_all()（可测试）。
"""
from __future__ import annotations

import typer

from app.config import get_config
from app.db import check_connection_for, make_sessionmaker
from app.ingest.parse import run_ingest
from app.ingest import conflict, writer
from app.ingest.manifest import require_active_files

app = typer.Typer(help="Novel Dashboard ingest")


def _session_for(env: str):
    """按 `--env` 构造 sessionmaker（with-block 兼容 SessionLocal 接口）。"""
    return make_sessionmaker(env)()


def _resolved_env(env: str | None) -> str:
    """解析 --env/APP_ENV 回退链的实际环境名（issue #107：缺省不再硬编码 dev，
    未传 --env 时回落 APP_ENV；回显一律用本函数结果，防打印 None）。"""
    return get_config(env).env


@app.command()
def ping(env: str = typer.Option(None, "--env")):
    """数据库连通自检（按 `--env` 真正打到对应 DSN）。"""
    cfg = get_config(env)
    ok = check_connection_for(env)
    typer.echo(f"[{cfg.env}] dsn={cfg.dsn}")
    typer.secho("连接成功" if ok else "连接失败", fg=typer.colors.GREEN if ok else typer.colors.RED)


@app.command()
def run(
    env: str = typer.Option(None, "--env"),
):
    """扫描输入目录 → detect → parse → 输出报告（F-P0-02；不落库）。"""
    cfg = get_config(env)
    typer.echo(f"[{cfg.env}] 输入目录={cfg.input_dir}")
    report = run_ingest(cfg.input_dir)
    typer.echo(f"识别 {len(report.ok)} 个可解析文件 · {len(report.failed)} 需人工 · {len(report.skipped)} 跳过")
    for r in report.ok:
        typer.echo(f"  ✅ {r.category:12s} {r.file} ({len(r.records)} 条)")
    for r in report.failed:
        typer.echo(f"  ❌ {r.category:12s} {r.file} — {r.error}")
    warns = report.warnings
    if warns:
        typer.echo(f"⚠ 解析告警 {len(warns)} 条：")
        for f, w in warns:
            typer.echo(f"   ⚠ {f}: {w}")


@app.command()
def ingest(
    env: str = typer.Option(None, "--env"),
    force: bool = typer.Option(False, "--force",
                               help="跳过文件指纹 gate 重导四类收益文件"
                                    "（先清该文件旧 income_stream/finance 镜像行）；"
                                    "用于展开因子等代码口径变更后的重浇灌"),
):
    """F-P0-04..06 落库：从 Design_Folder（source_dir）读取基础数据并入库。

    Session 严格按 `--env` 构造（issue #3）；核心循环见 import_all。
    """
    cfg = get_config(env)
    manifest_active = require_active_files(cfg.env, cfg)   # prod 门控；dev/test 返回 None
    with _session_for(env) as s:
        stats = import_all(s, cfg.source_dir, log=typer.echo, force=force,
                           manifest_active=manifest_active)
        s.commit()          # issue #68：import_all 只 flush；commit 由命令层负责（勿丢）
    typer.echo(f"[{cfg.env}] {stats['summary']}")


@app.command()
def reset(env: str = typer.Option(None, "--env"),
          yes: bool = typer.Option(False, "--yes",
                                   help="跳过确认（脚本/CI 用；非交互下必加）")):
    """清空并重建某环境 schema（危险！删除全部数据表后 alembic upgrade head）。

    只删 public schema 的**表**（保留 pgvector 扩展、库本体、DSN），随后程序化把
    `alembic` 迁到 head 重建空表。用于数据整理员清库后的逐块导入。不可逆。
    """
    cfg = get_config(env)
    typer.secho(
        f"[{cfg.env}] 危险！即将删除 {cfg.env} 库全部数据表并 alembic 重建到 head（不可逆）",
        fg=typer.colors.RED)
    if not yes and not typer.confirm(f"确认清空 {cfg.env} 数据库？"):
        raise typer.Exit("已取消")
    from app.db import make_engine
    eng = make_engine(env)
    drop_all = """
    DO $$ DECLARE r RECORD; BEGIN
      FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
        EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
      END LOOP;
    END $$;
    """
    with eng.begin() as conn:
        conn.exec_driver_sql(drop_all)
    typer.echo(f"[{cfg.env}] 已删除全部 public 表，正在 alembic upgrade head ...")
    import os
    from app.config import PROJECT_ROOT
    from alembic import command as _al_command
    from alembic.config import Config as _AlConfig
    prev = os.environ.get("APP_ENV")
    os.environ["APP_ENV"] = cfg.env            # migrations/env.py 按 APP_ENV 解析 DSN
    try:
        _al_command.upgrade(_AlConfig(str(PROJECT_ROOT / "alembic.ini")), "head")
    finally:
        if prev is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = prev
    typer.secho(f"[{cfg.env}] schema 已重建（head），可开始清单导入。",
                fg=typer.colors.GREEN)


def _reload_date_rules(session) -> int:
    """把 DB 中的用户 date_rule 装载进 normalize 进程内缓存（issue #119 消费侧）。"""
    from sqlalchemy import select as _sel
    from app.model import DateRule
    from app.ingest.normalize import load_date_rules
    rows = session.execute(_sel(DateRule.id, DateRule.pattern, DateRule.resolve)).all()
    return load_date_rules(rows)


def _earliest_affected_year(r) -> int | None:
    """§9.1 受影响起点（issue #120）：从已解析记录推导最早影响年。

    返回 None = 全局性内容（人物/收益曲线/汇率/初始资产/基本收入，基本收入商业流自 1947）
    → 调用方按 1947 全量处理；否则返回记录中的最小年份，重算只向后传播。
    """
    cat = r.category
    # issue #211：basic_income 为逐年终值（股债/房产/商业，最早 1947），全局性内容 → 1947
    if cat in ("character", "return_table", "fx", "initial_asset", "basic_income"):
        return None
    if cat == "timeline":
        ys = [int(x["event_year"]) for x in r.records if x.get("event_year")]
        return min(ys) if ys else None
    if cat == "bank":
        ys = [row["date"].year for seg in r.records for row in (seg.get("rows") or [])
              if getattr(row.get("date"), "year", None)]
        return min(ys) if ys else None
    return None  # salary/household 等结构未逐年暴露 → 保守全量


def import_all(session, source_dir, log=None, force: bool = False, force_files=None,
               manifest_active: set | None = None) -> dict:
    """扫描 source_dir → 解析 → 冲突检测 → 落库 → 重算快照（F-P0-02..06 主链路）。

    issue #68 抽取自原 ingest 命令体以便测试；幂等语义：
    - 所有文件类目经 _file_import_state 判定 new/unchanged/changed，
      unchanged 静默跳过、changed 提示待 P2 版本决策并跳过；
    - writer 层另有自然键去重兜底（初始现金 / 家庭支出），兼容无版本记录的存量库。
    - force=True（issue #114）：收益文件（issue #211 起为 basic_income 基本收入.md）
      跳过指纹 gate，且先清除该文件旧 income_stream + finance 镜像行再重导
      （口径变更后的重浇灌）。issue #220 起 salary 同样可 force/force_files 重导：
      writer 为按人替换式（先删该 entity 旧 salary 流+镜像再插），文件名更替或
      口径修正（CNY 修正版）均安全，不触碰 ledger。
    - force_files（F-P2-06）：`set[str]`，命中文件强制重导入（版本决策「采纳新版本」）。
    commit 前整批事务，失败回滚（由调用方管理 session/commit）。
    """
    log = log or (lambda msg: print(msg))
    n_rules = _reload_date_rules(session)
    if n_rules:
        log(f"   ✓ 已装载用户 date_rule {n_rules} 条（超规则日期将按其解析）")
    ck = 0; ia = {"asset": 0, "cash": 0}; bi = 0
    sal = 0; he = 0; rcur = 0; fx_total = 0; tl_n = 0; bank_n = 0; bank_seg_skip = 0
    blocked_files = 0
    soft_warnings = 0
    imported_rs: list = []   # issue #120：本批实际落库的文件，用于推导最小传播起点
    job: dict = {}
    fx_files = []
    rep = run_ingest(source_dir)
    # prod 激活清单门控（import_files.yaml）：仅导入 active:true 的文件，未激活一律跳过。
    if manifest_active is not None:
        inactive = sorted({r.file for r in rep.results} - manifest_active)
        rep.results = [r for r in rep.results if r.file in manifest_active]
        if inactive:
            log(f"   ⏭ {len(inactive)} 个文件未在 import_files.yaml 激活，已跳过"
                f"（例：{inactive[:3]}）")
    # issue #223：版本对账——已被整合取代的文件（detect=SKIP_SUPERSEDED）其 is_current
    # 版本记录失活（旧文件已从磁盘删除、扫描扫不到，故反向从版本表判定），
    # 否则 diff/版本屏会把已删文件一直挂为「当前版本」。
    _deactivate_superseded_versions(session, log)
    # DESIGN §6.5 摄入顺序锁死：人物(entity) 先入 → 初始资产 → 收益/薪资/支出 → 银行。
    # 收益挂账依赖 entity_id，不能按文件名字典序处理（issue #68）。
    # issue #211：四类配置推导收益（security/rent/property/shop）整合为 basic_income
    # （基本收入.md 逐年终值），取代原 income_* 四类位置。
    _ORDER = ("character", "return_table", "timeline", "initial_asset",
              "basic_income", "salary", "household_expense", "bank")
    ordered = sorted(
        rep.ok,
        key=lambda r: (_ORDER.index(r.category) if r.category in _ORDER else 99, r.file),
    )
    # issue #118：解析失败也落库（level='error'），不再只留在 run 报告里
    for r in rep.failed:
        _record_parse_error(session, r.file, r.category, r.error)
    if rep.failed:
        log(f"   ⚠ {len(rep.failed)} 个文件解析失败，已记入 ingest_report")
    for r in ordered:
        if r.category == "stock_tx" and r.records:
            # issue #70：股票台账解析成功但持仓/事件落库属 Phase 2（DESIGN §19.6），显式说明而非静默
            log(f"   ⏭ {r.file}: 股票台账解析成功（{len(r.records)} 组基本信息/"
                f"{sum(len(x.get('events') or []) for x in r.records)} 条明细），"
                f"持仓/事件落库属 Phase 2（§19.6），本次跳过")
            continue
        if r.category in ("event_movie", "event_stock") and r.records:
            # issue #144：Phase2 事件由 events-movie/events-stock CLI 显式导入；
            # 主扫描链显式跳过并说明（与 stock_tx 对称），不再静默白解析
            log(f"   ⏭ {r.file}: Phase2 事件素材已解析（{len(r.records)} 条），"
                f"落库请用 events-movie / events-stock CLI（§19.6）；本次跳过")
            continue
        if r.category == "character" and r.records:
            imported_rs.append(r)
            ck += writer.import_characters(session, r.records, r.file)["imported"]
            # issue #209：人物/收益曲线/时间线同为权威基准，导入后记版本，
            # 否则 diff 屏永远判「新增」、采纳新版本也落不了版（与 fx-authority 对齐）
            _record_current_version(session, r, source_dir)
        if r.category == "return_table" and r.records:
            imported_rs.append(r)
            rst = writer.import_return_curves(session, r.records)
            rcur += rst["n"]
            if rst["updated"]:
                # issue #214：同键数值/来源刷新（如整合文件取代分地区表）——显式说明而非静默
                log(f"   ↻ {r.file}: 收益曲线刷新 {rst['updated']} 行"
                    f"（同键数值/来源更新，新增 {rst['inserted']} 行）")
            _record_current_version(session, r, source_dir)   # issue #209
        if r.category == "return_table" and not r.records and r.warnings:
            # 五轮审计 #177：零条告警达主链路（此前只进 run stdout，ingest 静默）——
            # 落 ingest_report(warn) + 日志，供数据调整员察觉 catch-all 误归档
            _record_parse_warning(session, r.file, "; ".join(r.warnings))
            log(f"   ⚠ {r.file}: {r.warnings[0]}")
        if r.category == "fx" and r.records:
            fx_files.append(r)        # 收集，权威优先 + 冲突检测在下方统一处理
        if r.category == "timeline" and r.records:
            imported_rs.append(r)
            tl_n += writer.import_timeline(session, r.records)["n"]
            _record_current_version(session, r, source_dir)   # issue #209
        if r.category == "bank" and r.records:
            src = r.file
            if _skip_by_state(session, r, source_dir, log, force_files):
                continue
            crep = conflict.check_bank_import_conflict(session, src, r.records)
            if crep.blocked:
                blocked_files += 1
                for p in crep.problems:
                    log(f"   ❌ {src}: [{p['rule']}] {p['line']}: {p['detail']}")
                _record_findings(session, src, crep)
                continue
            _record_findings(session, src, crep)
            soft_warnings += _log_soft(log, src, crep)
            st = writer.import_bank(session, r.records, source_file=src)
            bank_n += st["ledger"]; bank_seg_skip += st["skipped"]
            _record_current_version(session, r, source_dir)
            imported_rs.append(r)
        if r.category == "initial_asset" and r.records:
            if _skip_by_state(session, r, source_dir, log, force_files):
                continue
            st = writer.import_initial_assets(session, r.records)
            ia["asset"] += st["asset"]; ia["cash"] += st["cash"]
            _record_current_version(session, r, source_dir)
            imported_rs.append(r)
        if r.category in ("basic_income", "salary") and r.records:
            bypass = force or bool(force_files and r.file in force_files)
            if not bypass and _skip_by_state(session, r, source_dir, log, force_files):
                continue
            norm = _normalize_conflict_recs(r.category, r.records)
            if bypass:
                # issue #135 回归修复：清场后必须继续走下方冲突检测+重导。
                # 此前 purge 后直接 continue，--force(#114) 重浇灌与 F-P2-06「采纳新版本」
                # 均退化为「只删不补」的数据清零事故。
                if r.category == "basic_income":
                    purged = _purge_income_derived(session, r.file)
                    log(f"   ♻ {r.file}: 清除旧派生行 {purged} 条后重导")
                else:
                    # issue #220：salary 为按人替换式（writer 内按 entity 清场），
                    # 文件名更替 / 口径修正版替换时老行自然清除，无需按文件名 purge
                    log(f"   ♻ {r.file}: 薪资台账按人替换式重导")
            if r.category == "basic_income":
                # H2 金额冲突：basic_income 为多文件汇聚的逐年收益，跨文件同键不同值须拦
                crep = conflict.check_income_stream_conflict(session, r.file, norm)
                crep.merge(conflict.check_timeline_alignment(session, r.file, norm))
                if crep.blocked:
                    blocked_files += 1
                    for p in crep.problems:
                        log(f"   ❌ {r.file}: [{p['rule']}] {p['line']}: {p['detail']}")
                    _record_findings(session, r.file, crep)
                    continue
            else:
                # issue #220：salary 为按人全量替换（薪资文件即该人唯一权威台账），
                # 旧值被整段覆盖，H2 金额比对不再适用——否则口径修正版（CNY 修正版）
                # 会被自身老值永久拦死；仅保留 H1 时间线对齐 soft 提示。
                crep = conflict.check_timeline_alignment(session, r.file, norm)
            _record_findings(session, r.file, crep)
            soft_warnings += _log_soft(log, r.file, crep)
            for rec in r.records:
                rec.setdefault("source_file", r.file)
            # issue #211：basic_income 记录已逐年展开且自带 stream_type（股债 security /
            # 租房 rent / 经营房 property / 开店 shop），writer 直写终值。
            if r.category == "basic_income":
                bi += writer.import_basic_income(session, r.records)["stream"]
            if r.category == "salary":
                sal += writer.import_salary(session, r.records)["stream"]
            _record_current_version(session, r, source_dir)
            imported_rs.append(r)
        if r.category == "household_expense" and r.records:
            if _skip_by_state(session, r, source_dir, log, force_files):
                continue
            hst = writer.import_household_expense(session, r.records)
            he += hst["n"]
            if hst.get("updated"):
                # issue #216：同键金额/来源刷新（修正版替换或金额修订）——显式说明而非静默
                log(f"   ↻ {r.file}: 家庭支出刷新 {hst['updated']} 行"
                    f"（同键金额/来源更新，新增 {hst['n']} 行）")
            _record_current_version(session, r, source_dir)
            imported_rs.append(r)
    # —— 汇率两轮（issue #116：接入文件指纹 gate；权威表变更 → upsert 更新）——
    authority = [r for r in fx_files if conflict.is_authority_fx(r.file)]
    others = [r for r in fx_files if not conflict.is_authority_fx(r.file)]
    for r in authority:
        st = _file_import_state(session, r, source_dir)
        if st["status"] == "unchanged":
            continue
        # new → insert-only 即可；changed → 权威表为基准，同键不同值 upsert 覆盖
        res = writer.import_fx(session, r.records, update=(st["status"] == "changed"))
        fx_total += res["n"]
        imported_rs.append(r)
        if st["status"] == "changed":
            log(f"   ♻ {r.file}: 权威汇率表重导，更新 {res['updated']} / 新增 {res['n']} 条")
        _record_current_version(session, r, source_dir)
    for r in others:
        if _skip_by_state(session, r, source_dir, log, force_files):
            continue
        crep = conflict.check_fx_authority_conflict(session, r.file, r.records)
        # issue #72：H3 链式闭合增量预检（新汇率 ∪ DB 视图，两跳 vs 直接 >0.5% → 挡）
        crep.merge(conflict.check_fx_chain_closure(session, r.file, r.records))
        if crep.blocked:
            log(f"   ⚠ fx冲突拦截 {r.file}: {len(crep.problems)} 处（以权威表为准）")
            blocked_files += 1
            _record_findings(session, r.file, crep)
            continue
        _record_findings(session, r.file, crep)
        soft_warnings += _log_soft(log, r.file, crep)
        fx_total += writer.import_fx(session, r.records)["n"]
        _record_current_version(session, r, source_dir)
        imported_rs.append(r)
    cc = writer.close_2002_currency(session)
    closed = cc["closed"]
    # —— DESIGN §9 摄入因果链尾巴：增量重算 + 重建快照 + recompute-done 通知（issue #13）
    # issue #117：重算不再被同批 hard-block 文件连坐搁置——被拦文件本就不入库，
    # 已成功入库文件的余额/快照必须及时收敛到一致状态。
    from app.core.recompute import recompute_all, record_recompute_done
    from app.core.snapshot import rebuild_snapshots as _rebuild
    # issue #120：最小传播起点——本批成功导入文件的最早影响年；全局性内容 → 1947
    affected = [_earliest_affected_year(r) for r in imported_rs]
    start_year = 1947 if any(y is None for y in affected) else min(
        (y for y in affected if y is not None), default=1947)
    recompute_all(session, start_year)
    _rebuild(session, from_year=start_year)   # issue #152：years 缺省走 calendar_years() 动态上限
    job = record_recompute_done(
        session, start_year,
        reason="ingest" if not blocked_files else f"ingest(部分：{blocked_files} 文件被拦)")
    session.flush()
    summary = (
        f"落库完成：人物 {ck}、初始资产 {ia['asset']}、现金 {ia['cash']}、基本收入流 {bi}、"
        f"薪资 {sal}、家庭支出 {he}、"
        f"收益曲线 {rcur}、汇率 {fx_total}、时间线 {tl_n}、银行流水 {bank_n}"
        f"（seg 跳过 {bank_seg_skip}）、2002关池 {closed}"
        f"（EUR承接 {cc['migrated']} / 零结转跳过 {cc['skipped_zero']}）、冲突拦截 {blocked_files}"
        + (f"；recompute job#{job['job_id']} 通知#{job['notification_id']}" if job else "")
        + (f"；软警告 {soft_warnings}" if soft_warnings else "")
    )
    return {"summary": summary, "blocked": blocked_files, "soft_warnings": soft_warnings,
            "characters": ck, "initial_assets": ia["asset"], "cash": ia["cash"],
            "basic_income": bi,
            "salary": sal, "household": he, "return_curves": rcur, "fx": fx_total,
            "timeline": tl_n, "ledger": bank_n, "job": job}


# ---- 收益/银行文件的幂等 + 内容变更提示（issue #14；issue #68 通用化） ----

def _purge_income_derived(session, source_file: str) -> int:
    """清除某收益文件的历史派生行（income_stream + finance_entry 镜像）。

    issue #114：--force 重导前的清场步骤；两类行都带 source_file 标记，
    不触碰 ledger/entity 等其他数据。
    """
    from app.model import FinanceEntry, IncomeStream
    n = 0
    for model in (IncomeStream, FinanceEntry):
        rows = session.execute(
            _select_model_by_source(model, source_file)).scalars().all()
        for row in rows:
            session.delete(row)
            n += 1
    session.flush()
    return n


def _select_model_by_source(model, source_file: str):
    from sqlalchemy import select
    return select(model).where(model.source_file == source_file)


def _deactivate_superseded_versions(session, log) -> int:
    """SKIP_SUPERSEDED 文件的 is_current 版本记录置 false（issue #223）。

    整合取代（#211 旧收益 4 文件 / #214 分地区 R1-R5 表 / #218 CPI工资 / #220
    老薪资表）后旧文件从 Design_Folder 删除，但其历史 source_file_version 记录仍
    is_current=True——diff/版本屏据版本表展示，已删文件会一直挂为「当前版本」。
    反向扫描版本表：detect(file_path) 命中 SKIP_SUPERSEDED 护栏即失活（代码层面的
    「已取代」信号，不依赖磁盘存在性——旧文件早已扫不到）。返回失活条数。
    """
    from sqlalchemy import select as _sel
    from app.ingest.detect import detect
    from app.model import SourceFileVersion
    rows = session.execute(
        _sel(SourceFileVersion).where(SourceFileVersion.is_current.is_(True))
    ).scalars().all()
    n = 0
    for v in rows:
        if detect(v.file_path).category == "SKIP_SUPERSEDED":
            v.is_current = False
            n += 1
            log(f"   ↻ {v.file_path}: 文件已被整合取代（SKIP_SUPERSEDED），版本 v{v.version} 失活")
    if n:
        session.flush()
    return n


def _coerce_line(v) -> int | None:
    """issue #144：problem.line 形态不一（行号/年份/dict/文本）——尽力取结构化行号，
    取不到落 None（§11.4「文件/行」四要素尽量不缺）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _record_findings(session, file_path: str, crep) -> None:
    """冲突报告落库（issue #118 · §11.4）：problems→block、warnings→warn。

    此前仅 stdout，进程结束即失；现与 echo 并行写入 ingest_report 表，
    供数据调整员事后回看与「导入状态」屏展示。
    四轮审计 #168：同 (file_path, rule, level) 幂等 upsert——每轮重导不再重复插行。
    """
    from sqlalchemy import select
    from app.model import IngestReport
    for level, items in (("block", crep.problems), ("warn", crep.warnings)):
        for p in items:
            rule = p.get("rule")
            detail = str(p.get("detail", ""))
            existing = session.execute(select(IngestReport).where(
                IngestReport.file_path == file_path,
                IngestReport.rule == rule,
                IngestReport.level == level).limit(1)).scalar_one_or_none()
            if existing is not None:
                existing.line = _coerce_line(p.get("line"))
                existing.detail = detail   # 最新一次为准
            else:
                session.add(IngestReport(
                    file_path=file_path, rule=rule, level=level,
                    line=_coerce_line(p.get("line")), detail=detail))


def _record_parse_error(session, file_path: str, category: str, error: str | None) -> None:
    """解析失败落库（issue #118：level='error'，需人工处理）。
    四轮审计 #168：同文件幂等——更新既有 error 行而非每轮新插。"""
    from sqlalchemy import select
    from app.model import IngestReport
    detail = f"[{category}] {error or '解析失败'}"
    existing = session.execute(select(IngestReport).where(
        IngestReport.file_path == file_path,
        IngestReport.level == "error").limit(1)).scalar_one_or_none()
    if existing is not None:
        existing.detail = detail
        existing.rule = category or None
    else:
        session.add(IngestReport(file_path=file_path, rule=category or None,
                                 level="error", line=None, detail=detail))

def _record_parse_warning(session, file_path: str, detail: str) -> None:
    """五轮审计 #177：解析层 warning 落库主链路（level='warn'，同键幂等）。"""
    from sqlalchemy import select as _s
    from app.model import IngestReport
    existing = session.execute(_s(IngestReport).where(
        IngestReport.file_path == file_path,
        IngestReport.rule.is_(None),
        IngestReport.level == "warn").limit(1)).scalar_one_or_none()
    if existing is not None:
        existing.detail = detail
    else:
        session.add(IngestReport(file_path=file_path, rule=None,
                                 level="warn", line=None, detail=detail))


def _log_soft(log, file: str, crep) -> int:
    """输出软警告（§11.4「标」：入库但高亮），返回条数（issue #72）。"""
    for w in crep.warnings:
        log(f"   ⚠ {file}: [{w['rule']}] {w['line']}: {w['detail']}")
    return len(crep.warnings)


def _content_fingerprint(content: str) -> str:
    import hashlib
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()


def _has_legacy_rows(session, rel_path: str) -> bool:
    """无版本记录时探测「该文件是否曾以旧机制导入过」（issue #68 兼容存量库）。"""
    from sqlalchemy import select as _sel
    from app.model import IncomeStream, InitialAsset, LedgerEntry
    for model in (IncomeStream, LedgerEntry, InitialAsset):
        hit = session.execute(
            _sel(model.id).where(model.source_file == rel_path).limit(1)
        ).scalar_one_or_none()
        if hit is not None:
            return True
    return False


def _file_import_state(session, r, source_dir) -> dict:
    """文件导入状态（issue #68：通用判定，供所有文件类目复用）。

    以 source_file_version(is_current=True) 的内容哈希为权威基准：
    - 无当前版本记录：
        · 若旧表（income_stream/ledger_entry/initial_asset）已有该文件行 →
          视为「unchanged」（存量库早于版本机制导入过，保守跳过防双计）；
        · 否则 → new。
    - 有版本记录：对比当前磁盘内容哈希 → 一致 unchanged / 不一致 changed。
    返回 {"status": "new"|"unchanged"|"changed"}。
    """
    from sqlalchemy import select as _sel
    from app.model import SourceFileVersion
    row = session.execute(
        _sel(SourceFileVersion).where(
            SourceFileVersion.file_path == r.file,
            SourceFileVersion.is_current.is_(True))
        .order_by(SourceFileVersion.version.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return {"status": "unchanged" if _has_legacy_rows(session, r.file) else "new"}
    path = source_dir / r.file
    try:
        cur_content = path.read_text(encoding="utf-8") if path.exists() else None
    except (OSError, UnicodeDecodeError):
        cur_content = None
    if cur_content is None:
        return {"status": "unchanged"}
    if _content_fingerprint(cur_content) != _content_fingerprint(row.content):
        return {"status": "changed"}
    return {"status": "unchanged"}


def _skip_by_state(session, r, source_dir, log, force_files=None) -> bool:
    """按文件状态决定是否跳过导入：changed 提示并跳过、unchanged 静默跳过。

    force_files（F-P2-06）：被强制重导入的文件即便 unchanged/changed 也不跳过
    （版本决策「采纳新版本」时复用整条 import_all 管道重导入该文件）。
    """
    if force_files and r.file in force_files:
        return False
    st = _file_import_state(session, r, source_dir)
    if st["status"] == "changed":
        log(f"   ⚠ {r.file}: 检测到内容变更，待版本决策流程处理（P2）；本次跳过")
        return True
    if st["status"] == "unchanged":
        return True
    return False


def _record_current_version(session, r, source_dir):
    """导入成功后记录当前内容版本（issue #68：递增版本号，旧版失活）。"""
    from datetime import datetime
    from sqlalchemy import func as _func, select as _sel
    from app.model import SourceFileVersion
    path = source_dir / r.file
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    prev = session.execute(
        _sel(SourceFileVersion).where(
            SourceFileVersion.file_path == r.file,
            SourceFileVersion.is_current.is_(True))
        .order_by(SourceFileVersion.version.desc()).limit(1)
    ).scalar_one_or_none()
    if prev is not None and _content_fingerprint(prev.content) == _content_fingerprint(content):
        return                                  # 同内容不重复记版
    next_v = (session.execute(
        _sel(_func.max(SourceFileVersion.version))
        .where(SourceFileVersion.file_path == r.file)
    ).scalar() or 0) + 1
    if prev is not None:
        prev.is_current = False
    session.add(SourceFileVersion(file_path=r.file, version=next_v, content=content,
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


# issue #211：basic_income 记录自带 stream_type，无需类别归一；仅 salary 保留兜底。
_CAT_STREAM = {"salary": "salary"}


@app.command()
def health(env: str = typer.Option(None, "--env")):
    """运行全库健康校验（H1-H5）并输出问题清单。"""
    from app.core.health import run_report, summarize
    with _session_for(env) as s:
        summ = summarize(s)
        typer.echo(f"[{_resolved_env(env)}] 健康校验汇总（H1-H5）：")
        for rule in ("H1", "H2", "H3", "H4", "H5"):
            x = summ.get(rule, {"total": 0})
            typer.echo(f"  {rule}: {x['total']} 项"
                       + (f"（warn {x.get('warn',0)} / crit {x.get('crit',0)}）" if x["total"] else " ✓"))
        for f in run_report(s):
            typer.echo(f"  [{f['rule']}/{f['level']}] {f['location']}: {f['detail']}")


@app.command()
def recompute(env: str = typer.Option(None, "--env"), from_year: int = typer.Option(1947, "--from")):
    """全库增量重算：从受影响起点年向后滚动账户余额（F-P0-12）。

    完成后写 recompute_job + recompute-done 通知（DESIGN §9.2 步骤3-4；issue #13）。
    """
    from app.core.recompute import recompute_all, record_recompute_done
    from app.core.snapshot import rebuild_snapshots
    with _session_for(env) as s:
        res = recompute_all(s, from_year)
        # §9.2c 重算后重建受影响起点起的快照（account/entity/family 三层，增量）
        rebuild_snapshots(s, from_year=from_year)   # issue #152：动态上限
        # §9.2 步骤3-4：写 job + 通知
        job = record_recompute_done(s, from_year, reason="recompute")
        s.commit()
        total_updated = sum(r["updated"] for r in res)
        typer.echo(f"[{_resolved_env(env)}] 重算 {len(res)} 个账户，更新 {total_updated} 行余额（自 {from_year} 起)"
                   f"；job#{job['job_id']} 通知#{job['notification_id']}")


@app.command()
def calendar(env: str = typer.Option(None, "--env"), as_of: str = typer.Option("2001-12-30", "--as-of")):
    """全局日历游标：按截至日期读取快照。"""
    from datetime import date
    from app.core.calendar import snapshot_as_of
    d = date.fromisoformat(as_of)
    with _session_for(env) as s:
        snaps = snapshot_as_of(s, d)
        typer.echo(f"[{_resolved_env(env)}] 截至 {d} 快照 {len(snaps)} 条：")
        for x in snaps:
            typer.echo(f"  {x['scope']}: {x['value']:,.0f} ({x['currency']})")


@app.command()
def wealth(env: str = typer.Option(None, "--env"), year: int = typer.Option(2001, "--year")):
    """财富曲线视图：某年家族合计(USD) + 各币种分项。

    汇率缺失币种不计入合计，并在底部显式告警（issue #2 修复：杜绝 1.0 静默 fallback）。
    """
    from app.core.wealth import wealth_series
    with _session_for(env) as s:
        w = wealth_series(s, year, year)
        d = w.get(year, {})
        typer.echo(f"[{_resolved_env(env)}] {year} 家族合计(展示USD) = {d.get('family_total_usd', 0):,.0f}")
        for cur, val in d.get("currencies", {}).items():
            typer.echo(f"   {cur}: {val:,.0f}")
        missing = d.get("missing_rates", [])
        if missing:
            typer.secho(
                f"   ⚠ 缺汇率未折算币种: {', '.join(missing)}（合计不含此部分）",
                fg=typer.colors.YELLOW,
            )


@app.command()
def snapshot(env: str = typer.Option(None, "--env"),
             from_year: int = typer.Option(1947, "--from",
                                            help="仅重建 from_year 起的快照（旧段保留）")):
    """重建逐年 as-of 快照（account/entity/family 三层；F-P0-08 + issue #12）。"""
    from app.core.snapshot import rebuild_snapshots
    with _session_for(env) as s:
        r = rebuild_snapshots(s, from_year=from_year)   # issue #152：动态上限
        s.commit()
        typer.echo(f"[{_resolved_env(env)}] 快照重建完成：{r['snapshots']} 条 / {r['accounts']} 账户 / {r['entities']} 实体聚合 / {r['family_years']} 家族合计年（自 {from_year} 起）")


@app.command()
def labor_baseline(env: str = typer.Option(None, "--env"),
                   office: str = typer.Option("", "--office",
                                              help="仅采集指定税率 office（缺省全部；"
                                                   "支持中文或 ISO 缩写 be/lu/nl/dk/se/uk）")):
    """用工成本基准落库（API② · F-P1-10）：工资(12地区)+CPI(12地区)+税率(12 office)。

    issue #218 起数据源为 用工成本/ 下 2 个汇总文件（12地CPI修正版 + 各国雇主社保税率
    逐年展开版），替换式落 labor_wage_benchmark/labor_cpi_growth/labor_tax_benchmark。
    """
    from app.config import get_config
    from app.ingest.labor_baseline import import_labor_baseline, import_tax
    # issue #144：ISO 缩写映射（源文件 office 键为中文；此前 --office be 静默 skipped）
    _OFFICE_ALIAS = {"be": "比利时", "lu": "卢森堡", "nl": "荷兰", "dk": "丹麦",
                     "se": "瑞典", "uk": "英国", "gb": "英国"}
    cfg = get_config(env)
    manifest_active = require_active_files(cfg.env, cfg)   # prod 门控；dev/test 返回 None
    with _session_for(env) as s:
        if office:
            office_key = _OFFICE_ALIAS.get(office.strip().lower(), office.strip())
            r = import_tax(s, cfg.source_dir, log=typer.echo, office_list=[office_key],
                           manifest_active=manifest_active)
        else:
            r = import_labor_baseline(s, cfg.source_dir, log=typer.echo,
                                      manifest_active=manifest_active)
        s.commit()
    for k, v in r.items():
        if isinstance(v, dict):
            typer.echo(f"[{_resolved_env(env)}] {k}: {v}")
        else:
            typer.echo(f"[{_resolved_env(env)}] {k}: {v}")


@app.command()
def search_index(env: str = typer.Option(None, "--env"),
                 source: str = typer.Option("", "--source", help="仅索引指定 source（缺省全部）")):
    """统一搜索索引构建（F-P1-08 · DESIGN §18）：条目→embedding 落 pgvector。

    omlx 未启动时抛错（LlmUnavailable）并提示，不落脏索引。后台/增量慢跑。
    """
    from app.search.indexer import build_index
    from app.core.llm import LlmUnavailable
    with _session_for(env) as s:
        try:
            r = build_index(s, source or None, log=typer.echo)
            s.commit()
        except LlmUnavailable as e:
            typer.secho(f"✗ {e}（请先启动本地 omlx:8000）", fg=typer.colors.RED)
            raise typer.Exit(1)
        typer.echo(f"[{_resolved_env(env)}] 索引完成：{r}")


@app.command()
def finance_backfill(env: str = typer.Option(None, "--env")):
    """F-P1-07 财务收支回填：把 issue #80 前已导入的 income_stream/家庭支出 镜像到 finance_entry。

    现有真实库数据早于 _mirror_to_finance，重浇灌幂等跳过 → 财务收支屏无数据；此命令补上。
    """
    from app.ingest.writer import backfill_finance_entries
    with _session_for(env) as s:
        r = backfill_finance_entries(s)
        s.commit()
        typer.echo(f"[{_resolved_env(env)}] 财务收支回填：收入 {r['income']}、支出 {r['expense']}"
                   f"（跳过 收入{r['skipped_income']}/支出{r['skipped_expense']}）")


@app.command("merge-alias-persons")
def merge_alias_persons_cmd(env: str = typer.Option(None, "--env"),
                            dry_run: bool = typer.Option(False, "--dry-run",
                                                         help="只报告将合并的名单，不写库")):
    """issue #136 存量修复：职称别名 person（养祖父/养父…）引用并入规范实体后删别名。

    writer 已接 TITLE_ENTITY 归一；本命令收口历史数据（收益挂别名、账户挂规范名的分裂）。
    合并 >0 时自动重算 + 重建快照。
    """
    from app.core.entity_merge import merge_alias_persons
    with _session_for(env) as s:
        r = merge_alias_persons(s, log=typer.echo, dry_run=dry_run)
        if dry_run:
            typer.echo(f"[{_resolved_env(env)}] {r}")
            return
        merged = r.get("merged", 0)
        if merged:
            from app.core.recompute import recompute_all
            from app.core.snapshot import rebuild_snapshots
            recompute_all(s, 1947)
            rebuild_snapshots(s)   # issue #152：全量重建走 calendar_years() 动态上限
        s.commit()
    typer.echo(f"[{_resolved_env(env)}] 别名实体合并完成：{r}"
               + ("；已重算+重建快照" if merged else ""))


@app.command()
def events_movie(env: str = typer.Option(None, "--env")):
    """F-P2-01 事件·电影导入：扫 基准/事件/电影/ → 解析 → 落库 movie_event（幂等 upsert）。"""
    from app.ingest.parsers.event_movie import parse_event_movie
    from app.ingest.writer import import_movie_events
    cfg = get_config(env)
    active = require_active_files(cfg.env, cfg)   # prod 门控；dev/test 返回 None
    base = cfg.source_dir / "基准" / "事件" / "电影"
    if not base.exists():
        typer.echo(f"[{_resolved_env(env)}] 无电影事件目录: {base}")
        return
    all_records = []
    for f in sorted(base.glob("*.md")):
        if active is not None and f.relative_to(cfg.source_dir).as_posix() not in active:
            continue
        all_records.extend(parse_event_movie(f))
    with _session_for(env) as s:
        r = import_movie_events(s, all_records)
        s.commit()
    typer.echo(f"[{_resolved_env(env)}] 电影事件导入 {len(all_records)} 条；新增 {r['inserted']} 跳过 {r['skipped']}")


@app.command()
def events_stock(env: str = typer.Option(None, "--env")):
    """F-P2-02 事件·股票导入：扫 基准/事件/股票/ 顶层 USD Style A → 解析 → 落库 stock_event（幂等）。

    阶段一只接受 USD 流水表（虎牙/哔哩等根级 *.md）；快手/香港/英国（万港元/万英镑）与
    收购/ 子目录（分拆并购链，F-P2-03/04）本轮跳过。导入不关联账户，UI 同币种手动关联补 ledger。
    """
    from app.ingest.parsers.event_stock import parse_event_stock
    from app.ingest.writer import import_stock_events
    cfg = get_config(env)
    active = require_active_files(cfg.env, cfg)   # prod 门控；dev/test 返回 None
    base = cfg.source_dir / "基准" / "事件" / "股票"
    if not base.exists():
        typer.echo(f"[{_resolved_env(env)}] 无股票事件目录: {base}")
        return
    all_records = []
    for f in sorted(base.glob("*.md")):   # 仅顶层否（收购/英国/香港 子目录本轮跳过）
        if active is not None and f.relative_to(cfg.source_dir).as_posix() not in active:
            continue
        all_records.extend(parse_event_stock(f))
    # §11.4 冲突检测：按 source_file 分组，逐文件跑 stock 冲突，blocked 文件不入库（F-P2-04）
    from app.ingest import conflict
    by_file: dict[str, list[dict]] = {}
    for rec in all_records:
        by_file.setdefault(rec.get("source_file") or "?" , []).append(rec)
    ok_records: list[dict] = []
    blocked = 0
    with _session_for(env) as s:
        for src, recs in by_file.items():
            crep = conflict.check_stock_event_conflict(s, src, recs)
            if crep.blocked:
                blocked += 1
                for p in crep.problems:
                    typer.echo(f"  ❌ [{p['rule']}] {src}: {p['detail']}")
                _record_findings(s, src, crep)   # issue #118
                continue
            _record_findings(s, src, crep)
            ok_records.extend(recs)
        r = import_stock_events(s, ok_records)
        s.commit()
    typer.echo(f"[{_resolved_env(env)}] 股票事件解析 {len(all_records)} 条；新增 {r['inserted']} 跳过 {r['skipped']}"
               f"；阻塞文件 {blocked}")


@app.command()
def timeline_defaults(env: str = typer.Option(None, "--env"),
                      rebuild: bool = typer.Option(False, "--rebuild",
                                                   help="先清除已生成的默认事件再重建（不触碰手工/overlay 行）")):
    """F-P2+-04 时间线默认事件：按导入数据生成（只加首次投资/发生 + 每年 R1-5 投资）。

    `Design_Folder/时间线.md` 已清空为占位；时间线改由本命令据 DB 自动生成默认事件。
    独立命令，数据整理员在每次导入后手动运行；幂等合并（重跑只并集）。
    """
    from app.core.timeline_defaults import derive_default_timeline
    with _session_for(env) as s:
        r = derive_default_timeline(s, rebuild=rebuild, log=typer.echo)
        s.commit()
    typer.echo(f"[{_resolved_env(env)}] 时间线默认事件：新增 {r['inserted']} / 跳过 {r['skipped']}（共 {r['total']}）")


if __name__ == "__main__":
    app()
