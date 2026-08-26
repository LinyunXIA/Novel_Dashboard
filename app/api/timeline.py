"""编年史 API（F-P2-05 · DESIGN §12/§6.4）：overlay 增改删 + 差异 + 重置回源 + 以源为最新。

普通 UI 放行（deps.py:3 已注明 timeline 是 importer 例外，不挂 require_importer）。
变更走 app/core/overlay.py（user_data_overlay 覆盖层 + timeline_event(overlay=True)）。
系统 overlay 行（投资/划拨 source_file=NULL）只读、不可通过本端点改/删。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import CALENDAR_MAX_YEAR as _YEAR_MAX  # issue #141
from app.core.overlay import (_is_user_overlay_row, create_overlay, delete_overlay,
                              diff_overlay, make_key, merge_overlay, restore_overlay,
                              source_as_latest, update_overlay)
from app.core.recompute import record_recompute_done
from app.core.snapshot import rebuild_snapshots
from app.model import TimelineEvent

router = APIRouter(prefix="/api/v1", tags=["timeline-events"])


def _after_timeline_write(db: Session, start_year: int) -> None:
    """§12/issue #120：overlay 变更 → 自条目年起重建快照 + recompute-done 通知。

    时间线事件不参与余额链，但按 §12「merge 后触发增量重算（起点=条目年份）」
    固化口径，保证派生视图与编年史游标一致。
    """
    rebuild_snapshots(db, from_year=start_year)
    record_recompute_done(db, start_year, reason="timeline-overlay")
    db.commit()


class TimelineCreate(BaseModel):
    event_year: int = Field(ge=1947, le=_YEAR_MAX)
    event_date: Optional[date] = None
    title: str = Field(min_length=1)
    note: Optional[str] = None
    decade: Optional[str] = None


class TimelinePatch(BaseModel):
    event_year: Optional[int] = Field(default=None, ge=1947, le=_YEAR_MAX)
    event_date: Optional[date] = None
    title: Optional[str] = None
    note: Optional[str] = None
    decade: Optional[str] = None


def _guard_user_overlay(db: Session, event_id: int) -> TimelineEvent:
    """命中必须是用户覆盖行（overlay=True AND source_file LIKE overlay:timeline:%）；否则 404。"""
    t = db.get(TimelineEvent, event_id)
    if t is None or not _is_user_overlay_row(t):
        raise HTTPException(status_code=404, detail="timeline overlay row not found / not user-editable")
    return t


def _row(t: TimelineEvent, *, overlay_status=None, editable=False, system=False, has_source=False) -> dict:
    return {
        "id": t.id, "event_year": t.event_year,
        "event_date": t.event_date.isoformat() if t.event_date else None,
        "title": t.title, "note": t.note, "decade": t.decade, "overlay": t.overlay,
        "overlay_status": overlay_status, "editable": editable, "system": system,
        "has_source": has_source, "source_file": t.source_file,
    }


@router.get("/timeline-events")
def list_timeline_events(
    year: Optional[int] = None, decade: Optional[str] = None,
    as_of: Optional[date] = None,
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """编年史合并视图：按 (event_year, title) 每 key 恰一行——用户覆盖行优先，源行标 has_source。

    issue #127：page/page_size 加 Query 校验（page=0 曾致负 OFFSET → DB 500），
    默认 page_size 对齐 §14.1 的 50；补文档承诺的 ?as_of（截至该日的已发生事件）。
    """
    q = select(TimelineEvent).order_by(TimelineEvent.event_year, TimelineEvent.id)
    if year is not None:
        q = q.where(TimelineEvent.event_year == year)
    if decade:
        q = q.where(TimelineEvent.decade == decade)
    if as_of is not None:
        # 已发生：有具体日期的按日期；仅年份粒度的按年 ≤ as_of 年
        from sqlalchemy import or_
        q = q.where(or_(
            TimelineEvent.event_date <= as_of,
            (TimelineEvent.event_date.is_(None)) & (TimelineEvent.event_year <= as_of.year),
        ))
    rows = db.execute(q).scalars().all()
    diff = {d["key"]: d["status"] for d in diff_overlay(db)}

    # 分组
    by_key: dict[tuple, dict] = {}
    for t in rows:
        k = (t.event_year, t.title)
        g = by_key.setdefault(k, {"user": None, "system": None, "source": None})
        if _is_user_overlay_row(t):
            if g["user"] is None:
                g["user"] = t
        elif t.overlay and t.source_file is None:      # 系统 overlay 行（issue #86）
            if g["system"] is None:
                g["system"] = t
        elif not t.overlay:                            # 源行
            if g["source"] is None:
                g["source"] = t

    out = []
    for k in sorted(by_key):
        g = by_key[k]
        if g["user"] is not None:
            t = g["user"]
            status = diff.get(make_key(t.event_year, t.title), "unchanged")
            out.append(_row(t, overlay_status=status, editable=True, has_source=g["source"] is not None))
        elif g["system"] is not None:
            out.append(_row(g["system"], system=True))          # 只读系统行
        elif g["source"] is not None:
            out.append(_row(g["source"], editable=False))       # 纯源行（可"覆盖编辑"）

    total = len(out)
    start = (page - 1) * page_size
    return {"items": out[start:start + page_size], "total": total, "page": page, "page_size": page_size}


@router.get("/timeline-events/{event_id}")
def get_timeline_event(event_id: int, db: Session = Depends(get_db)):
    t = db.get(TimelineEvent, event_id)
    if not t:
        raise HTTPException(status_code=404, detail="timeline event not found")
    extra = {}
    if _is_user_overlay_row(t):
        statuses = {d["key"]: d["status"] for d in diff_overlay(db)}
        extra = {"overlay_status": statuses.get(make_key(t.event_year, t.title)),
                 "editable": True}
    return {**_row(t), **{"source_line": t.source_line}, **extra}


@router.post("/timeline-events", status_code=201)
def post_timeline(body: TimelineCreate, response: Response,
                  db: Session = Depends(get_db)):
    r = create_overlay(db, event_year=body.event_year, event_date=body.event_date,
                       title=body.title, note=body.note, decade=body.decade)
    db.commit()
    _after_timeline_write(db, body.event_year)
    # issue #142：201 统一带 Location（#127 契约；此前声明 response 未用）
    new_id = r.get("timeline_event_id") if isinstance(r, dict) else None
    if new_id is not None:
        response.headers["Location"] = f"/api/v1/timeline-events/{new_id}"
    return r


@router.patch("/timeline-events/{event_id}")
def patch_timeline(event_id: int, body: TimelinePatch, db: Session = Depends(get_db)):
    t = _guard_user_overlay(db, event_id)
    key = make_key(t.event_year, t.title)
    try:
        r = update_overlay(db, key, event_year=body.event_year, event_date=body.event_date,
                           title=body.title, note=body.note, decade=body.decade)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.commit()
    _after_timeline_write(db, (body.event_year or t.event_year))
    return r


@router.put("/timeline-events/{event_id}")
def put_timeline(event_id: int, body: TimelineCreate, db: Session = Depends(get_db)):
    """全量替换（§14.2，issue #127）：仅用户覆盖行；未提供字段清空。

    以 删旧覆盖+重建 实现真全量语义（update_overlay 是 None=保留 的部分更新）。
    """
    t = _guard_user_overlay(db, event_id)
    old_key = make_key(t.event_year, t.title)
    delete_overlay(db, old_key)
    r2 = create_overlay(db, event_year=body.event_year, event_date=body.event_date,
                        title=body.title, note=body.note, decade=body.decade)
    db.commit()
    _after_timeline_write(db, body.event_year)
    return r2


@router.delete("/timeline-events/{event_id}")
def delete_timeline(event_id: int, db: Session = Depends(get_db)):
    t = _guard_user_overlay(db, event_id)
    year = t.event_year
    key = make_key(t.event_year, t.title)
    r = delete_overlay(db, key)
    db.commit()
    _after_timeline_write(db, year)
    return {**r, "source_preserved": r["source_preserved"]}


@router.post("/timeline-events/{event_id}/overlay/restore")
def restore_timeline(event_id: int, db: Session = Depends(get_db)):
    """重置回源：删覆盖条目，源行保留、重新生效。"""
    t = _guard_user_overlay(db, event_id)
    key = make_key(t.event_year, t.title)
    r = restore_overlay(db, key)
    db.commit()
    return r


@router.post("/timeline-events/{event_id}/overlay/source-as-latest")
def source_as_latest_ep(event_id: int, db: Session = Depends(get_db)):
    """以源为最新：覆盖层吸收源当前值。"""
    t = _guard_user_overlay(db, event_id)
    key = make_key(t.event_year, t.title)
    r = source_as_latest(db, key)
    db.commit()
    return r


@router.get("/timeline-events/overlay/diff")
def get_overlay_diff(db: Session = Depends(get_db)):
    items = diff_overlay(db)
    return {"items": items, "total": len(items)}


@router.post("/timeline-events/overlay/merge")
def merge_timeline_overlay(db: Session = Depends(get_db)):
    r = merge_overlay(db)
    db.commit()
    return r