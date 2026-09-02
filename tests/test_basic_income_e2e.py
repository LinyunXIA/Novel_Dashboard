"""issue #211：基本收入.md 端到端导入（sqlite import_all）。

断言：
- basic_income 逐年终值落 income_stream，四类 stream_type（security/rent/property/shop）
  分布正确，finance_entry 镜像同步（source='file'）
- Henri 房产表 BEF/LUF 双币列按祖父 BEF / 先祖 LUF 拆分，同挂 Henri Peeters
- 旧 4 收益文件（如 惠民租房.md）在源树中也被 SKIP_SUPERSEDED，不产任何行
- 连续两轮 import_all：第二轮零新增（source_file_version 指纹幂等），无冲突拦截
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.model import Base, Entity, FinanceEntry, IncomeStream
from app.ingest.main import import_all


_BASIC = """# 五人初始资产逐年收益明细（e2e 测试）

## 一、养外祖父 Frederik van Oranje（荷兰）

### 1.1 股票债券逐年收益

| 年份 | 债券收益 | 股票收益 | 合计 | 货币 |
|---:|---:|---:|---:|---|
| 1950–1955 | 100 | 200 | 300 | NLG/年 |
| 2002-2003 | 50 | 60 | 110 | EUR/年 |

### 1.2 房产逐年收益

| 年份 | 惠民租房 | 经营性房产 | 合计 | 货币 |
|---:|---:|---:|---:|---|
| 1974 | 10 | 1,000 | 1,010 | NLG |
| 2008 | 0 | 500 | 500 | EUR |

## 四、Henri Peeters（比利时，含先祖卢森堡资产）

### 4.2 房产逐年收益

| 年份 | 惠民(祖父) | 惠民(先祖) | 经营性(祖父) | 经营性(先祖) | 合计 | 货币 |
|---:|---:|---:|---:|---:|---:|---|
| 1974 | 18 | 142 | 288 | 3,168 | 3,616 | BEF/LUF |
| 2002 | 1 | 2 | 3 | 4 | 10 | EUR |

### 4.3 商业逐年收益

| 年份 | 税后落袋 | 货币 |
|---:|---:|---|
| 1947–1949 | 800 | BEF/年 |
"""

# 旧文件内容（旧 parser 时代会产 rent 记录）；issue #211 起应整文件跳过
_OLD_RENT = """# 惠民租房（已被基本收入.md 取代，存档）

| 国家 | 持有人物 | 套数 | 币种 | 1974单套年租金 |
|---|---|---:|---|---|
| 比利时 | 养祖父 | 18 | BEF | 1974年 1,000 BEF |
"""


@pytest.fixture()
def session():
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
    root = tmp_path
    (root / "人物").mkdir(parents=True)
    (root / "人物" / "Henri Peeters.md").write_text(
        "- 姓名：Henri Peeters\n- 角色：养祖父\n", encoding="utf-8")
    (root / "人物" / "Frederik van Oranje.md").write_text(
        "- 姓名：Frederik van Oranje\n- 与主角的关系：养外祖父\n", encoding="utf-8")
    (root / "基准" / "收益表").mkdir(parents=True)
    (root / "基准" / "收益表" / "基本收入.md").write_text(_BASIC, encoding="utf-8")
    (root / "基准" / "收益表" / "惠民租房.md").write_text(_OLD_RENT, encoding="utf-8")
    return root


def _count(s, model) -> int:
    return s.execute(select(func.count()).select_from(model)).scalar() or 0


def test_basic_income_import_and_distribution(session, source_dir):
    st = import_all(session, source_dir)
    assert st["blocked"] == 0, st["summary"]
    # 16 股债 + 3 房产(Frederik) + 8 房产(Henri) + 3 商业 = 30
    assert st["basic_income"] == 30
    assert _count(session, IncomeStream) == 30
    # finance_entry 镜像：全部 person 实体，逐行镜像
    fins = session.execute(select(FinanceEntry).where(FinanceEntry.source == "file")).scalars().all()
    assert len(fins) == 30
    assert {f.kind for f in fins} == {"income"}

    # stream_type 分布
    rows = session.execute(select(IncomeStream)).scalars().all()
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r.stream_type] = by_type.get(r.stream_type, 0) + 1
    assert by_type == {"security": 16, "rent": 5, "property": 6, "shop": 3}

    # Henri BEF/LUF 拆分：1974 两行 BEF、两行 LUF，全挂 Henri Peeters
    henri = session.execute(
        select(Entity).where(Entity.name == "Henri Peeters")).scalar_one()
    frederik = session.execute(
        select(Entity).where(Entity.name == "Frederik van Oranje")).scalar_one()
    h74 = [r for r in rows if r.entity_id == henri.id and r.year == 1974]
    assert sorted(r.currency for r in h74) == ["BEF", "BEF", "LUF", "LUF"]
    luf_prop = [r for r in h74 if r.currency == "LUF" and r.stream_type == "property"]
    assert len(luf_prop) == 1 and luf_prop[0].amount == 3168
    # Frederik 股债 NLG 段展开 6 年 ×2 列
    f_sec_nlg = [r for r in rows if r.entity_id == frederik.id
                 and r.stream_type == "security" and r.currency == "NLG"]
    assert len(f_sec_nlg) == 12
    # 2008 惠民=0 无 rent 行
    assert not [r for r in rows if r.stream_type == "rent" and r.year == 2008]
    # 旧文件零贡献：无 source_file 指向 惠民租房.md
    assert not [r for r in rows if r.source_file and "惠民租房.md" in r.source_file]


def test_basic_income_idempotent(session, source_dir):
    st1 = import_all(session, source_dir)
    assert st1["blocked"] == 0 and st1["basic_income"] == 30
    n_stream = _count(session, IncomeStream)
    n_fin = _count(session, FinanceEntry)

    st2 = import_all(session, source_dir)
    assert st2["blocked"] == 0, st2["summary"]
    assert st2["basic_income"] == 0          # 指纹 unchanged → 跳过
    assert _count(session, IncomeStream) == n_stream
    assert _count(session, FinanceEntry) == n_fin
