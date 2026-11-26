"""算法包：导入本包即完成全部算法注册。

新增算法只需把 ``aNN_xxx.py`` 放进本目录，无需改动任何其他文件
（本文件自动扫描同目录下的 ``a*`` 模块）。

    >>> import algorithm
    >>> algorithm.catalog()          # 全部算法规格（给前端表单 / LLM / 文档）
    >>> algorithm.build("A-01", {"mode": "outside", "k": 3}).run(ctx)
"""
from __future__ import annotations

import importlib
import pkgutil

from .base import (  # noqa: F401 - 对外统一出口
    EPS,
    COMMON_SPECS,
    PARAM_TYPES,
    AlgorithmContext,
    AlgorithmResult,
    AnomalyPoint,
    BaseAlgorithm,
    GateResult,
    HitType,
    Level,
    ParamError,
    ParamSpec,
    Params,
    Precheck,
    RangeSpec,
    RunStatus,
    TelemetryMeta,
    TelemetrySeries,
    TriState,
    ValueKind,
    all_algorithms,
    build,
    catalog,
    confirm,
    counter_delta,
    dtw_distance,
    for_kind,
    get,
    json_schema,
    max_level,
    median,
    mad,
    nanquantile,
    nanstd,
    pearson,
    phase_resample,
    point,
    register,
    robust_sigma,
    rolling_median,
    rolling_std,
    runs,
    sliding,
    smooth3,
    validate_params,
)

# 自动发现：文件名以 a 开头（a01_threshold.py …）即视为一个算法模块
for _m in pkgutil.iter_modules(__path__):
    if _m.name.startswith("a") and _m.name[1:3].isdigit():
        importlib.import_module(f".{_m.name}", __name__)

__all__ = [
    "EPS", "COMMON_SPECS", "PARAM_TYPES",
    "AlgorithmContext", "AlgorithmResult", "AnomalyPoint", "BaseAlgorithm", "GateResult",
    "HitType", "Level", "ParamError", "ParamSpec", "Params", "Precheck", "RangeSpec",
    "RunStatus", "TelemetryMeta", "TelemetrySeries", "TriState", "ValueKind",
    "all_algorithms", "build", "catalog", "confirm", "counter_delta", "dtw_distance",
    "for_kind", "get", "json_schema", "max_level", "median", "mad", "nanquantile",
    "nanstd", "pearson", "phase_resample", "point", "register", "robust_sigma",
    "rolling_median", "rolling_std", "runs", "sliding", "smooth3", "validate_params",
]
