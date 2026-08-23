"""Shared enums/literals matching DESIGN §5.2 CHECK 约束."""
from enum import Enum


class EntityType(str, Enum):
    PERSON = "person"
    COMPANY = "company"
    ASSET = "asset"
    FAMILY = "family"


class SourceKind(str, Enum):
    FILE = "file"
    EXTERNAL_API = "external-api"
    UI = "ui"


class AccountStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class LedgerKind(str, Enum):
    INCOME = "income"
    EXPENSE = "expense"
    INVESTMENT = "investment"
    INVESTMENT_INCOME = "investment_income"
    POOL = "pool"


class StreamType(str, Enum):
    RENT = "rent"
    PROPERTY = "property"
    SECURITY = "security"
    SHOP = "shop"
    SALARY = "salary"


RISK_LEVELS = ("R1", "R2", "R3", "R4", "R5")