"""检测引擎：把规则 → 数据 → 算法 → 异常点 → 前端可渲染的视图模型。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

import algorithm as alg
from algorithm.base import AnomalyPoint, RunStatus, TelemetrySeries

from . import store
from .config import (CHART_MAX_POINTS, LEVEL_CN, LEVEL_COLOR, LEVEL_KEYS,
                     LEVEL_ORDER, SIM_MIN_HITS)
from .meta import build_meta
from .simulate import NEEDS_EXTERNAL, SimResult, autofill_params, simulate_rule

TZ = timezone(timedelta(hours=8))

STATUS_CN = {
    "OK": "命中", "NO_ANOMALY": "正常", "UNKNOWN": "数据不足", "SKIP": "不判",
    "NO_DATA": "无数据", "ERROR": "执行错误",
}


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")


def hms(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=TZ).strftime("%H:%M:%S")


@dataclass
class AlgoOutcome:
    algo_id: str
    name: str
    status: str
    status_cn: str
    reason: str
    points: list[dict]
    params_used: dict
    elapsed_ms: float
    coverage: dict = field(default_factory=dict)


@dataclass
class RuleOutcome:
    rule: dict
    algo_outcomes: list[AlgoOutcome]
    points: list[dict]
    series: dict            # {t:[...], v:[...], name, unit, code}
    period_s: float
    notes: list[str] = field(default_factory=list)
    sim_hits: dict = field(default_factory=dict)
    datasets: list[dict] = field(default_factory=list)
    status_summary: dict = field(default_factory=dict)

    @property
    def max_level(self) -> str:
        if not self.points:
            return ""
        return max((p["level"] for p in self.points), key=lambda x: LEVEL_ORDER.get(x, 0))

    def counters(self) -> dict:
        c = {"total": len(self.points), **{lv: 0 for lv in LEVEL_KEYS}}
        for p in self.points:
            c[p["level"]] = c.get(p["level"], 0) + 1
        return c


def _point_view(p: AnomalyPoint) -> dict:
    d = p.to_dict()
    d["level_cn"] = LEVEL_CN.get(p.level.value, p.level.value)
    d["color"] = LEVEL_COLOR.get(p.level.value, "#94a3b8")
    d["time"] = hms(p.t_start)
    d["time_end"] = hms(p.t_end)
    d["duration_s"] = round(p.duration_ms / 1000.0, 2)
    d["value_s"] = "" if p.value is None else f"{p.value:g}"
    d["threshold_s"] = "" if p.threshold is None else f"{p.threshold:g}"
    return d


def _run_algos(rule: dict, series: TelemetrySeries, peers: dict, meta, extra: dict) -> list[AlgoOutcome]:
    outs: list[AlgoOutcome] = []
    for a in rule.get("algorithms") or []:
        aid = a.get("algo_id")
        params = autofill_params(aid, dict(a.get("params") or {}), series.code)
        try:
            cls = alg.get(aid)
        except KeyError:
            outs.append(AlgoOutcome(aid or "?", "未注册算法", "ERROR", "未注册",
                                    f"算法 {aid} 不存在（可能已被删除）", [], {}, 0.0))
            continue
        if aid in NEEDS_EXTERNAL:
            outs.append(AlgoOutcome(aid, cls.ALGO_NAME, "SKIP", "不判", NEEDS_EXTERNAL[aid],
                                    [], params, 0.0))
            continue
        ctx = alg.AlgorithmContext(
            series=series, meta=meta, peers=peers, params=params, extra=extra,
            window=(int(series.t[0]), int(series.t[-1])) if len(series) else (None, None),
            rule_id=rule.get("rule_id", ""), scene_id=rule.get("scene_id", ""),
        )
        t0 = time.perf_counter()
        res = cls().run(ctx)
        pts = [_point_view(p) for p in res.points]
        outs.append(AlgoOutcome(
            algo_id=aid, name=cls.ALGO_NAME, status=res.status.value,
            status_cn=STATUS_CN.get(res.status.value, res.status.value),
            reason=res.reason, points=pts, params_used=res.params_used,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 2), coverage=res.coverage,
        ))
    return outs


def _series_view(series: TelemetrySeries, ideal_period_ms: float) -> dict:
    """抽稀到约 chart_max_points 个点用于绘图（保留峰值形态），异常点单独用散点画。"""
    t, v = series.t, series.v
    if len(t) == 0:
        return {"t": [], "v": [], "code": series.code, "name": series.meta.name if series.meta else series.code}
    step = max(1, len(t) // CHART_MAX_POINTS)
    idx = np.arange(0, len(t), step)
    if len(t) % step:
        idx = np.append(idx, len(t) - 1)
    return {
        "t": [int(x) for x in t[idx]],
        "v": [None if np.isnan(x) else round(float(x), 6) for x in v[idx]],
        "labels": [hms(int(x)) for x in t[idx]],
        "code": series.code,
        "name": (series.meta.name if series.meta else series.code),
        "unit": (series.meta.unit if series.meta else ""),
        "points": int(len(t)),
    }


def analyze_rule(rule: dict, t0: int, t1: int, *, save: bool = True,
                 min_hits: int = SIM_MIN_HITS, seed: int | None = None) -> RuleOutcome:
    sim: SimResult = simulate_rule(rule, t0, t1, min_hits=min_hits, seed=seed)
    series, peers = sim.series, sim.peers
    meta = series.meta
    extra = dict(sim.extra or {})
    # 指令记录（A-26 在模拟时写入；这里重新生成以保持一致）
    outs = _run_algos(rule, series, peers, meta, extra)
    points = []
    for o in outs:
        points.extend(o.points)
    points.sort(key=lambda p: (p["t_start"], p["telemetry_code"]))
    # 绘图纵坐标：缺口/无值的异常点落在曲线对应位置上，避免把 y 轴拉爆
    for p in points:
        p["plot"] = p.get("value")
        if p.get("hit_type") == "GAP" or p.get("value") is None:
            i = int(np.searchsorted(series.t, int(p["t_start"])))
            i = min(max(i, 0), len(series.t) - 1)
            v = float(series.v[i])
            p["plot"] = None if np.isnan(v) else round(v, 6)

    view = _series_view(series, sim.period_s * 1000)
    view["period_s"] = round(sim.period_s, 3)
    ds = []
    if save:
        rec = store.save_series(series.code, series.t, series.v,
                                int(series.t[0]), int(series.t[-1]), series.quality, source="sim")
        ds.append(rec)
    status_summary = {o.algo_id: o.status for o in outs}
    return RuleOutcome(rule=rule, algo_outcomes=outs, points=points, series=view,
                       period_s=sim.period_s, notes=sim.notes, sim_hits=sim.hits,
                       datasets=ds, status_summary=status_summary)


def analyze_scene(scene: dict, t0: int, t1: int, *, min_hits: int = SIM_MIN_HITS) -> list[RuleOutcome]:
    rules = store.get_rules(scene.get("rule_ids") or [])
    outs = []
    for r in rules:
        r = dict(r, scene_id=scene.get("scene_id", ""))
        outs.append(analyze_rule(r, t0, t1, min_hits=min_hits))
    return outs


def analyze_rules(rules: list[dict], t0: int, t1: int, *, min_hits: int = SIM_MIN_HITS) -> list[RuleOutcome]:
    return [analyze_rule(dict(r, scene_id=r.get("scene_id", "")), t0, t1, min_hits=min_hits)
            for r in rules]


def scene_summary(outs: list[RuleOutcome]) -> dict:
    total = sum(len(o.points) for o in outs)
    by_level = {lv: 0 for lv in LEVEL_KEYS}
    hit_rules = 0
    for o in outs:
        if o.points:
            hit_rules += 1
        for p in o.points:
            by_level[p["level"]] = by_level.get(p["level"], 0) + 1
    return {
        "rules": len(outs), "hit_rules": hit_rules, "total": total,
        "by_level": by_level,
        "coverage": sum(len(o.series.get("t", [])) for o in outs),
    }
