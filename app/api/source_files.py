"""文件版本 / diff 决策 API（F-P2-06 · DESIGN §11）。

普通 UI 放行（deps.py 已注明 source-files versions 是 importer 例外）。
用文件当前版本的 SourceFileVersion.id 做文件标识（规避 rel 路径中文/斜杠进 URL）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_config
from app.core import versioning
from app.model import SourceFileVersion

router = APIRouter(prefix="/api/v1", tags=["source-files"])


def _rel(db: Session, vid: int) -> str:
    v = db.get(SourceFileVersion, vid)
    if v is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    return v.file_path


@router.get("/source-files")
def list_source_files(db: Session = Depends(get_db)):
    return {"items": versioning.list_tracked(db, get_config()), "total": 0}


@router.get("/source-files/{vid}/versions")
def file_versions(vid: int, db: Session = Depends(get_db)):
    rel = _rel(db, vid)
    from sqlalchemy import select
    rows = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == rel).order_by(SourceFileVersion.version.desc())).scalars().all()
    return {"file": rel, "versions": [
        {"id": v.id, "version": v.version,
         "captured_at": v.captured_at.isoformat() if v.captured_at else None,
         "current": bool(v.is_current), "content_preview": (v.content or "")[:120]} for v in rows]}


@router.get("/source-files/{vid}/diff")
def file_diff(vid: int, version_id: Optional[int] = None, db: Session = Depends(get_db)):
    rel = _rel(db, vid)
    return versioning.file_diff(db, get_config(), rel, version_id)


@router.post("/source-files/{vid}/versions")
def adopt_new_version(vid: int, db: Session = Depends(get_db)):
    """采纳新版本：force 重导入该文件 + 记版 + notification。"""
    rel = _rel(db, vid)
    try:
        r = versioning.adopt_current(db, get_config(), rel)
        db.commit()
        return r
    except Exception as e:  # noqa: BLE001 - 冲突/解析错误透传给前端
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/source-files/{vid}/versions/{v2}/restore")
def restore_version(vid: int, v2: int, db: Session = Depends(get_db)):
    """回退到指定版本：复原 source_dir 磁盘文件 + 该版本置 is_current。"""
    rel = _rel(db, vid)
    try:
        r = versioning.restore_version(db, get_config(), rel, v2)
        db.commit()
        return r
    except KeyError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e))