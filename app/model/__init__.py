"""模型包：DESIGN §5.2 DDL 的 SQLAlchemy 表达。

所有模型在此导入，确保注册到 Base.metadata（供 alembic autogenerate 检测）。
"""
from app.db import Base
from app.model.core import (
    Account, Entity, FinanceEntry, HoldingEvent, IncomeStream, InitialAsset, LedgerEntry,
)
from app.model.derived import (
    DateRule, ExchangeRate, Investment, InvestmentAlloc, Notification, RecomputeJob,
    Relationship, ReturnCurve, Snapshot, SourceFileVersion, TimelineEvent, UserDataOverlay,
)
from app.model.labor import LaborCpiGrowth, LaborTaxBenchmark, LaborWageBenchmark

__all__ = [
    "Base",
    "Entity", "Account", "InitialAsset", "IncomeStream", "LedgerEntry",
    "FinanceEntry", "HoldingEvent",
    "ReturnCurve", "ExchangeRate", "DateRule", "TimelineEvent", "Relationship",
    "UserDataOverlay", "Snapshot", "SourceFileVersion", "RecomputeJob", "Notification",
    "Investment", "InvestmentAlloc",
    "LaborWageBenchmark", "LaborCpiGrowth", "LaborTaxBenchmark",
]