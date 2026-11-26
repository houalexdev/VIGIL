"""
A-21 段形比对（Segment Template Matching）
=========================================

判据原文：相位重采样后超出基线 P5~P95 带的相位点占比 >10% → 一般；>30% 或 DTW 距离 >3×
基线中位数 → 严重。

为什么按相位重采样而不是按索引：丢包会让段内采样不均，按索引比对等于把「缺了几个点」
当成「形状变了」。按时间归一到 [0,1] 再取 101 点，才是同一物理过程的比较。

四个坑：
1. 基线 < 10 段时必须返回 UNKNOWN；返回 0 等于「完全吻合」，会漏报；
2. 段首尾 5% 天然离散大，包络自然宽，须掐头去尾或分区阈值；
3. DTW 距离必须除以段长，否则长段必然更大；
4. 基线必须跨测试日，只取当天会把当天温漂固化成「正常形状」。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, dtw_distance,
    phase_resample, register,
)


@register
class SegmentTemplateAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-21"
    ALGO_NAME = "段形比对"
    ALGO_NAME_EN = "Segment Template Matching"
    LAYER = "L2"
    CATEGORY = "趋势"
    HIT_TYPE = HitType.SEGMENT_SHAPE
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS,)
    REQUIRES_BASELINE = True
    RATIONALE = (
        "A-20 回答「这一段形状像不像正常」，A-21 回答「这一段每一时刻偏出正常带多少」。"
        "前者定位到维，后者定位到相位点，两者配合才能在报告里指出"
        "「第 37% 处开始偏离」。这是段形判据从告警变成可复盘证据的关键一步。"
    )
    USE_CASES = "M-01⑨ 相位点超带占比 >10%/30%；M-02⑥ 某一路段形到同族中位形状距离 >3×MAD。"
    PARAM_SPECS = [
        ParamSpec("phase_points", "相位重采样点数", "int", 101, "点", minimum=11),
        ParamSpec("q_low", "包络低分位", "float", 5.0, unit="%", minimum=0, maximum=50),
        ParamSpec("q_high", "包络高分位", "float", 95.0, unit="%", minimum=50, maximum=100),
        ParamSpec("tol", "包络容差", "float", 0.02, minimum=0,
                  help="容差按包络宽度的比例给，不是绝对值"),
        ParamSpec("dev_ratio_l1", "一般级偏离占比", "float", 0.10, minimum=0, maximum=1),
        ParamSpec("dev_ratio_l3", "严重级偏离占比", "float", 0.30, minimum=0, maximum=1),
        ParamSpec("dtw_ratio", "DTW 倍数门限", "float", 3.0, minimum=1.0),
        ParamSpec("baseline_min_segments", "基线最少段数", "int", 10, "段", minimum=3),
        ParamSpec("trim_ratio", "首尾掐除比例", "float", 0.05, minimum=0, maximum=0.2),
    ]
    OUTPUT_FIELDS = [
        ("dev_ratio", "float", "超出包络的相位点占比"),
        ("dtw", "float", "到基线中位形状的 DTW 距离"),
        ("dtw_ratio_actual", "float", "DTW 相对基线中位数的倍数"),
        ("worst_phase", "float", "偏离最大的相位（0~1）"),
        ("band_width_med", "float", "包络中位宽度"),
        ("baseline_segments", "int", "基线段数"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        segs = ctx.baseline.get("segments") or []
        if len(segs) < int(p.baseline_min_segments):
            return Precheck(
                RunStatus.UNKNOWN,
                f"基线段 {len(segs)} < {p.baseline_min_segments}（或未跨 3 个测试日），包络无意义",
            )
        return pre

    def _segments(self, ctx: AlgorithmContext) -> list[tuple[int, int]]:
        segs = ctx.extra.get("segments")
        if segs:
            return [(int(a), int(b)) for a, b, *_ in segs]
        return [(0, len(ctx.series) - 1)]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        N = int(p.phase_points)
        base = ctx.baseline["segments"]
        B = []
        for seg in base:
            if isinstance(seg, dict):
                tt, vv = np.asarray(seg["t"], float), np.asarray(seg["v"], float)
            elif isinstance(seg, (tuple, list)) and len(seg) == 2:
                tt, vv = np.asarray(seg[0], float), np.asarray(seg[1], float)
            else:
                vv = np.asarray(seg, float)
                tt = np.arange(vv.size, dtype=float)
            B.append(phase_resample(tt, vv, N))
        Bm = np.vstack(B)
        lo = np.nanpercentile(Bm, float(p.q_low), axis=0)
        hi = np.nanpercentile(Bm, float(p.q_high), axis=0)
        band = np.maximum(hi - lo, ctx.meta.lsb())
        med_shape = np.nanmedian(Bm, axis=0)
        base_dtw = np.array([dtw_distance(b, med_shape) for b in B])
        base_dtw_med = float(np.nanmedian(base_dtw)) or 1e-6

        trim = int(float(p.trim_ratio) * N)
        sl = slice(trim, N - trim)
        out: list[AnomalyPoint] = []
        for sid, (a, b) in enumerate(self._segments(ctx)):
            tt, vv = s.t[a: b + 1].astype(float), s.v[a: b + 1]
            if np.count_nonzero(~np.isnan(vv)) < 5:
                continue
            cur = phase_resample(tt, vv, N)
            with np.errstate(invalid="ignore"):
                over = (cur > hi + float(p.tol) * band) | (cur < lo - float(p.tol) * band)
            dev_ratio = float(np.nanmean(over[sl]))
            dtw = dtw_distance(cur, med_shape)
            ratio_actual = dtw / base_dtw_med
            if dev_ratio > float(p.dev_ratio_l3) or ratio_actual > float(p.dtw_ratio):
                level = Level.parse("严重")
            elif dev_ratio > float(p.dev_ratio_l1):
                level = Level.parse(p.level)
            else:
                continue
            worst = float(np.nanargmax(np.abs(cur - med_shape)) / (N - 1))
            out.append(point(
                ctx, level, HitType.SEGMENT_SHAPE, int(tt[0]), int(tt[-1]),
                value=round(dev_ratio, 4),
                expected=float(p.dev_ratio_l1),
                deviation=round(dev_ratio - float(p.dev_ratio_l1), 4),
                threshold=float(p.dev_ratio_l1),
                dimension=f"seg:{sid}",
                strength=min(1.0, 0.5 + 0.5 * min(dev_ratio / max(float(p.dev_ratio_l3), 1e-6), 1.0)),
                sample_count=int(b - a + 1),
                evidence={
                    "dev_ratio": round(dev_ratio, 4),
                    "dtw": round(dtw, 6),
                    "dtw_ratio_actual": round(ratio_actual, 3),
                    "worst_phase": round(worst, 4),
                    "band_width_med": round(float(np.nanmedian(band)), 6),
                    "baseline_segments": len(B),
                },
            ))
        return out
