"""issue #70：ingest 噪音治理回归——SKIP_PARAM 显式跳过、stock_tx 解析后显式说明。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ingest.detect import detect, is_skip_category
from app.ingest.main import import_all
from app.model import Base


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


class TestCpiWageSkipParam:
    def test_detect_maps_skip_param(self):
        d = detect("基准/CPI工资.md")
        assert d.category == "SKIP_PARAM"
        assert is_skip_category(d.category)

    def test_parse_one_ok_not_failed(self, tmp_path):
        """有类别无 parser 的 ❌ 噪音不再出现：SKIP 类别 ok=True。"""
        from app.ingest.parse import parse_one
        p = tmp_path / "CPI工资.md"
        p.write_text("# CPI 与工资增幅\n", encoding="utf-8")
        pr = parse_one("基准/CPI工资.md", p)
        assert pr.ok is True and pr.error is None and pr.records == []


class TestStockTxDropped:
    @pytest.fixture()
    def source_dir(self, tmp_path):
        root = tmp_path
        (root / "经济" / "股票").mkdir(parents=True)
        (root / "经济" / "股票" / "腾讯.md").write_text(
            "### 基本信息\n- 公司：腾讯\n- 代码：0700.HK\n\n"
            "### 年度明细\n"
            "| 日期 | 代码 | 事件 | 股数 | 单价(USD) | 金额(万美金) | 持股比例 | 备注 |\n"
            "|---|---|---|---|---|---|---|---|\n"
            "| 2001-02-01 | 0700 | buy | 100 | 1 | 1 | 5% | 测试 |\n",
            encoding="utf-8")
        return root

    def test_stock_tx_logged_as_phase2_skip(self, session, source_dir):
        logs: list[str] = []
        st = import_all(session, source_dir, log=logs.append)
        assert any("股票台账解析成功" in m and "Phase 2" in m for m in logs)
        assert st["blocked"] == 0
