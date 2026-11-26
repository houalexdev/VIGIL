"""
A-18 阈值自校准（Quantile Calibration）—— 基础设施（离线日结）
===========================================================

用途：反查内置阈值是否被误上注。拿健康期分位数与当前配置阈值比，
偏离超过容差就提示"配置复核"。

三条纪律：
1. 健康窗必须剔除机动、开环、切换段、复位后 30 s，否则分位数被污染；
2. 只出提示，**绝不自动改阈值** —— 内置参数上位机改不了，自动改只会让问题沉默；
3. 健康窗 ≥7 天，样本不足返回 insufficient，不硬算。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, AlgorithmContext,
    AnomalyPoint, nanquantile, register,
)


@register
class CalibrationAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-18"
    ALGO_NAME = "阈值自校准"
    ALGO_NAME_EN = "Quantile Calibration"
    LAYER = "L3"
    CATEGORY = "基础设施"
    IS_DETECTOR = False
    SUPPORTED_KINDS = tuple()
    RATIONALE = (
        "M-13③ 要求判断内置阈值有没有被误上注。这件事只能离线日结做，"
        "而且要严格隔离健康窗——把机动段算进去，分位数会自己「适应」故障，永远校准不出问题。"
    )
    USE_CASES = "M-13③ |ELC6001 − 实测稳态 − 3σ| > 20%；全表阈值类判据的定期复核。"
    PARAM_SPECS = [
        ParamSpec("healthy_window_d", "健康窗天数", "int", 7, "天", minimum=1),
        ParamSpec("q_lo", "低分位", "float", 0.001, minimum=0, maximum=0.5),
        ParamSpec("q_hi", "高分位", "float", 0.999, minimum=0.5, maximum=1.0),
        ParamSpec("tolerance", "容差", "float", 0.2, minimum=0, maximum=1),
        ParamSpec("config_threshold", "当前配置阈值", "float", None, unit="工程值"),
        ParamSpec("min_samples", "最少样本", "int", 500, minimum=10),
    ]
    OUTPUT_FIELDS = [
        ("q_lo", "float", "健康期低分位"),
        ("q_hi", "float", "健康期高分位"),
        ("dev", "float", "配置阈值相对分位带的偏离"),
        ("suggestion", "str", "keep/review"),
        ("sufficient", "bool", "样本是否足够"),
    ]

    def detect(self, ctx: AlgorithmContext, p) -> list[AnomalyPoint]:
        return []

    def run_infra(self, ctx: AlgorithmContext, p: Params) -> dict:
        s = ctx.series
        v = s.v
        v = v[~np.isnan(v)]
        if v.size < int(p.min_samples):
            return {"sufficient": False, "n": int(v.size), "suggestion": "insufficient"}
        q_lo = nanquantile(v, float(p.q_lo))
        q_hi = nanquantile(v, float(p.q_hi))
        cfg = p.get("config_threshold")
        if cfg is None:
            return {"sufficient": True, "q_lo": round(q_lo, 6), "q_hi": round(q_hi, 6),
                    "n": int(v.size), "suggestion": "no_config"}
        band = max(q_hi - q_lo, ctx.meta.lsb())
        dev = abs(float(cfg) - q_lo) / band
        return {
            "sufficient": True,
            "q_lo": round(q_lo, 6),
            "q_hi": round(q_hi, 6),
            "dev": round(dev, 4),
            "n": int(v.size),
            "suggestion": "review" if dev > float(p.tolerance) else "keep",
        }
