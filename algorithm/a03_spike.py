"""
A-03 跳变 / 野值检测（Hampel / MAD step）
=========================================

判据原文：|V(k) − V(k−1)| > max(0.5 mm, 3.5·1.4826·MAD, 3 LSB)。

三个下限缺一不可：
- a_abs 物理下限：防止基线 MAD 很小的时候把量化跳变判成故障；
- z·σ 自适应：不同工况噪声不同，固定阈值一定有一头误报；
- 3 LSB 地板：MAD = 0（长稳态）时 σ 会退化为 0，阈值变 0，任何抖动都报警。

二级确认：下一拍若回落到 50% 以内，属于单点野值（毛刺）；未回落属于台阶（真实跳变）。
两者处置不同，必须分开标注。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, robust_sigma, sliding, register,
)


@register
class SpikeAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-03"
    ALGO_NAME = "跳变/野值检测"
    ALGO_NAME_EN = "Hampel / MAD Step"
    LAYER = "L1"
    CATEGORY = "跳变"
    HIT_TYPE = HitType.SPIKE
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS, ValueKind.COUNTER)
    RATIONALE = (
        "野值与台阶是两种完全不同的故障：野值多为传输误码或器件刷新偶发（如 FPGA 结温"
        "由 12.79℃ 跳到 200.8℃ 又恢复），台阶多为真实状态跃变。用「下一拍是否回落」"
        "一次判定即可分开，无需两套算法。"
    )
    USE_CASES = (
        "M-01③ 位移跳变；M-04② 不一致计数突增；自定义场景序号 11（FPGA 结温野值）、"
        "序号 18（SPN1001 相邻采样差值 >3°/2°）。"
    )
    PARAM_SPECS = [
        ParamSpec("window", "MAD 窗长", "int", 31, "拍", minimum=3,
                  help="用当前点之前的窗估计局部噪声，31 拍可容忍 1~2 个异常点"),
        ParamSpec("z", "稳健 z 倍数", "float", 3.5, minimum=1.0, maximum=10.0),
        ParamSpec("a_abs", "绝对下限", "float", 0.0, unit="工程值", minimum=0,
                  help="0 表示不启用；建议填物理上不可能出现的单拍变化量（如 0.5 mm、50 ℃）"),
        ParamSpec("lsb_floor", "LSB 地板倍数", "float", 3.0, minimum=0,
                  help="阈值不低于该倍数 × 1 LSB，防止 MAD=0 时阈值退化为 0"),
        ParamSpec("two_stage", "两级确认", "bool", True,
                  help="打开后标注 野值(已回落)/台阶(未回落)"),
        ParamSpec("revert_ratio", "回落判定比例", "float", 0.5, minimum=0.05, maximum=1.0,
                  depends_on={"two_stage": [True]},
                  help="下一拍变化量 < 本次跳变量 × 该比例，判为野值"),
        ParamSpec("max_gap_s", "跨缺口不判定", "seconds", 10.0, minimum=0,
                  help="两拍间隔超过该值时不判跳变（丢包后首拍差值不可信）"),
    ]
    OUTPUT_FIELDS = [
        ("step", "float", "相邻两拍变化量"),
        ("sigma", "float", "局部稳健 σ"),
        ("mad", "float", "局部 MAD"),
        ("applied_threshold", "float", "实际生效阈值"),
        ("nature", "str", "spike(野值已回落)/step(台阶未回落)"),
        ("revert_value", "float", "下一拍值"),
    ]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        v = s.v
        n = len(v)
        w = int(p.window)
        if n < 3:
            return []
        lsb = ctx.meta.lsb()
        step = np.abs(np.diff(v))
        dt_ms = np.diff(s.t)

        # 尾部对齐的滑窗 MAD（只用当前点之前的样本，避免把异常点算进基线）
        sigma = np.full(n, np.nan)
        mad_arr = np.full(n, np.nan)
        sw = sliding(v, w)
        if sw.size:
            with np.errstate(invalid="ignore"):
                med = np.nanmedian(sw, axis=1)
                mad = np.nanmedian(np.abs(sw - med[:, None]), axis=1)
            sigma[w - 1:] = 1.4826 * mad
            mad_arr[w - 1:] = mad

        thr = np.full(n, 0.0)
        for i in range(1, n):
            cand = [float(p.lsb_floor) * lsb]
            if p.a_abs and float(p.a_abs) > 0:
                cand.append(float(p.a_abs))
            sig = sigma[i - 1]
            if not np.isnan(sig):
                cand.append(float(p.z) * sig)
            thr[i] = max(cand)

        with np.errstate(invalid="ignore"):
            hit = step > thr[1:]
        hit &= dt_ms <= float(p.max_gap_s) * 1000.0
        hit &= ~np.isnan(step)

        out: list[AnomalyPoint] = []
        for k in np.where(hit)[0]:
            i = int(k) + 1  # 当前拍
            st_val = float(step[k])
            nature = "step"
            revert_val = None
            if p.two_stage and i + 1 < n:
                back = abs(float(v[i + 1]) - float(v[i - 1]))
                if back <= float(p.revert_ratio) * st_val:
                    nature = "spike"
                    revert_val = float(v[i + 1])
            level = Level.parse(p.level)
            strength = min(1.0, 0.5 + 0.5 * min(st_val / max(thr[i], 1e-12) / 5.0, 1.0))
            out.append(point(
                ctx, level, HitType.SPIKE, int(s.t[i]), int(s.t[i]),
                value=float(v[i]),
                prev_value=float(v[i - 1]),
                expected=float(v[i - 1]),
                deviation=st_val,
                threshold=float(thr[i]),
                strength=strength,
                sample_count=2,
                evidence={
                    "step": round(st_val, 6),
                    "sigma": round(float(sigma[i - 1]), 6) if not np.isnan(sigma[i - 1]) else None,
                    "mad": round(float(mad_arr[i - 1]), 6) if not np.isnan(mad_arr[i - 1]) else None,
                    "applied_threshold": round(float(thr[i]), 6),
                    "nature": nature,
                    "revert_value": round(revert_val, 6) if revert_val is not None else None,
                    "window": w,
                },
            ))
        return out
