"""
A-19 工况段切分（Operating-Segment Segmentation）—— 基础设施
==========================================================

段 = 下列门控同时成立的连续区间：
    SPN7001 = 2 ∧ SPN0001 ∈ {常态, 跟随} ∧ SPN7002 = 0 ∧ HYG4001 = 0

为什么必须切段：段是**可比性单位**。开环段与闭环段的位移形状本来就不一样，
混在一起比，基线和当前段都会被污染。趋势类判据（A-20/A-21）全部以段为输入。

四条纪律：
1. 上电/切换瞬态必须掐掉（前后各 edge 拍），否则段形被起步爬升污染；
2. 过短段必须计数上报——静默丢弃会让趋势分析停摆而看板仍显示正常；
3. UNKNOWN 不能当「段结束」，否则一段被劈两段，两段都进不了基线；
4. cond 不同的段绝不混比。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, AlgorithmContext, AnomalyPoint,
    TriState, register,
)


@register
class SegmentAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-19"
    ALGO_NAME = "工况段切分"
    ALGO_NAME_EN = "Operating-Segment Segmentation"
    LAYER = "L3"
    CATEGORY = "基础设施"
    IS_DETECTOR = False
    SUPPORTED_KINDS = tuple()
    RATIONALE = (
        "趋势判据最容易犯的错是「拿今天早上的段跟昨天机动段比」。段切分的唯一目的"
        "就是保证比较对象同工况。它不产出告警，但决定了 A-20/A-21 的结论是否成立。"
    )
    USE_CASES = "M-01⑨⑩、M-02⑤⑥ 的全部段形判据的前置。"
    PARAM_SPECS = [
        ParamSpec("min_len", "最短段长", "int", 300, "拍", minimum=10),
        ParamSpec("edge", "前后掐掉的瞬态", "int", 60, "拍", minimum=0),
        ParamSpec("on_unknown", "遇到未知门控", "enum", "continue",
                  choices=["continue", "break"],
                  help="continue=延续当前段（推荐）；break=强制断段"),
        ParamSpec("report_short", "上报过短段", "bool", True),
    ]
    OUTPUT_FIELDS = [
        ("segments", "list", "[(start_idx, end_idx, cond_key)]"),
        ("short_count", "int", "被丢弃的过短段数"),
        ("coverage", "float", "有效段覆盖比例"),
    ]

    def detect(self, ctx: AlgorithmContext, p) -> list[AnomalyPoint]:
        return []

    def run_infra(self, flags: np.ndarray, cond_key: str = "default", p: dict | None = None) -> dict:
        p = p or {}
        min_len = int(p.get("min_len", 300))
        edge = int(p.get("edge", 60))
        on_unknown = p.get("on_unknown", "continue")
        n = len(flags)
        segs: list[tuple[int, int, str]] = []
        short = 0
        start = None
        for k, g in enumerate(flags):
            unknown = g is None or (isinstance(g, float) and np.isnan(g))
            if unknown:
                if on_unknown == "break" and start is not None:
                    if k - start >= min_len:
                        segs.append((start + edge, k - edge, cond_key))
                    else:
                        short += 1
                    start = None
                continue
            on = bool(g)
            if on and start is None:
                start = k
            elif not on and start is not None:
                a, b = start + edge, k - edge
                if b - a >= min_len:
                    segs.append((a, b, cond_key))
                else:
                    short += 1
                start = None
        if start is not None:
            a, b = start + edge, n - edge
            if b - a >= min_len:
                segs.append((a, b, cond_key))
            else:
                short += 1
        covered = sum(b - a + 1 for a, b, _ in segs)
        return {
            "segments": segs,
            "short_count": short,
            "coverage": round(covered / max(n, 1), 4),
        }
