"""
A-04 噪声水平比（Noise Inflation / Collapse）
=============================================

std(V)/σ_base > 3 → 噪声膨胀（松动、干扰、接地不良）
std(V)/σ_base < 0.2 → 噪声塌缩（卡死前兆、采样电路失效）

σ_base 必须按工况分段（开环/闭环、台架/运行、锁紧/悬浮），不能全任务共用一个。
量化空载时 sd ≈ 0.29 LSB，下阈 0.2 已经接近地板，不能再抬。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, nanstd,
    runs, rolling_std, register,
)


@register
class NoiseAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-04"
    ALGO_NAME = "噪声水平比"
    ALGO_NAME_EN = "Noise Inflation / Collapse"
    LAYER = "L1"
    CATEGORY = "噪声"
    HIT_TYPE = HitType.NOISE
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS,)
    REQUIRES_BASELINE = True
    RATIONALE = (
        "越限判据看的是「偏了多少」，噪声判据看的是「抖得对不对」。探头松动、"
        "线缆屏蔽失效、采集电路退化，在均值还没跑偏时噪声已经变了；反过来，"
        "噪声塌缩往往出现在通道卡死之前。这一对方向必须同时监视。"
    )
    USE_CASES = "M-01④ std(V)/σ_base >3 或 <0.2（窗 60 拍）。"
    PARAM_SPECS = [
        ParamSpec("window", "统计窗长", "int", 60, "拍", minimum=5),
        ParamSpec("ratio_up", "膨胀阈值", "float", 3.0, minimum=1.0),
        ParamSpec("ratio_down", "塌缩阈值", "float", 0.2, minimum=0.0, maximum=1.0),
        ParamSpec("sigma_base", "基线 σ", "float", None, unit="工程值", minimum=0,
                  auto_from="baseline.sigma", help="留空则取基线服务（A-06）按工况给出的 σ"),
        ParamSpec("baseline_window_s", "在线估计基线窗", "seconds", 7200.0, minimum=60,
                  help="没有离线基线时，用序列开头这段时长估计 σ_base"),
        ParamSpec("detect_collapse", "检测噪声塌缩", "bool", True),
        ParamSpec("hold_points", "需持续点数", "int", 3, "拍", minimum=1,
                  help="ratio 连续超阈多少拍才确认"),
    ]
    OUTPUT_FIELDS = [
        ("sd", "float", "窗内标准差"),
        ("sigma_base", "float", "基线标准差"),
        ("ratio", "float", "sd / σ_base"),
        ("direction", "str", "inflate/collapse"),
    ]

    def _sigma_base(self, ctx: AlgorithmContext, p: Params) -> float:
        if p.sigma_base:
            return float(p.sigma_base)
        if ctx.baseline.get("sigma"):
            return float(ctx.baseline["sigma"])
        # 在线回退：取序列最前面的一段估计
        s = ctx.series
        warm = s.t[0] + int(float(p.baseline_window_s) * 1000)
        head = s.v[s.t <= warm]
        return nanstd(head)

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        sb = self._sigma_base(ctx, p)
        if not np.isfinite(sb) or sb <= 0:
            return Precheck(RunStatus.UNKNOWN, "缺少可用基线 σ（σ_base ≤ 0 或全为缺失），无法比值判定")
        if int(p.window) >= len(ctx.series):
            return Precheck(RunStatus.UNKNOWN, f"序列长度 {len(ctx.series)} 小于统计窗 {p.window}")
        return pre

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        sd = rolling_std(s.v, int(p.window))
        sb = self._sigma_base(ctx, p)
        ratio = sd / sb
        base_level = Level.parse(p.level)
        out: list[AnomalyPoint] = []

        def collect(mask: np.ndarray, direction: str, level: Level) -> None:
            for a, b in runs(mask):
                if (b - a + 1) < int(p.hold_points):
                    continue
                r = ratio[a: b + 1]
                rep = float(np.nanmax(r) if direction == "inflate" else np.nanmin(r))
                out.append(point(
                    ctx, level, HitType.NOISE, int(s.t[a]), int(s.t[b]),
                    value=float(s.v[b]),
                    expected=sb,
                    deviation=round(rep - 1.0, 6),
                    threshold=float(p.ratio_up if direction == "inflate" else p.ratio_down),
                    strength=min(1.0, 0.5 + 0.5 * min(abs(rep - 1.0), 3.0) / 3.0),
                    sample_count=int(b - a + 1),
                    evidence={
                        "sd": round(float(np.nanmedian(sd[a:b + 1])), 6),
                        "sigma_base": round(sb, 6),
                        "ratio": round(rep, 4),
                        "direction": direction,
                    },
                ))

        with np.errstate(invalid="ignore"):
            collect(ratio > float(p.ratio_up), "inflate", base_level)
            if p.detect_collapse:
                # 塌缩单独给低一级：噪声变小多数是卡死前兆，需与 A-02 交叉确认
                collect(ratio < float(p.ratio_down), "collapse", Level.L1)
        return out
