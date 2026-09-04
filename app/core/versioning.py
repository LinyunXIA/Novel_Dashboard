"""文件版本 / diff / 回退服务层（F-P2-06 · DESIGN §11）。

基于 `source_file_version`（每次成功导入存的全文快照 + is_current）提供：
- `list_tracked`：跟踪文件 + 当前磁盘 vs is_current 的版本状态（new/unchanged/changed）。
- `file_diff`：磁盘当前内容 vs is_current（或任意两版本）的 unified diff。
- `adopt_current`：采纳新版本 → 复用整个 import_all(force_files=该文件) 重导入 + 记版 + notification。
- `restore_version`：回退 → §11.3 安全写盘复原 source_dir 文件 + 更新 is_current + notification。

**写盘目标偏离说明（print explicitly）**：DESIGN §11.3 写 input_dir、绝不写 source_dir；但本仓库
实际 ingest 直接从 `source_dir`（Design_Folder，gitignored 的真数据）导入，无独立 input_dir 流。
故 F-P2-06 的回退写回 source_dir 是对 §11.3 的**有意偏离**（否则磁盘无从复原），docstring/CHANGELOG 明示。
安全：resolve 后 `is_relative_to(source_dir)` 防越权，原子写（tmp + os.replace）。
"""
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.main import import_all
from app.model import Notification, SourceFileVersion

_HASH_RE = re.compile(r"[^0-9a-zA-Z]")


