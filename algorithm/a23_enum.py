"""
A-23 枚举合法性 / 特征码识别（Enum Legality & Signature）★新增
============================================================

来源：《自定义场景》序号 5「电源测点包更新状态 = 0；出现 2/3 立即告警」、
序号 8/10「热敏电阻未接，源码 0FF5/0FFB 换算为 -75.37℃/-83.43℃，属固定特征值」。

这两类都是「值本身有含义」的判据，A-01 的区间比较表达不了：
- 枚举型：取值必须落在合法集合内，且某些"合法但表示故障"的值要单独定级；
- 特征码型：某个特定值（通常是量程两端的悬空码）出现即代表传感器开路，
  应当标注原因并按虚警屏蔽，而不是逐点配区间。

把两者合成一个算法，是因为它们的输入输出完全同构：值 → 命中集合 → 带标签的异常点。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, confirm, runs, register,
)


@register
class EnumAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-23"
    ALGO_NAME = "枚举合法性/特征码识别"
    ALGO_NAME_EN = "Enum Legality & Signature"
    LAYER = "L1"
    CATEGORY = "状态"
    HIT_TYPE = HitType.ENUM_ILLEGAL
    SUPPORTED_KINDS = (ValueKind.ENUM, ValueKind.FLAG, ValueKind.BITFIELD, ValueKind.CONTINUOUS)
    RATIONALE = (
        "测点里有大量离散量：模式字、更新状态、锁定指示、包准禁。它们没有「量程」，"
        "只有「含义」，用区间判据必然写错。更麻烦的是悬空热敏电阻这类固定特征码，"
        "值本身落在量程内但物理上不可能——必须靠特征码表识别，"
        "命中即标注开路并按虚警屏蔽，避免逐点配区间造成大片误报。"
    )
    USE_CASES = "自定义场景序号 5（更新状态 0/1/2/3）、序号 8/10（0FF5/0FFB 开路特征）；M-12 内置诊断位。"
    PARAM_SPECS = [
        ParamSpec("legal_values", "合法取值集合", "list[int]", None,
                  help="留空则自动取目录枚举定义；给定后以参数为准"),
        ParamSpec("strict", "严格模式", "bool", True,
                  help="严格：不在合法集合内即告警；宽松：仅特征码与已知坏值告警"),
        ParamSpec("bad_values", "已知故障值→等级", "dict", None,
                  help='{"2": "一般", "3": "严重"}；命中的值按此定级并标注含义'),
        ParamSpec("signature_values", "特征码→原因", "dict", None,
                  help='{"4077": "热敏电阻开路(0FF5)"}；命中即标注原因'),
        ParamSpec("signature_level", "特征码等级", "enum", "提示",
                  choices=["提示", "一般", "严重", "致命"],
                  help="默认提示并按虚警屏蔽，避免开路特征刷屏"),
        ParamSpec("k", "连续成立拍数", "int", 1, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("value", "int", "命中值"),
        ("label", "str", "该值的含义（来自目录枚举或参数）"),
        ("matched", "str", "illegal/bad/signature"),
        ("legal_values", "list", "生效的合法集合"),
        ("raw_hex", "str", "原始值十六进制"),
    ]

    def _legal(self, ctx: AlgorithmContext, p: Params) -> list[int]:
        if p.legal_values:
            return [int(x) for x in p.legal_values]
        return sorted(ctx.meta.enum_map().keys())

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        legal = self._legal(ctx, p)
        emap = ctx.meta.enum_map()
        bad = {int(k): v for k, v in (p.get("bad_values") or {}).items()}
        sig = {int(k): v for k, v in (p.get("signature_values") or {}).items()}

        vi = np.nan_to_num(s.v, nan=-999999).astype(np.int64)
        valid = ~np.isnan(s.v)
        if p.strict and legal:
            illegal = (~np.isin(vi, legal)) & valid
        else:
            illegal = np.zeros(len(s), dtype=bool)

        # 逐类聚合：signature / bad / illegal，同一个值只报一次
        targets: list[tuple[int, str, Level, str]] = []
        for value in np.unique(vi[valid]).tolist():
            if value in sig:
                targets.append((value, "signature", Level.parse(p.signature_level), sig[value]))
            elif value in bad:
                targets.append((value, "bad", Level.parse(bad[value]), emap.get(value, f"故障值 {value}")))
            elif illegal.size and np.any((vi == value) & illegal):
                targets.append((value, "illegal", Level.parse(p.level), emap.get(value, f"未定义取值 {value}")))

        out: list[AnomalyPoint] = []
        for value, kind, level, label in targets:
            mask = (vi == value) & valid
            confirmed = confirm(mask, int(p.k), allow_unknown=False) if int(p.k) > 1 else mask
            for a, b in runs(confirmed):
                out.append(point(
                    ctx, level, HitType.ENUM_ILLEGAL, int(s.t[a]), int(s.t[b]),
                    value=float(value),
                    expected=None,
                    threshold=float(len(legal)) if legal else None,
                    dimension=f"enum:{kind}",
                    strength=0.85 if kind != "signature" else 0.55,
                    sample_count=int(b - a + 1),
                    suppressed=(kind == "signature"),
                    suppress_reason="开路/悬空特征码，按虚警屏蔽（保留记录）" if kind == "signature" else "",
                    evidence={
                        "value": int(value),
                        "label": label,
                        "matched": kind,
                        "legal_values": legal,
                        "raw_hex": hex(int(value)),
                    },
                ))
        return out
