"""
A-05 速率 / 斜率估计（Theil-Sen / 平滑差分）
============================================

判据原文：|dV/dt| > 0.5 mm/s，连续 5 拍。

两个必须踩住的坑：
1. 直接 (V[k]-V[k-1])/Δt 会被量化噪声淹没 —— 1 LSB / 1 s 就是 0.001 mm/s，
   必须先 3 点中值平滑再差分。
2. Δt 必须用实际时间戳，不能用标称包周期 —— 传输有抖动，标称值会系统性偏差。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, confirm, runs, smooth3,
    register,
)


@register
class RateAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-05"
    ALGO_NAME = "速率/斜率估计"
    ALGO_NAME_EN = "Theil-Sen / Smoothed Difference"
    LAYER = "L1"
    CATEGORY = "趋势"
    HIT_TYPE = HitType.RATE
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS, ValueKind.COUNTER)
    RATIONALE = (
        "慢漂不越限、但一定超速率。位移被闭环拉回来之前会先快速变化，"
        "温度在故障前会有异常爬升，计数类突增也是速率问题。"
        "速率判据的价值在于比越限早一个时间常数。"
    )
    USE_CASES = "M-01⑤ |dV/dt|>0.5 mm/s 连续 5 拍；M-04② 不一致计数 10 min 增量；M-02⑤ 段内衰减斜率。"
    PARAM_SPECS = [
        ParamSpec("window", "差分跨度", "int", 11, "拍", minimum=2,
                  help="首尾相隔多少拍算斜率；太小被噪声淹没，太大漏掉短时快变"),
        ParamSpec("max_rate", "速率阈值", "float", 0.5, unit="工程值/秒", minimum=0,
                  required=True, help="超过该值判异常；按物理量级给，如位移 0.5 mm/s、温度 1 ℃/s"),
        ParamSpec("direction", "方向", "enum", "both", choices=["both", "up", "down"],
                  help="both=双向；up=只报上升；down=只报下降"),
        ParamSpec("k", "连续成立拍数", "int", 5, "拍", minimum=1),
        ParamSpec("smooth", "先做 3 点中值平滑", "bool", True,
                  help="强烈建议开启，否则量化噪声会被微分放大"),
        ParamSpec("max_gap_s", "跨缺口不判定", "seconds", 10.0, minimum=0),
    ]
    OUTPUT_FIELDS = [
        ("rate", "float", "估计速率（工程值/秒）"),
        ("direction", "str", "up/down"),
        ("dt_s", "float", "首尾实际时间差"),
        ("applied_threshold", "float", "生效阈值"),
    ]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        w = int(p.window)
        if len(s) <= w:
            return []
        v = smooth3(s.v) if p.smooth else s.v.copy()
        dt = (s.t[w:] - s.t[:-w]) / 1000.0
        with np.errstate(invalid="ignore", divide="ignore"):
            rate = (v[w:] - v[:-w]) / dt
        rate = np.concatenate([np.full(w, np.nan), rate])
        thr = float(p.max_rate)

        with np.errstate(invalid="ignore"):
            if p.direction == "up":
                hit = rate > thr
            elif p.direction == "down":
                hit = rate < -thr
            else:
                hit = np.abs(rate) > thr
        # 缺口保护：跨度内有丢包则不算
        gap = np.concatenate([np.zeros(w, dtype=bool), (s.t[w:] - s.t[:-w]) > float(p.max_gap_s) * 1000])
        hit &= ~gap & ~np.isnan(rate)

        confirmed = confirm(hit, int(p.k), allow_unknown=False) if int(p.k) > 1 else hit
        out: list[AnomalyPoint] = []
        for a, b in runs(confirmed):
            seg = rate[a: b + 1]
            rep = float(seg[np.argmax(np.abs(seg))])
            out.append(point(
                ctx, Level.parse(p.level), HitType.RATE, int(s.t[a]), int(s.t[b]),
                value=float(s.v[b]),
                prev_value=float(s.v[max(0, a - w)]),
                deviation=round(abs(rep), 6),
                threshold=thr,
                strength=min(1.0, 0.5 + 0.5 * min(abs(rep) / thr / 3.0, 1.0)) if thr else 0.5,
                sample_count=int(b - a + 1),
                evidence={
                    "rate": round(rep, 6),
                    "direction": "up" if rep > 0 else "down",
                    "dt_s": round(float(dt[max(0, a - w): b - w + 1].max()) if b >= w else dt[0], 3),
                    "applied_threshold": thr,
                    "window": w,
                },
            ))
        return out