class RestoreConflict(Exception):
    """issue #139：回退前置校验失败——磁盘内容已偏离当前版本，拒绝覆盖（API→409）。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _fingerprint(content: str) -> str:
    """内容指纹（与 ingest main._content_fingerprint 同构：归一化空白后）。"""
    return _HASH_RE.sub("", (content or "").strip())


# ---------------- 读 ----------------
def _versions_for(db: Session, rel: str) -> list[SourceFileVersion]:
    return list(db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == rel).order_by(SourceFileVersion.version.desc())).scalars())


def list_tracked(db: Session, config=None, limit_versions: int = 3) -> list[dict]:
    """所有被跟踪文件：每文件最新 is_current 版本 + 磁盘状态（new/unchanged/changed）+ 近期版本。

    范围 = 有版本记录的文件 ∪ source_dir 下磁盘存在但未版本化的源文件（标 new）。
    """
    rows = db.execute(select(SourceFileVersion)).scalars().all()
    by_file: dict[str, list] = {}
    for r in rows:
        by_file.setdefault(r.file_path, []).append(r)
    files = set(by_file)
    if config is not None:
        for p in config.source_dir.rglob("*.md"):
            rel = str(p.relative_to(config.source_dir))
            if rel not in files:
                by_file.setdefault(rel, [])
                files.add(rel)
    out = []
    for rel, vers in sorted(by_file.items()):
        # issue #226：失活文件（无 is_current 版本）不回退 vers[0] 冒充当前版——
        # 否则已整合取代/删除的旧文件在 diff 屏照常显示、还带可点的回退入口。
        cur = next((v for v in vers if v.is_current), None)
        disk = ""
        disk_exists = False
        if config is not None:
            p = config.source_dir / rel
            disk_exists = p.exists()
            try:
                disk = p.read_text(encoding="utf-8") if disk_exists else ""
            except (OSError, UnicodeDecodeError):
                disk = ""
        if cur is None:
            # 无生效版本：磁盘有文件=未版本化的新文件；磁盘也没有=已被整合取代/删除
            # （#223 失活的 SKIP_SUPERSEDED 文件即此态），标 superseded 供前端折叠留痕。
            status = "new" if disk_exists else "superseded"
        else:
            cur_fp = _fingerprint(cur.content)
            status = "unchanged" if _fingerprint(disk) == cur_fp and disk else "changed" if disk else "unchanged"
        out.append({
            "file": rel, "status": status,
            "current_version": cur.version if cur else None,
            "versions": [{"id": v.id, "version": v.version,
                          "captured_at": v.captured_at.isoformat() if v.captured_at else None,
                          "current": bool(v.is_current),
                          "content_preview": (v.content or "")[:120]}
                         for v in vers[:limit_versions]],
        })
    return out


def diff_texts(old: str, new: str, fromfile: str = "old", tofile: str = "new") -> str:
    """两段文本的 unified diff（用于 UI 展示）。"""
    diff = difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(),
                                fromfile=fromfile, tofile=tofile, lineterm="")
    return "\n".join(diff)


def file_diff(db: Session, config, rel: str, version_id: int | None = None) -> dict:
    """磁盘当前 vs is_current（或指定历史版本）的 diff。"""
    cur = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == rel,
        SourceFileVersion.is_current.is_(True))).scalar_one_or_none()
    new_content = ""
    if version_id is not None:
        v = db.get(SourceFileVersion, version_id)
        if v is not None and v.file_path == rel:
            # 对比两个历史版本
            old_content = cur.content if cur else ""
            new_content = v.content or ""
            return {"file": rel, "changed": old_content != new_content,
                    "diff_str": diff_texts(old_content, new_content, "v(cur)",
                                           f"v{v.version}"),
                    "lines_add": None, "lines_rem": None}
    path = config.source_dir / rel
    try:
        disk = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError):
        disk = ""
    old_content = cur.content if cur else ""
    diff = diff_texts(old_content, disk, "is_current", "disk")
    add = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    rem = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return {"file": rel, "changed": _fingerprint(disk) != _fingerprint(old_content),
            "diff_str": diff, "lines_add": add, "lines_rem": rem}


# ---------------- 写 ----------------
def _notify(db: Session, rel: str, status: str, extra: dict | None = None):
    payload = {"file": rel, "status": status}
    if extra:
        payload.update(extra)
    db.add(Notification(kind="file-updated", title=f"{status} {rel}", message=status,
                        payload=payload))


def adopt_current(db: Session, config, rel: str) -> dict:
    """采纳新版本：force 重导入该文件（含 recompute/rebuild）→ is_current 更新为新内容 → notification。"""
    # issue #226：已整合取代文件无可采纳内容（采纳=复活旧文件）；磁盘已删同理，
    # 否则 force 导入空跑还留一条 adopted 通知。
    from app.ingest.detect import detect
    if detect(rel).category == "SKIP_SUPERSEDED":
        raise RestoreConflict(f"{rel}: 文件已被整合取代（SKIP_SUPERSEDED），不可采纳")
    if not (config.source_dir / rel).exists():
        raise ValueError(f"{rel}: 磁盘文件不存在，无法采纳新版本")
    import_all(db, config.source_dir, force_files={rel})
    cur = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == rel,
        SourceFileVersion.is_current.is_(True))).scalar_one_or_none()
    _notify(db, rel, "adopted", {"version": cur.version if cur else None})
    db.flush()
    return {"file": rel, "status": "adopted", "version": cur.version if cur else None}


def _safe_target(source_dir: Path, rel: str) -> Path:
    """§11.3 安全：rel 必须落在 source_dir 内，防路径越权。"""
    base = source_dir.resolve()
    target = (source_dir / rel).resolve()
    if target != base and not target.is_relative_to(base):
        raise ValueError(f"目标路径越权: {rel}")
    return target


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def restore_version(db: Session, config, rel: str, version_id: int) -> dict:
    """回退到指定版本：写盘复原 source_dir 文件 + 该版本置 is_current（旧失活）+ notification。

    issue #139 前置校验（§11.3「仍为『待回退』版本」）：写盘前核对磁盘现内容与
    当前生效版（is_current）一致；已偏离（数据调整员在 diff 决策期间又手改过）→
    RestoreConflict，绝不无提示覆盖第三方改动。
    """
    v = db.get(SourceFileVersion, version_id)
    if v is None or v.file_path != rel:
        raise KeyError(f"版本不存在或不属于该文件: {rel}#{version_id}")
    # issue #226：已整合取代（SKIP_SUPERSEDED）或无生效版本的文件禁止回退——
    # 否则下方 _atomic_write 会把已删除的旧文件写回磁盘复活（#139 校验在 cur is None
    # 时整体跳过），diff 屏与扫描链随之被污染。
    from app.ingest.detect import detect
    if detect(rel).category == "SKIP_SUPERSEDED":
        raise RestoreConflict(f"{rel}: 文件已被整合取代（SKIP_SUPERSEDED），不可回退复活")
    target = _safe_target(config.source_dir, rel)
    cur = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == rel,
        SourceFileVersion.is_current.is_(True))).scalar_one_or_none()
    if cur is None:
        raise RestoreConflict(f"{rel}: 无生效版本（is_current），文件可能已被整合取代或删除，不可回退")
    if cur is not None and target.exists():
        try:
            disk = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            disk = None
        if disk is None or _fingerprint(disk) != _fingerprint(cur.content or ""):
            raise RestoreConflict(
                f"{rel}: 磁盘内容已偏离当前版本 v{cur.version}"
                f"{'（读取失败）' if disk is None else ''}；请刷新 diff 确认后再决策")
    _atomic_write(target, v.content or "")
    for row in db.execute(select(SourceFileVersion).where(
            SourceFileVersion.file_path == rel)).scalars().all():
        row.is_current = (row.id == v.id)
    _notify(db, rel, "restored", {"version": v.version})
    db.flush()
    return {"file": rel, "status": "restored", "version": v.version, "target": str(target)}