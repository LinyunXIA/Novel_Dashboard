"""用工成本基准模型（API② · F-P1-10；DESIGN §13.2）。

三张表承载本地基准，供 labor_cost.py 成本公式查询：
- labor_wage_benchmark  工资基准（10 区×年）：投资/金融年薪 + 全行业人均 + CPI 定基
- labor_cpi_growth      CPI 通胀 / 工资增幅（10 区×年，同比%）
- labor_tax_benchmark   税率基准（12 office×年）：异结构参数 JSONB + 成本模型类型
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.model.core import JSONBCompat


class LaborWageBenchmark(Base):
    __tablename__ = "labor_wage_benchmark"
    # 含 currency：比/卢 2002 关池转 EUR 的行同时存 BEF/EUR 两值（同 region×year 两行）
    __table_args__ = (UniqueConstraint("region", "year", "currency", name="uq_labor_wage_ryc"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    region: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    investment_fin_salary: Mapped[Optional[float]] = mapped_column(Numeric, comment="投资/金融行业年薪（税前，region 币种）")
    avg_salary: Mapped[Optional[float]] = mapped_column(Numeric, comment="全行业人均名义年薪（基准）")
    cpi_index: Mapped[Optional[float]] = mapped_column(Numeric, comment="CPI 定基指数（cpi_base_year=100）")
    cpi_base_year: Mapped[Optional[int]] = mapped_column(Integer, comment="CPI 基年（中国系列2013；日本2015）")
    source_file: Mapped[Optional[str]] = mapped_column(Text)


class LaborCpiGrowth(Base):
    __tablename__ = "labor_cpi_growth"
    __table_args__ = (UniqueConstraint("region", "year", name="uq_labor_cpi_region_year"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    region: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    wage_growth_pct: Mapped[Optional[float]] = mapped_column(Numeric, comment="名义工资同比增幅%")
    cpi_pct: Mapped[Optional[float]] = mapped_column(Numeric, comment="CPI 同比通胀%")
    # issue #132：补 source_file（另两张 labor 基准表均有，溯源口径一致）
    source_file: Mapped[Optional[str]] = mapped_column(String, comment="来源基准文件")


class LaborTaxBenchmark(Base):
    __tablename__ = "labor_tax_benchmark"
    __table_args__ = (UniqueConstraint("office", "year", name="uq_labor_tax_office_year"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    office: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    formula: Mapped[str] = mapped_column(String, nullable=False,
                                         comment="成本模型类型：single_pct/single_pct_cap/multi_cap/clamp/headcount/uk_nic/onss/ccss/jp")
    params: Mapped[dict] = mapped_column(JSONBCompat, default=dict, nullable=False, server_default="{}")
    source_file: Mapped[Optional[str]] = mapped_column(Text)