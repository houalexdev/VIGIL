"""
A-15 帧完整性校验（Frame Integrity）
===================================

判据原文：帧头 ≠ 常量 / 顺序号 Δ≠1 / 包长 ≠ 期望 / 和校验不符，两个自定义包各施一遍。

这类错误的危害不是"数据错"，而是"错得很合理"：一旦错位，后面的字节会被当成
合法字段读，产生一批看似正常的假值，比越限难查得多。

数据依赖：需要原始字节流（或已解析的帧字段）。若只拿到工程量，本算法应 SKIP，
由 A-27 序列缺口检测兜底——不要用工程量去伪造帧校验。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, Params, point,
    AlgorithmContext, AnomalyPoint, HitType, Level,
    Precheck, RunStatus, ValueKind, register,
)


@register
class FrameIntegrityAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-15"
    ALGO_NAME = "帧完整性校验"
    ALGO_NAME_EN = "Frame Integrity"
    LAYER = "L1"
    CATEGORY = "完整性"
    HIT_TYPE = HitType.FRAME
    SUPPORTED_KINDS = tuple(ValueKind)
    RATIONALE = (
        "传输包靠帧头定位、顺序号发现丢包、长度发现截断、和校验发现误码。"
        "缺任一项，一次错位就会污染后续所有判据——而且是静默污染。"
        "这是唯一必须在字节层做的判断，不能由工程量反推。"
    )
    USE_CASES = "M-07①②③④ 工作状态包 / 位移包的帧头、顺序号、长度、和校验。"
    PARAM_SPECS = [
        ParamSpec("header_value", "帧头常量", "int", None, help="协议常量；留空则跳过帧头检查"),
        ParamSpec("header_offset", "帧头偏移", "int", 0, "byte", minimum=0),
        ParamSpec("header_bytes", "帧头字节数", "int", 1, "byte", minimum=1),
        ParamSpec("len_index", "长度字段偏移", "int", None, help="留空则跳过；HYG4001 是包内自带长度"),
        ParamSpec("expect_len", "期望包长", "int", None, minimum=1),
        ParamSpec("sum_index", "校验字偏移", "int", None, help="留空则跳过"),
        ParamSpec("sum_mode", "校验方式", "enum", "u16_mod", choices=["u16_mod", "u8_xor", "sum8_mod"]),
        ParamSpec("seq_index", "顺序号偏移", "int", None),
        ParamSpec("seq_mod", "顺序号模", "int", 256, minimum=2),
        ParamSpec("max_consecutive_bad", "连续坏帧上限", "int", 3, "帧", minimum=1,
                  help="连续坏帧达到该值才升级为严重，避免单帧误码刷屏"),
    ]
    OUTPUT_FIELDS = [
        ("check", "str", "head/len/sum/seq"),
        ("got", "int", "实际值"),
        ("expect", "int", "期望值"),
        ("consecutive_bad", "int", "连续坏帧数"),
        ("packet_index", "int", "帧序号"),
    ]

    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        pre = super().precheck(ctx, p)
        if not pre.passed:
            return pre
        if not ctx.extra.get("packets"):
            return Precheck(RunStatus.SKIP, "未提供原始帧数据（packets），帧校验无法执行；由 A-27 兜底")
        return pre

    @staticmethod
    def _field(data: list[int], idx: int, nbytes: int = 1) -> int:
        return int.from_bytes(bytes(data[idx: idx + nbytes]), "big")

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        packets = ctx.extra["packets"]
        out: list[AnomalyPoint] = []
        bad_streak = 0
        prev_seq = None
        for k, pkt in enumerate(packets):
            data = list(pkt.get("data") or [])
            ts = int(pkt.get("ts", 0))
            if not data:
                continue
            problems = []
            # 1) 帧头
            if p.header_value is not None:
                got = self._field(data, int(p.header_offset), int(p.header_bytes))
                if got != int(p.header_value):
                    problems.append(("head", got, int(p.header_value)))
            # 2) 长度（优先于和校验：长度不符先截断，否则越界）
            if p.len_index is not None and p.expect_len is not None:
                got = self._field(data, int(p.len_index))
                if got != int(p.expect_len) or len(data) != int(p.expect_len):
                    problems.append(("len", len(data), int(p.expect_len)))
            # 3) 和校验
            if p.sum_index is not None and not problems:
                idx = int(p.sum_index)
                body = data[:idx]
                if p.sum_mode == "u16_mod":
                    calc = sum(body) & 0xFFFF
                elif p.sum_mode == "u8_xor":
                    calc = 0
                    for b in body:
                        calc ^= b
                else:
                    calc = sum(body) & 0xFF
                got = self._field(data, idx, 2 if p.sum_mode == "u16_mod" else 1)
                if calc != got:
                    problems.append(("sum", got, calc))
            # 4) 顺序号
            if p.seq_index is not None:
                seq = self._field(data, int(p.seq_index))
                if prev_seq is not None:
                    d = (seq - prev_seq) % int(p.seq_mod)
                    if d != 1:
                        problems.append(("seq", d, 1))
                prev_seq = seq

            if problems:
                bad_streak += 1
                level = Level.parse(p.level) if bad_streak >= int(p.max_consecutive_bad) else Level.L1
            else:
                bad_streak = 0
                continue

            for check, got, exp in problems:
                out.append(point(
                    ctx, level, HitType.FRAME, ts, ts,
                    value=float(got), expected=float(exp), threshold=float(exp),
                    dimension=f"frame:{check}",
                    strength=0.9 if check in ("head", "len", "sum") else 0.5,
                    evidence={
                        "check": check,
                        "got": int(got),
                        "expect": int(exp),
                        "consecutive_bad": bad_streak,
                        "packet_index": k,
                    },
                ))
        return out
