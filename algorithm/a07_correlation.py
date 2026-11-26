"""
A-07 滑窗皮尔逊相关（Rolling Pearson）
======================================

判据原文：corr(V_振动, V_位移) 由 < −0.5 升至 > −0.1（窗 30 min）。

两个坑：
1. 常量序列 σ=0 时相关系数**未定义**，必须返回 None（UNKNOWN），返回 0 会误判成"不相关"。
2. 两条序列必须先做 A-16 时间对齐，否则相关性被包率差与时间错位污染。

看的是"变化"而不是绝对值：从强负相关掉到弱相关才是异常，某些工况本来就该接近 0。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, pearson,
    runs, confirm, register,
)


def rolling_corr(x: np.ndarray, y: np.ndarray, w: int) -> np.ndarray:
    """尾部对齐滑窗相关，长度与输入一致，前 w-1 为 nan。σ=0 的窗为 nan（未定义）。"""
    n = x.size
    out = np.full(n, np.nan)
    if n < w or w < 3:
        return out
    sx = np.lib.stride_tricks.sliding_window_view(x, w)
    sy = np.lib.stride_tricks.sliding_window_view(y, w)
    mx, my = sx.mean(axis=1), sy.mean(axis=1)
    dx, dy = sx - mx[:, None], sy - my[:, None]
    den = np.sqrt((dx * dx).sum(axis=1) * (dy * dy).sum(axis=1))
    with np.errstate(invalid="ignore", divide="ignore"):
        r = (dx * dy).sum(axis=1) / den
    r[~np.isfinite(r)] = np.nan
    out[w - 1:] = np.clip(r, -1.0, 1.0)
    return out


@register
class CorrelationAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-07"
    ALGO_NAME = "滑窗皮尔逊相关"
    ALGO_NAME_EN = "Rolling Pearson"
    LAYER = "L2"
    CATEGORY = "一致性"
    HIT_TYPE = HitType.CORRELATION
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS,)
    REQUIRES_PEERS = True
    RATIONALE = (
        "物理上耦合的两个量（振动与位移、100V 与 5V、主轴与参考通道）一旦相关性结构变了，"
        "说明链路里某一环不再是原来那个物理过程。它比单点阈值早，也比单点阈值稳，"
        "因为它对同向的温漂、同向的负载变化天然免疫。"
    )
    USE_CASES = "M-02③ 振动-位移相关由强负相关升至弱相关；M-14③ 两路供电同拍跌幅。"
    PARAM_SPECS = [
        ParamSpec("peer_code", "对端测点代号", "code", None, required=True,
                  help="参与相关计算的对端代号，必须已订阅"),
        ParamSpec("window_s", "相关窗长", "seconds", 1800.0, minimum=10,
                  help="30 min 为判据值；折算点数 = 窗长 / 包周期"),
        ParamSpec("mode", "判定方式", "enum", "change",
                  choices=["change", "absolute", "drop"],
                  help="change=由正常带跌出（判据原文）；absolute=低于阈值即报；drop=相对基线下降超量"),
        ParamSpec("r_normal_low", "正常下界", "float", -0.5, minimum=-1, maximum=1,
                  depends_on={"mode": ["change", "drop"]}, help="正常应低于该值（强负相关）"),
        ParamSpec("r_alarm_high", "告警上界", "float", -0.1, minimum=-1, maximum=1,
                  depends_on={"mode": ["change"]}, help="升至该值以上判异常"),
        ParamSpec("r_threshold", "绝对阈值", "float", 0.5, minimum=-1, maximum=1,
                  depends_on={"mode": ["absolute"]}, help="|r| 低于该值判异常"),
        ParamSpec("drop_ratio", "下降比例", "float", 0.5, minimum=0, maximum=1,
                  depends_on={"mode": ["drop"]}, help="|r| 相对基线下降超过该比例判异常"),
        ParamSpec("ref_ratio", "参照段比例", "float", 0.2, minimum=0.02, maximum=0.8,
                  depends_on={"mode": ["change"]},
                  help="用序列开头这段比例的相关中位数作为「原来的正常相关」。"
                       "不能用上一拍：相关是滑窗量，劣化过程是渐变的，逐拍比较永远不触发"),
        ParamSpec("k", "连续成立拍数", "int", 5, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("r", "float", "当前相关系数"),
        ("r_ref", "float", "参照相关（基线/上一状态）"),
        ("pairs", "int", "有效配对数"),
        ("peer_code", "str", "对端代号"),
        ("undefined_windows", "int", "σ=0 导致未定义的窗数"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        if p.peer_code not in ctx.peers:
            return Precheck(RunStatus.SKIP, f"对端 {p.peer_code} 未提供")
        if len(ctx.peers[p.peer_code]) < 3:
            return Precheck(RunStatus.UNKNOWN, f"对端 {p.peer_code} 数据不足")
        return pre

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        peer = ctx.peers[p.peer_code]
        # A-16 零阶保持对齐到主序列栅格
        y = np.array([peer.value_at(t) for t in s.t], dtype=float)
        x = s.v
        w = max(3, min(s.to_points(float(p.window_s)), len(x)))
        r = rolling_corr(x, y, w)
        n_undef = int(np.count_nonzero(np.isnan(r[w - 1:])))

        base_r = ctx.baseline.get("r")
        ref_n = max(3, int(len(r) * float(p.ref_ratio)))
        ref_r = float(np.nanmedian(r[w - 1: w - 1 + ref_n])) if len(r) > w else float("nan")
        if base_r is not None:
            ref_r = float(base_r)
        with np.errstate(invalid="ignore"):
            if p.mode == "absolute":
                hit = np.abs(r) < float(p.r_threshold)
                ref = float(p.r_threshold)
            elif p.mode == "drop":
                ref = float(base_r) if base_r is not None else float(p.r_normal_low)
                hit = np.abs(r) < abs(ref) * (1 - float(p.drop_ratio))
            else:  # change：参照段仍是强相关，当前窗已跌出正常带
                ref = ref_r if np.isfinite(ref_r) else float(p.r_normal_low)
                hit = (ref < float(p.r_normal_low)) & (r > float(p.r_alarm_high))
        hit &= ~np.isnan(r)

        confirmed = confirm(hit, int(p.k), allow_unknown=False) if int(p.k) > 1 else hit
        out: list[AnomalyPoint] = []
        for a, b in runs(confirmed):
            rep = float(r[b])
            n_pairs = int(np.count_nonzero(~np.isnan(x[a: b + 1]) & ~np.isnan(y[a: b + 1])))
            out.append(point(
                ctx, Level.parse(p.level), HitType.CORRELATION, int(s.t[a]), int(s.t[b]),
                value=round(rep, 4), expected=ref, threshold=float(p.r_alarm_high),
                deviation=round(rep - ref, 4),
                strength=min(1.0, 0.5 + 0.5 * min(abs(rep - ref) / 0.5, 1.0)),
                dimension=f"peer:{p.peer_code}",
                sample_count=int(b - a + 1),
                evidence={
                    "r": round(rep, 4),
                    "r_ref": round(ref, 4),
                    "pairs": n_pairs,
                    "peer_code": p.peer_code,
                    "undefined_windows": n_undef,
                    "window_points": w,
                },
            ))
        return out
