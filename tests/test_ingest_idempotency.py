"""issue #68：ingest 幂等回归保护。

核心断言：同一份源目录连续两次 import_all —— 第二次零新增行、无冲突拦截，
各表行数不变；文件内容变更后进入「changed 跳过」而非重复导入；
版本记录递增且旧版失活。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.model import (
    Account, Base, Entity, IncomeStream, InitialAsset, LedgerEntry,
    Snapshot, SourceFileVersion, TimelineEvent,
)
from app.ingest.main import _file_import_state, _record_current_version, import_all


@pytest.fixture()
def session():
    # issue #21 模式：SQLite 无 BigInteger 自增主键，建表前降级为 Integer
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def source_dir(tmp_path):
    """最小可解析源树：人物 / 银行 / 初始资产 / 家庭支出 / 薪资。"""
    root = tmp_path
    (root / "人物").mkdir(parents=True)
    (root / "人物" / "Henri Peeters.md").write_text(
        "- 姓名：Henri Peeters\n- 角色：养祖父\n", encoding="utf-8")
    # 薪资冲突检测经 TITLE_ENTITY 别名归一（养父→Joren Peeters），需该实体先在
    (root / "人物" / "Joren Peeters.md").write_text(
        "- 姓名：Joren Peeters\n- 与主角的关系：养父\n", encoding="utf-8")
    (root / "经济" / "银行").mkdir(parents=True)
    (root / "经济" / "银行" / "祖父.md").write_text(
        "# 开户行：测试银行\n\n"
        "## 一、BEF（祖父）\n\n"
        "| 日期 | 理由 | 收入 | 支出 | 余额 | 备注 |\n"
        "|---|---|---|---|---|---|\n"
        "| 1990-03-15 | 测试存入 | 1,000 |  | 1,000 |  |\n"
        "| 1990-06-01 | 测试支取 |  | 300 | 700 |  |\n",
        encoding="utf-8")
    (root / "基准" / "初始资产").mkdir(parents=True)
    (root / "基准" / "初始资产" / "祖父.md").write_text(
        "- 现金：2,000,000 BEF\n", encoding="utf-8")
    (root / "基准" / "薪资").mkdir(parents=True)
    (root / "基准" / "薪资" / "养父薪资.md").write_text(
        "| 年份 | 税后收入 | 币种 |\n|---|---|---|\n| 1990 | 800,000 | BEF |\n",
        encoding="utf-8")
    (root / "基准" / "1974-2001家庭支出.md").write_text(
        "| 年份 | 年度总支出 | 币种 |\n|---|---|---|\n| 1990 | 50,000 | BEF |\n",
        encoding="utf-8")
    return root


def _counts(s) -> dict:
    def n(model):
        return s.execute(select(func.count()).select_from(model)).scalar() or 0
    return {
        "entity": n(Entity), "account": n(Account),
        "ledger": n(LedgerEntry), "income": n(IncomeStream),
        "initial_asset": n(InitialAsset), "timeline": n(TimelineEvent),
        "snapshot": n(Snapshot), "sfv": n(SourceFileVersion),
    }


def test_double_ingest_idempotent(session, source_dir):
    st1 = import_all(session, source_dir)
    assert st1["blocked"] == 0, st1["summary"]
    c1 = _counts(session)
    # 首轮应实际落库：银行 2 行 + 初始现金 1 笔 + 薪资 1 条 + 家庭支出 1 条
    assert st1["ledger"] == 2          # 仅银行流水（初始现金计入 cash）
    assert st1["cash"] == 1
    assert st1["salary"] == 1
    assert st1["household"] == 1

    st2 = import_all(session, source_dir)
    assert st2["blocked"] == 0, st2["summary"]
    assert _counts(session) == c1      # 行数零增长
    assert st2["ledger"] == 0 and st2["cash"] == 0
    assert st2["salary"] == 0 and st2["household"] == 0


def test_writer_level_natural_key_dedupe(session, source_dir):
    """绕过文件级指纹，直接重放 writer：自然键去重兜底生效（兼容存量库）。"""
    import_all(session, source_dir)
    c1 = _counts(session)
    from app.ingest import parsers, writer
    recs = parsers.parse_initial_asset(source_dir / "基准" / "初始资产" / "祖父.md")
    for r in recs:
        r.setdefault("source_file", None)   # 模拟旧库无 source_file 场景
    st = writer.import_initial_assets(session, recs)
    assert st["cash_skipped"] == 1 and st["cash"] == 0

    hh = parsers.parse_household_expense(source_dir / "基准" / "1974-2001家庭支出.md")[0]
    st2 = writer.import_household_expense(session, hh)
    assert st2["skipped"] == 1 and st2["n"] == 0
    assert _counts(session) == c1


def test_changed_file_skipped_and_version_flow(session, source_dir):
    st1 = import_all(session, source_dir)
    assert st1["blocked"] == 0
    c1 = _counts(session)

    bank_rel = "经济/银行/祖父.md"
    bank_path = source_dir / bank_rel
    det = type("R", (), {"file": bank_rel})()

    # 内容未变 → unchanged（经 SourceFileVersion 哈希比对）
    assert _file_import_state(session, det, source_dir)["status"] == "unchanged"

    # 内容变更 → changed（不导入、不记新版本）
    bank_path.write_text(
        bank_path.read_text(encoding="utf-8").replace("测试存入", "测试存入改"),
        encoding="utf-8")
    assert _file_import_state(session, det, source_dir)["status"] == "changed"
    st2 = import_all(session, source_dir)
    assert st2["blocked"] == 0 and st2["ledger"] == 0
    assert _counts(session) == c1       # 数据保持上一版

    # 模拟 P2「采纳新版本」：直接记新内容版本 → v2、v1 失活、状态回到 unchanged
    _record_current_version(session, det, source_dir)
    versions = session.execute(
        select(SourceFileVersion).where(SourceFileVersion.file_path == bank_rel)
        .order_by(SourceFileVersion.version)
    ).scalars().all()
    assert [v.version for v in versions] == [1, 2]
    assert [v.is_current for v in versions] == [False, True]
    assert _file_import_state(session, det, source_dir)["status"] == "unchanged"


def test_legacy_rows_without_version_treated_unchanged(session, source_dir):
    """存量库兼容：有数据行但无版本记录 → 保守 unchanged，防双计。"""
    from app.model import IncomeStream as IS
    session.add(IS(entity_id=1, stream_type="salary", currency="BEF",
                   year=1990, amount=1, source_file="基准/薪资/养父薪资.md"))
    session.flush()
    det = type("R", (), {"file": "基准/薪资/养父薪资.md"})()
    assert _has_no_sfv(session)
    assert _file_import_state(session, det, source_dir)["status"] == "unchanged"


def _has_no_sfv(session) -> bool:
    return session.execute(
        select(func.count()).select_from(SourceFileVersion)).scalar() == 0
