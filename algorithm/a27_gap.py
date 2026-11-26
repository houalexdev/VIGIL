"""
A-27 数据缺口 / 中断检测（Data Gap）★新增
========================================

来源：规则集 M-06「采集组数不增」只覆盖了内置采集停摆，M-07「帧完整性」只覆盖
包级错位——两者都依赖特定代号。剩余 全部测点的"没数据"没人管。

缺数据是最容易静默的一类故障：判据不执行、看板不报警、报告写"正常"。
本算法把"缺失"本身变成告警，是所有其它判据的兜底：它不出结果时，
上层必须把该代号标记为"未评估"，而不是"正常"。

三种形态：
- 中断（gap）：两拍间隔超过 max_gap_s；
- 频率下降（loss）：统计窗内缺失率超过 max_loss_ratio；
- 全程无数据：序列为空（由引擎的 NO_DATA 状态承担，本算法负责"部分缺失"）。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, runs, register,
)


@register
class GapAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-27"
    ALGO_NAME = "数据缺口/中断检测"
    ALGO_NAME_EN = "Data Gap / Loss"
    LAYER = "L1"
    CATEGORY = "完整性"
    HIT_TYPE = HitType.GAP
    SUPPORTED_KINDS = tuple(ValueKind)
    RATIONALE = (
        "没有数据时，所有判据都会安静地返回「无异常」——这是最危险的假阴性。"
        "缺数据本身必须成为一条告警，且必须与「值正常」区分开：前者是看不见，后者是看见了没事。"
        "它也是唯一对所有测点普适的判据，不依赖任何物理量程。"
    )
    USE_CASES = "全部测点的兜底判据；采集停摆的序列侧印证；传输丢包评估。"
    PARAM_SPECS = [
        ParamSpec("max_gap_s", "最大允许间隔", "seconds", None,
                  help="留空则取 2.5 × 标称包周期；超过即判中断"),
        ParamSpec("gap_k", "缺口倍数", "float", 2.5, minimum=1.0,
                  help="未给 max_gap_s 时，按 包周期 × 该倍数 计算"),
        ParamSpec("min_gap_points", "最少缺失点数", "int", 1, "点", minimum=1,
                  help="缺口折算成缺失点数达到该值才报，避免单拍抖动刷屏"),
        ParamSpec("rate_window_s", "缺失率统计窗", "seconds", 600.0, minimum=10),
        ParamSpec("max_loss_ratio", "缺失率上限", "float", 0.05, minimum=0, maximum=1,
                  help="窗内缺失比例超过该值 → 频率下降告警；0 表示不检查"),
        ParamSpec("count_nan_as_gap", "NaN 视为缺失", "bool", True),
    ]
    OUTPUT_FIELDS = [
        ("gap_s", "float", "缺口时长"),
        ("missing_points", "int", "折算缺失点数"),
        ("expect_period_s", "float", "标称包周期"),
        ("loss_ratio", "float", "统计窗内缺失率"),
        ("kind", "str", "gap/loss"),
    ]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        period = s.period_ms() / 1000.0
        max_gap = float(p.max_gap_s) if p.max_gap_s else period * float(p.gap_k)
        out: list[AnomalyPoint] = []
        t = s.t
        d = np.diff(t) / 1000.0
        with np.errstate(invalid="ignore"):
            big = d > max_gap
        for i in np.where(big)[0]:
            missing = max(0, int(round(d[i] / max(period, 1e-6))) - 1)
            if missing < int(p.min_gap_points):
                continue
            out.append(point(
                ctx, Level.parse(p.level), HitType.GAP, int(t[i]), int(t[i + 1]),
                value=round(float(d[i]), 3),
                expected=round(period, 3),
                deviation=round(float(d[i]) - period, 3),
                threshold=max_gap,
                dimension="gap:interval",
                strength=min(1.0, 0.4 + 0.6 * min(missing / 10.0, 1.0)),
                sample_count=missing,
                evidence={
                    "gap_s": round(float(d[i]), 3),
                    "missing_points": missing,
                    "expect_period_s": round(period, 3),
                    "loss_ratio": None,
                    "kind": "gap",
                },
            ))

        if float(p.max_loss_ratio) > 0:
            w = max(2, s.to_points(float(p.rate_window_s)))
            if len(s) > w:
                miss = (~s.valid_mask).astype(float) if p.count_nan_as_gap else (s.quality != 0).astype(float)
                cs = np.concatenate([[0.0], np.cumsum(miss)])
                ratio = (cs[w:] - cs[:-w]) / w
                hit = ratio > float(p.max_loss_ratio)
                hit = np.concatenate([np.zeros(w, dtype=bool), hit])
                for a, b in runs(hit):
                    i = b
                    out.append(point(
                        ctx, Level.parse(p.level), HitType.GAP, int(t[a]), int(t[b]),
                        value=round(float(ratio[i - w]), 4),
                        expected=float(p.max_loss_ratio),
                        threshold=float(p.max_loss_ratio),
                        dimension="gap:loss",
                        strength=0.6,
                        sample_count=w,
                        evidence={
                            "gap_s": None,
                            "missing_points": int(round(float(ratio[i - w]) * w)),
                            "expect_period_s": round(period, 3),
                            "loss_ratio": round(float(ratio[i - w]), 4),
                            "kind": "loss",
                        },
                    ))
        return out
