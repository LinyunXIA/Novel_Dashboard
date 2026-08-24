"""事件·电影 API（F-P2-01 · DESIGN §19.6）。

- GET    /movie-events(?linked=&title=)   列电影事件
- GET    /movie-events/{id}               详情
- POST   /movie-events/{id}/link          关联账户：写 ledger（投资出/本金返还/分红入），幂等
- POST   /movie-events/{id}/unlink        解关联（仅清 linked_*，不动历史 ledger）
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.model import LedgerEntry, MovieEvent

router = APIRouter(prefix="/api/v1", tags=["movie-events"])


class LinkIn(BaseModel):
    account_id: int


def _me(movie: MovieEvent) -> dict:
    return {"id": movie.id, "title": movie.title, "currency": movie.currency,
            "region": movie.region, "investment_total": float(movie.investment_total)
            if movie.investment_total is not None else None,
            "investment_date": movie.investment_date.isoformat() if movie.investment_date else None,
            "principal_return_date": movie.principal_return_date.isoformat() if movie.principal_return_date else None,
            "principal_return_amount": float(movie.principal_return_amount)
            if movie.principal_return_amount is not None else None,
            "dividends_total": float(movie.dividends_total)
            if movie.dividends_total is not None else None,
            "linked_account_id": movie.linked_account_id, "linked": movie.linked_account_id is not None}


@router.get("/movie-events")
def list_movies(linked: Optional[bool] = None, title: Optional[str] = None,
                db: Session = Depends(get_db)):
    q = select(MovieEvent)
    if linked is not None:
        q = q.where(MovieEvent.linked_account_id.isnot(None) if linked
                    else MovieEvent.linked_account_id.is_(None))
    if title:
        q = q.where(MovieEvent.title.ilike(f"%{title}%"))
    rows = db.execute(q.order_by(MovieEvent.id)).scalars().all()
    return {"items": [_me(m) for m in rows], "total": len(rows)}


@router.get("/movie-events/{movie_id}")
def get_movie(movie_id: int, db: Session = Depends(get_db)):
    m = db.get(MovieEvent, movie_id)
    if not m:
        raise HTTPException(404, "movie event not found")
    return _me(m)


def _write_movie_ledger(movie: MovieEvent, account_id: int, db: Session) -> int:
    """把已知现金流写 ledger（投资出 expense / 本金返还 income / 分红 investment_income）。"""
    written = 0
    flows = [
        (movie.investment_date, movie.investment_total, "expense",
         f"电影投资·{movie.title}"),
        (movie.principal_return_date, movie.principal_return_amount, "income",
         f"电影本金返还·{movie.title}"),
        (movie.principal_return_date or movie.investment_date,
         movie.dividends_total, "investment_income", f"电影分红·{movie.title}"),
    ]
    for d, amt, kind, reason in flows:
        if not d or not amt:
            continue
        db.add(LedgerEntry(account_id=account_id, date=d, reason=reason,
                           inflow=amt if kind != "expense" else None,
                           outflow=amt if kind == "expense" else None,
                           balance=None, kind=kind, note=f"电影事件关联 F-P2-01"))
        written += 1
    return written


@router.post("/movie-events/{movie_id}/link")
def link_movie(movie_id: int, body: LinkIn, db: Session = Depends(get_db)):
    m = db.get(MovieEvent, movie_id)
    if not m:
        raise HTTPException(404, "movie event not found")
    if m.linked_account_id is not None:
        return {"linked": True, "skipped": True, "account_id": m.linked_account_id}
    written = _write_movie_ledger(m, body.account_id, db)
    m.linked_account_id = body.account_id
    m.linked_at = datetime.now()
    db.commit()
    return {"linked": True, "skipped": False, "ledger_written": written, "account_id": body.account_id}


@router.post("/movie-events/{movie_id}/unlink")
def unlink_movie(movie_id: int, db: Session = Depends(get_db)):
    m = db.get(MovieEvent, movie_id)
    if not m:
        raise HTTPException(404, "movie event not found")
    # 仅清关联标记，不动历史 ledger（DESIGN §19.6）
    prev = m.linked_account_id
    m.linked_account_id = None
    m.linked_at = None
    db.commit()
    return {"unlinked": True, "was_account_id": prev}