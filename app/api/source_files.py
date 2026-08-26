"""文件版本 / diff 决策 API（F-P2-06 · DESIGN §11）。

普通 UI 放行（deps.py 已注明 source-files versions 是 importer 例外）。
用文件当前版本的 SourceFileVersion.id 做文件标识（规避 rel 路径中文/斜杠进 URL）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
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


@router.get("/source-files/{vid}")
@router.get("/source-files/{vid}/meta")
def get_source_file(vid: int, db: Session = Depends(get_db)):
    """单文件元信息（§14.2 GET /source-files/{id}，issue #142 补端点；/meta 为别名）。

    文件标识沿用本路由的 current_version id 约定（规避中文 rel 进 URL）。
    """
    v = db.get(SourceFileVersion, vid)
    if v is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    rel = v.file_path
    rows = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == rel).order_by(SourceFileVersion.version)).scalars().all()
    cur = next((x for x in rows if x.is_current), None)
    return {
        "id": vid, "file": rel,
        "current_version": cur.version if cur else None,
        "current_version_row_id": cur.id if cur else None,
        "version_count": len(rows),
        "versions": [{"id": x.id, "version": x.version, "current": bool(x.is_current)} for x in rows],
    }


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


@router.get("/source-files/{vid}/versions/{vnum}")
def file_version_content(vid: int, vnum: int, db: Session = Depends(get_db)):
    """单版本完整内容（§14.2 GET /source-files/{id}/versions/{vid}，issue #155 补端点）。"""
    rel = _rel(db, vid)
    v = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == rel,
        SourceFileVersion.version == vnum)).scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=404, detail=f"版本 v{vnum} 不存在")
    return {"id": v.id, "file": rel, "version": v.version,
            "captured_at": v.captured_at.isoformat() if v.captured_at else None,
            "current": bool(v.is_current), "content": v.content or ""}


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
    """回退到指定版本：复原 source_dir 磁盘文件 + 该版本置 is_current。

    issue #139：磁盘内容已偏离当前生效版 → 409（前端引导刷新 diff 重决策）。
    """
    rel = _rel(db, vid)
    try:
        r = versioning.restore_version(db, get_config(), rel, v2)
        db.commit()
        return r
    except versioning.RestoreConflict as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=e.detail)
    except KeyError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e))