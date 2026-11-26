"""
A-02 死值 / 冻结检测（Frozen Value）
====================================

判据原文：窗内 64 拍逐位相同（极差 = 0）。

为什么不能用「极差 ≤ 2 LSB」：正常量化抖动就是 ±1 LSB，用极差阈值会 100% 误报。
为什么不能用它判缓慢漂移：每拍 +1 LSB 的爬升不是死值，那是 A-05 的职责。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, runs, register,
)


@register
class FrozenAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-02"
    ALGO_NAME = "死值/冻结检测"
    ALGO_NAME_EN = "Frozen Value"
    LAYER = "L1"
    CATEGORY = "冻结"
    HIT_TYPE = HitType.FROZEN
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS, ValueKind.ENUM, ValueKind.COUNTER)
    RATIONALE = (
        "传感器卡死、采集停摆、缓存不刷新，都会表现为数值一字不变。"
        "这类失效最隐蔽：包还在发、字段还在刷，看板全绿，但里面装的是旧值。"
        "判据必须严格用「逐位相同」，因为量化抖动本身就是 ±1 LSB。"
    )
    USE_CASES = "M-01② 位移读数 64 拍不变；M-02① 振动冻结；M-12③ 内置「数据不变」标志的上位机互校。"
    PARAM_SPECS = [
        ParamSpec("n_dead", "冻结判据点数", "int", 64, "拍", minimum=2,
                  help="连续多少拍完全相同判为死值；按包周期折算，64 拍约等于判据给的窗口"),
        ParamSpec("tolerance", "允许的极差", "float", 0.0, unit="工程值", minimum=0,
                  help="默认 0（严格逐位相同）。仅对极少数必须容忍量化抖动的代号放开，且不得超过 1 LSB"),
        ParamSpec("ignore_zero", "忽略全零段", "bool", False,
                  help="关机/未上电时读数为 0 属正常，打开后全零冻结不算异常（需配合门控使用）"),
        ParamSpec("merge_gap_s", "同段合并间隔", "seconds", 5.0, minimum=0,
                  help="两段冻结间隔小于该值则合并，避免一次卡死被拆成几十条"),
    ]
    OUTPUT_FIELDS = [
        ("frozen_value", "float", "冻结期间的值"),
        ("frozen_points", "int", "冻结样本数"),
        ("frozen_s", "float", "冻结时长（秒）"),
        ("span", "float", "窗内极差"),
        ("ignored_zero", "bool", "是否因全零被忽略"),
    ]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        v = s.v
        n = len(v)
        n_dead = int(p.n_dead)
        if n < n_dead:
            return []
        tol = float(p.tolerance)
        # 与前一拍差值的绝对值 ≤ tol 视为"未变"
        d = np.abs(np.diff(v))
        same = np.zeros(n, dtype=bool)
        same[1:] = (d <= tol) & ~np.isnan(d)

        out: list[AnomalyPoint] = []
        gap_ms = float(p.merge_gap_s) * 1000.0
        last_end = -1
        for a, b in runs(same):
            # same[a..b] 为 True 表示 v[a-1..b] 未变化，共 b-a+2 个样本
            if (b - a + 1) < n_dead - 1:
                continue
            st, en = a - 1, b
            if st <= last_end and out and (s.t[st] - out[-1].t_end) <= gap_ms:
                out[-1].t_end = int(s.t[en])
                out[-1].idx_end = en
                continue
            val = float(v[st])
            if np.isnan(val):
                continue
            if p.ignore_zero and val == 0.0:
                continue
            seg = v[st: en + 1]
            dur_s = (int(s.t[en]) - int(s.t[st])) / 1000.0
            out.append(point(
                ctx, Level.parse(p.level), HitType.FROZEN, int(s.t[st]), int(s.t[en]),
                value=val,
                expected=None,
                deviation=0.0,
                threshold=float(n_dead),
                strength=min(1.0, 0.7 + 0.3 * min(dur_s / 3600.0, 1.0)),
                sample_count=int(en - st + 1),
                evidence={
                    "frozen_value": round(val, 6),
                    "frozen_points": int(en - st + 1),
                    "frozen_s": round(dur_s, 3),
                    "span": round(float(np.nanmax(seg) - np.nanmin(seg)), 6),
                    "ignored_zero": False,
                },
            ))
            last_end = en
        return out
