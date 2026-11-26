"""
A-26 设定点-响应关系（Setpoint-Response Expectation）★新增
=======================================================

来源：《自定义场景》序号 3「发送 SET01/SET02 上电指令后，SPN1002/SPN1002 应 ≥4.5 V
但实测为 0」、填写说明中「设定点-响应关系：指令号 + 期望状态 + 延时」模板。

工业设备测试里大量异常不是"测点自己不对"，而是"指令发出去了，测点没跟上"。
这类判据必须拿到指令记录（上位机发送时刻 + 指令号），单独看测点永远判不出来。

判定三段：
1. 指令发出后 delay_s 内不应期（指令传输+执行时延，此段不判）；
2. [delay, timeout] 窗口内必须出现期望状态，且至少要维持 hold_s（防抖）；
3. 超时未出现 → 异常；出现后立即回落 → 异常（早退）。

纪律：指令记录缺失时返回 SKIP，不能默认"没有指令"——那会把所有正常数据判成漏响应。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, runs,
    register,
)


@register
class CmdRelationAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-26"
    ALGO_NAME = "设定点-响应关系"
    ALGO_NAME_EN = "Setpoint-Response Expectation"
    LAYER = "L2"
    CATEGORY = "时序"
    HIT_TYPE = HitType.CMD_RELATION
    SUPPORTED_KINDS = tuple(ValueKind)
    RATIONALE = (
        "「指令执行了但测点没变」是测试现场最高频的异常，也是最容易被漏掉的一类——"
        "因为测点本身完全正常，只是它没有响应。只有把指令时刻拉进来做时序断言，"
        "才能把「未响应」「响应过慢」「响应后早退」三种形态分开。"
    )
    USE_CASES = "自定义场景序号 3（SET01/SET02 上电后表头 5 V 应 ≥4.5 V）；全部控制指令的响应校验。"
    PARAM_SPECS = [
        ParamSpec("commands", "指令记录", "list[dict]", None,
                  help='[{"cmd_id":"SET01","t":1699999999000}]；留空则取 context.extra["commands"]'),
        ParamSpec("expect_op", "期望条件", "enum", "ge",
                  choices=["gt", "ge", "lt", "le", "eq", "ne"], required=True),
        ParamSpec("expect_value", "期望值", "float", None, required=True, unit="工程值"),
        ParamSpec("tolerance", "容差", "float", 0.0, minimum=0),
        ParamSpec("delay_s", "不应期", "seconds", 5.0, minimum=0,
                  help="指令发出后的传输+执行时延，此段不判定"),
        ParamSpec("timeout_s", "响应超时", "seconds", 60.0, minimum=1,
                  help="超过该时长仍未达到期望 → 未响应"),
        ParamSpec("hold_s", "期望状态需维持", "seconds", 1.0, minimum=0,
                  help="达到后至少要维持这么久才算响应成功，防瞬时抖动"),
        ParamSpec("check_early_exit", "检查响应后早退", "bool", False,
                  help="打开后，响应成功又在 hold 内掉回 → 单独报早退"),
    ]
    OUTPUT_FIELDS = [
        ("cmd_id", "str", "指令号"),
        ("cmd_time", "int", "指令时刻"),
        ("response_time", "int", "响应时刻（未响应为 None）"),
        ("observed", "float", "窗口内实测极值"),
        ("expect", "float", "期望值"),
        ("verdict", "str", "no_response/slow_response/early_exit/ok"),
        ("latency_s", "float", "响应时延"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        cmds = p.get("commands") or ctx.extra.get("commands")
        if not cmds:
            return Precheck(RunStatus.SKIP, "缺少指令记录，设定点-响应关系无法判定")
        if p.expect_value is None:
            return Precheck(RunStatus.SKIP, "未给出期望值")
        return pre

    def _meets(self, v: float, op: str, ref: float, tol: float) -> bool:
        if np.isnan(v):
            return False
        if op == "gt":
            return v > ref
        if op == "ge":
            return v >= ref
        if op == "lt":
            return v < ref
        if op == "le":
            return v <= ref
        if op == "eq":
            return abs(v - ref) <= tol
        return abs(v - ref) > tol

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        cmds = p.get("commands") or ctx.extra.get("commands") or []
        op, ref, tol = p.expect_op, float(p.expect_value), float(p.tolerance or 0)
        out: list[AnomalyPoint] = []
        for cmd in cmds:
            t_cmd = int(cmd.get("t", 0))
            t_lo = t_cmd + int(float(p.delay_s) * 1000)
            t_hi = t_lo + int(float(p.timeout_s) * 1000)
            m = (s.t >= t_lo) & (s.t <= t_hi)
            if not m.any():
                continue
            seg_v, seg_t = s.v[m], s.t[m]
            ok = np.array([self._meets(float(v), op, ref, tol) for v in seg_v])
            if not ok.any():
                vals = seg_v[~np.isnan(seg_v)]
                observed = float(vals[np.argmin(np.abs(vals - ref))]) if vals.size else float("nan")
                out.append(point(
                    ctx, Level.parse(p.level), HitType.CMD_RELATION, t_cmd, t_hi,
                    value=observed, expected=ref, threshold=ref,
                    dimension=f"cmd:{cmd.get('cmd_id', '')}",
                    strength=0.9,
                    evidence={
                        "cmd_id": cmd.get("cmd_id", ""), "cmd_time": t_cmd,
                        "response_time": None, "observed": round(observed, 6) if np.isfinite(observed) else None,
                        "expect": ref, "verdict": "no_response", "latency_s": None,
                    },
                ))
                continue
            first = int(np.argmax(ok))
            t_resp = int(seg_t[first])
            hold_ms = int(float(p.hold_s) * 1000)
            if p.check_early_exit and hold_ms > 0:
                after = (s.t > t_resp) & (s.t <= t_resp + hold_ms)
                if after.any():
                    ok_after = np.array([self._meets(float(v), op, ref, tol) for v in s.v[after]])
                    if not ok_after.all():
                        out.append(point(
                            ctx, Level.parse(p.level), HitType.CMD_RELATION, t_resp, int(s.t[after][-1]),
                            value=float(s.v[after][-1]), expected=ref, threshold=ref,
                            dimension=f"cmd:{cmd.get('cmd_id', '')}", strength=0.7,
                            evidence={"cmd_id": cmd.get("cmd_id", ""), "cmd_time": t_cmd,
                                      "response_time": t_resp, "observed": float(s.v[after][-1]),
                                      "expect": ref, "verdict": "early_exit",
                                      "latency_s": round((t_resp - t_cmd) / 1000.0, 3)},
                        ))
        return out
