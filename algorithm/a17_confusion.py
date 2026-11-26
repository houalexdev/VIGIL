"""
A-17 参考通道交叉校验（Reference Channel Cross-Check）
===================================================

内置以全采样率算诊断，上位机按传输包率算判据，两者**不可互推**。
把两边按 2×2 混淆矩阵逐日累计：
- FN 高 → 上位机阈值偏松（该报没报）；
- FP 高 → 上位机阈值偏紧（误报）；
- 内置开关被关 → 内置永远不报，上位机判据再准也校不出来（转 A-13 配置缺陷）。

纪律：只输出「提示」级，不参与告警分级，也不自动改阈值——内置参数上位机不能改。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, register,
)


@register
class ConfusionAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-17"
    ALGO_NAME = "参考通道交叉校验"
    ALGO_NAME_EN = "Reference Channel Cross-Check"
    LAYER = "L2"
    CATEGORY = "互校"
    HIT_TYPE = HitType.CONFUSION
    SUPPORTED_KINDS = (ValueKind.BITFIELD, ValueKind.FLAG, ValueKind.ENUM)
    RATIONALE = (
        "整张表 全部判据里，上位机自己算的那一部分阈值到底松不松，只有内置标志能回答。"
        "互校矩阵同时完成两件事：发现漏报、反向校准上位机阈值。它是唯一带「自我修正」"
        "性质的判据，但必须停在提示级——自动改阈值会掩盖问题。"
    )
    USE_CASES = "M-01⑧ 上位机死值/跳变 与 SPN9001 对应位；M-12③④ 内置「数据不变」与上位机死值互校。"
    PARAM_SPECS = [
        ParamSpec("onboard_code", "内置标志代号", "code", None, required=True,
                  help="承载内置诊断位的测点代号（如 SPN9001 的某一位）"),
        ParamSpec("bit_offset", "位偏移", "int", None, minimum=0, maximum=63,
                  help="内置标志位号；整字全 1 判故障时可留空"),
        ParamSpec("ground_source", "上位机判据来源", "enum", "upstream",
                  choices=["upstream", "param_threshold"],
                  help="upstream=取同一规则内其它算法结果；param_threshold=按给定阈值现算"),
        ParamSpec("ground_threshold", "上位机阈值", "float", None,
                  depends_on={"ground_source": ["param_threshold"]}),
        ParamSpec("stat_window_s", "统计窗长", "seconds", 86400.0, minimum=60),
        ParamSpec("fn_rate_max", "漏报率上限", "float", 0.2, minimum=0, maximum=1),
        ParamSpec("fp_rate_max", "误报率上限", "float", 0.2, minimum=0, maximum=1),
        ParamSpec("min_events", "最少样本数", "int", 5, minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("tp", "int", "同报"),
        ("fp", "int", "上位机报内置未报"),
        ("fn", "int", "内置报上位机未报"),
        ("tn", "int", "同不报"),
        ("fn_rate", "float", "漏报率"),
        ("fp_rate", "float", "误报率"),
        ("verdict", "str", "ground_loose/ground_tight/agree"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        if p.onboard_code not in ctx.peers:
            return Precheck(RunStatus.SKIP, f"内置标志 {p.onboard_code} 未提供")
        if not ctx.extra.get("ground_points"):
            return Precheck(RunStatus.UNKNOWN, "缺少上位机判据结果，无法做互校")
        return pre

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        ob = ctx.peers[p.onboard_code]
        ob_v = np.array([ob.value_at(t) for t in s.t], dtype=float)
        if p.bit_offset is not None:
            ob_hit = ((np.nan_to_num(ob_v, nan=0).astype(np.int64) >> int(p.bit_offset)) & 1) == 1
        else:
            ob_hit = np.nan_to_num(ob_v, nan=0) != 0

        gr = ctx.extra.get("ground_points") or []
        g_hit = np.zeros(len(s), dtype=bool)
        for gp in gr:
            g_hit |= np.abs(s.t - int(gp.get("t_start", 0))) <= 1000

        win = int(float(p.stat_window_s) * 1000)
        n_win = max(1, int(np.ceil((s.t[-1] - s.t[0]) / max(win, 1))))
        out: list[AnomalyPoint] = []
        for w in range(n_win):
            t0 = int(s.t[0]) + w * win
            m = (s.t >= t0) & (s.t < t0 + win)
            if not m.any():
                continue
            tp = int(np.count_nonzero(g_hit[m] & ob_hit[m]))
            fp = int(np.count_nonzero(g_hit[m] & ~ob_hit[m]))
            fn = int(np.count_nonzero(~g_hit[m] & ob_hit[m]))
            tn = int(np.count_nonzero(~g_hit[m] & ~ob_hit[m]))
            if tp + fn + fp < int(p.min_events):
                continue
            fn_rate = fn / max(tp + fn, 1)
            fp_rate = fp / max(tp + fp, 1)
            if fn_rate > float(p.fn_rate_max):
                verdict, level, msg = "ground_loose", Level.L1, "上位机阈值偏松：内置报了上位机没报"
            elif fp_rate > float(p.fp_rate_max):
                verdict, level, msg = "ground_tight", Level.L1, "上位机阈值偏紧：上位机报了内置没报"
            else:
                continue
            out.append(point(
                ctx, level, HitType.CONFUSION, t0, t0 + win - 1,
                value=round(fn_rate if verdict == "ground_loose" else fp_rate, 4),
                expected=float(p.fn_rate_max if verdict == "ground_loose" else p.fp_rate_max),
                threshold=float(p.fn_rate_max if verdict == "ground_loose" else p.fp_rate_max),
                dimension=f"confusion:{verdict}", strength=0.4,
                evidence={
                    "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                    "fn_rate": round(fn_rate, 4), "fp_rate": round(fp_rate, 4),
                    "verdict": verdict, "note": msg,
                },
            ))
        return out
