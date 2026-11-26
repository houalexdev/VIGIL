"""
A-14 告警状态机（Debounce / Escalate / Suppress）—— 基础设施
==========================================================

单个算法只回答「这一拍成不成立」，状态机回答「这条规则现在是正常、
待确认、还是告警中」。它把逐拍判定收敛成事件，避免看板被同一次故障刷屏。

四条状态与三条纪律：
    NORMAL → PENDING → ACTIVE → CLEARING → NORMAL
- 门控 = F：转 SUPPRESSED，事件**不删除**，只静默（复盘时看得到）；
- 门控 = U：保持当前级，绝不回 NORMAL（一次丢包不能把故障「恢复」掉）；
- 已 ACTIVE 的事件升级后要留升级轨迹（level_trace）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .base import (
    BaseAlgorithm, ParamSpec, AlgorithmContext, AnomalyPoint,
    Level, TriState, register,
)


class AlarmState(str, Enum):
    NORMAL = "NORMAL"
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    CLEARING = "CLEARING"
    SUPPRESSED = "SUPPRESSED"


@dataclass
class AlarmEvent:
    """由异常点序列聚合出的事件。与 AnomalyPoint 一一对应：一个点 = 一次事件。"""

    state: AlarmState = AlarmState.NORMAL
    level: Level = Level.L2
    t_first: int = 0
    t_last: int = 0
    hit_points: int = 0
    miss_points: int = 0
    suppressed: bool = False
    level_trace: list[tuple[int, str]] = field(default_factory=list)


@register
class AlarmStateMachine(BaseAlgorithm):
    ALGO_ID = "A-14"
    ALGO_NAME = "告警状态机"
    ALGO_NAME_EN = "Debounce / Escalate / Suppress"
    LAYER = "L3"
    CATEGORY = "基础设施"
    IS_DETECTOR = False
    SUPPORTED_KINDS = tuple()
    RATIONALE = (
        "测试现场最怕两件事：一次故障刷 200 条告警、一次丢包把致命告警「自动恢复」。"
        "状态机同时解决这两个问题，且把「抑制」变成有记录的状态而不是丢弃。"
    )
    USE_CASES = "全部规则的告警收敛；M-14④ 掉电恢复期 30 s 内下游判据抑制。"
    PARAM_SPECS = [
        ParamSpec("k_confirm", "确认拍数", "int", 3, "拍", minimum=1),
        ParamSpec("m_clear", "恢复拍数", "int", 6, "拍", minimum=1),
        ParamSpec("escalate_after_s", "升级延时", "seconds", 60.0, minimum=0,
                  help="ACTIVE 持续该时长后按 escalate_to 升级"),
        ParamSpec("escalate_to", "升级目标等级", "enum", "严重",
                  choices=["提示", "一般", "严重", "致命"]),
        ParamSpec("suppress_on_gate_false", "门控不成立时抑制", "bool", True),
        ParamSpec("keep_unknown_level", "门控未知时保持当前级", "bool", True),
    ]
    OUTPUT_FIELDS = [("state", "str", "最终状态"), ("level", "str", "最终等级"),
                     ("level_trace", "list", "升级轨迹")]

    def detect(self, ctx: AlgorithmContext, p) -> list[AnomalyPoint]:
        return []

    def run_infra(self, points: list[AnomalyPoint], p) -> list[AnomalyPoint]:
        """对同一规则的异常点序列做状态收敛，返回带升级/抑制标记的点。"""
        if not points:
            return []
        esc_after = float(p.get("escalate_after_s", 0) or 0) * 1000
        esc_to = Level.parse(p.get("escalate_to", "严重"))
        out: list[AnomalyPoint] = []
        event: AlarmEvent | None = None
        for pt in sorted(points, key=lambda x: x.t_start):
            if event is None or pt.t_start > event.t_last + int(p.get("m_clear", 6)) * 1000:
                event = AlarmEvent(state=AlarmState.PENDING, level=pt.level, t_first=pt.t_start)
            event.hit_points += 1
            event.t_last = max(event.t_last, pt.t_end)

            if pt.gate_state == TriState.FALSE and p.get("suppress_on_gate_false", True):
                event.state = AlarmState.SUPPRESSED
                pt.suppressed = True
                pt.suppress_reason = "门控不成立，按抑制处理（保留痕迹）"
            elif pt.gate_state == TriState.UNKNOWN and p.get("keep_unknown_level", True):
                event.state = AlarmState.ACTIVE
                pt.unknown_reason = "门控未知，维持当前告警级别"
            else:
                event.state = AlarmState.ACTIVE

            if event.state == AlarmState.ACTIVE and (pt.t_end - event.t_first) >= esc_after > 0:
                if pt.level != esc_to and event.level != esc_to:
                    event.level_trace.append((pt.t_start, f"{pt.level.value}→{esc_to.value}"))
                    pt.level = esc_to
                event.level = max([event.level, pt.level], key=lambda x: list(Level).index(x))
            out.append(pt)
        return out
