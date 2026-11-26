"""
A-11 位域解析与掩码比较（Bit-field Extract）
===========================================

判据原文：SPN9001_B0…B31 任一位 = 1；ELC9001_B0…B7 任一位 = 0；SPN7001 整段取 2。

三个必须守住的约定：
1. 位序：表内 B0 = 最低位（LSB），与协议一致；
2. 极性：ELC9001 是「隔离:[0,0] / 不隔离:[1,1]」，即 0 = 隔离、1 = 健康，
   写成 if V: "已隔离" 会全反。极性必须做成参数，不能硬编码；
3. 多位字段（B3B4、B6B7）必须整段取值，拆位判会漏掉"两位都不在合法组合里"的情形。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    ValueKind, confirm, runs, register,
)


@register
class BitfieldAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-11"
    ALGO_NAME = "位域解析与掩码比较"
    ALGO_NAME_EN = "Bit-field Extract"
    LAYER = "L1"
    CATEGORY = "状态字"
    HIT_TYPE = HitType.BITFIELD
    SUPPORTED_KINDS = (ValueKind.BITFIELD, ValueKind.FLAG, ValueKind.ENUM, ValueKind.CONTINUOUS)
    RATIONALE = (
        "一个参数包换 32 位结论，是整张表性价比最高的取数。内置以全采样率算出的跳变/不变/"
        "阈值/比较四类诊断都在这里，也是参考通道交叉校验的唯一依据。"
        "位域判据的正确性 90% 取决于位序与极性有没有搞反，而不是算法复杂度。"
    )
    USE_CASES = (
        "M-05 隔离标志 8 位（0=隔离）；M-08 链路故障 3+3 位；M-12 内置诊断 32 位；"
        "M-13 诊断开关 6 位；M-11③④ 自主重启开关位。"
    )
    PARAM_SPECS = [
        ParamSpec("bit_offset", "起始位", "int", 0, "bit", minimum=0, maximum=63,
                  help="B0 为最低位；如 B3B4 填 3"),
        ParamSpec("bit_width", "位宽", "int", 1, "bit", minimum=1, maximum=32,
                  help="1=单比特；B3B4 填 2；整个状态字填 32"),
        ParamSpec("mode", "判据形态", "enum", "any_set",
                  choices=["any_set", "all_set", "equals", "changed"],
                  help="any_set=该位为 fault_value 即告警；equals=整段等于期望值；changed=相对基线发生变化"),
        ParamSpec("fault_value", "故障位值", "int", 1, minimum=0, maximum=1,
                  depends_on={"mode": ["any_set", "all_set"]},
                  help="极性参数！ELC9001 填 0（0=隔离=故障）"),
        ParamSpec("expect_value", "期望取值", "int", 0, minimum=0,
                  depends_on={"mode": ["equals"]}, help="整段取值不为该值即告警，如 SPN7001 期望 2"),
        ParamSpec("legal_values", "合法取值集合", "list[int]", None,
                  depends_on={"mode": ["equals"]}, help="给定时优先于 expect_value"),
        ParamSpec("watch_bits", "监视位列表", "list[int]", None,
                  help="留空=全字段；填 [0,1,2] 只监视这几位（逐位展开）"),
        ParamSpec("expand_bits", "逐位展开输出", "bool", True,
                  help="打开后每一位单独成点，定位到 bit:N；关闭后整个字段一个点"),
        ParamSpec("k", "连续成立拍数", "int", 2, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [
        ("word_value", "int", "原始字值"),
        ("bit_index", "int", "位号（B0 起）"),
        ("bit_value", "int", "该位取值"),
        ("field_value", "int", "整段取值"),
        ("expect", "str", "期望值/合法集合"),
        ("raw_hex", "str", "原始字十六进制"),
    ]

    def _extract(self, v: np.ndarray, off: int, width: int) -> np.ndarray:
        mask = (1 << width) - 1
        return (np.nan_to_num(v, nan=0).astype(np.int64) >> off) & mask

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        off, width = int(p.bit_offset), int(p.bit_width)
        field = self._extract(s.v, off, width)
        base_level = Level.parse(p.level)
        out: list[AnomalyPoint] = []

        if p.mode == "equals":
            legal = p.get("legal_values")
            if legal:
                mask_hit = ~np.isin(field, np.asarray(legal, dtype=np.int64))
                expect_txt = f"in {legal}"
                # 非连续取值无法区分哪一位，只能整段报
                dims = [("", mask_hit, field, expect_txt)]
            else:
                exp = int(p.expect_value or 0)
                dims = [("", field != exp, field, str(exp))]
        else:
            bits = p.watch_bits or list(range(off, off + width))
            dims = []
            for b in bits:
                bv = (np.nan_to_num(s.v, nan=0).astype(np.int64) >> int(b)) & 1
                if p.mode == "any_set":
                    m = bv == int(p.fault_value)
                elif p.mode == "all_set":
                    m = bv == int(p.fault_value)
                else:  # changed
                    base = ctx.baseline.get("word")
                    if base is None:
                        continue
                    m = bv != ((int(base) >> int(b)) & 1)
                dims.append((f"bit:{b}", m, bv, str(p.fault_value if p.mode != "changed" else "≠基线")))

        for dim, mask, vals, expect_txt in dims:
            confirmed = confirm(mask, int(p.k), allow_unknown=False) if int(p.k) > 1 else mask
            for a, b in runs(confirmed):
                i = b
                out.append(point(
                    ctx, base_level, HitType.BITFIELD, int(s.t[a]), int(s.t[b]),
                    value=float(field[i]) if not dim else float(vals[i]),
                    expected=float(p.expect_value) if p.mode == "equals" and p.expect_value is not None else None,
                    threshold=float(p.fault_value) if p.mode in ("any_set", "all_set") else None,
                    dimension=dim or f"field:b{off}b{off + width - 1}",
                    sample_count=int(b - a + 1),
                    strength=0.8,
                    evidence={
                        "word_value": int(s.v[i]),
                        "bit_index": int(dim.split(":")[1]) if dim.startswith("bit:") else None,
                        "bit_value": int(vals[i]),
                        "field_value": int(field[i]),
                        "expect": expect_txt,
                        "raw_hex": hex(int(s.v[i])),
                        "mode": p.mode,
                    },
                ))
        return out
