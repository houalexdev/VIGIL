"""
A-20 趋势特征向量（Trend Feature Vector）
========================================

把一段曲线压成 8 维定长特征，与同工况基线段特征集做稳健 z 比较。
判据原文：8 维中任一维稳健 z > 3 且连续 2 段 → 一般。

8 维含义：归一化斜率、曲率、拐点数、单调性、极差比、峰相位、净漂移、振荡度。

四个坑（都会导致结果不可用）：
1. 必须用中位数/MAD，用均值/标准差会被基线里一个异常段带偏；
2. MAD = 0 的维（如恒定单调 f4 = 1.0）必须剔除，否则 z 除零；
3. 单位统一用 LSB，别用 mm —— 0.001 的系数会把曲率维淹掉；
4. 拐点对噪声极敏感，必须先 3 点中值平滑。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, smooth3,
    register,
)

DIM_NAMES = ["f1_斜率", "f2_曲率", "f3_拐点数", "f4_单调性", "f5_极差比", "f6_峰相位", "f7_净漂移", "f8_振荡度"]


def trend_features(v: np.ndarray, range_base: float = 1.0) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    v = v[~np.isnan(v)]
    n = v.size
    if n < 5:
        return np.full(8, np.nan)
    t = np.arange(n) / n
    rng = max(float(v.max() - v.min()), 1.0)
    d = np.diff(v)
    d2 = np.abs(np.diff(v, 2))
    flips = 0
    if d.size > 1:
        s = np.sign(d)
        s = s[s != 0]
        if s.size > 1:
            flips = int(np.count_nonzero(np.diff(s) != 0))
    return np.array([
        float(np.polyfit(t, v, 1)[0] * n / rng),
        float(np.median(d2) / rng) if d2.size else 0.0,
        float(flips),
        float(np.mean(d > 0)) if d.size else 0.0,
        float(rng / max(range_base, 1e-12)),
        float(np.argmax(v) / n),
        float((v[-1] - v[0]) / rng),
        float(np.count_nonzero(np.isclose(d, 0, atol=1e-12)) / max(n - 1, 1)),
    ])


@register
class TrendFeatureAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-20"
    ALGO_NAME = "趋势特征向量"
    ALGO_NAME_EN = "Trend Feature Vector"
    LAYER = "L2"
    CATEGORY = "趋势"
    HIT_TYPE = HitType.SEGMENT_SHAPE
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS,)
    REQUIRES_BASELINE = True
    RATIONALE = (
        "光看幅值抓不到「形状变了」：同样在 [0,45] mm 区间内，正常段是缓慢收敛，"
        "故障段可能先抖后塌。8 维特征把形状量化，再用稳健 z 判断，"
        "是唯一能在幅值还没越限时发现趋势异常的判据。"
    )
    USE_CASES = "M-01⑩、M-02⑤⑥ 段形特征任一维稳健 z>3 且连续 2 段。"
    PARAM_SPECS = [
        ParamSpec("z_threshold", "稳健 z 门限", "float", 3.0, minimum=1.0),
        ParamSpec("consecutive_segments", "连续异常段数", "int", 2, "段", minimum=1,
                  help="单段异常多为噪声，连续 2 段才升级"),
        ParamSpec("baseline_min_segments", "基线最少段数", "int", 10, "段", minimum=3,
                  help="不足则输出 UNKNOWN；返回 0 等于「完全吻合」，是错的"),
        ParamSpec("smooth", "拐点前先平滑", "bool", True),
        ParamSpec("min_len", "最短段长", "int", 300, "拍", minimum=10,
                  help="未提供 segments 时，用整段按该长度粗切"),
    ]
    OUTPUT_FIELDS = [
        ("features", "list", "8 维特征值"),
        ("z", "list", "各维稳健 z"),
        ("offending_dims", "list", "超限维名称"),
        ("segment_id", "str", "段标识"),
        ("baseline_segments", "int", "基线段数"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        base = ctx.baseline.get("features") or []
        if len(base) < int(p.baseline_min_segments):
            return Precheck(
                RunStatus.UNKNOWN,
                f"基线特征段 {len(base)} < {p.baseline_min_segments}，分位数与 z 无统计意义（冷启动期输出不知道）",
            )
        return pre

    def _segments(self, ctx: AlgorithmContext, p: Params) -> list[tuple[int, int]]:
        segs = ctx.extra.get("segments")
        if segs:
            return [(int(a), int(b)) for a, b, *_ in segs]
        n = len(ctx.series)
        step = int(p.min_len)
        return [(a, min(a + step - 1, n - 1)) for a in range(0, n, step) if n - a >= 10]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        base = np.asarray(ctx.baseline["features"], dtype=float)
        med = np.nanmedian(base, axis=0)
        mad = 1.4826 * np.nanmedian(np.abs(base - med), axis=0)
        ok = mad > 0
        lsb = ctx.meta.lsb()
        out: list[AnomalyPoint] = []
        streak = 0
        for sid, (a, b) in enumerate(self._segments(ctx, p)):
            v = s.v[a: b + 1]
            if p.smooth:
                v = smooth3(v)
            f = trend_features(v, range_base=max(ctx.baseline.get("range_med", lsb * 100), lsb))
            if not np.all(np.isfinite(f[ok])):
                continue
            z = np.zeros(8)
            z[ok] = (f[ok] - med[ok]) / mad[ok]
            bad = [DIM_NAMES[i] for i in range(8) if ok[i] and abs(z[i]) > float(p.z_threshold)]
            if bad:
                streak += 1
            else:
                streak = 0
            if not bad or streak < int(p.consecutive_segments):
                continue
            out.append(point(
                ctx, Level.parse(p.level), HitType.SEGMENT_SHAPE, int(s.t[a]), int(s.t[b]),
                value=float(np.nanmean(v)),
                deviation=float(np.nanmax(np.abs(z[ok]))) if ok.any() else None,
                threshold=float(p.z_threshold),
                dimension=f"seg:{sid}",
                sample_count=int(b - a + 1),
                strength=min(1.0, 0.5 + 0.5 * min(float(np.nanmax(np.abs(z[ok]))) / 10.0, 1.0)),
                evidence={
                    "features": [round(float(x), 4) for x in f],
                    "z": [round(float(x), 3) for x in z],
                    "offending_dims": bad,
                    "segment_id": f"seg:{sid}",
                    "baseline_segments": int(base.shape[0]),
                    "consecutive": streak,
                },
            ))
        return out
