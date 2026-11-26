"""
A-10 模回绕差分（Modular Counter Delta）
=======================================

判据原文：Δ = (V(k) − V(k−1)) mod 2^bits。

这是本表**最高频的实现错误**：8 bit 从 255 到 0 直接相减得 −255，
会被当成巨大跳变，满屏误报。9 个 8 bit 计数器全部需要取模。

能力边界：
- d > M/2 只能判「回绕」，真正的倒退（复位清零）要靠 d 与 status 结合，
  或者由 A-14 状态机按「归零且伴随重启计数变化」确认。
- 「包在发但计数不增」是最隐蔽的失效（采集停摆），Δ=0 必须单独成一条判据。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, confirm, counter_delta, runs,
    register,
)


@register
class CounterAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-10"
    ALGO_NAME = "模回绕计数差分"
    ALGO_NAME_EN = "Modular Counter Delta"
    LAYER = "L1"
    CATEGORY = "计数"
    HIT_TYPE = HitType.COUNTER
    SUPPORTED_KINDS = (ValueKind.COUNTER, ValueKind.CONTINUOUS)
    RATIONALE = (
        "计数类测点承载的是「发生了什么」：收了几条指令、采集走了几轮、重启了几次。"
        "它不越限，只在增量上出问题——不增是停摆，暴涨是异常触发，回绕是位宽陷阱。"
        "三种形态共用一套取模差分，是性价比最高的实现。"
    )
    USE_CASES = (
        "M-06①② 采集组数 Δ>2 / Δ=0；M-09 指令链三段；M-10 参数上注偏序；"
        "M-11① 重启次数；M-07② 顺序号 Δ≠1；自定义场景序号 2/4/6/16 的「计数恒 0」。"
    )
    PARAM_SPECS = [
        ParamSpec("bits", "计数器位宽", "int", 8, "bit", minimum=1, maximum=64,
                  help="8→256，16→65536；位宽填错必然满屏误报，务必按目录填"),
        ParamSpec("mode", "判据形态", "enum", "no_increase",
                  choices=["no_increase", "delta_gt", "delta_ne", "any_change"],
                  help="no_increase=不增（停摆）；delta_gt=增量超限；delta_ne=增量不等于步长；any_change=任何变化"),
        ParamSpec("step", "期望步长", "int", 1, minimum=0,
                  depends_on={"mode": ["delta_ne"]}, help="顺序号/组数通常为 1"),
        ParamSpec("max_delta", "最大允许增量", "int", 0, minimum=0,
                  depends_on={"mode": ["delta_gt"]}, help="每个采样周期允许的最大增量"),
        ParamSpec("treat_backward_as_reset", "大幅倒退判为复位", "bool", True,
                  help="d > M/2 视为计数器归零/复位，而不是巨大跳变"),
        ParamSpec("k", "连续成立拍数", "int", 2, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("delta", "int", "取模后的增量"),
        ("raw_delta", "int", "未取模的裸差值"),
        ("modulus", "int", "计数器模 2^bits"),
        ("wrap", "bool", "是否发生回绕"),
        ("reset", "bool", "是否判为复位归零"),
        ("prev_value", "int", "前一拍值"),
    ]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        if len(s) < 2:
            return []
        bits = int(p.bits)
        mod = 1 << bits
        n = s.v
        d = counter_delta(n, bits)
        raw = np.diff(n)
        wrap = raw < 0
        reset = wrap & (d > mod // 2) if p.treat_backward_as_reset else np.zeros_like(wrap, dtype=bool)

        with np.errstate(invalid="ignore"):
            if p.mode == "no_increase":
                hit = d == 0
            elif p.mode == "delta_gt":
                hit = d > int(p.max_delta)
                hit &= ~reset
            elif p.mode == "delta_ne":
                hit = d != int(p.step)
                hit &= ~reset
            else:
                hit = d != 0
        hit &= ~np.isnan(d)

        idx = np.concatenate([[False], hit])
        confirmed = confirm(idx, int(p.k), allow_unknown=False) if int(p.k) > 1 else idx

        out: list[AnomalyPoint] = []
        for a, b in runs(confirmed):
            i = b  # 段末拍
            out.append(point(
                ctx, Level.parse(p.level), HitType.COUNTER, int(s.t[i]), int(s.t[i]),
                value=float(n[i]),
                prev_value=float(n[i - 1]) if i >= 1 else None,
                deviation=float(d[i - 1]) if i >= 1 else None,
                threshold=float(p.max_delta if p.mode == "delta_gt" else p.step),
                sample_count=int(b - a + 1),
                evidence={
                    "delta": int(d[i - 1]) if i >= 1 else None,
                    "raw_delta": int(raw[i - 1]) if i >= 1 else None,
                    "modulus": mod,
                    "wrap": bool(wrap[i - 1]) if i >= 1 else False,
                    "reset": bool(reset[i - 1]) if i >= 1 else False,
                    "prev_value": int(n[i - 1]) if i >= 1 else None,
                    "mode": p.mode,
                },
            ))
        return out
