"""PDF 报告（F-P2-07 · DESIGN §15「报告 PDF（图表内嵌）」）。

reportlab 实现：家族总资产年度曲线（reportlab.graphics 内嵌折线图）
+ 实体/账户/财务计数摘要 + 编年史近段表。只读 DB，产物由调用方写 exports_dir。
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.export.render import effective_timeline, family_total_series
from app.model import Account, Entity, FinanceEntry

_PAGE_W, _PAGE_H = 842, 595   # A4 landscape


def render_pdf(db: Session) -> bytes:
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(_PAGE_W, _PAGE_H), title="家族财富报告")
    styles = getSampleStyleSheet()
    story: list = []

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph("家族财富报告", styles["Title"]))
    story.append(Paragraph(f"导出于 {now} · Dashboard 生成（仅导出不回写源）",
                           styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    # ---- 摘要 ----
    n_ent = db.execute(select(func.count()).select_from(Entity)).scalar() or 0
    n_acc = db.execute(select(func.count()).select_from(Account)).scalar() or 0
    n_fin = db.execute(select(func.count()).select_from(FinanceEntry)).scalar() or 0
    series = family_total_series(db)
    total_now = series[-1][1] if series else 0.0
    summary = Table([
        ["实体", "账户", "财务分录", "家族总资产（最新年，USD）"],
        [str(n_ent), str(n_acc), str(n_fin), f"{total_now:,.2f}"],
    ], colWidths=[40 * mm] * 4)
    summary.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                 ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke)]))
    story += [summary, Spacer(1, 8 * mm)]

    # ---- 家族总资产年度曲线（图表内嵌）----
    story.append(Paragraph("家族总资产随时间（family:total · USD 展示折算）",
                           styles["Heading2"]))
    if series:
        chart = HorizontalLineChart()
        chart.width, chart.height = 240 * mm, 70 * mm
        chart.data = [[v for _, v in series]]
        chart.categoryAxis.categoryNames = [str(y) for y, _ in series]
        chart.categoryAxis.labels.fontName = "Helvetica"
        chart.categoryAxis.labels.fontSize = 6
        chart.valueAxis.valueMin = min(v for _, v in series) or 0
        chart.lines[0].strokeColor = colors.HexColor("#2563eb")
        drawing = Drawing(260 * mm, 85 * mm)
        drawing.add(chart)
        drawing.add(String(0, 80 * mm,
                           f"{series[0][0]}–{series[-1][0]} · 共 {len(series)} 年",
                           fontSize=8))
        story.append(drawing)
    else:
        story.append(Paragraph("（无 family:total 快照——先运行 snapshot 重建）",
                               styles["Normal"]))
    story.append(Spacer(1, 6 * mm))

    # ---- 编年史近段（合并生效，取最近 20 条）----
    story.append(Paragraph("编年史近段（合并生效 · 最近 20 条）", styles["Heading2"]))
    tl = effective_timeline(db)[-20:]
    rows = [["年份", "日期", "标题", "备注"]]
    for t in tl:
        rows.append([t.event_year,
                     t.event_date.isoformat() if t.event_date else "",
                     t.title or "", (t.note or "")[:60]])
    tbl = Table(rows, colWidths=[18 * mm, 24 * mm, 90 * mm, 108 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                             ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                             ("FONTSIZE", (0, 0), (-1, -1), 7)]))
    story.append(tbl)

    doc.build(story)
    return buf.getvalue()
