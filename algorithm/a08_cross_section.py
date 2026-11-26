"""
A-08 横截面离群（Cross-sectional Median / MAD）
==============================================

判据原文：|V_i − median(V1…V8)| > 3·max(MAD, 1 LSB)。

为什么用横截面而不是固定阈值：8 路探头安装位置不同，静态值本来就有差。
横截面比的是"同一时刻这一路相对其余 7 路偏了多少"，天然抵消了工况漂移与安装差异。

两个必须做的预处理：
- 先扣掉各路的基线偏置 b_i，否则开机即报；
- MAD 必须有 1 LSB 地板，8 路全一致时 MAD=0 会让阈值变 0。
崩溃点 50%：n=8 时最多 3 路同时异常仍能正确识别，4 路以上异常应转 A-12 共因判定。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, confirm,
    runs, register,
)


@register
class CrossSectionAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-08"
    ALGO_NAME = "横截面离群"
    ALGO_NAME_EN = "Cross-sectional Median/MAD"
    LAYER = "L2"
    CATEGORY = "一致性"
    HIT_TYPE = HitType.CROSS_SECTION
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS,)
    REQUIRES_PEERS = True
    RATIONALE = (
        "同一族的多路传感器（8 路主轴位移、8 路振动）在正常时应当同步。"
        "某一路单独跑偏，先于它自己越限就能被发现，而且不会因为整星温漂而误报——"
        "因为中位数跟着一起漂。这是「少一路就是盲区」之外，多路冗余真正被用起来的地方。"
    )
    USE_CASES = "M-01⑥ 8 路位移横截面；M-02④ 8 路振动横截面；M-01⑦ 同拍越限路数（转 A-12）。"
    PARAM_SPECS = [
        ParamSpec("peer_codes", "同族代号列表", "code_list", None, required=True,
                  help="参与横截面比较的全部代号（含本路），建议按族全量传入"),
        ParamSpec("k", "稳健 z 倍数", "float", 3.0, minimum=1.0, maximum=10.0),
        ParamSpec("lsb_floor", "MAD 地板(LSB)", "float", 1.0, minimum=0),
        ParamSpec("remove_bias", "先扣各路基线偏置", "bool", True,
                  help="强烈建议开启；否则安装位置差会被当成异常"),
        ParamSpec("warmup_ratio", "基线预热比例", "float", 0.1, minimum=0.01, maximum=0.5,
                  help="用序列开头这段比例估计各路基线偏置；不能用全程中位数——"
                       "故障占了一半时全程中位数会被故障拖走"),
        ParamSpec("min_peers", "最少路数", "int", 5, minimum=3,
                  help="少于该路数时横截面统计失去意义，应改用 A-09 成对比较"),
        ParamSpec("hold_points", "连续成立拍数", "int", 5, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("peer_median", "float", "同刻横截面中位数"),
        ("peer_mad", "float", "横截面 MAD"),
        ("z", "float", "稳健 z 值"),
        ("rank", "int", "偏离程度排名（1 = 最离群）"),
        ("peer_count", "int", "有效路数"),
        ("bias", "float", "已扣除的基线偏置"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        codes = [c for c in (p.peer_codes or []) if c in ctx.peers]
        if len(codes) < int(p.min_peers):
            return Precheck(RunStatus.SKIP, f"同族有效路数 {len(codes)} < {p.min_peers}，改用 A-09 成对比较")
        return pre

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        codes = [c for c in p.peer_codes if c in ctx.peers]
        mat = np.vstack([s.v] + [np.array([ctx.peers[c].value_at(t) for t in s.t], dtype=float) for c in codes])
        own_row = 0
        if p.remove_bias:
            # 偏置只从"假定健康"的预热段估计，绝不能用全程中位数
            warm = max(5, int(mat.shape[1] * float(p.warmup_ratio)))
            bias = np.nanmedian(mat[:, :warm], axis=1)
            bias = np.where(np.isfinite(bias), bias, np.nanmedian(mat, axis=1))
            mat = mat - bias[:, None]
        else:
            bias = np.zeros(mat.shape[0])
        with np.errstate(invalid="ignore"):
            med = np.nanmedian(mat, axis=0)
            mad = np.nanmedian(np.abs(mat - med), axis=0)
        lsb = ctx.meta.lsb()
        scale = np.maximum(mad, float(p.lsb_floor) * lsb)
        z = np.abs(mat[own_row] - med) / scale
        with np.errstate(invalid="ignore"):
            hit = z > float(p.k)
        hit &= ~np.isnan(z)
        confirmed = confirm(hit, int(p.hold_points), allow_unknown=False) if int(p.hold_points) > 1 else hit

        out: list[AnomalyPoint] = []
        for a, b in runs(confirmed):
            seg_z = z[a: b + 1]
            peak = int(np.nanargmax(seg_z))
            at = a + peak
            # 排名：本路在同刻的偏离程度
            devs = np.abs(mat[:, at] - med[at])
            rank = int(np.argsort(-np.nan_to_num(devs))[own_row]) + 1
            out.append(point(
                ctx, Level.parse(p.level), HitType.CROSS_SECTION, int(s.t[a]), int(s.t[b]),
                value=float(s.v[at]),
                expected=float(med[at] + bias[own_row]),
                deviation=float(s.v[at] - (med[at] + bias[own_row])),
                threshold=float(p.k),
                strength=min(1.0, 0.5 + 0.5 * min(float(np.nanmax(seg_z)) / 10.0, 1.0)),
                dimension=f"peer:{s.code}",
                sample_count=int(b - a + 1),
                evidence={
                    "peer_median": round(float(med[at] + bias[own_row]), 6),
                    "peer_mad": round(float(np.nanmedian(mad[a:b + 1])), 6),
                    "z": round(float(np.nanmax(seg_z)), 3),
                    "rank": rank,
                    "peer_count": len(codes) + 1,
                    "bias": round(float(bias[own_row]), 6),
                },
            ))
        return out
