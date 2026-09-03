"""issue #214：全球五地 R1-R5 整合文件（史实版，Markdown 表格型）。

- parser 分流：`## x、XX市场（…）` 节 → 表格分支（节标题定地区、仅 x.4 逐年表、
  表头定位 R1-R5、背景列忽略；x.5 复合年化 / §六 横向对比 / 0.2 验证表不入库）；
  旧分地区格式（年份标题 + R 行/pair）仍走旧分支，且旧文件「风险分级定义
  （…资产市场）」类标题不得误触分流。
- writer upsert：新键插入；同键 rate/source_file 有变则更新、无变化不写
  （第二轮 n=0）；整合文件取代时 source_file 溯源刷新、数值修订可落地。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ingest.parsers import parse_return_table

_GLOBAL = """# 全球五地R1-R5投资风险分级收益测算（史实版）（测试）

## 〇、数据精度与史实验证说明

### 0.2 关键年份验证（R4宽基股票，与真实历史指数对比）

| 市场 | 年份 | 本文R4 | 真实指数全收益 | 误差 |
|---|---|---:|---:|---:|
| 欧洲 | 1947 | 99.90 | 26.0 | 73.9 |

## 一、欧洲市场（1947–2025）

### 1.4 逐年收益率（%）

| 年份 | R1 | R2 | R3 | R4 | R5 | 背景 |
|---|---:|---:|---:|---:|---:|---|
| 1947 | 14.8 | -11.2 | 18.4 | 26.7 | 42.1 | 战后重建启动 |
| 1948 | 10.0 | 5.0 | 8.0 | 9.0 | 10.0 | 复苏 |

### 1.5 分阶段复合年化（%）

| 周期 | 年份 | R1 | R2 | R3 | R4 | R5 |
|---|---|---:|---:|---:|---:|---:|
| 1947–1950 | 4 | 11.0 | 2.0 | 13.0 | 17.0 | 25.0 |

## 四、香港市场（1999–2025）

### 4.4 逐年收益率（%）

| 年份 | R1 | R2 | R3 | R4 | R5 | 背景 |
|---|---:|---:|---:|---:|---:|---|
| 2006 | 4.11 | 6.16 | 10.79 | 125.41 | 34.25 | 港股史诗级牛市 |

## 六、五地全周期横向对比

### 6.1 全周期复合年化收益对比（%）

| 市场 | R1 | R2 | R3 | R4 | R5 |
|---|---:|---:|---:|---:|---:|
| 欧洲 | 5.0 | 6.0 | 9.0 | 11.0 | 14.0 |
"""

# 旧分地区格式：标题含「市场」二字但非「XX市场」节，不得误触分流
_OLD_HK = """# 1999-2025 香港R1-R5投资风险分级收益测算表（测试）

## 一、风险分级定义（港币计价，离岸跨境资产市场）

- R1：低风险
- R5：高风险

## 四、逐年年化收益率明细

#### 1999（亚洲金融风暴复苏）

R1：4.35｜R2：6.12｜R3：8.64｜R4：10.0｜R5：12.0
"""


def _unpack(out):
    return (out, []) if isinstance(out, list) else out


def test_parse_global_table(tmp_path):
    f = tmp_path / "全球五地R1-R5投资风险分级收益测算（史实版）.md"
    f.write_text(_GLOBAL, encoding="utf-8")
    recs, warnings = _unpack(parse_return_table(f))

    assert warnings == []
    assert len(recs) == 15                      # 欧洲 2 年 + 香港 1 年，各 5 档
    by = {(r["country"], r["risk_lvl"], r["year"]): r["rate"] for r in recs}
    # 欧洲 1947：含负数、一位小数；R4 取 x.4 的 26.7 而非 0.2 验证表的 99.90
    assert by[("欧洲", "R1", 1947)] == 14.8
    assert by[("欧洲", "R2", 1947)] == -11.2
    assert by[("欧洲", "R4", 1947)] == 26.7
    assert by[("欧洲", "R5", 1948)] == 10.0
    # 香港 2006：两位小数
    assert by[("香港", "R4", 2006)] == 125.41
    assert by[("香港", "R1", 2006)] == 4.11
    # x.5 附录（1947–1950 周期行）与 §六 对比表（市场行）均未混入
    assert ("欧洲", "R1", 1950) not in by
    assert all(r["source_file"] == f.name for r in recs)


def test_old_format_not_misrouted(tmp_path):
    f = tmp_path / "1999-2025 香港R1-R5投资风险分级收益测算表.md"
    f.write_text(_OLD_HK, encoding="utf-8")
    recs, warnings = _unpack(parse_return_table(f))

    assert warnings == []
    assert len(recs) == 5
    assert {(r["country"], r["year"]) for r in recs} == {("香港", 1999)}
    assert {r["risk_lvl"] for r in recs} == {"R1", "R2", "R3", "R4", "R5"}


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


def test_import_return_curves_upsert(session):
    from app.ingest.writer import import_return_curves
    from app.model import ReturnCurve

    recs = [{"country": "欧洲", "risk_lvl": f"R{i}", "year": 1947,
             "rate": float(i), "source_file": "1947-2025 欧洲.md"}
            for i in range(1, 3)]
    r1 = import_return_curves(session, recs)
    assert r1["inserted"] == 2 and r1["updated"] == 0 and r1["n"] == 2

    # 同值重导：幂等，0 写入
    r2 = import_return_curves(session, [dict(x) for x in recs])
    assert r2["n"] == 0 and r2["inserted"] == 0 and r2["updated"] == 0

    # 整合文件取代：同键、source_file 变、rate 不变 → 仅溯源刷新
    merged = [{"country": "欧洲", "risk_lvl": f"R{i}", "year": 1947,
               "rate": float(i), "source_file": "全球五地.md"}
              for i in range(1, 3)]
    r3 = import_return_curves(session, merged)
    assert r3["inserted"] == 0 and r3["updated"] == 2 and r3["n"] == 2
    rows = session.query(ReturnCurve).filter_by(country="欧洲").all()
    assert len(rows) == 2
    assert {x.source_file for x in rows} == {"全球五地.md"}
    assert sorted(x.rate for x in rows) == [1.0, 2.0]

    # 史实数值修订：rate 变更可落地（DO NOTHING 时代会静默丢失）
    merged[0]["rate"] = 99.9
    r4 = import_return_curves(session, merged)
    assert r4["updated"] == 1 and r4["inserted"] == 0
    got = session.query(ReturnCurve).filter_by(
        country="欧洲", risk_lvl="R1", year=1947).one()
    assert got.rate == 99.9 and got.source_file == "全球五地.md"
