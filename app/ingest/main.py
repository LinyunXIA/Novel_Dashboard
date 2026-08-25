"""Ingest CLI（DESIGN §3/§6）。

用法：
    python -m app.ingest.main --env dev            # 默认
    python -m app.ingest.main --env prod --full    # 全量重建
    python -m app.ingest.main ping                 # DB 连通自检

F-P0-01：骨架（config/db/CLI）；F-P0-02..06 落库链路。

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

app = typer.Typer(help="Novel Dashboard ingest")


def _session_for(env: str):
    """按 `--env` 构造 sessionmaker（with-block 兼容 SessionLocal 接口）。"""
    return make_sessionmaker(env)()


@app.command()
def ping(env: str = typer.Option("dev", "--env")):
    """数据库连通自检（按 `--env` 真正打到对应 DSN）。"""
    cfg = get_config(env)
    ok = check_connection_for(env)
    typer.echo(f"[{cfg.env}] dsn={cfg.dsn}")
    typer.secho("连接成功" if ok else "连接失败", fg=typer.colors.GREEN if ok else typer.colors.RED)


@app.command()
def run(
    env: str = typer.Option("dev", "--env"),
    full: bool = typer.Option(False, "--full", help="全量重建"),
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
    env: str = typer.Option("dev", "--env"),
):
    """F-P0-04..06 落库：从 Design_Folder（source_dir）读取基础数据并入库。

    Session 严格按 `--env` 构造（issue #3）；核心循环见 import_all。
    """
    cfg = get_config(env)
    with _session_for(env) as s:
        stats = import_all(s, cfg.source_dir, log=typer.echo)
        s.commit()          # issue #68：import_all 只 flush；commit 由命令层负责（勿丢）
    typer.echo(f"[{cfg.env}] {stats['summary']}")


def import_all(session, source_dir, log=None) -> dict:
    """扫描 source_dir → 解析 → 冲突检测 → 落库 → 重算快照（F-P0-02..06 主链路）。

    issue #68 抽取自原 ingest 命令体以便测试；幂等语义：
    - 所有文件类目经 _file_import_state 判定 new/unchanged/changed，
      unchanged 静默跳过、changed 提示待 P2 版本决策并跳过；
    - writer 层另有自然键去重兜底（初始现金 / 家庭支出），兼容无版本记录的存量库。
    commit 前整批事务，失败回滚（由调用方管理 session/commit）。
    """
    log = log or (lambda msg: print(msg))
    ck = 0; ia = {"asset": 0, "cash": 0}; sec = 0; rent = 0; prop = 0; shop = 0
    sal = 0; he = 0; rcur = 0; fx_total = 0; tl_n = 0; bank_n = 0; bank_seg_skip = 0
    blocked_files = 0
    soft_warnings = 0
    job: dict = {}
    fx_files = []
    rep = run_ingest(source_dir)
    # DESIGN §6.5 摄入顺序锁死：人物(entity) 先入 → 初始资产 → 收益/薪资/支出 → 银行。
    # 收益挂账依赖 entity_id，不能按文件名字典序处理（issue #68）。
    _ORDER = ("character", "return_table", "timeline", "initial_asset",
              "income_security", "income_rent", "income_property",
              "income_shop", "salary", "household_expense", "bank")
    ordered = sorted(
        rep.ok,
        key=lambda r: (_ORDER.index(r.category) if r.category in _ORDER else 99, r.file),
    )
    for r in ordered:
        if r.category == "stock_tx" and r.records:
            # issue #70：股票台账解析成功但持仓/事件落库属 Phase 2（DESIGN §19.6），显式说明而非静默
            log(f"   ⏭ {r.file}: 股票台账解析成功（{len(r.records)} 组基本信息/"
                f"{sum(len(x.get('events') or []) for x in r.records)} 条明细），"
                f"持仓/事件落库属 Phase 2（§19.6），本次跳过")
            continue
        if r.category == "character" and r.records:
            ck += writer.import_characters(session, r.records, r.file)["imported"]
        if r.category == "return_table" and r.records:
            rcur += writer.import_return_curves(session, r.records)["n"]
        if r.category == "fx" and r.records:
            fx_files.append(r)        # 收集，权威优先 + 冲突检测在下方统一处理
        if r.category == "timeline" and r.records:
            tl_n += writer.import_timeline(session, r.records)["n"]
        if r.category == "bank" and r.records:
            src = r.file
            if _skip_by_state(session, r, source_dir, log):
                continue
            crep = conflict.check_bank_import_conflict(session, src, r.records)
            if crep.blocked:
                blocked_files += 1
                for p in crep.problems:
                    log(f"   ❌ {src}: [{p['rule']}] {p['line']}: {p['detail']}")
                continue
            soft_warnings += _log_soft(log, src, crep)
            st = writer.import_bank(session, r.records, source_file=src)
            bank_n += st["ledger"]; bank_seg_skip += st["skipped"]
            _record_current_version(session, r, source_dir)
        if r.category == "initial_asset" and r.records:
            if _skip_by_state(session, r, source_dir, log):
                continue
            st = writer.import_initial_assets(session, r.records)
            ia["asset"] += st["asset"]; ia["cash"] += st["cash"]
            _record_current_version(session, r, source_dir)
        if r.category in ("income_security", "income_rent", "income_property",
                          "income_shop", "salary") and r.records:
            if _skip_by_state(session, r, source_dir, log):
                continue
            crep = conflict.check_income_stream_conflict(
                session, r.file, _normalize_conflict_recs(r.category, r.records))
            # issue #72：H1 增量瘦版（收益年份 vs 编年史覆盖）随 H2 一并预检
            crep.merge(conflict.check_timeline_alignment(
                session, r.file, _normalize_conflict_recs(r.category, r.records)))
            if crep.blocked:
                blocked_files += 1
                for p in crep.problems:
                    log(f"   ❌ {r.file}: [{p['rule']}] {p['line']}: {p['detail']}")
                continue
            soft_warnings += _log_soft(log, r.file, crep)
            for rec in r.records:
                rec.setdefault("source_file", r.file)
            if r.category == "income_security": sec += writer.import_income_security(session, r.records)["stream"]
            if r.category == "income_rent": rent += writer.import_income_rent(session, r.records)["stream"]
            if r.category == "income_property": prop += writer.import_income_property(session, r.records)["stream"]
            if r.category == "income_shop": shop += writer.import_income_shop(session, r.records)["stream"]
            if r.category == "salary": sal += writer.import_salary(session, r.records)["stream"]
            _record_current_version(session, r, source_dir)
        if r.category == "household_expense" and r.records:
            if _skip_by_state(session, r, source_dir, log):
                continue
            he += writer.import_household_expense(session, r.records)["n"]
            _record_current_version(session, r, source_dir)
    # —— 汇率两轮：权威文件(W全量)先入库为基准；其它 fx 文件检测与权威冲突，冲突则拦 ——
    authority = [r for r in fx_files if conflict.is_authority_fx(r.file)]
    others = [r for r in fx_files if not conflict.is_authority_fx(r.file)]
    for r in authority:
        fx_total += writer.import_fx(session, r.records)["n"]
    for r in others:
        crep = conflict.check_fx_authority_conflict(session, r.file, r.records)
        # issue #72：H3 链式闭合增量预检（新汇率 ∪ DB 视图，两跳 vs 直接 >0.5% → 挡）
        crep.merge(conflict.check_fx_chain_closure(session, r.file, r.records))
        if crep.blocked:
            log(f"   ⚠ fx冲突拦截 {r.file}: {len(crep.problems)} 处（以权威表为准）")
            blocked_files += 1
            continue
        soft_warnings += _log_soft(log, r.file, crep)
        fx_total += writer.import_fx(session, r.records)["n"]
    cc = writer.close_2002_currency(session)
    closed = cc["closed"]
    # —— DESIGN §9 摄入因果链尾巴：增量重算 + 重建快照 + recompute-done 通知（issue #13）——
    if not blocked_files:
        from app.core.recompute import recompute_all, record_recompute_done
        from app.core.snapshot import rebuild_snapshots as _rebuild
        recompute_all(session, 1947)
        _rebuild(session, range(1947, 2026), from_year=1947)
        job = record_recompute_done(session, 1947, reason="ingest")
    session.flush()
    summary = (
        f"落库完成：人物 {ck}、初始资产 {ia['asset']}、现金 {ia['cash']}、票息 {sec}、"
        f"租房 {rent}、经营房 {prop}、开店 {shop}、薪资 {sal}、家庭支出 {he}、"
        f"收益曲线 {rcur}、汇率 {fx_total}、时间线 {tl_n}、银行流水 {bank_n}"
        f"（seg 跳过 {bank_seg_skip}）、2002关池 {closed}"
        f"（EUR承接 {cc['migrated']} / 零结转跳过 {cc['skipped_zero']}）、冲突拦截 {blocked_files}"
        + (f"；recompute job#{job['job_id']} 通知#{job['notification_id']}" if job else "")
        + (f"；软警告 {soft_warnings}" if soft_warnings else "")
    )
    return {"summary": summary, "blocked": blocked_files, "soft_warnings": soft_warnings,
            "characters": ck, "initial_assets": ia["asset"], "cash": ia["cash"],
            "security": sec, "rent": rent, "property": prop, "shop": shop,
            "salary": sal, "household": he, "return_curves": rcur, "fx": fx_total,
            "timeline": tl_n, "ledger": bank_n, "job": job}


# ---- 收益/银行文件的幂等 + 内容变更提示（issue #14；issue #68 通用化） ----

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


def _skip_by_state(session, r, source_dir, log) -> bool:
    """按文件状态决定是否跳过导入：changed 提示并跳过、unchanged 静默跳过。"""
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


_CAT_STREAM = {"income_security": "security", "income_rent": "rent",
               "income_property": "property", "income_shop": "shop", "salary": "salary"}


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
        typer.echo(f"[{env}] 重算 {len(res)} 个账户，更新 {total_updated} 行余额（自 {from_year} 起)"
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


@app.command()
def labor_baseline(env: str = typer.Option("dev", "--env"),
                   office: str = typer.Option("", "--office", help="仅采集指定税率 office（缺省全部）")):
    """用工成本基准落库（API② · F-P1-10）：工资(10区)+CPI(10区)+税率(12 office)。

    从 Design_Folder 解析三份基准 → labor_wage_benchmark/labor_cpi_growth/labor_tax_benchmark。
    """
    from app.config import get_config
    from app.ingest.labor_baseline import import_labor_baseline, import_wage, import_cpi, import_tax
    cfg = get_config(env)
    with _session_for(env) as s:
        if office:
            r = import_tax(s, cfg.source_dir, log=typer.echo, office_list=[office])
        else:
            r = import_labor_baseline(s, cfg.source_dir, log=typer.echo)
        s.commit()
    for k, v in r.items():
        if isinstance(v, dict):
            typer.echo(f"[{env}] {k}: {v}")
        else:
            typer.echo(f"[{env}] {k}: {v}")


@app.command()
def search_index(env: str = typer.Option("dev", "--env"),
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
        typer.echo(f"[{env}] 索引完成：{r}")


@app.command()
def finance_backfill(env: str = typer.Option("dev", "--env")):
    """F-P1-07 财务收支回填：把 issue #80 前已导入的 income_stream/家庭支出 镜像到 finance_entry。

    现有真实库数据早于 _mirror_to_finance，重浇灌幂等跳过 → 财务收支屏无数据；此命令补上。
    """
    from app.ingest.writer import backfill_finance_entries
    with _session_for(env) as s:
        r = backfill_finance_entries(s)
        s.commit()
        typer.echo(f"[{env}] 财务收支回填：收入 {r['income']}、支出 {r['expense']}"
                   f"（跳过 收入{r['skipped_income']}/支出{r['skipped_expense']}）")


@app.command()
def events_movie(env: str = typer.Option("dev", "--env")):
    """F-P2-01 事件·电影导入：扫 基准/事件/电影/ → 解析 → 落库 movie_event（幂等 upsert）。"""
    from app.ingest.parsers.event_movie import parse_event_movie
    from app.ingest.writer import import_movie_events
    cfg = get_config(env)
    base = cfg.source_dir / "基准" / "事件" / "电影"
    if not base.exists():
        typer.echo(f"[{env}] 无电影事件目录: {base}")
        return
    all_records = []
    for f in sorted(base.glob("*.md")):
        all_records.extend(parse_event_movie(f))
    with _session_for(env) as s:
        r = import_movie_events(s, all_records)
        s.commit()
    typer.echo(f"[{env}] 电影事件导入 {len(all_records)} 条；新增 {r['inserted']} 跳过 {r['skipped']}")


@app.command()
def events_stock(env: str = typer.Option("dev", "--env")):
    """F-P2-02 事件·股票导入：扫 基准/事件/股票/ 顶层 USD Style A → 解析 → 落库 stock_event（幂等）。

    阶段一只接受 USD 流水表（虎牙/哔哩等根级 *.md）；快手/香港/英国（万港元/万英镑）与
    收购/ 子目录（分拆并购链，F-P2-03/04）本轮跳过。导入不关联账户，UI 同币种手动关联补 ledger。
    """
    from app.ingest.parsers.event_stock import parse_event_stock
    from app.ingest.writer import import_stock_events
    cfg = get_config(env)
    base = cfg.source_dir / "基准" / "事件" / "股票"
    if not base.exists():
        typer.echo(f"[{env}] 无股票事件目录: {base}")
        return
    all_records = []
    for f in sorted(base.glob("*.md")):   # 仅顶层否（收购/英国/香港 子目录本轮跳过）
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
                continue
            ok_records.extend(recs)
        r = import_stock_events(s, ok_records)
        s.commit()
    typer.echo(f"[{env}] 股票事件解析 {len(all_records)} 条；新增 {r['inserted']} 跳过 {r['skipped']}"
               f"；阻塞文件 {blocked}")


if __name__ == "__main__":
    app()
