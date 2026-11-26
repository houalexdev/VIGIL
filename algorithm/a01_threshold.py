"""
A-01 超限 / 定值 + 持续确认（Threshold & Debounce）
==================================================

覆盖《自定义场景》里的「超限·定值」「超限·区间」「超限·下限/上限」，
以及规则集里所有「连续 ≥k 拍」的越限条款。

设计要点：
- 阈值三级来源：客户判据 > 目录正常值范围 > 算法参数；前两级自动带入，人工可覆盖。
- 缺数据（NaN）不参与成立，且**不会**中断已确认的越限段，但会在 evidence 里记 missing。
- 「持续 0 s」= 单拍即报（k=1），对应计数类测点「≠0 立即告警」的写法。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, confirm, runs, register,
)


@register
class ThresholdAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-01"
    ALGO_NAME = "超限/定值 + 持续确认"
    ALGO_NAME_EN = "Threshold & Debounce"
    LAYER = "L1"
    CATEGORY = "超限"
    HIT_TYPE = HitType.THRESHOLD
    SUPPORTED_KINDS = (
        ValueKind.CONTINUOUS, ValueKind.COUNTER, ValueKind.ENUM, ValueKind.FLAG,
    )
    RATIONALE = (
        "最基础也最高频的判据形态。真正的难点不在比较大小，而在两件事："
        "阈值从哪来、持续多久才算数。单拍越限 90% 是量化抖动或传输误码，"
        "直接用会把看板刷满；而计数类测点恰恰相反，+1 就必须立即报，不能被持续时间过滤掉。"
    )
    USE_CASES = (
        "M-01① 位移越界 [0.5,44.5]mm 连续≥3拍；M-05① 隔离位置0；"
        "M-11② 重启次数≥上限；M-14①② 供电越界；自定义场景序号 2/4/14/15/16/17 的定值与区间判据。"
    )
    PARAM_SPECS = [
        ParamSpec("mode", "异常条件", "enum", "outside",
                  choices=["outside", "gt", "ge", "lt", "le", "eq", "ne", "set"],
                  help="outside=越界；gt/ge/lt/le=单侧；eq/ne=定值；set=命中给定值集"),
        ParamSpec("low", "下界", "float", None, auto_from="meta.primary_normal.low",
                  depends_on={"mode": ["outside", "lt", "le"]}, help="留空则取目录正常值范围下界"),
        ParamSpec("high", "上界", "float", None, auto_from="meta.primary_normal.high",
                  depends_on={"mode": ["outside", "gt", "ge"]}, help="留空则取目录正常值范围上界"),
        ParamSpec("target_value", "定值", "float", None,
                  depends_on={"mode": ["eq", "ne"]}, help="eq/ne 的基准值，如计数类测点填 0"),
        ParamSpec("tolerance", "定值容差", "float", 0.0, unit="工程值",
                  depends_on={"mode": ["eq", "ne"]}, help="|v-target| ≤ 容差视为等于"),
        ParamSpec("value_set", "非法值集", "list[float]", None,
                  depends_on={"mode": ["set"]}, help="命中集合内任一值即告警，如开路特征码"),
        ParamSpec("k", "连续成立拍数", "int", 3, "拍", minimum=1,
                  help="计数类/单拍判据填 1；模拟量建议 3"),
        ParamSpec("min_duration_s", "最短持续(秒)", "seconds", 0.0, minimum=0,
                  help="0 表示只按拍数判定；>0 时还要求持续时长达到该值"),
        ParamSpec("use_warn_range", "启用预警区", "bool", True,
                  help="命中预警值范围时输出提示级（L1），与越限分开展示"),
        ParamSpec("inclusive", "边界含等号", "bool", True,
                  help="关闭后 v==阈值不算越限（用于严格区间）"),
    ]
    OUTPUT_FIELDS = [
        ("mode", "str", "实际使用的异常条件"),
        ("breach_count", "int", "段内越限点数"),
        ("breach_ratio", "float", "段内越限点占比"),
        ("extreme_value", "float", "段内最偏离的值"),
        ("zone", "str", "normal/warn/alarm"),
        ("missing_in_run", "int", "段内缺失点数"),
    ]

    # -- 阈值解析 ---------------------------------------------------------
    def _bounds(self, ctx: AlgorithmContext, p: Params) -> tuple[float, float]:
        low, high = p.get("low"), p.get("high")
        normal = ctx.meta.primary_normal()
        if low is None and normal is not None:
            low = normal.low
        if high is None and normal is not None:
            high = normal.high
        # 定值模式下允许只给 target
        if low is None and p.mode in ("eq", "ne", "set"):
            low = p.get("target_value")
        if high is None and p.mode in ("eq", "ne", "set"):
            high = p.get("target_value")
        return (float("-inf") if low is None else float(low),
                float("inf") if high is None else float(high))

    def _anomaly_mask(self, v: np.ndarray, p: Params, low: float, high: float) -> np.ndarray:
        inc = bool(p.inclusive)
        with np.errstate(invalid="ignore"):
            mode = p.mode
            if mode == "outside":
                m = (v > high) | (v < low) if inc else (v >= high) | (v <= low)
            elif mode == "gt":
                m = v > high
            elif mode == "ge":
                m = v >= high
            elif mode == "lt":
                m = v < low
            elif mode == "le":
                m = v <= low
            elif mode in ("eq", "ne"):
                tgt, tol = float(p.target_value or 0.0), float(p.tolerance or 0.0)
                eq = np.abs(v - tgt) <= tol
                m = eq if mode == "eq" else ~eq
            elif mode == "set":
                ss = p.get("value_set") or []
                m = np.isin(v, np.asarray(ss, dtype=float)) if ss else np.zeros_like(v, dtype=bool)
            else:  # pragma: no cover
                m = np.zeros_like(v, dtype=bool)
        return m & ~np.isnan(v)

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        v = s.v
        low, high = self._bounds(ctx, p)
        if low == float("-inf") and high == float("inf"):
            return []
        hit = self._anomaly_mask(v, p, low, high)
        k = int(p.k)
        confirmed = confirm(hit, k, allow_unknown=False) if k > 1 else hit
        if not confirmed.any():
            return []

        base_level = Level.parse(p.level)
        warn_mask = np.zeros_like(hit)
        if p.use_warn_range and ctx.meta.warn_ranges:
            for r in ctx.meta.warn_ranges:
                with np.errstate(invalid="ignore"):
                    warn_mask |= (v >= r.low) & (v <= r.high)

        period = s.period_ms()
        out: list[AnomalyPoint] = []
        for a, b in runs(confirmed):
            t0, t1 = int(s.t[a]), int(s.t[b])
            if p.min_duration_s and (t1 - t0) < float(p.min_duration_s) * 1000:
                continue
            seg_v = v[a: b + 1]
            seg_ok = ~np.isnan(seg_v)
            if not seg_ok.any():
                continue
            vals = seg_v[seg_ok]
            # 取最偏离正常区间的值作为代表值
            if np.isfinite(high) and np.nanmax(vals) > high:
                extreme = float(np.nanmax(vals))
            elif np.isfinite(low) and np.nanmin(vals) < low:
                extreme = float(np.nanmin(vals))
            else:
                extreme = float(vals[-1])
            zone = "warn" if warn_mask[a: b + 1].any() else "alarm"
            level = Level.L1 if (zone == "warn" and base_level != Level.L1) else base_level
            breach = int(np.count_nonzero(seg_ok))
            out.append(point(
                ctx, level, HitType.THRESHOLD, t0, t1,
                value=float(vals[-1]),
                expected=float(np.clip(extreme, low, high)) if np.isfinite(low) and np.isfinite(high) else None,
                low=low if np.isfinite(low) else None,
                high=high if np.isfinite(high) else None,
                deviation=float(extreme - high) if np.isfinite(high) and extreme > high
                else (float(extreme - low) if np.isfinite(low) and extreme < low else 0.0),
                threshold=high if (np.isfinite(high) and extreme > high) else (low if np.isfinite(low) else None),
                strength=min(1.0, 0.6 + 0.4 * min(breach / max(k, 1) / 5.0, 1.0)),
                sample_count=int(b - a + 1),
                evidence={
                    "mode": p.mode,
                    "breach_count": breach,
                    "breach_ratio": round(breach / max(b - a + 1, 1), 4),
                    "extreme_value": round(extreme, 6),
                    "zone": zone,
                    "missing_in_run": int(np.count_nonzero(~seg_ok)),
                    "k": k,
                    "duration_s": round((t1 - t0) / 1000.0, 3),
                },
            ))
        return out
