"""issue #216：家庭支出修正版导入——parser 干扰表排除 + writer upsert。

- parser：修正版结构（前置计列项目/CPI 验证/§六累计等非逐年表，首格非「年份」）
  不干扰逐年支出表定位；千分位逗号金额正常解析。
- writer upsert：(account, date, reason='家庭支出') 同键行金额/来源有变则更新
  ledger 并同步 finance_entry 镜像；无变化不写（二轮 n=0）；金额修订不再同年插重复行。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.ingest.parsers import parse_household_expense

# 修正版结构缩影：前置说明表（首格「项目」「区间」）+ 逐年支出表（千分位逗号）+ 累计表
_REVISION = """# 1974–2001 比利时SPA镇Peeters家族年度支出（修正版）

## 一、基准设定（基期：1997年，单位：BEF，不变价）

| 项目 | 1997年不变价（BEF） | 说明 |
|---|---:|---|
| 刚性固定 | 3,400,000 | 安保、庄园修缮、保险 |
| 贵族社交 | 1,600,000 | 家宴、俱乐部 |

### CPI精确性验证

| 区间 | 文件隐含累积通胀 | 世行数据累积通胀 | 偏差 |
|---|---:|---:|---:|
| 1974→1997 | 2.7996× | 2.7996× | 0.00% |

## 四、1974–2001 家庭年度支出（名义BEF，经BEL CPI通胀折算）

| 年份 | 分段 | N_A | N_M | 刚性固定 | 贵族社交 | 未成年教育 | 高等教育专项 | 年度总支出 |
|---|---|---|---|---:|---:|---:|---:|---:|
| 1974 | S1c | 3 | 0 | 1,214,470 | 571,515 | 0 | 0 | 1,785,985 |
| 1975 | S1c | 3 | 0 | 1,369,557 | 644,498 | 0 | 0 | 2,014,055 |

## 六、28年累计支出

| 项目 | 累计金额（BEF） | 折合1997年不变价（BEF） |
|---|---:|---:|
| 刚性固定 | 75,074,847 | 79,560,000 |
| **28年总计** | **146,888,994** | **155,300,000** |
"""


def test_parse_revision_skips_aux_tables(tmp_path):
    f = tmp_path / "1974-2001家庭支出.md"
    f.write_text(_REVISION, encoding="utf-8")
    recs, warnings = parse_household_expense(f)

    assert warnings == []
    assert len(recs) == 2                       # 仅逐年表 2 行；CPI/累计表未混入
    assert {(r["year"], r["amount"]) for r in recs} == {
        (1974, 1785985.0), (1975, 2014055.0)}  # 千分位逗号正确解析
    assert all(r["currency"] == "BEF" and r["holder"] == "Henri Peeters" for r in recs)
    assert all(r["source_file"] == f.name for r in recs)   # issue #216：溯源随记录落库


@pytest.fixture()
def session():
    from sqlalchemy import BigInteger, Integer
    from app.model import Base
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _recs(year_amounts, source_file):
    return [{"holder": "Henri Peeters", "year": y, "amount": float(a),
             "currency": "BEF", "source_file": source_file}
            for y, a in year_amounts]


def test_household_expense_upsert(session):
    from app.ingest.writer import import_household_expense
    from app.model import FinanceEntry, LedgerEntry

    def counts():
        led = session.execute(select(func.count()).select_from(LedgerEntry)).scalar()
        fin = session.execute(
            select(func.count()).select_from(FinanceEntry).where(
                FinanceEntry.kind == "expense")).scalar()
        return led, fin

    r1 = import_household_expense(session, _recs([(1974, 100), (1975, 200)], "旧版.md"))
    assert r1["n"] == 2 and r1["updated"] == 0 and r1["skipped"] == 0
    assert counts() == (2, 2)

    # 同值重导：幂等，0 写入
    r2 = import_household_expense(session, _recs([(1974, 100), (1975, 200)], "旧版.md"))
    assert r2["n"] == 0 and r2["updated"] == 0 and r2["skipped"] == 2
    assert counts() == (2, 2)

    # 修正版替换（金额不变、来源变）：ledger + 镜像溯源刷新，不插重复行
    r3 = import_household_expense(session, _recs([(1974, 100), (1975, 200)], "家庭支出.md"))
    assert r3["updated"] == 2 and r3["n"] == 0
    assert counts() == (2, 2)
    led_srcs = {x[0] for x in session.execute(
        select(LedgerEntry.source_file).where(LedgerEntry.reason == "家庭支出")).all()}
    fin_srcs = {x[0] for x in session.execute(
        select(FinanceEntry.source_file).where(FinanceEntry.label == "家庭支出")).all()}
    assert led_srcs == fin_srcs == {"家庭支出.md"}

    # 金额修订：同键更新（旧 DO NOTHING 语义会同年插第二笔支出）
    r4 = import_household_expense(session, _recs([(1974, 110), (1975, 200)], "家庭支出.md"))
    assert r4["updated"] == 1 and r4["skipped"] == 1
    assert counts() == (2, 2)
    got = session.execute(
        select(LedgerEntry.outflow).where(
            LedgerEntry.reason == "家庭支出", LedgerEntry.date.like("1974%"))
    ).scalar_one()
    assert float(got) == 110.0
    mir = session.execute(
        select(FinanceEntry.amount).where(
            FinanceEntry.label == "家庭支出", FinanceEntry.year == 1974)
    ).scalar_one()
    assert float(mir) == 110.0
