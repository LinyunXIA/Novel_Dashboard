"""编年史覆盖层编辑服务层（F-P2-05 · DESIGN §12/§6.4）。

普通 UI 用户编辑编年史：变更走 **user_data_overlay**（DB-backed 覆盖层，权威）+ 合并到
`timeline_event(overlay=True)`。不写源 md。

隔离约定（issue #86 兼容）：
- **系统 overlay 行**（投资/划拨/活期结息直写，`invest.py`/`transfer.py`）：`overlay=True AND source_file IS NULL`，
  note 含 `inv#{id}`/`demand#{year}`/`UI 转移` —— **只读**，不纳入编辑/差异/重置。
- **用户覆盖行**：`overlay=True AND source_file LIKE 'overlay:timeline:%'` —— 可编辑/差异/重置。
- **源行**：`overlay=False`。
所有用户路径只碰 `overlay:timeline:%` 前缀 → 系统行结构上不可触及。

幂等：user_data_overlay/timeline_event 无唯一约束 → 用「先查后插/更」（app 层），风格同
writer.import_timeline。定位按 (event_year, title) 列，不解析 source_file。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.model import TimelineEvent, UserDataOverlay

_SECTION = "timeline"
_PREFIX = "overlay:timeline:"


def make_key(event_year: int, title: str) -> str:
    """覆盖条目键：`{year}:{title}`（对齐源幂等键 event_year+title）。"""
    return f"{event_year}:{title.strip()}"


def _user_source(key: str) -> str:
    return f"{_PREFIX}{key}"


def _is_user_overlay_row(t: TimelineEvent) -> bool:
    return bool(t.overlay and t.source_file and t.source_file.startswith(_PREFIX))


def _payload(event_year, event_date, title, note, decade) -> dict:
    return {"event_year": event_year, "event_date": event_date.isoformat() if event_date else None,
            "title": title, "note": note, "decade": decade}


# ---------------- 读 ----------------
def list_user_overlays(session: Session) -> list[UserDataOverlay]:
    """全部编年史覆盖条目。"""
    return list(session.execute(
        select(UserDataOverlay).where(UserDataOverlay.section == _SECTION)
        .order_by(UserDataOverlay.key)).scalars())


def _get_overlay_row(session: Session, key: str) -> TimelineEvent | None:
    src = _user_source(key)
    return session.execute(select(TimelineEvent).where(
        TimelineEvent.overlay.is_(True),
        TimelineEvent.source_file == src)).scalars().first()


def _find_source(session: Session, event_year: int, title: str) -> TimelineEvent | None:
    return session.execute(select(TimelineEvent).where(
        TimelineEvent.overlay.is_(False),
        TimelineEvent.event_year == event_year,
        TimelineEvent.title == title)).scalars().first()


# ---------------- 写 ----------------
def create_overlay(session: Session, *, event_year: int, event_date=None, title: str,
                   note=None, decade=None) -> dict:
    """创建/幂等覆盖条目：upsert user_data_overlay + 覆盖 timeline_event(overlay=True)。"""
    key = make_key(event_year, title)
    payload = _payload(event_year, event_date, title, note, decade)
    o = session.execute(select(UserDataOverlay).where(
        UserDataOverlay.section == _SECTION, UserDataOverlay.key == key)).scalar_one_or_none()
    idempotent = True
    if o is None:
        o = UserDataOverlay(section=_SECTION, key=key, payload=payload)
        session.add(o)
        idempotent = False
    else:
        o.payload = payload
    row = _get_overlay_row(session, key)
    if row is None:
        row = TimelineEvent(event_year=event_year, event_date=event_date, title=title,
                            note=note, decade=decade, overlay=True, source_file=_user_source(key))
        session.add(row)
        idempotent = False
    else:
        row.event_date = event_date
        row.note = note
        row.decade = decade
    session.flush()
    return {"key": key, "event_year": event_year, "event_date": event_date.isoformat() if event_date else None,
            "title": title, "note": note, "decade": decade,
            "timeline_event_id": row.id, "idempotent": idempotent}


def update_overlay(session: Session, key: str, *, event_year=None, event_date=None,
                   title=None, note=None, decade=None) -> dict:
    """就地更新覆盖条目；key 变（title/year 改）→ 迁移到新 key（单覆盖行不变量）。"""
    o = session.execute(select(UserDataOverlay).where(
        UserDataOverlay.section == _SECTION, UserDataOverlay.key == key)).scalar_one_or_none()
    if o is None:
        raise KeyError(f"覆盖条目不存在: {key}")
    p = dict(o.payload or {})
    p["event_year"] = event_year if event_year is not None else p.get("event_year")
    p["event_date"] = (event_date.isoformat() if event_date else None) if event_date is not None \
        else p.get("event_date")
    p["title"] = title if title is not None else p.get("title")
    p["note"] = note if note is not None else p.get("note")
    p["decade"] = decade if decade is not None else p.get("decade")
    new_key = make_key(int(p["event_year"]), p["title"])
    migrated = new_key != key
    if migrated:
        # 删旧覆盖行 + 旧 user 行，重建到新 key
        old = _get_overlay_row(session, key)
        if old is not None:
            session.delete(old)
        session.delete(o)
        session.flush()
        created = create_overlay(session, event_year=int(p["event_year"]),
                                 event_date=date.fromisoformat(p["event_date"]) if p.get("event_date") else None,
                                 title=p["title"], note=p.get("note"), decade=p.get("decade"))
        created["migrated"] = True
        return created
    o.payload = p
    row = _get_overlay_row(session, key)
    if row is not None:
        row.event_year = int(p["event_year"])
        row.event_date = date.fromisoformat(p["event_date"]) if p.get("event_date") else None
        row.title = p["title"]
        row.note = p.get("note")
        row.decade = p.get("decade")
    session.flush()
    return {"key": key, "event_year": p["event_year"], "event_date": p.get("event_date"),
            "title": p["title"], "note": p.get("note"), "decade": p.get("decade"),
            "timeline_event_id": row.id if row else None, "idempotent": False, "migrated": False}


def delete_overlay(session: Session, key: str) -> dict:
    """删覆盖条目：删 user 行 + 覆盖行（source_file 匹配）；源行(overlay=False)保留、重新生效。"""
    o = session.execute(select(UserDataOverlay).where(
        UserDataOverlay.section == _SECTION, UserDataOverlay.key == key)).scalar_one_or_none()
    src = _user_source(key)
    row = session.execute(select(TimelineEvent).where(
        TimelineEvent.overlay.is_(True), TimelineEvent.source_file == src)).scalars().first()
    # 源是否存在：key = year:title
    try:
        y, title = key.split(":", 1)
        source_preserved = _find_source(session, int(y), title) is not None
    except (ValueError, KeyError):
        source_preserved = False
    deleted = 0
    if row is not None:
        session.delete(row)
        deleted += 1
    if o is not None:
        session.delete(o)
        deleted += 1
    session.flush()
    return {"deleted": deleted, "key": key, "source_preserved": source_preserved}


def merge_overlay(session: Session) -> dict:
    """把 user_data_overlay(section='timeline') reconcile 到覆盖 timeline_event(overlay=True)。

    确保每个 overlay key 恰一条覆盖行（create/update 漂移校正），并清孤儿覆盖行
    （overlay=True 且 source_file 是 overlay:timeline:% 但无对应 user 行的）。
    """
    reconciled = 0
    for o in list_user_overlays(session):
        p = o.payload or {}
        try:
            create_overlay(session, event_year=int(p["event_year"]),
                           event_date=date.fromisoformat(p["event_date"]) if p.get("event_date") else None,
                           title=p["title"], note=p.get("note"), decade=p.get("decade"))
            reconciled += 1
        except (KeyError, ValueError):
            continue
    cleaned = 0
    prefix = f"{_PREFIX}%"
    for row in session.execute(select(TimelineEvent).where(
            TimelineEvent.overlay.is_(True),
            TimelineEvent.source_file.like(prefix))).scalars().all():
        key = row.source_file[len(_PREFIX):]
        has = session.execute(select(UserDataOverlay.id).where(
            UserDataOverlay.section == _SECTION, UserDataOverlay.key == key)).scalar_one_or_none()
        if has is None:
            session.delete(row)
            cleaned += 1
    session.flush()
    return {"reconciled": reconciled, "cleaned": cleaned}


def diff_overlay(session: Session) -> list[dict]:
    """覆盖层 vs 源 差异。每 key：无源→'new'；比 event_date/title/note/decade → 'modified'/'unchanged'。"""
    out = []
    srv = session
    for o in list_user_overlays(srv):
        p = o.payload or {}
        key = o.key
        try:
            src = _find_source(session, int(p["event_year"]), p["title"])
        except (KeyError, ValueError):
            src = None
        if src is None:
            out.append({"key": key, "status": "new", "changed_fields": []})
            continue
        changed = []
        if (src.event_date.isoformat() if src.event_date else None) != p.get("event_date"):
            changed.append("event_date")
        if src.title != p.get("title"):
            changed.append("title")
        if src.note != p.get("note"):
            changed.append("note")
        if src.decade != p.get("decade"):
            changed.append("decade")
        out.append({"key": key, "status": "modified" if changed else "unchanged", "changed_fields": changed})
    return out


def restore_overlay(session: Session, key: str) -> dict:
    """重置回源：删覆盖条目（user 行 + 覆盖行），源保留、重新生效。语义同 delete_overlay。"""
    return delete_overlay(session, key)


def source_as_latest(session: Session, key: str) -> dict:
    """以源为最新：把覆盖 payload 吸收为源当前值（消除偏离），保留覆盖行。无源 → no-op。"""
    o = session.execute(select(UserDataOverlay).where(
        UserDataOverlay.section == _SECTION, UserDataOverlay.key == key)).scalar_one_or_none()
    if o is None:
        return {"key": key, "status": "no_source"}
    p = dict(o.payload or {})   # 先 copy：in-place 污染 o.payload 会致 SQLAlchemy 判无变更不持久化
    try:
        src = _find_source(session, int(p["event_year"]), p["title"])
    except (KeyError, ValueError):
        src = None
    if src is None:
        return {"key": key, "status": "no_source"}
    synced = []
    p["event_date"] = src.event_date.isoformat() if src.event_date else None
    p["title"] = src.title
    p["note"] = src.note
    p["decade"] = src.decade
    for f in ("event_date", "title", "note", "decade"):
        synced.append(f)
    o.payload = dict(p)   # 新对象 → JSONB 变更检测（in-place 改不触发持久化）
    row = _get_overlay_row(session, key)
    if row is not None:
        row.event_date = src.event_date
        row.title = src.title
        row.note = src.note
        row.decade = src.decade
    session.flush()
    return {"key": key, "status": "synced", "synced_fields": synced}