"""
A-22 抖动 / 振荡检测（Jitter / Oscillation）★新增
================================================

来源：《自定义场景》序号 13「正负 Y 翼执行机构当前角度随机跳变」——
判据形态是「相邻采样 |Δ| > 5°」+「60 s 内变化 ≥10 次」。
原 21 个算法里没有一条能表达"变化次数"，A-03 只能抓单次幅度，
所以必须补这一条，否则这类异常只能靠 LLM 现场拼复合条件，无法复用。

抖动与跳变的分界：
- A-03 抓一次大幅变化；
- A-22 抓**小幅但高频往复**的变化——幅度可能每次都合格，但方向在反复翻转。
  这类异常是机械/电气振荡的典型特征（旋变信号缺失、驱动闭环不稳）。

三个必须同时成立的条件：变化次数、方向翻转次数、振幅下限。
只数次数会把正常噪声算进去，所以必须有 step_thr；只看幅度会把缓慢锯齿漏掉，所以要数翻转。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, confirm, robust_sigma, runs,
    register,
)


@register
class JitterAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-22"
    ALGO_NAME = "抖动/振荡检测"
    ALGO_NAME_EN = "Jitter / Oscillation"
    LAYER = "L1"
    CATEGORY = "抖动"
    HIT_TYPE = HitType.JITTER
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS,)
    RATIONALE = (
        "机械振荡、驱动闭环失稳、旋变信号缺相，表现都是「角度在原地来回跳」，"
        "单次幅度不大、也不越限，但 60 s 内变化十几次。这类异常 A-03 抓不到"
        "（每次跳变都没超阈值），越限判据更抓不到（值一直在正常区间内）。"
    )
    USE_CASES = "自定义场景序号 13 执行机构角度抖动；机构类/电位计类测点的往复跳变。"
    PARAM_SPECS = [
        ParamSpec("window_s", "统计窗长", "seconds", 60.0, minimum=1,
                  help="判据给的 60 s；窗内同时统计变化次数与翻转次数"),
        ParamSpec("step_thr", "单次变化阈值", "float", 0.0, unit="工程值", minimum=0,
                  help="0 = 自动取 3σ(Δ)；机构类建议按转速与采样周期标定（如 5°）"),
        ParamSpec("min_changes", "最少变化次数", "int", 10, "次", minimum=2),
        ParamSpec("min_reversals", "最少方向翻转次数", "int", 3, "次", minimum=1,
                  help="翻转才是振荡；单向锯齿不算"),
        ParamSpec("min_amplitude", "窗内最小振幅", "float", 0.0, unit="工程值", minimum=0,
                  help="0 = 自动取 step_thr；用于排除原地微小抖动"),
        ParamSpec("k", "连续成立拍数", "int", 1, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("changes", "int", "窗内变化次数"),
        ("reversals", "int", "窗内方向翻转次数"),
        ("amplitude", "float", "窗内振幅"),
        ("step_thr", "float", "实际生效的单次变化阈值"),
        ("freq_hz", "float", "等效振荡频率"),
    ]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        v = s.v
        if len(v) < 5:
            return []
        d = np.diff(v)
        sig = robust_sigma(d)
        step_thr = float(p.step_thr) if p.step_thr else max(3.0 * (sig if np.isfinite(sig) else 0.0),
                                                           3.0 * ctx.meta.lsb())
        amp_min = float(p.min_amplitude) if p.min_amplitude else step_thr
        move = np.abs(d) > step_thr
        # 方向翻转：相邻两次有效移动方向相反
        sign = np.sign(d)
        reversal = np.zeros_like(move)
        last = 0
        for i in range(len(d)):
            if not move[i]:
                continue
            if last != 0 and sign[i] * last < 0:
                reversal[i] = True
            last = sign[i]

        w = max(2, s.to_points(float(p.window_s)))
        if len(v) <= w:
            return []
        csum_move = np.concatenate([[0], np.cumsum(move)])
        csum_rev = np.concatenate([[0], np.cumsum(reversal)])
        changes = csum_move[w:] - csum_move[:-w]
        revs = csum_rev[w:] - csum_rev[:-w]
        # 滑窗振幅
        amp = np.full(len(changes), np.nan)
        sw = np.lib.stride_tricks.sliding_window_view(v, w + 1)
        with np.errstate(invalid="ignore"):
            amp = np.nanmax(sw, axis=1) - np.nanmin(sw, axis=1)

        with np.errstate(invalid="ignore"):
            hit = (changes >= int(p.min_changes)) & (revs >= int(p.min_reversals)) & (amp >= amp_min)
        hit = np.concatenate([np.zeros(w, dtype=bool), hit])
        confirmed = confirm(hit, int(p.k), allow_unknown=False) if int(p.k) > 1 else hit

        out: list[AnomalyPoint] = []
        for a, b in runs(confirmed):
            i = b
            ch = int(changes[min(i - w, len(changes) - 1)])
            rv = int(revs[min(i - w, len(revs) - 1)])
            am = float(amp[min(i - w, len(amp) - 1)])
            out.append(point(
                ctx, Level.parse(p.level), HitType.JITTER, int(s.t[a]), int(s.t[b]),
                value=round(am, 6),
                expected=float(p.min_changes),
                deviation=float(ch - int(p.min_changes)),
                threshold=step_thr,
                strength=min(1.0, 0.5 + 0.5 * min(rv / max(int(p.min_reversals), 1) - 1, 1.0)),
                dimension="jitter",
                sample_count=int(b - a + 1),
                evidence={
                    "changes": ch,
                    "reversals": rv,
                    "amplitude": round(am, 6),
                    "step_thr": round(step_thr, 6),
                    "freq_hz": round(rv / max(float(p.window_s), 1e-6), 3),
                    "window_s": float(p.window_s),
                },
            ))
        return out
