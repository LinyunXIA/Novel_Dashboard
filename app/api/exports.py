"""导出任务资源（F-P2-07 · DESIGN §14.2/§15）。

- POST /api/v1/exports        创建导出（同步生成；数据量小、本地单机）→ 201 + Location + 下载 URL
- GET  /api/v1/exports        产物清单（实现超集，供前端「导出中心」列历史）
- GET  /api/v1/exports/{id}   获取产物文件流（id 不合法/不存在 → 404）

原则：只读 DB，产物仅落 config.exports_dir；绝不触碰 source_dir/input_dir。
普通 UI 放行（导出为只读动作）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_config
from app.export import pdf as pdf_mod
from app.export import render
from app.export.render import CSV_SCOPES, FORMATS

router = APIRouter(prefix="/api/v1", tags=["exports"])

_ID_SAFE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")


class ExportCreate(BaseModel):
    format: str
    scope: Optional[str] = None


def _do_export(db: Session, fmt: str, scope: Optional[str]) -> str:
    """渲染并写盘，返回 export_id。"""
    from pathlib import Path
    cfg = get_config()
    cfg.exports_dir.mkdir(parents=True, exist_ok=True)
    export_id = render.new_export_id(fmt)
    path = cfg.exports_dir / f"{export_id}{render._EXT[fmt]}"
    if not path.resolve().is_relative_to(Path(cfg.exports_dir).resolve()):
        raise HTTPException(status_code=500, detail="导出目录解析异常")
    if fmt == "markdown":
        text = render.render_markdown(db)
        path.write_text(text, encoding="utf-8")
    elif fmt == "csv":
        text = render.render_csv(db, scope)   # scope 已校验非空
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(pdf_mod.render_pdf(db))
    return export_id


@router.post("/exports", status_code=201)
def create_export(body: ExportCreate, response: Response, db: Session = Depends(get_db)):
    if body.format not in FORMATS:
        raise HTTPException(status_code=422,
                            detail=f"format 须为 {'/'.join(FORMATS)}")
    if body.format == "csv":
        if not body.scope or body.scope not in CSV_SCOPES:
            raise HTTPException(status_code=422,
                                detail=f"csv 导出须指定 scope ∈ {'/'.join(CSV_SCOPES)}")
    elif body.scope:
        raise HTTPException(status_code=422,
                            detail="scope 仅 csv 导出支持")

    export_id = _do_export(db, body.format, body.scope)
    cfg = get_config()
    path = render.export_path(cfg, export_id)
    response.headers["Location"] = f"/api/v1/exports/{export_id}"   # §14.1：201 + Location
    return {
        "id": export_id,
        "format": body.format,
        "scope": body.scope,
        "filename": path.name if path else None,
        "size_bytes": path.stat().st_size if path else None,
        "download_url": f"/api/v1/exports/{export_id}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/exports")
def list_exports():
    """产物清单（按创建时间倒序）。"""
    cfg = get_config()
    items = []
    if cfg.exports_dir.exists():
        for p in sorted(cfg.exports_dir.iterdir(), key=lambda x: x.stat().st_mtime,
                        reverse=True):
            stem = p.stem
            if render.export_path(cfg, stem) is None:
                continue   # 非法名/非产物文件跳过
            items.append({"id": stem,
                          "format": {"md": "markdown", "csv": "csv",
                                     "pdf": "pdf"}[p.suffix.lstrip(".")],
                          "filename": p.name,
                          "size_bytes": p.stat().st_size,
                          "download_url": f"/api/v1/exports/{stem}"})
    return {"items": items, "total": len(items)}


@router.get("/exports/{export_id}")
def download_export(export_id: str):
    """获取导出产物文件流。"""
    if not _ID_SAFE.match(export_id):
        raise HTTPException(status_code=404, detail="导出产物不存在")
    path = render.export_path(get_config(), export_id)
    if path is None:
        raise HTTPException(status_code=404, detail="导出产物不存在")
    return FileResponse(path, media_type=render.content_type(export_id),
                        filename=path.name)
