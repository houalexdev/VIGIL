"""
A-24 状态驻留超时（State Dwell Timeout）★新增
============================================

来源：《自定义场景》序号 5「电源测点包更新状态 = 1（更新中）持续超 2 个采样周期告警」。

它和 A-01「持续 ≥k 拍」的区别：
- A-01 判的是"异常值持续存在"，持续时间越长越严重；
- A-24 判的是"合法状态停留太久"——值本身完全合法（更新中是对的），
  只是它该结束而没结束。这类判据在流程类测试里极多（写入中、重构中、复位中、
  加热中、机动中），没有一个能在原 21 个算法里干净表达。

同时支持反向：「该离开却没离开」与「该进入却没进入」（应开未开/应关未关）。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, runs, register,
)


@register
class DwellAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-24"
    ALGO_NAME = "状态驻留超时"
    ALGO_NAME_EN = "State Dwell Timeout"
    LAYER = "L1"
    CATEGORY = "状态"
    HIT_TYPE = HitType.DWELL
    SUPPORTED_KINDS = (ValueKind.ENUM, ValueKind.FLAG, ValueKind.CONTINUOUS)
    RATIONALE = (
        "流程类测试的判据大多是「这个状态该结束了」。值本身合法，所以越限判据不报；"
        "持续确认只会让它越来越严重，说不出「超时」这件事。"
        "驻留超时把「时长」变成一等公民，也是唯一能表达「超 2 个采样周期」这种判据的算法。"
    )
    USE_CASES = (
        "自定义场景序号 5（更新中状态超 2 个采样周期）；序号 3 上电指令后未进入预期态；"
        "序号 7 热控恢复标志长期为 0；各类「写入中/重构中」流程判据。"
    )
    PARAM_SPECS = [
        ParamSpec("target_values", "监视的状态值", "list[int]", None, required=True,
                  help="停留过久即告警的状态值，如 [1]（更新中）"),
        ParamSpec("max_dwell_s", "最长驻留时长", "seconds", None,
                  help="留空则用 max_dwell_periods × 包周期"),
        ParamSpec("max_dwell_periods", "最长驻留周期数", "int", 2, "个包周期", minimum=1,
                  help="判据「超 2 个采样周期」就填 2"),
        ParamSpec("mode", "判据方向", "enum", "too_long",
                  choices=["too_long", "not_reached"],
                  help="too_long=停留太久；not_reached=窗口内从未到达该状态"),
        ParamSpec("expect_window_s", "期望到达窗口", "seconds", None,
                  depends_on={"mode": ["not_reached"]},
                  help="not_reached 模式下，从门控成立起多久内必须到达"),
        ParamSpec("k", "连续成立拍数", "int", 1, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("state_value", "int", "驻留的状态值"),
        ("dwell_s", "float", "实际驻留时长"),
        ("limit_s", "float", "允许上限"),
        ("over_s", "float", "超出时长"),
        ("state_label", "str", "状态含义"),
    ]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        targets = set(int(x) for x in (p.target_values or []))
        if not targets:
            return []
        period = s.period_ms() / 1000.0
        limit_s = float(p.max_dwell_s) if p.max_dwell_s else float(p.max_dwell_periods) * period
        vi = np.nan_to_num(s.v, nan=-999999).astype(np.int64)
        in_state = np.isin(vi, list(targets))
        emap = ctx.meta.enum_map()
        out: list[AnomalyPoint] = []

        if p.mode == "not_reached":
            win = float(p.expect_window_s or limit_s)
            if not in_state.any():
                out.append(point(
                    ctx, Level.parse(p.level), HitType.DWELL, int(s.t[0]), int(s.t[-1]),
                    value=float(vi[-1]), expected=float(min(targets)),
                    threshold=win, dimension="dwell:not_reached",
                    evidence={"state_value": int(vi[-1]), "state_label": emap.get(int(vi[-1]), ""),
                              "dwell_s": round((s.t[-1] - s.t[0]) / 1000.0, 3), "limit_s": win,
                              "over_s": round((s.t[-1] - s.t[0]) / 1000.0 - win, 3)},
                ))
            return out

        for a, b in runs(in_state):
            dur = (int(s.t[b]) - int(s.t[a])) / 1000.0
            if dur <= limit_s:
                continue
            val = int(vi[b])
            out.append(point(
                ctx, Level.parse(p.level), HitType.DWELL, int(s.t[a]), int(s.t[b]),
                value=float(val),
                expected=float(limit_s),
                deviation=round(dur - limit_s, 3),
                threshold=limit_s,
                dimension=f"dwell:{val}",
                strength=min(1.0, 0.5 + 0.5 * min((dur - limit_s) / max(limit_s, 1e-6), 1.0)),
                sample_count=int(b - a + 1),
                evidence={
                    "state_value": val,
                    "dwell_s": round(dur, 3),
                    "limit_s": round(limit_s, 3),
                    "over_s": round(dur - limit_s, 3),
                    "state_label": emap.get(val, ""),
                    "period_s": round(period, 3),
                },
            ))
        return out
