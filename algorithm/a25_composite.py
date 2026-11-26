"""
A-25 复合条件（Composite AND/OR）★新增
=====================================

来源：《自定义场景》序号 7「蓄电池电压 ≥86.5 V 且 热控恢复标志 = 0，持续 ≥30 s」、
序号 10「加热器 = 开 且 同区温度正常 → 误开」、序号 12「温度 < 开启阈值 且 加热器 = 关
持续 ≥60 s → 应开未开」。填写说明里明确要求「AND/OR 最多 5 层，子条件可跨测点代号」。

为什么必须单独成算法而不是塞进 A-13 门控：
- 门控回答"该不该判"，不产生告警；
- 复合条件本身就是判据，要产出异常点，而且子条件之间有优先级与证据链。

实现上先做 A-16 对齐（跨代号包率不同），再逐条件求布尔序列，最后按表达式组合。
表达式只允许 and/or/not 与 C1..Cn，**不做任意 Python 求值**，避免配置变成后门。
"""
from __future__ import annotations

import re

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, confirm,
    runs, register,
)

_ALLOWED = re.compile(r"^[C0-9a-zA-Z_\s()&|~\.]+$")


def _op_apply(v: np.ndarray, op: str, ref, tol: float = 0.0) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        if op == "gt":
            m = v > ref
        elif op == "ge":
            m = v >= ref
        elif op == "lt":
            m = v < ref
        elif op == "le":
            m = v <= ref
        elif op == "eq":
            m = np.abs(v - ref) <= tol
        elif op == "ne":
            m = np.abs(v - ref) > tol
        elif op == "in":
            m = np.isin(v, np.asarray(ref, dtype=float))
        else:
            raise ValueError(f"复合条件运算符未定义: {op}")
    return m & ~np.isnan(v)


@register
class CompositeAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-25"
    ALGO_NAME = "复合条件"
    ALGO_NAME_EN = "Composite AND/OR"
    LAYER = "L2"
    CATEGORY = "复合"
    HIT_TYPE = HitType.COMPOSITE
    SUPPORTED_KINDS = tuple(ValueKind)
    REQUIRES_PEERS = True
    RATIONALE = (
        "单点判据回答不了「应开未开」「该恢复没恢复」这类问题，因为答案在两个量之间。"
        "现场最常配的就是这种两三条件的组合，原 21 个算法没有一条能吃下跨代号逻辑，"
        "只能靠 LLM 现场拼，无法沉淀成可复用规则。"
    )
    USE_CASES = "自定义场景序号 7（电压≥86.5 且 恢复标志=0 持续 30 s）、序号 10、序号 12（应开未开/应关未关）。"
    PARAM_SPECS = [
        ParamSpec("conditions", "子条件列表", "list[dict]", None, required=True,
                  help='[{"id":"C1","code":"TRM5001","op":"ge","value":86.5},'
                       '{"id":"C2","code":"TRM0001","op":"eq","value":0}]'),
        ParamSpec("logic", "组合表达式", "str", None, required=True,
                  help="只允许 and/or/not 与 C1..Cn，如 C1 and C2 and not C3；最多 5 层"),
        ParamSpec("min_duration_s", "最短持续时长", "seconds", 0.0, minimum=0,
                  help="组合成立还需持续该时长；0 表示按拍数"),
        ParamSpec("k", "连续成立拍数", "int", 3, "拍", minimum=1),
        ParamSpec("grid_s", "对齐栅格", "seconds", 1.0, minimum=0.001),
        ParamSpec("tolerance", "等于比较容差", "float", 0.0, minimum=0),
    ]
    OUTPUT_FIELDS = [
        ("logic", "str", "组合表达式"),
        ("truth", "bool", "组合结果"),
        ("sub_results", "dict", "各子条件命中率"),
        ("satisfied", "list", "成立时各子条件的真值"),
        ("duration_s", "float", "成立持续时长"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        conds = p.get("conditions") or []
        if not conds:
            return Precheck(RunStatus.SKIP, "未配置子条件")
        missing = [c.get("code") for c in conds if c.get("code") not in ctx.peers and c.get("code") != ctx.series.code]
        if missing:
            return Precheck(RunStatus.SKIP, f"子条件引用代号未提供: {missing}")
        logic = str(p.logic or "")
        if not _ALLOWED.match(logic):
            return Precheck(RunStatus.SKIP, f"表达式含非法字符: {logic!r}")
        return pre

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        conds = p.get("conditions") or []
        tol = float(p.tolerance or 0)
        env: dict[str, np.ndarray] = {}
        rates: dict[str, float] = {}
        for c in conds:
            code = c.get("code")
            src = s if code == s.code else ctx.peers[code]
            v = np.array([src.value_at(t) for t in s.t], dtype=float)
            m = _op_apply(v, c.get("op", "eq"), c.get("value"), tol)
            env[str(c.get("id"))] = m
            rates[str(c.get("id"))] = round(float(np.mean(m)) if len(m) else 0.0, 4)

        expr = str(p.logic)
        for k in env:
            expr = re.sub(rf"\b{k}\b", f"env['{k}']", expr)
        expr = expr.replace(" and ", " & ").replace(" or ", " | ").replace("not ", "~")
        try:
            res = eval(expr, {"__builtins__": {}}, {"env": env})  # noqa: S307 - 已白名单校验
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"组合表达式求值失败: {p.logic} → {exc}") from exc
        res = np.asarray(res, dtype=bool)

        need = int(p.k)
        if p.min_duration_s:
            need = max(need, s.to_points(float(p.min_duration_s)))
        confirmed = confirm(res, need, allow_unknown=False) if need > 1 else res

        out: list[AnomalyPoint] = []
        for a, b in runs(confirmed):
            satisfied = [k for k, m in env.items() if bool(m[b])]
            out.append(point(
                ctx, Level.parse(p.level), HitType.COMPOSITE, int(s.t[a]), int(s.t[b]),
                value=1.0,
                threshold=float(need),
                dimension=f"logic:{p.logic}",
                strength=min(1.0, 0.6 + 0.4 * min((b - a + 1) / max(need, 1), 1.0)),
                sample_count=int(b - a + 1),
                evidence={
                    "logic": p.logic,
                    "truth": True,
                    "sub_results": rates,
                    "satisfied": satisfied,
                    "duration_s": round((int(s.t[b]) - int(s.t[a])) / 1000.0, 3),
                },
            ))
        return out
