"""
A-13 三值门控求值（Kleene 3-valued Logic）—— 基础设施
====================================================

这是整套判据的设计红线：门控不成立时输出「不知道」，不是「正常」。
- 当成 False → 判据被静默跳过，故障漏报且看板全绿；
- 当成 True  → 无门控乱报，锁紧段、未上电段的读数全被判异常。

门控表达式用极简 DSL：``{"all": [...]}, {"any": [...]}, {"not": {...}}``，
叶子为 ``{"code": "SPN7001", "op": "eq", "value": 2}``。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, AlgorithmContext, AnomalyPoint,
    GateResult, TelemetrySeries, TriState, register,
)

_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "in", "not_in", "between")


def eval_leaf(leaf: dict, series_map: dict[str, TelemetrySeries], ts: int | None = None) -> TriState:
    code = leaf.get("code")
    s = series_map.get(code)
    if s is None or len(s) == 0:
        return TriState.UNKNOWN
    v = s.value_at(ts) if ts is not None else float(s.v[-1])
    if np.isnan(v):
        return TriState.UNKNOWN
    op, ref = leaf.get("op", "eq"), leaf.get("value")
    if op == "eq":
        return TriState.TRUE if v == ref else TriState.FALSE
    if op == "ne":
        return TriState.TRUE if v != ref else TriState.FALSE
    if op == "gt":
        return TriState.TRUE if v > ref else TriState.FALSE
    if op == "ge":
        return TriState.TRUE if v >= ref else TriState.FALSE
    if op == "lt":
        return TriState.TRUE if v < ref else TriState.FALSE
    if op == "le":
        return TriState.TRUE if v <= ref else TriState.FALSE
    if op == "in":
        return TriState.TRUE if v in set(ref or []) else TriState.FALSE
    if op == "not_in":
        return TriState.TRUE if v not in set(ref or []) else TriState.FALSE
    if op == "between":
        lo, hi = (ref or [None, None])[:2]
        return TriState.TRUE if (lo is not None and hi is not None and lo <= v <= hi) else TriState.FALSE
    raise ValueError(f"门控运算符未定义: {op}，可选 {_OPS}")


def eval_gate(expr: dict | None, series_map: dict[str, TelemetrySeries], ts: int | None = None) -> GateResult:
    if not expr:
        return GateResult(TriState.TRUE, [])
    if "all" in expr:
        subs = [eval_gate(e, series_map, ts) for e in expr["all"]]
        if any(r.state == TriState.FALSE for r in subs):
            bad = [r for r in subs if r.state == TriState.FALSE]
            return GateResult(TriState.FALSE, bad[0].reasons or ["门控条件不成立"])
        if any(r.state == TriState.UNKNOWN for r in subs):
            reasons = [x for r in subs for x in r.reasons]
            return GateResult(TriState.UNKNOWN, reasons or ["门控条件中有未知项"])
        return GateResult(TriState.TRUE, [])
    if "any" in expr:
        subs = [eval_gate(e, series_map, ts) for e in expr["any"]]
        if any(r.state == TriState.TRUE for r in subs):
            return GateResult(TriState.TRUE, [])
        if any(r.state == TriState.UNKNOWN for r in subs):
            return GateResult(TriState.UNKNOWN, ["任一分支未知，整体未知"])
        return GateResult(TriState.FALSE, ["全部分支不成立"])
    if "not" in expr:
        r = eval_gate(expr["not"], series_map, ts)
        st = {TriState.TRUE: TriState.FALSE, TriState.FALSE: TriState.TRUE, TriState.UNKNOWN: TriState.UNKNOWN}[r.state]
        return GateResult(st, r.reasons)
    if "code" in expr:
        st = eval_leaf(expr, series_map, ts)
        if st == TriState.UNKNOWN:
            return GateResult(st, [f"{expr['code']} 缺数或为 NaN，门控未知"])
        if st == TriState.FALSE:
            return GateResult(st, [f"{expr['code']} {expr.get('op', 'eq')} {expr.get('value')} 不成立"])
        return GateResult(st, [])
    raise ValueError(f"无法识别的门控表达式: {expr}")


@register
class GateAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-13"
    ALGO_NAME = "三值门控求值"
    ALGO_NAME_EN = "Kleene 3-valued Logic"
    LAYER = "L3"
    CATEGORY = "基础设施"
    IS_DETECTOR = False
    SUPPORTED_KINDS = tuple()  # 不区分值域
    RATIONALE = (
        "工业设备测试有大量「此刻该不该判」的前提：主轴/参考通道选了谁、是否开环、是否锁紧、"
        "包准禁是否放开、参考通道是否上电。门控没做对，前面所有算法都是白算。"
        "三值而不是二值，是因为丢包时正确答案是「不知道」，而不是「正常」。"
    )
    USE_CASES = "规则集中 全部测点的 8 个门控（SPN7001 / SPN0001 / SPN7002 / HYG4001 / ELC0002 等）。"
    PARAM_SPECS = [
        ParamSpec("expr", "门控表达式", "dict", None,
                  help='{"all":[{"code":"SPN7001","op":"eq","value":2}]}'),
        ParamSpec("unknown_policy", "无门控数据时", "enum", "unknown",
                  choices=["unknown", "pass", "block"],
                  help="unknown=输出不知道（推荐）；pass=当作成立；block=当作不成立"),
    ]
    OUTPUT_FIELDS = [("state", "str", "T/F/U"), ("reasons", "list", "不成立或未知的原因")]

    def detect(self, ctx: AlgorithmContext, p) -> list[AnomalyPoint]:
        return []

    def run_infra(self, ctx: AlgorithmContext, p) -> GateResult:
        res = eval_gate(p.get("expr"), ctx.peers, ctx.window[0])
        if res.state == TriState.UNKNOWN and p.get("unknown_policy") == "pass":
            return GateResult(TriState.TRUE, res.reasons)
        if res.state == TriState.UNKNOWN and p.get("unknown_policy") == "block":
            return GateResult(TriState.FALSE, res.reasons)
        return res
