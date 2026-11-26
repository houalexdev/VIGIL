"""
A-12 k-of-n 多证据融合（k-of-n Voting）
======================================

判据原文：同拍越限路数 ≥ 4（8 路）→ 致命；8 对中 6 对同向且离散度 <0.2 mm → 判真实位移。

两个不能省的条件：
1. k 不能等于 n —— 单点误报就会触发全局致命告警；
2. 必须同时看离散度 —— 8 路同时越限但彼此差很大，是真实大位移（物理真实），
   不是共因故障。只看计数会把"姿势变了"判成"系统坏了"。

本算法有两种用法，参数不同而已：
- 正向：k 路同时命中 → 报共因故障（M-01⑦、M-05②、M-08⑦）；
- 反向：k 路同向 + 离散度小 → 判为真实物理变化，**抑制**上游单路告警（M-03③）。
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
class VotingAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-12"
    ALGO_NAME = "k-of-n 多证据融合"
    ALGO_NAME_EN = "k-of-n Voting"
    LAYER = "L2"
    CATEGORY = "融合"
    HIT_TYPE = HitType.VOTING
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS, ValueKind.ENUM, ValueKind.FLAG)
    REQUIRES_PEERS = True
    RATIONALE = (
        "单路告警永远分不清「这一路坏了」还是「整机真的变了」。k-of-n 用多数派给出结论："
        "多路同时越限且彼此接近 = 共因；多路越限但离散很大 = 真实大幅变化。"
        "这条判据是抑制误报的关键，也是唯一能把级别直接抬到致命的地方。"
    )
    USE_CASES = "M-01⑦ 同拍越限 ≥4 路致命；M-03③ 6/8 对同向 → 抑制单对告警；M-05② 3 位隔离致命；M-12② 4×8 全 1。"
    PARAM_SPECS = [
        ParamSpec("peer_codes", "参与代号列表", "code_list", None, required=True,
                  help="参与投票的全部代号（含本路）"),
        ParamSpec("op", "单路命中条件", "enum", "gt", choices=["gt", "ge", "lt", "le", "eq", "ne"]),
        ParamSpec("value", "单路阈值", "float", None, required=True, unit="工程值"),
        ParamSpec("k", "命中路数门限", "int", 4, "路", minimum=1),
        ParamSpec("spread_max", "离散度上限", "float", None, unit="工程值",
                  help="留空则不检查离散度；填了以后 max-min 超过该值判为真实变化，不报共因"),
        ParamSpec("invert", "反向使用（抑制上游）", "bool", False,
                  help="打开后本算法输出的是「这是一次真实物理变化」的判定点，引擎据此抑制单路告警"),
        ParamSpec("k_confirm", "连续成立拍数", "int", 3, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("hit_count", "int", "命中路数"),
        ("n", "int", "总路数"),
        ("spread", "float", "同刻跨路极差"),
        ("peer_codes", "list", "参与代号"),
        ("verdict", "str", "common_cause / real_change"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        codes = [c for c in (p.peer_codes or []) if c in ctx.peers] + [ctx.series.code]
        if len(set(codes)) < 2:
            return Precheck(RunStatus.SKIP, "参与投票的代号不足 2 路")
        if int(p.k) > len(set(codes)):
            return Precheck(RunStatus.SKIP, f"k={p.k} 大于路数 {len(set(codes))}，判据永远不成立")
        return pre

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        codes = list(dict.fromkeys([s.code] + [c for c in p.peer_codes if c in ctx.peers]))
        series = [s] + [ctx.peers[c] for c in codes[1:]]
        mat = np.vstack([np.array([x.value_at(t) for t in s.t], dtype=float) for x in series])
        thr = float(p.value)
        with np.errstate(invalid="ignore"):
            if p.op == "gt":
                flags = mat > thr
            elif p.op == "ge":
                flags = mat >= thr
            elif p.op == "lt":
                flags = mat < thr
            elif p.op == "le":
                flags = mat <= thr
            elif p.op == "eq":
                flags = np.isclose(mat, thr)
            else:
                flags = ~np.isclose(mat, thr)
            flags &= ~np.isnan(mat)
            count = flags.sum(axis=0)
            spread = np.nanmax(mat, axis=0) - np.nanmin(mat, axis=0)

        need = int(p.k)
        if p.invert:
            # 反向：同向变化 + 离散度小 → 真实物理变化（用于抑制上游单路告警）
            d = np.diff(np.nan_to_num(mat, nan=np.nan), axis=1)
            with np.errstate(invalid="ignore"):
                up = np.all(d >= 0, axis=0)
                down = np.all(d <= 0, axis=0)
            same_dir = np.concatenate([[False], up | down])
            hit = same_dir & (count >= need)
        else:
            hit = count >= need
        if p.spread_max is not None:
            hit &= spread <= float(p.spread_max)

        confirmed = confirm(hit, int(p.k_confirm), allow_unknown=False) if int(p.k_confirm) > 1 else hit
        out: list[AnomalyPoint] = []
        for a, b in runs(confirmed):
            i = b
            verdict = "real_change" if p.invert else (
                "common_cause" if (p.spread_max is None or spread[i] <= float(p.spread_max)) else "real_change")
            level = Level.L1 if p.invert else Level.parse(p.level)
            out.append(point(
                ctx, level, HitType.VOTING, int(s.t[a]), int(s.t[b]),
                value=float(count[i]),
                expected=float(need),
                deviation=float(count[i] - need),
                threshold=float(need),
                strength=min(1.0, 0.5 + 0.5 * min(count[i] / max(need, 1) - 1, 1.0)),
                sample_count=int(b - a + 1),
                evidence={
                    "hit_count": int(count[i]),
                    "n": len(codes),
                    "spread": round(float(spread[i]), 6) if not np.isnan(spread[i]) else None,
                    "peer_codes": codes,
                    "verdict": verdict,
                },
            ))
        return out
