"""
A-16 时间对齐与重采样（ZOH Alignment）—— 基础设施
================================================

所有跨参数判据的前置：包率不同、时刻不齐、有丢包。

三条纪律：
1. 零阶保持取"栅格时刻之前最近的样本"，不做线性外推（外推会造出不存在的中间值）；
2. 丢包必须填 NaN，绝不能填 0 —— 填 0 会让所有越限判据同时误报；
3. 跳变类判据要按源包自己的时间戳算，不能按对齐后的栅格算 —— ZOH 最多引入一个包周期滞后。
"""
from __future__ import annotations

import numpy as np

from .base import (
    BaseAlgorithm, ParamSpec, AlgorithmContext, AnomalyPoint,
    TelemetrySeries, register,
)


@register
class AlignAlgorithm(BaseAlgorithm):
    ALGO_ID = "A-16"
    ALGO_NAME = "时间对齐与重采样"
    ALGO_NAME_EN = "ZOH Alignment"
    LAYER = "L3"
    CATEGORY = "基础设施"
    IS_DETECTOR = False
    SUPPORTED_KINDS = tuple()
    RATIONALE = (
        "M-01（位移）、M-02（振动）、M-03（参考通道）包率不同，直接按下标配对会错位几十拍，"
        "相关性、成对偏置、横截面全部失效。对齐没做对，上层算法再精细都是错的。"
    )
    USE_CASES = "全部跨代号判据：A-07 相关、A-08 横截面、A-09 成对、A-17 参考通道交叉校验、A-25 复合条件。"
    PARAM_SPECS = [
        ParamSpec("grid_s", "栅格周期", "seconds", 1.0, minimum=0.001,
                  help="建议取最慢包率的整倍数"),
        ParamSpec("method", "插值方式", "enum", "zoh", choices=["zoh", "linear"],
                  help="默认零阶保持；linear 仅用于绘图，不用于判据"),
        ParamSpec("max_gap_s", "最大填充间隔", "seconds", 10.0,
                  help="超过该间隔仍无样本则填 NaN，不做长距离保持"),
    ]
    OUTPUT_FIELDS = [("t", "array", "公共栅格"), ("aligned", "dict", "代号→对齐序列")]

    def detect(self, ctx: AlgorithmContext, p) -> list[AnomalyPoint]:
        return []

    def run_infra(self, series_map: dict[str, TelemetrySeries], p) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        if not series_map:
            return np.empty(0, dtype=np.int64), {}
        t0 = max(int(s.t[0]) for s in series_map.values() if len(s))
        t1 = min(int(s.t[-1]) for s in series_map.values() if len(s))
        step = int(float(p.get("grid_s", 1.0)) * 1000)
        grid = np.arange(t0, t1 + 1, step, dtype=np.int64)
        aligned: dict[str, np.ndarray] = {}
        method = p.get("method", "zoh")
        max_gap = float(p.get("max_gap_s", 10.0)) * 1000
        for code, s in series_map.items():
            vals = np.array([s.value_at(t, method) for t in grid], dtype=float)
            # 超出最大保持间隔的位置置 NaN
            idx = np.searchsorted(s.t, grid, side="right") - 1
            age = np.where(idx >= 0, grid - s.t[np.clip(idx, 0, len(s) - 1)], np.inf)
            vals[age > max_gap] = np.nan
            aligned[code] = vals
        return grid, aligned
