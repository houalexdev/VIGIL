"""预置内容（数据源：``config/presets.yaml``）。

不再在代码里写死判据：规则、算法参数、场景组成全部来自 YAML。
启动时写入数据库，规则 ID 固定，重复启动不会重复插入，也不会覆盖用户改动。
"""
from __future__ import annotations

from . import store, yamls
from .meta import get_def

_PRE = yamls.file("presets.yaml")

# 兼容旧引用：presets.SCENE_ID / presets._RULES
_RULES: list[dict] = list(_PRE.get("rules") or [])
_SCENES: list[dict] = list(_PRE.get("scenes") or [])
SCENE_ID = str(_SCENES[0]["id"]) if _SCENES else ""


def preset_rule_ids() -> list[str]:
    return [str(r["id"]) for r in _RULES]


def build_preset_rules() -> list[dict]:
    """把 YAML 规则展开成 store.upsert_rule 需要的结构。

    元数据默认取自测点目录；YAML 里显式给了就以 YAML 为准（允许预置规则
    用与目录不同的值域，例如特殊工况下的收窄判据）。
    """
    out = []
    for i, r in enumerate(_RULES, start=1):
        code = str(r["code"])
        d = get_def(code)
        override = {k: r[k] for k in ("code_name", "unit", "kind", "low", "high",
                                      "bit_width", "quantum") if r.get(k) is not None}
        judge = str(r.get("judge") or "").strip()
        note = str(r.get("note") or "").strip()
        out.append({
            "rule_id": str(r["id"]),
            "name": str(r["name"]),
            "code": code,
            "code_name": override.pop("code_name", d.name),
            "unit": override.pop("unit", d.unit),
            "kind": override.pop("kind", d.kind),
            "low": override.pop("low", d.low),
            "high": override.pop("high", d.high),
            "bit_width": override.pop("bit_width", d.bit_width),
            "quantum": override.pop("quantum", d.quantum),
            "note": "；".join(x for x in (judge, note) if x),
            "source": str(r.get("source") or "preset"),
            "sort_no": int(r.get("sort_no", i)),
            "algorithms": [
                {"algo_id": str(a["algo_id"]),
                 "params": dict(a.get("params") or {}),
                 "note": str(a.get("note") or "")}
                for a in (r.get("algorithms") or [])
            ],
        })
    return out


def seed(force: bool = False) -> bool:
    """写入预置规则与场景。

    ``force=False`` 时做**增量补齐**：逐条比对，只写数据库里还没有的预置规则，
    再把场景的 rule_ids 补上缺的那部分；已有的规则/场景内容不覆盖，避免抹掉用户改动。
    这样后续往 presets.yaml 里追加判据时，老库升级也能自动跟上。

    ``force=True`` 时按 YAML 全量重建（用于预置内容本身被修正的场景）。
    """
    if not _RULES:
        return False
    ids = preset_rule_ids()

    rules = build_preset_rules()
    changed = False
    for r in rules:
        if force or not store.get_rule(r["rule_id"]):
            store.upsert_rule(r, new=False)
            changed = True

    for s in _SCENES:
        sid = str(s["id"])
        scene_rules = [str(x) for x in (s.get("rules") or [])] or ids
        old = store.get_scene(sid)
        if force or not old:
            store.upsert_scene({
                "scene_id": sid,
                "name": str(s.get("name") or sid),
                "desc": str(s.get("desc") or ""),
                "rule_ids": scene_rules,
                "source": str(s.get("source") or "preset"),
            })
            changed = True
            continue
        # 已存在的场景：只补缺失的规则，保留用户自行增删的顺序
        merged = list(old.get("rule_ids") or [])
        missing = [rid for rid in scene_rules if rid not in merged]
        if missing:
            store.upsert_scene({
                "scene_id": sid,
                "name": old.get("name") or str(s.get("name") or sid),
                "desc": old.get("desc") or str(s.get("desc") or ""),
                "rule_ids": merged + missing,
                "source": old.get("source") or "preset",
            })
            changed = True
    return changed
