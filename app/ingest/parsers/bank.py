"""银行台账解析（DESIGN §6.3）。薄封装：实现委托 parsers 包。"""
from app.ingest.parsers import parse_bank as parse  # noqa: F401
from app.ingest.parsers import _extract_bank_name_from_header, _extract_holder_from_title  # noqa: F401

# 兼容 DESIGN §3 的独立模块路径：app.ingest.parsers.bank
# 实际实现在 app/ingest/parsers/__init__.py::parse_bank
