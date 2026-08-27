"""人物亲缘关系推理（#197 · 图谱/`relationship` 边来源之一）。

从每个 person 实体 `entity.fields["与主角的关系"]`（称谓，如「养父的父亲」「养母的母亲」）
推导成对亲缘边，供图谱虚线「建议」显示。只推理、不写库（写库/抑制走 graph API）。

称谓 → 亲缘（相对主角 Stijn）：
- 养父/父亲 → Stijn 之父；养母/母亲 → Stijn 之母（夫妻组=「主角」锚）
- 「养父的父/母亲」= 祖父/祖母；「养母的父/母亲」= 外祖父/外祖母（同锚且父/母互补 → 夫妻）
- 哥哥/姐姐/弟弟/妹妹/双胞胎* → Stijn 的兄弟姐妹
- 始祖/世祖/先祖 → Stijn 的祖先
职称经 `TITLE_ENTITY` 解析到规范实体（养父→Joren、养母→Johanna…）。

边只在两端都在当前 person 集合内时产出；自环跳过、去重（含反向）。
"""
from __future__ import annotations

import re
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Entity
from app.ingest.holders import TITLE_ENTITY

PROTAGONISTS = {"Stijn Peeters", "夏LY"}

_RE_LINK = re.compile(r"^(.+?)(?:的)?(父亲|母亲)$")   # 「养父的父亲」→(养父,父亲)
_SIB = ("哥哥", "弟弟", "姐姐", "妹妹")
_ANCESTOR = ("始祖", "世祖", "先祖")

# 称谓 → (anchor, sex)。anchor 为相对主角的父/母侧（养父/养母/主角）；父/母互补成夫妻。
_ROLE_MAP: dict[str, tuple[str, str]] = {
    "养父": ("主角", "male"), "父亲": ("主角", "male"),
    "养母": ("主角", "female"), "母亲": ("主角", "female"),
    "祖父": ("养父", "male"), "养祖父": ("养父", "male"), "养父的父亲": ("养父", "male"),
    "祖母": ("养父", "female"), "养祖母": ("养父", "female"), "养父的母亲": ("养父", "female"),
    "外祖父": ("养母", "male"), "养外祖父": ("养母", "male"), "养母的父亲": ("养母", "male"),
    "外祖母": ("养母", "female"), "养外祖母": ("养母", "female"), "养母的母亲": ("养母", "female"),
}


def _role_spec(role: str) -> dict | None:
    """称谓 → 亲缘描述；未知返回 None（调用方兜底连主角）。"""
    r = (role or "").strip().replace("，", "").replace(" ", "")
    if not r or "领养养子" in r or "主角" == r:
        return {"kind": "self"}
    if any(a in r for a in _ANCESTOR):
        return {"kind": "ancestor"}
    if r.startswith("双胞胎"):
        core = r.replace("双胞胎", "")
        if core in _SIB or core == "":
            return {"kind": "sibling"}
    if r in _SIB:
        return {"kind": "sibling"}
    if r in _ROLE_MAP:
        anchor, sex = _ROLE_MAP[r]
        return {"kind": "parent", "anchor": anchor, "sex": sex}
    m = _RE_LINK.match(r)                       # 「X的父亲/母亲」
    if m:
        anchor = m.group(1)
        sex = "male" if m.group(2) == "父亲" else "female"
        return {"kind": "parent", "anchor": anchor, "sex": sex}
    return None


def _canonical(name: str, by_name_id: dict[str, int]) -> int | None:
    """职称/别名 → 规范实体 id（先按 name 精确，再按 TITLE_ENTITY 映射）。"""
    direct = by_name_id.get(name)
    if direct is not None:
        return direct
    return by_name_id.get(TITLE_ENTITY.get(name, name))


def infer_person_edges(session: Session) -> list[dict]:
    """推理由 from/to/rel_type/note 组成的亲缘边；两端都须为当前 person 实体。"""
    ents = session.execute(select(Entity).where(
        Entity.entity_type == "person")).scalars().all()
    if not ents:
        return []
    by_id = {e.id: e for e in ents}
    by_name_id = {e.name: e.id for e in ents}
    root_id = next((e.id for e in ents if e.name in PROTAGONISTS), None)

    specs: dict[int, dict | None] = {}
    roles: dict[int, str] = {}
    for e in ents:
        role = ((e.fields or {}).get("与主角的关系") or "").strip()
        specs[e.id] = _role_spec(role) if role else {"kind": "self"}
        roles[e.id] = role

    edges: list[dict] = []
    seen: set[tuple[int, int]] = set()
    spouse_groups: dict[str, dict[str, int]] = {}   # anchor → {sex: entity_id}

    def add(f, t, rel_type, note):
        if f == t or (f, t) in seen or (t, f) in seen:
            return
        seen.add((f, t))
        edges.append({"from": f, "to": t, "rel_type": rel_type,
                      "note": note, "inferred": True})

    for pid in by_id:
        spec = specs.get(pid)
        if not spec:
            continue
        kind = spec.get("kind")
        if kind == "self":
            continue
        if kind == "ancestor":
            if root_id is not None:
                add(pid, root_id, "祖先", roles[pid])
            continue
        if kind == "sibling":
            if root_id is not None:
                add(pid, root_id, "兄弟姐妹", roles[pid])
            continue
        anchor = spec.get("anchor")
        sex = spec.get("sex")
        child_id = root_id if anchor in ("主角", "本人") \
            else (_canonical(anchor, by_name_id) if anchor else root_id)
        if child_id is not None and child_id != pid:
            rel = "父子" if sex == "male" else "母女"
            add(pid, child_id, rel, roles[pid])
        # 夫妻组按 anchor 分（避免多个同 child=None 的祖辈组互相覆盖）
        key = anchor or "主角"
        spouse_groups.setdefault(key, {})[sex] = pid

    for key, grp in spouse_groups.items():
        m, f = grp.get("male"), grp.get("female")
        if m is not None and f is not None:
            add(m, f, "夫妻", "#197 称谓互补（父/母 → 夫妻）")

    # 兜底：称谓未知且非主角 → 连主角（保证有线）
    for pid in by_id:
        if specs.get(pid) is None and root_id is not None:
            add(pid, root_id, roles[pid] or "关系", roles[pid])

    return edges