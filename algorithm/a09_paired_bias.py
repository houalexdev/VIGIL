"""
A-09 成对偏置 + 累计分离（Paired Bias & CUSUM）
==============================================

判据原文：|V_主轴i − V_参考通道i − b_i| > max(0.5 mm, 3·MAD_b)，连续 ≥5 拍；
          符号相反且 60 s 累计分离 > 0.3 mm。

两类对象共用同一套算法，靠 pair_mode 区分：
- 原理级交叉校验（主轴/参考通道）：两个独立原理测同一点，同步漂 = 真实位移，
  只有一路偏 = 探头或解算错。这是全场唯一能分开"真位移/假位移"的手段。
- 冗余比对（A/B 机、主备通道）：两台设备输出应一致，b 固定为 0，
  差值超限即单机劣化；两路同时为 0 或同时不变则是共因。

坑：b 必须在基线期扣掉。两个探头安装位置差几毫米是正常的，不扣 b 会开机即报。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, confirm,
    mad, runs, register,
)


@register
class PairedBiasAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-09"
    ALGO_NAME = "成对偏置/累计分离"
    ALGO_NAME_EN = "Paired Bias & CUSUM"
    LAYER = "L2"
    CATEGORY = "一致性"
    HIT_TYPE = HitType.PAIRED_BIAS
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS,)
    REQUIRES_PEERS = True
    RATIONALE = (
        "单路判据永远回答不了「这个读数是真是假」。成对比较用另一个独立原理（或另一台设备）"
        "做裁判：同步变化是物理真实，单侧偏置是器件问题。CUSUM 负责抓缓慢累积的分离，"
        "这类分离单拍永远不超限，但几小时后会变成真实偏差。"
    )
    USE_CASES = "M-03①② 主轴×参考通道 8 对；A/B 机双机比对；主备通道一致性（扩展用法）。"
    PARAM_SPECS = [
        ParamSpec("peer_code", "对端测点代号", "code", None, required=True),
        ParamSpec("pair_mode", "配对模式", "enum", "cross_check",
                  choices=["cross_check", "redundant"],
                  help="cross_check=原理级交叉（主轴/参考通道）；redundant=冗余比对（A/B 机、主备）"),
        ParamSpec("bias_mode", "偏置处理", "enum", "auto",
                  choices=["auto", "fixed", "zero"],
                  help="auto=用基线期中位数拟合；fixed=用 bias 参数；zero=不扣偏置（冗余比对常用）"),
        ParamSpec("bias", "固定偏置 b", "float", 0.0, unit="工程值",
                  depends_on={"bias_mode": ["fixed"]}),
        ParamSpec("dev_abs", "偏差绝对下限", "float", 0.5, unit="工程值", minimum=0),
        ParamSpec("dev_k", "偏差 MAD 倍数", "float", 3.0, minimum=1.0),
        ParamSpec("cusum_window_s", "累计窗长", "seconds", 60.0, minimum=1),
        ParamSpec("cusum_thr", "累计分离门限", "float", 0.3, unit="工程值", minimum=0),
        ParamSpec("sign_check", "启用符号相反判定", "bool", True,
                  help="真实位移两路同向；只有一路动才会符号相反"),
        ParamSpec("k", "连续成立拍数", "int", 5, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("peer_value", "float", "对端值"),
        ("dev", "float", "扣除偏置后的偏差"),
        ("bias", "float", "生效偏置 b"),
        ("cusum", "float", "累计分离量"),
        ("sign_conflict", "bool", "两路变化方向是否相反"),
        ("trigger", "str", "bias/cusum/both"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        if p.peer_code not in ctx.peers:
            return Precheck(RunStatus.SKIP, f"对端 {p.peer_code} 未提供")
        return pre

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        peer = ctx.peers[p.peer_code]
        y = np.array([peer.value_at(t) for t in s.t], dtype=float)
        x = s.v
        diff = x - y

        if p.bias_mode == "zero":
            b = 0.0
        elif p.bias_mode == "fixed":
            b = float(p.bias or 0.0)
        else:
            b = ctx.baseline.get("pair_bias")
            b = float(b) if b is not None else float(np.nanmedian(diff))
        dev = np.abs(diff - b)
        mad_dev = mad(diff - b)
        floor = float(p.dev_abs)
        scale = max(floor, float(p.dev_k) * (1.4826 * mad_dev if np.isfinite(mad_dev) else 0.0))
        with np.errstate(invalid="ignore"):
            hit_bias = dev > scale

        w = max(2, s.to_points(float(p.cusum_window_s)))
        sep = diff - b
        cusum = np.full(len(s), np.nan)
        if len(s) >= w:
            cs = np.lib.stride_tricks.sliding_window_view(sep, w)
            cusum[w - 1:] = np.nansum(cs, axis=1)
        with np.errstate(invalid="ignore"):
            hit_cusum = np.abs(cusum) > float(p.cusum_thr)

            dx = np.concatenate([[0.0], np.diff(x)])
            dy = np.concatenate([[0.0], np.diff(y)])
            sign_conflict = (np.sign(dx) * np.sign(dy) < 0) if p.sign_check else np.zeros(len(x), dtype=bool)

        hit = hit_bias.copy()
        if p.sign_check:
            hit = hit & sign_conflict
        hit |= hit_cusum
        hit &= ~np.isnan(dev)
        confirmed = confirm(hit, int(p.k), allow_unknown=False) if int(p.k) > 1 else hit

        out: list[AnomalyPoint] = []
        for a, b_ in runs(confirmed):
            idx = a + int(np.nanargmax(dev[a: b_ + 1]))
            trig = "both" if (hit_bias[idx] and hit_cusum[idx]) else ("bias" if hit_bias[idx] else "cusum")
            out.append(point(
                ctx, Level.parse(p.level), HitType.PAIRED_BIAS, int(s.t[a]), int(s.t[b_]),
                value=float(x[idx]),
                prev_value=float(y[idx]),
                expected=float(y[idx] + b),
                deviation=float(dev[idx]),
                threshold=float(scale),
                dimension=f"peer:{p.peer_code}",
                strength=min(1.0, 0.5 + 0.5 * min(dev[idx] / max(scale, 1e-12) / 5.0, 1.0)),
                sample_count=int(b_ - a + 1),
                evidence={
                    "peer_value": round(float(y[idx]), 6),
                    "dev": round(float(dev[idx]), 6),
                    "bias": round(b, 6),
                    "cusum": round(float(cusum[idx]), 6) if not np.isnan(cusum[idx]) else None,
                    "sign_conflict": bool(sign_conflict[idx]),
                    "trigger": trig,
                    "pair_mode": p.pair_mode,
                },
            ))
        return out
