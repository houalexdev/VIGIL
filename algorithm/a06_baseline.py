"""
A-06 稳健基线（Rolling Median / Quantile）—— 基础设施
=====================================================

不产生异常点，只回答一个问题：这个代号在**当前工况**下的正常水平是多少。

两条硬规则：
1. 用中位数/分位数，不用均值 —— 污染段会把均值拖低，阈值跟着降，越污染越不报警。
2. 基线必须按工况分段（台架/运行、开环/闭环、锁紧/悬浮），cond_key 不同绝不混用。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, AlgorithmContext,
    AnomalyPoint, ValueKind, mad, median,
    nanquantile, robust_sigma, register,
)


@register
class BaselineAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-06"
    ALGO_NAME = "稳健基线"
    ALGO_NAME_EN = "Rolling Median / Quantile"
    LAYER = "L3"
    CATEGORY = "基础设施"
    IS_DETECTOR = False
    SUPPORTED_KINDS = tuple(ValueKind)
    REQUIRES_BASELINE = False
    RATIONALE = (
        "所有自适应判据都依赖基线。基线一旦用均值，污染持续越久阈值越低，最终永远不报警——"
        "这是自适应检测最经典的反面教材。分位数基线还顺带给出 P5/P95 带，供段形比对使用。"
    )
    USE_CASES = "M-02① 振动基线中位数 B50；A-04 的 σ_base；A-20/A-21 的段形基线集。"
    PARAM_SPECS = [
        ParamSpec("baseline_window_s", "基线窗长", "seconds", 21600.0, minimum=60,
                  help="建议 ≥6 h 干净数据；冷启动不足时返回不足标记，不硬算"),
        ParamSpec("min_samples", "最少样本", "int", 100, minimum=5),
        ParamSpec("q_low", "低分位", "float", 0.05, minimum=0, maximum=0.5),
        ParamSpec("q_high", "高分位", "float", 0.95, minimum=0.5, maximum=1.0),
        ParamSpec("segment_by", "工况分段键", "str", "",
                  help="如 SPN0001|SPN7002，由引擎按门控拼成 cond_key，不同段各存一份基线"),
    ]
    OUTPUT_FIELDS = [
        ("cond_key", "str", "工况标识"),
        ("median", "float", "中位数"),
        ("mad", "float", "中位绝对偏差"),
        ("sigma", "float", "稳健 σ（1.4826·MAD）"),
        ("q05", "float", "5% 分位"),
        ("q95", "float", "95% 分位"),
        ("n", "int", "参与样本数"),
        ("sufficient", "bool", "样本是否足够"),
    ]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        return []  # 基础设施不出异常点

    def compute(
        self,
        v: np.ndarray,
        cond_key: str = "",
        min_samples: int = 100,
        q_low: float = 0.05,
        q_high: float = 0.95,
    ) -> dict:
        v = np.asarray(v, dtype=float)
        v = v[~np.isnan(v)]
        if v.size < min_samples:
            return {"cond_key": cond_key, "n": int(v.size), "sufficient": False}
        return {
            "cond_key": cond_key,
            "median": round(median(v), 6),
            "mad": round(mad(v), 6),
            "sigma": round(robust_sigma(v), 6),
            "q05": round(nanquantile(v, q_low), 6),
            "q95": round(nanquantile(v, q_high), 6),
            "min": round(float(np.min(v)), 6),
            "max": round(float(np.max(v)), 6),
            "n": int(v.size),
            "sufficient": True,
        }

    def run_infra(self, ctx: AlgorithmContext, p: Params) -> dict:
        s = ctx.series
        t1 = s.t[-1] if len(s) else 0
        t0 = t1 - int(float(p.baseline_window_s) * 1000)
        seg = s.window(t0, t1)
        key = str(ctx.baseline.get("cond_key", "")) or "default"
        return self.compute(seg.v, key, int(p.min_samples), float(p.q_low), float(p.q_high))
