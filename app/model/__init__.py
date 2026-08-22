"""模型包：SQLAlchemy ORM 表达 DESIGN §5.2 的 DDL。

F-P0-07 将在此填充 entity/account/ledger_entry/income_stream/initial_asset/snapshot 等表。
迁移 metadata 基类复用 app.db.Base。
"""
from app.db import Base

__all__ = ["Base"]