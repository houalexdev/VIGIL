"""
算法统一模板（base.py）
======================

本文件是**所有异常检测算法的唯一依赖**。任何算法只允许从这里取类型、取工具，
从而保证「新增算法不动框架、框架换实现不动算法」。

一个算法 = 一个文件 + 一个 ``@register`` 装饰器，文件内只需写三样东西：

    class XxxAlgorithm(BaseAlgorithm):
        ALGO_ID / ALGO_NAME / LAYER / HIT_TYPE / SUPPORTED_KINDS   # 身份
        PARAM_SPECS = [...]                                        # 参数（表单/LLM/文档共用）
        def detect(self, ctx, p) -> list[AnomalyPoint]: ...         # 只写检测逻辑

``run()`` 是模板方法，顺序固定，子类不得覆写：

    校验参数 → precheck（门控/值域/样本量）→ detect() → 归一化盖章 → AlgorithmResult

四条铁律
--------
1. 时间统一用 **毫秒级 epoch 整数**（int64），不用 datetime、不用浮点时间。
2. 缺失值一律 NaN + quality 标记，**禁止填 0**（否则越限判据全误报）。
3. "不知道"必须用 ``TriState.UNKNOWN`` / ``RunStatus.UNKNOWN`` 表达，**禁止退化成 False**。
4. 算法不写库、不读文件、不打印，只吃 context、只吐 points。

最小可用示例
------------
    >>> from algorithm.base import *
    >>> class Demo(BaseAlgorithm):
    ...     ALGO_ID, ALGO_NAME = "A-99", "演示"
    ...     PARAM_SPECS = [ParamSpec("thr", "阈值", "float", 1.0, minimum=0)]
    ...     def detect(self, ctx, p):
    ...         hit = ctx.series.v > p.thr
    ...         return [point(ctx, "一般", HitType.THRESHOLD, ctx.series.t[i], value=v)
    ...                 for i, v in zip(*np.where(hit))]
"""
from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

import numpy as np

EPS = 1e-12

__all__ = [
    # 枚举
    "Level", "TriState", "ValueKind", "HitType", "RunStatus", "max_level",
    # 数据
    "RangeSpec", "TelemetryMeta", "TelemetrySeries",
    "GateResult", "AlgorithmContext",
    # 输出
    "AnomalyPoint", "Precheck", "AlgorithmResult",
    # 参数
    "ParamSpec", "Params", "ParamError", "validate_params", "COMMON_SPECS", "PARAM_TYPES",
    # 框架
    "BaseAlgorithm", "register", "all_algorithms", "get", "build", "for_kind", "catalog", "json_schema",
    # 工具
    "point", "median", "mad", "robust_sigma", "nanquantile", "nanstd", "sliding",
    "rolling_median", "rolling_std", "smooth3", "runs", "confirm", "counter_delta",
    "pearson", "phase_resample", "dtw_distance", "EPS",
]


# ===========================================================================
# 一、枚举
# ===========================================================================


class Level(str, Enum):
    """告警等级，与《指标与判据集》的四级中文一一对应。"""

    L1 = "L1"  # 提示：仅供复核，不打断测试
    L2 = "L2"  # 一般
    L3 = "L3"  # 严重
    L4 = "L4"  # 致命

    @property
    def label(self) -> str:
        return _LEVEL_LABEL[self]

    @classmethod
    def parse(cls, value: Any) -> "Level":
        if isinstance(value, Level):
            return value
        alias = {
            "提示": cls.L1, "L1": cls.L1, "INFO": cls.L1, "INFO.": cls.L1,
            "一般": cls.L2, "L2": cls.L2, "WARN": cls.L2, "WARNING": cls.L2,
            "严重": cls.L3, "L3": cls.L3, "MAJOR": cls.L3,
            "致命": cls.L4, "L4": cls.L4, "CRITICAL": cls.L4, "FATAL": cls.L4,
        }
        key = str(value).strip().upper()
        if key in alias:
            return alias[key]
        raise ValueError(f"未定义的告警等级: {value!r}（可选 提示/一般/严重/致命 或 L1~L4）")


_LEVEL_LABEL = {Level.L1: "提示", Level.L2: "一般", Level.L3: "严重", Level.L4: "致命"}
_LEVEL_ORDER = {Level.L1: 1, Level.L2: 2, Level.L3: 3, Level.L4: 4}


def max_level(*levels: Level) -> Level:
    """取最高等级，空输入返回 L1。"""
    valid = [lv for lv in levels if lv is not None]
    if not valid:
        return Level.L1
    return max(valid, key=lambda x: _LEVEL_ORDER[x])


class TriState(str, Enum):
    """三值逻辑。UNKNOWN 既不是 True 也不是 False。"""

    TRUE = "T"
    FALSE = "F"
    UNKNOWN = "U"


class ValueKind(str, Enum):
    """测点值域形态，决定哪些算法适配该代号（见 BaseAlgorithm.SUPPORTED_KINDS）。"""

    CONTINUOUS = "continuous"  # 连续物理量：温度、电压、位移、电流
    ENUM = "enum"              # 离散多态：模式字、状态码、锁定/失锁
    COUNTER = "counter"        # 递增计数：组数、次数、包计数
    BITFIELD = "bitfield"      # 状态字：32 位诊断标志、位域展开的 b0b7
    FLAG = "flag"              # 单比特布尔


class HitType(str, Enum):
    """命中类型。前端图标、根因聚类、报告分类都依赖它，禁止自由文本。"""

    THRESHOLD = "THRESHOLD"          # 越限/定值
    FROZEN = "FROZEN"                # 死值/冻结
    SPIKE = "SPIKE"                  # 跳变/野值
    NOISE = "NOISE"                  # 噪声膨胀/塌缩
    RATE = "RATE"                    # 速率/斜率
    COUNTER = "COUNTER"              # 计数异常/回绕
    BITFIELD = "BITFIELD"            # 状态字位异常
    ENUM_ILLEGAL = "ENUM_ILLEGAL"    # 非法状态/开路特征码
    DWELL = "DWELL"                  # 状态驻留超时
    JITTER = "JITTER"                # 抖动/振荡
    GAP = "GAP"                      # 数据缺口/中断
    FRAME = "FRAME"                  # 帧完整性
    CROSS_SECTION = "CROSS_SECTION"  # 横截面离群
    CORRELATION = "CORRELATION"      # 相关性劣化
    PAIRED_BIAS = "PAIRED_BIAS"      # 成对偏置/累计分离
    VOTING = "VOTING"                # 多证据融合
    SEGMENT_SHAPE = "SEGMENT_SHAPE"  # 段形/趋势一致性
    COMPOSITE = "COMPOSITE"          # 复合条件
    CMD_RELATION = "CMD_RELATION"    # 指令-测点关系
    CONFUSION = "CONFUSION"          # 交叉校验分歧


class RunStatus(str, Enum):
    """算法执行状态。除 OK / NO_ANOMALY 外，其余都不产生异常点。"""

    OK = "OK"                  # 执行完成且有异常点
    NO_ANOMALY = "NO_ANOMALY"  # 执行完成，无异常
    UNKNOWN = "UNKNOWN"        # 数据不足/基线不足/门控未知 → 输出「不知道」
    SKIP = "SKIP"              # 门控不满足/值域不适配 → 不判
    NO_DATA = "NO_DATA"        # 无数据
    ERROR = "ERROR"            # 参数或实现异常


# ===========================================================================
# 二、元数据（来自测点目录）
# ===========================================================================


@dataclass
class RangeSpec:
    """正常值范围中的一个命名区间，例如 "正常:[65,95]" / "失锁:[0,0]"。"""

    label: str
    low: float
    high: float
    is_normal: bool = True

    def contains(self, v: float, inclusive: bool = True) -> bool:
        if np.isnan(v):
            return False
        return self.low <= v <= self.high if inclusive else self.low < v < self.high


@dataclass
class TelemetryMeta:
    """单条测点代号的静态元数据。逐字段对应目录列，另加派生量。"""

    code: str
    name: str = ""
    system: str = ""
    bit_width: int = 0
    formula_type: int = 0
    formula: str = ""
    formula_params: str = ""
    decimals: int = 0
    alarm_enabled: bool = False
    normal_ranges: list[RangeSpec] = field(default_factory=list)
    warn_ranges: list[RangeSpec] = field(default_factory=list)
    state_transition: str = ""
    related_cmd: str = ""
    channel: str = ""
    # 派生：工程值最小分辨力（1 LSB 对应工程值）。取不到时为 None，
    # 算法内应回退为 span/(2**bit_width-1)，绝不允许用 0。
    quantum: float | None = None
    kind: ValueKind = ValueKind.CONTINUOUS
    unit: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def normal_span(self) -> float | None:
        if not self.normal_ranges:
            return None
        return max(r.high for r in self.normal_ranges) - min(r.low for r in self.normal_ranges)

    def primary_normal(self) -> RangeSpec | None:
        """优先取标签为「正常」的区间，否则取第一个。"""
        for r in self.normal_ranges:
            if r.label in ("正常", "normal", ""):
                return r
        return self.normal_ranges[0] if self.normal_ranges else None

    def enum_map(self) -> dict[int, str]:
        """离散量的取值→含义。仅当区间退化为单点（low == high）时视为枚举。"""
        out: dict[int, str] = {}
        for r in list(self.normal_ranges) + list(self.warn_ranges):
            if r.low == r.high and float(r.low).is_integer():
                out.setdefault(int(r.low), r.label)
        return out

    def lsb(self, fallback_span: float | None = None) -> float:
        """工程值 1 LSB。三级回退：quantum → 量程/编码空间 → 1.0。"""
        if self.quantum and self.quantum > 0:
            return float(self.quantum)
        span = fallback_span if fallback_span is not None else self.normal_span
        if span and self.bit_width and self.bit_width > 0:
            return float(span) / float(2 ** self.bit_width - 1)
        return 1.0


@dataclass
class TelemetrySeries:
    """单代号时序数据。构建后视为只读。"""

    code: str
    t: np.ndarray                       # int64, epoch 毫秒，严格升序
    v: np.ndarray                       # float64, 工程值，缺失为 NaN
    quality: np.ndarray | None = None   # uint8: 0 正常 / 1 缺失 / 2 无效（源码越界等）
    meta: TelemetryMeta | None = None
    packet_period_ms: float | None = None  # 标称包周期，用于折算时窗↔点数

    def __post_init__(self) -> None:
        self.t = np.asarray(self.t, dtype=np.int64).reshape(-1)
        self.v = np.asarray(self.v, dtype=np.float64).reshape(-1)
        if self.t.size != self.v.size:
            raise ValueError(f"{self.code}: 时间戳与数值长度不一致 ({self.t.size} vs {self.v.size})")
        if self.t.size > 1 and np.any(np.diff(self.t) < 0):
            order = np.argsort(self.t, kind="stable")
            self.t, self.v = self.t[order], self.v[order]
            if self.quality is not None:
                self.quality = np.asarray(self.quality)[order]
        if self.quality is None:
            self.quality = np.where(np.isnan(self.v), 1, 0).astype(np.uint8)
        else:
            self.quality = np.asarray(self.quality, dtype=np.uint8).reshape(-1)

    def __len__(self) -> int:
        return int(self.t.size)

    @property
    def valid_mask(self) -> np.ndarray:
        return (~np.isnan(self.v)) & (self.quality == 0)

    @property
    def first_ts(self) -> int | None:
        return int(self.t[0]) if self.t.size else None

    @property
    def last_ts(self) -> int | None:
        return int(self.t[-1]) if self.t.size else None

    def period_ms(self) -> float:
        if self.packet_period_ms:
            return float(self.packet_period_ms)
        d = np.diff(self.t)
        d = d[d > 0]
        return float(np.median(d)) if d.size else 1000.0

    def to_points(self, seconds: float) -> int:
        """秒 → 点数，至少 1。"""
        return max(1, int(round(float(seconds) * 1000.0 / self.period_ms())))

    def window(self, t0: int | None = None, t1: int | None = None) -> "TelemetrySeries":
        mask = np.ones(len(self), dtype=bool)
        if t0 is not None:
            mask &= self.t >= int(t0)
        if t1 is not None:
            mask &= self.t <= int(t1)
        return TelemetrySeries(
            code=self.code, t=self.t[mask], v=self.v[mask], quality=self.quality[mask],
            meta=self.meta, packet_period_ms=self.packet_period_ms,
        )

    def value_at(self, ts: int, method: str = "zoh") -> float:
        """零阶保持（或线性插值）取值。"""
        if not len(self):
            return float("nan")
        idx = int(np.searchsorted(self.t, int(ts), side="right")) - 1
        if idx < 0:
            return float("nan")
        if method == "zoh" or idx >= len(self) - 1:
            return float(self.v[idx])
        t0, t1 = self.t[idx], self.t[idx + 1]
        v0, v1 = self.v[idx], self.v[idx + 1]
        if t1 == t0 or np.isnan(v0) or np.isnan(v1):
            return float(v0)
        return float(v0 + (v1 - v0) * (ts - t0) / (t1 - t0))


# ===========================================================================
# 三、算法输入
# ===========================================================================


@dataclass
class GateResult:
    """门控求值结果。三值：成立 / 不成立 / 不知道。"""

    state: TriState = TriState.TRUE
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"state": self.state.value, "reasons": list(self.reasons)}


@dataclass
class AlgorithmContext:
    """算法唯一输入。由引擎组装，算法只读。"""

    series: TelemetrySeries
    meta: TelemetryMeta
    params: dict = field(default_factory=dict)
    # 同族/对端序列：横截面、成对、相关、交叉校验用
    peers: dict[str, TelemetrySeries] = field(default_factory=dict)
    # 基线（A-06 产出，或离线落库的按工况分段基线）
    baseline: dict = field(default_factory=dict)
    # 三值门控结果
    gates: GateResult = field(default_factory=GateResult)
    # 分析时段（测试窗口 / 指令窗口 / 流程窗口）
    window: tuple[int | None, int | None] = (None, None)
    # 透传上下文：指令记录、内置标志位、子检测结果等
    extra: dict = field(default_factory=dict)
    rule_id: str = ""
    scene_id: str = ""
    run_id: str = ""

    @property
    def code(self) -> str:
        return self.series.code

    def peer(self, code: str) -> TelemetrySeries | None:
        return self.peers.get(code)


# ===========================================================================
# 四、算法输出
# ===========================================================================


@dataclass
class AnomalyPoint:
    """统一异常数据点 —— 全系统唯一的异常表达。

    一条规则命中一次 → 一个点。区间型异常用 t_start/t_end 表示，瞬时异常两者相等；
    跨路/跨位/跨段异常用 ``dimension`` 定位（peer:TRM5002 / bit:5 / seg:12）。
    """

    # 定位
    telemetry_code: str
    t_start: int                       # epoch ms
    t_end: int                         # epoch ms（瞬时 = t_start）
    algorithm_id: str
    level: Level = Level.L2
    hit_type: HitType = HitType.THRESHOLD

    # 关联
    point_id: str = ""
    rule_id: str = ""
    scene_id: str = ""
    telemetry_name: str = ""
    system: str = ""
    algorithm_version: str = ""

    # 时序位置
    idx_start: int = -1
    idx_end: int = -1

    # 数值与判据
    value: float | None = None          # 触发值
    prev_value: float | None = None     # 前一有效值（跳变/计数用）
    expected: float | None = None       # 期望值/基线值
    low: float | None = None            # 判据下界
    high: float | None = None           # 判据上界
    deviation: float | None = None      # 偏离量
    threshold: float | None = None      # 实际生效阈值（不是配置值）
    unit: str = ""

    dimension: str = ""                 # 定位维度：bit:5 / peer:TRM5002 / seg:12
    strength: float | None = None       # 证据强度 0~1，用于融合排序（不是概率）
    sample_count: int = 0

    # 证据明细：算法私有，但键名须登记在 OUTPUT_FIELDS
    evidence: dict = field(default_factory=dict)

    # 运行态
    gate_state: TriState = TriState.TRUE
    suppressed: bool = False            # 被抑制后仍保留痕迹，不直接丢弃
    suppress_reason: str = ""
    unknown_reason: str = ""
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def __post_init__(self) -> None:
        self.level = Level.parse(self.level)
        if isinstance(self.hit_type, str):
            self.hit_type = HitType(self.hit_type)
        if isinstance(self.gate_state, str):
            self.gate_state = TriState(self.gate_state)
        if not self.point_id:
            self.point_id = uuid.uuid4().hex[:16]
        if self.t_end < self.t_start:
            self.t_end = self.t_start

    @property
    def duration_ms(self) -> int:
        return int(self.t_end - self.t_start)

    def dedup_key(self) -> str:
        """同规则、同维度、同命中类型且时间相邻的点视为同一事件。"""
        return (f"{self.rule_id}|{self.telemetry_code}|{self.algorithm_id}"
                f"|{self.hit_type.value}|{self.dimension}")

    def to_dict(self, iso: bool = True) -> dict:
        return {
            "point_id": self.point_id,
            "rule_id": self.rule_id,
            "scene_id": self.scene_id,
            "telemetry_code": self.telemetry_code,
            "telemetry_name": self.telemetry_name,
            "system": self.system,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "hit_type": self.hit_type.value,
            "level": self.level.value,
            "level_label": self.level.label,
            "t_start": int(self.t_start),
            "t_end": int(self.t_end),
            "ts_start": _iso(self.t_start) if iso else None,
            "ts_end": _iso(self.t_end) if iso else None,
            "duration_ms": self.duration_ms,
            "idx_start": self.idx_start,
            "idx_end": self.idx_end,
            "value": _num(self.value),
            "prev_value": _num(self.prev_value),
            "expected": _num(self.expected),
            "low": _num(self.low),
            "high": _num(self.high),
            "deviation": _num(self.deviation),
            "threshold": _num(self.threshold),
            "unit": self.unit,
            "dimension": self.dimension,
            "strength": _num(self.strength),
            "sample_count": self.sample_count,
            "gate_state": self.gate_state.value,
            "suppressed": self.suppressed,
            "suppress_reason": self.suppress_reason,
            "unknown_reason": self.unknown_reason,
            "evidence": _jsonable(self.evidence),
            "created_at": self.created_at,
        }


@dataclass
class Precheck:
    """前置检查结果。未通过时算法不得进入 detect。"""

    status: RunStatus = RunStatus.SKIP
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.status in (RunStatus.OK, RunStatus.NO_ANOMALY)


@dataclass
class AlgorithmResult:
    """算法唯一输出。"""

    algorithm_id: str
    telemetry_code: str
    status: RunStatus = RunStatus.NO_ANOMALY
    points: list[AnomalyPoint] = field(default_factory=list)
    reason: str = ""
    rule_id: str = ""
    scene_id: str = ""
    algorithm_version: str = ""
    params_used: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)   # 数据覆盖率、有效点数
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in (RunStatus.OK, RunStatus.NO_ANOMALY)

    @property
    def hit(self) -> bool:
        return bool(self.points)

    def to_dict(self) -> dict:
        return {
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "telemetry_code": self.telemetry_code,
            "rule_id": self.rule_id,
            "scene_id": self.scene_id,
            "status": self.status.value,
            "reason": self.reason,
            "hit_count": len(self.points),
            "max_level": max((p.level.value for p in self.points), default=None),
            "params_used": _jsonable(self.params_used),
            "features": _jsonable(self.features),
            "coverage": _jsonable(self.coverage),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "points": [p.to_dict() for p in self.points],
        }


# ===========================================================================
# 五、参数规格：前端表单 / LLM 填参 / 参数文档三处共用同一份定义
# ===========================================================================


PARAM_TYPES = {
    "int", "float", "bool", "str", "enum",
    "list[int]", "list[float]", "list[str]",
    "dict", "list[dict]", "code", "code_list", "seconds", "ms",
}


@dataclass
class ParamSpec:
    """一个可配置参数。默认值即「推荐值」，界面不应要求用户从空白开始填。"""

    name: str
    label: str
    type: str = "float"
    default: Any = None
    unit: str = ""
    choices: list | None = None
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False
    help: str = ""
    advanced: bool = False
    # 条件显示：{"mode": ["gt", "ge"]} 表示仅当 mode 取这些值时才展示
    depends_on: dict | None = None
    # 该参数不填时的自动推导来源，如 "meta.primary_normal.low"
    auto_from: str = ""

    def __post_init__(self) -> None:
        if self.type not in PARAM_TYPES:
            raise ValueError(f"{self.name}: 未支持的参数类型 {self.type}")
        if self.type == "enum" and not self.choices:
            raise ValueError(f"{self.name}: enum 类型必须提供 choices")

    def to_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "type": self.type,
            "default": self.default, "unit": self.unit, "choices": self.choices,
            "minimum": self.minimum, "maximum": self.maximum,
            "required": self.required, "help": self.help, "advanced": self.advanced,
            "depends_on": self.depends_on, "auto_from": self.auto_from,
        }


class ParamError(ValueError):
    """参数校验失败。信息直接展示给配置页面，必须带参数名与合法范围。"""


class Params(dict):
    """支持属性访问的参数字典：p.window 等价于 p["window"]。"""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - 防御式
            raise AttributeError(f"参数 {item!r} 不存在") from exc

    def get_float(self, key: str, default: float = float("nan")) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default


_COERCE = {
    "int": int,
    "float": float,
    "seconds": float,
    "ms": float,
    "bool": lambda v: v if isinstance(v, bool)
    else str(v).strip().lower() in ("1", "true", "yes", "y", "是", "开"),
    "str": lambda v: str(v),
    "enum": str,
    "code": str,
}


def validate_params(specs: Sequence[ParamSpec], given: dict | None) -> Params:
    """按规格补默认值、做类型转换与边界检查，返回参数副本。"""
    given = dict(given or {})
    out = Params()

    known = {s.name for s in specs}
    unknown = set(given) - known
    if unknown:
        raise ParamError(f"未知参数: {sorted(unknown)}；该算法可用参数: {sorted(known)}")

    for spec in specs:
        if given.get(spec.name) is not None:
            raw = given[spec.name]
        else:
            if spec.required and spec.default is None:
                raise ParamError(f"缺少必填参数: {spec.name}（{spec.label}）")
            raw = spec.default

        if raw is None:
            out[spec.name] = None
            continue
        try:
            if spec.type in ("list[int]", "list[float]"):
                cast = int if spec.type == "list[int]" else float
                val = [cast(x) for x in (raw if isinstance(raw, (list, tuple, set)) else [raw])]
            elif spec.type in ("list[str]", "code_list"):
                val = [str(x) for x in (raw if isinstance(raw, (list, tuple, set)) else [raw])]
            elif spec.type == "dict":
                val = dict(raw)
            elif spec.type == "list[dict]":
                items = raw if isinstance(raw, (list, tuple)) else [raw]
                val = [dict(x) for x in items]
            else:
                val = _COERCE[spec.type](raw)
        except (TypeError, ValueError) as exc:
            raise ParamError(f"参数 {spec.name}（{spec.label}）取值非法: {raw!r} → {exc}") from exc

        if spec.type == "enum" and val not in spec.choices:
            raise ParamError(f"参数 {spec.name} 必须是 {spec.choices} 之一，收到 {val!r}")
        if spec.minimum is not None and isinstance(val, (int, float)) and val < spec.minimum:
            raise ParamError(f"参数 {spec.name}={val} 小于下限 {spec.minimum}{spec.unit}")
        if spec.maximum is not None and isinstance(val, (int, float)) and val > spec.maximum:
            raise ParamError(f"参数 {spec.name}={val} 超过上限 {spec.maximum}{spec.unit}")

        out[spec.name] = val
    return out


# 所有检测器共有的参数（表单最底部的高级区）
COMMON_SPECS: list[ParamSpec] = [
    ParamSpec("level", "告警等级", "enum", "一般",
              choices=["提示", "一般", "严重", "致命"], help="命中后输出的等级；规则层可再升级"),
    ParamSpec("min_samples", "最少有效样本", "int", 10, "点", minimum=1,
              help="有效点少于此值 → 输出 UNKNOWN（不知道），不是正常", advanced=True),
    ParamSpec("max_points", "异常点上限", "int", 2000, "个", minimum=1,
              help="超过则按等级与强度截断，防止配置错误导致刷屏", advanced=True),
]


# ===========================================================================
# 六、基类：模板方法 run() 不可覆写，子类只实现 detect()
# ===========================================================================


class BaseAlgorithm(abc.ABC):
    """所有异常检测算法的父类。

    子类声明：ALGO_ID / ALGO_NAME / LAYER / CATEGORY / HIT_TYPE /
              SUPPORTED_KINDS / PARAM_SPECS / OUTPUT_FIELDS / RATIONALE / USE_CASES
    子类实现：detect(ctx, p) -> list[AnomalyPoint]
    """

    ALGO_ID: str = ""
    ALGO_NAME: str = ""
    ALGO_NAME_EN: str = ""
    VERSION: str = "1.0.0"
    LAYER: str = "L1"                  # L1 单点 / L2 关系 / L3 基础设施
    CATEGORY: str = ""
    HIT_TYPE: HitType = HitType.THRESHOLD
    SUPPORTED_KINDS: tuple[ValueKind, ...] = tuple(ValueKind)
    REQUIRES_PEERS: bool = False
    REQUIRES_BASELINE: bool = False
    IS_DETECTOR: bool = True           # False = 基础设施（不成规则、不出异常点）
    PARAM_SPECS: list[ParamSpec] = []
    OUTPUT_FIELDS: list[tuple[str, str, str]] = []   # (字段, 类型, 含义)
    RATIONALE: str = ""                # 为什么需要它
    USE_CASES: str = ""                # 典型判据映射

    def __init__(self, params: dict | None = None) -> None:
        # 允许 build("A-01", params) 把参数挂在实例上；ctx.params 为空时回退到这里，
        # 避免「校验用这套、执行用另一套」的静默错误。
        self.default_params: dict = dict(params or {})

    # -- 规格 -------------------------------------------------------------
    @classmethod
    def param_specs(cls, include_common: bool = True) -> list[ParamSpec]:
        return list(cls.PARAM_SPECS) + (COMMON_SPECS if include_common else [])

    @classmethod
    def to_spec(cls) -> dict:
        return {
            "algorithm_id": cls.ALGO_ID,
            "name": cls.ALGO_NAME,
            "name_en": cls.ALGO_NAME_EN,
            "version": cls.VERSION,
            "layer": cls.LAYER,
            "category": cls.CATEGORY,
            "hit_type": cls.HIT_TYPE.value,
            "is_detector": cls.IS_DETECTOR,
            "supported_kinds": [k.value for k in cls.SUPPORTED_KINDS],
            "requires_peers": cls.REQUIRES_PEERS,
            "requires_baseline": cls.REQUIRES_BASELINE,
            "rationale": cls.RATIONALE,
            "use_cases": cls.USE_CASES,
            "params": [p.to_dict() for p in cls.param_specs()],
            "output_fields": [dict(zip(("name", "type", "desc"), f)) for f in cls.OUTPUT_FIELDS],
        }

    # -- 模板方法：禁止子类覆写 -------------------------------------------
    def run(self, ctx: AlgorithmContext) -> AlgorithmResult:
        t0 = time.perf_counter()
        res = AlgorithmResult(
            algorithm_id=self.ALGO_ID,
            algorithm_version=self.VERSION,
            telemetry_code=ctx.series.code,
            rule_id=ctx.rule_id,
            scene_id=ctx.scene_id,
        )
        try:
            p = validate_params(self.param_specs(),
                                ctx.params if ctx.params else self.default_params)
            res.params_used = dict(p)
            pre = self.precheck(ctx, p)
            res.coverage = self.coverage(ctx)
            if not pre.passed:
                res.status, res.reason = pre.status, pre.reason
                return res

            points = self._normalize(self.detect(ctx, p) or [], ctx, p)
            res.points = points
            res.status = RunStatus.OK if points else RunStatus.NO_ANOMALY
        except ParamError as exc:
            res.status, res.reason = RunStatus.ERROR, f"参数错误: {exc}"
        except Exception as exc:  # noqa: BLE001 - 单算法异常不能拖垮整批检测
            res.status, res.reason = RunStatus.ERROR, f"{type(exc).__name__}: {exc}"
        finally:
            res.elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return res

    # -- 前置检查 ---------------------------------------------------------
    def precheck(self, ctx: AlgorithmContext, p: Params) -> Precheck:
        if not self.IS_DETECTOR:
            # 基础设施（基线/门控/状态机/对齐/自校准/段切分）由引擎调用各自的 compute/run_infra，
            # 不参与规则检测。若被误配进规则，这里直接挡掉，而不是让它跑出半成品结果。
            return Precheck(RunStatus.SKIP, "基础设施算法：由引擎调用，不参与规则检测")
        s = ctx.series
        if len(s) == 0:
            return Precheck(RunStatus.NO_DATA, "分析时段内无数据")
        if ctx.gates.state is TriState.FALSE:
            return Precheck(RunStatus.SKIP, "门控不满足: " + "；".join(ctx.gates.reasons or ["未说明"]))
        if ctx.gates.state is TriState.UNKNOWN:
            return Precheck(RunStatus.UNKNOWN,
                            "门控未知: " + "；".join(ctx.gates.reasons or ["门控测点缺数"]))
        kind = ctx.meta.kind
        if kind not in self.SUPPORTED_KINDS:
            return Precheck(RunStatus.SKIP,
                            f"值域类型 {kind.value} 不适用于本算法"
                            f"（支持 {[k.value for k in self.SUPPORTED_KINDS]}）")
        n_valid = int(np.count_nonzero(s.valid_mask))
        if n_valid < int(p.min_samples):
            return Precheck(RunStatus.UNKNOWN, f"有效样本 {n_valid} < 下限 {p.min_samples}")
        if self.REQUIRES_PEERS and not ctx.peers:
            return Precheck(RunStatus.SKIP, "本算法需要同族/对端序列，但未提供 peers")
        return Precheck(RunStatus.OK, "")

    def coverage(self, ctx: AlgorithmContext) -> dict:
        s = ctx.series
        if not len(s):
            return {"points": 0}
        valid = int(np.count_nonzero(s.valid_mask))
        span = max(int(s.t[-1] - s.t[0]), 1)
        expected = max(1, int(round(span / s.period_ms())) + 1)
        return {
            "points": len(s),
            "valid": valid,
            "missing": len(s) - valid,
            "ratio": round(valid / expected, 4),
            "t_from": int(s.t[0]),
            "t_to": int(s.t[-1]),
        }

    # -- 子类实现 ---------------------------------------------------------
    @abc.abstractmethod
    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        """核心检测。只允许返回异常点，不做 IO、不打印。"""

    # -- 输出归一化：公共字段由基类统一盖章，算法不得自己编 -----------------
    def _normalize(self, points: list[AnomalyPoint], ctx: AlgorithmContext,
                   p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        t0, t1 = ctx.window
        out: list[AnomalyPoint] = []
        for pt in points:
            if not isinstance(pt, AnomalyPoint):  # pragma: no cover
                raise TypeError(f"{self.ALGO_ID}: detect 必须返回 AnomalyPoint，收到 {type(pt)}")
            pt.algorithm_id = self.ALGO_ID
            pt.algorithm_version = self.VERSION
            pt.telemetry_code = s.code
            pt.telemetry_name = pt.telemetry_name or ctx.meta.name
            pt.system = ctx.meta.system
            pt.unit = pt.unit or ctx.meta.unit
            pt.rule_id = ctx.rule_id
            pt.scene_id = ctx.scene_id
            pt.gate_state = ctx.gates.state
            if pt.idx_start < 0 or pt.idx_end < 0:
                pt.idx_start = _index_at(s, pt.t_start)
                pt.idx_end = _index_at(s, pt.t_end)
            if pt.sample_count == 0:
                pt.sample_count = max(1, pt.idx_end - pt.idx_start + 1)
            # 只保留落在分析窗内的点
            if t0 is not None and pt.t_end < t0:
                continue
            if t1 is not None and pt.t_start > t1:
                continue
            out.append(pt)

        out.sort(key=lambda x: (x.t_start, x.dimension))
        cap = int(p.max_points or 0)
        if cap and len(out) > cap:
            out.sort(key=lambda x: (-_LEVEL_ORDER[x.level], -(x.strength or 0), x.t_start))
            out = out[:cap]
        return out


# ===========================================================================
# 七、注册表：发现、实例化、导出规格（给前端表单 / LLM / 文档）
# ===========================================================================

_REGISTRY: dict[str, type[BaseAlgorithm]] = {}


def register(cls: type[BaseAlgorithm]) -> type[BaseAlgorithm]:
    """类装饰器：把算法登记进注册表。ID 重复立即报错，不允许静默覆盖。"""
    if not cls.ALGO_ID:
        raise ValueError(f"{cls.__name__} 缺少 ALGO_ID")
    if cls.ALGO_ID in _REGISTRY:
        raise ValueError(f"算法 ID 重复: {cls.ALGO_ID}"
                         f"（{cls.__name__} 与 {_REGISTRY[cls.ALGO_ID].__name__}）")
    _REGISTRY[cls.ALGO_ID] = cls
    return cls


def all_algorithms() -> list[type[BaseAlgorithm]]:
    return sorted(_REGISTRY.values(), key=lambda c: c.ALGO_ID)


def get(algorithm_id: str) -> type[BaseAlgorithm]:
    try:
        return _REGISTRY[algorithm_id]
    except KeyError:
        raise KeyError(f"未注册的算法: {algorithm_id}；已注册: {sorted(_REGISTRY)}") from None


def build(algorithm_id: str, params: dict | None = None) -> BaseAlgorithm:
    """实例化并做一次参数校验（失败立即抛 ParamError，不等到运行期）。"""
    cls = get(algorithm_id)
    validate_params(cls.param_specs(), params or {})
    return cls(params)


def for_kind(kind: ValueKind | str, detectors_only: bool = True) -> list[str]:
    """按测点值域类型推荐可用算法 —— 规则配置页的「可选算法」列表靠它。"""
    kind = ValueKind(kind) if isinstance(kind, str) else kind
    return [c.ALGO_ID for c in all_algorithms()
            if (c.IS_DETECTOR or not detectors_only) and kind in c.SUPPORTED_KINDS]


def catalog() -> list[dict]:
    return [cls.to_spec() for cls in all_algorithms()]


_JSON_TYPE = {
    "int": "integer", "float": "number", "seconds": "number", "ms": "number",
    "bool": "boolean", "str": "string", "enum": "string", "code": "string",
    "list[int]": "array", "list[float]": "array", "list[str]": "array",
    "code_list": "array", "dict": "object", "list[dict]": "array",
}


def json_schema(algorithm_id: str) -> dict:
    """给 LLM / 前端校验用的 JSON Schema（仅参数部分）。"""
    cls = get(algorithm_id)
    props, required = {}, []
    for p in cls.param_specs():
        jtype = _JSON_TYPE[p.type]
        item = {"type": jtype, "title": p.label, "description": p.help}
        if p.choices:
            item["enum"] = p.choices
        if p.minimum is not None:
            item["minimum"] = p.minimum
        if p.maximum is not None:
            item["maximum"] = p.maximum
        if p.default is not None:
            item["default"] = p.default
        if jtype == "array":
            if p.type == "list[dict]":
                item["items"] = {"type": "object"}
            else:
                item["items"] = {"type": "integer" if "int" in p.type
                                 else "number" if "float" in p.type else "string"}
        props[p.name] = item
        if p.required:
            required.append(p.name)
    return {
        "title": f"{cls.ALGO_ID} {cls.ALGO_NAME}",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


# ===========================================================================
# 八、数值工具（纯函数，不持有状态）
# ===========================================================================


def median(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    return float(np.median(x)) if x.size else float("nan")


def mad(x: np.ndarray) -> float:
    """中位数绝对偏差（未乘 1.4826）。空输入返回 nan。"""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if x.size == 0:
        return float("nan")
    return float(np.median(np.abs(x - np.median(x))))


def robust_sigma(x: np.ndarray) -> float:
    """MAD 一致性估计 σ = 1.4826 · MAD。"""
    m = mad(x)
    return float("nan") if np.isnan(m) else 1.4826 * m


def nanquantile(x: np.ndarray, q: float) -> float:
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    return float(np.quantile(x, q)) if x.size else float("nan")


def nanstd(x: np.ndarray, ddof: int = 1) -> float:
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    return float(np.std(x, ddof=ddof)) if x.size > ddof else float("nan")


def sliding(x: np.ndarray, w: int) -> np.ndarray:
    """返回形状 (n-w+1, w) 的滑窗视图（零拷贝）。样本不足时返回空数组。"""
    x = np.asarray(x, dtype=float)
    if w <= 0 or x.size < w:
        return np.empty((0, max(w, 0)), dtype=float)
    return np.lib.stride_tricks.sliding_window_view(x, w)


def rolling_median(x: np.ndarray, w: int) -> np.ndarray:
    """居中滑窗中位数，长度与输入对齐，边界用可用样本计算。"""
    x = np.asarray(x, dtype=float)
    n, out = x.size, np.full(x.size, np.nan)
    if n == 0 or w <= 0:
        return out
    half = w // 2
    for i in range(n):
        seg = x[max(0, i - half): min(n, i + half + 1)]
        seg = seg[~np.isnan(seg)]
        if seg.size:
            out[i] = np.median(seg)
    return out


def rolling_std(x: np.ndarray, w: int) -> np.ndarray:
    """滑窗标准差（尾部对齐：out[i] 用 x[i-w+1..i]），前 w-1 个为 nan。"""
    x = np.asarray(x, dtype=float)
    out = np.full(x.size, np.nan)
    if x.size < w or w < 2:
        return out
    sw = np.lib.stride_tricks.sliding_window_view(x, w)
    with np.errstate(invalid="ignore"):
        out[w - 1:] = np.nanstd(sw, axis=1, ddof=1)
    return out


def smooth3(x: np.ndarray) -> np.ndarray:
    """3 点中值平滑（A-03/A-20 要求：先平滑再微分，否则量化噪声被放大）。"""
    x = np.asarray(x, dtype=float)
    if x.size < 3:
        return x.copy()
    out = x.copy()
    out[1:-1] = np.nanmedian(np.vstack([x[:-2], x[1:-1], x[2:]]), axis=0)
    return out


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """布尔序列中连续 True 的区间，返回 [(start, end_inclusive), ...]。"""
    m = np.asarray(mask, dtype=bool)
    if m.size == 0:
        return []
    d = np.diff(np.concatenate([[False], m, [False]]).astype(np.int8))
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0] - 1
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def confirm(mask: np.ndarray, k: int, allow_unknown: bool = True) -> np.ndarray:
    """连续 k 个 True 的末点标记为确认（越限去抖的标准写法）。

    allow_unknown=False 时，窗内出现 NaN 的段一律不确认（缺数据 ≠ 恢复正常）。
    """
    m = np.asarray(mask, dtype=float)
    out = np.zeros(m.size, dtype=bool)
    if m.size == 0 or k <= 0:
        return out
    sw = np.lib.stride_tricks.sliding_window_view(m, k)
    hit = np.all(sw == 1, axis=1)
    if not allow_unknown:
        hit &= ~np.any(np.isnan(sw), axis=1)
    out[k - 1:] = hit
    return out


def counter_delta(n: np.ndarray, bits: int) -> np.ndarray:
    """模回绕差分。返回长度 n-1，d = (N[k]-N[k-1]) mod 2^bits。"""
    n = np.asarray(n, dtype=float)
    if n.size < 2:
        return np.empty(0)
    return np.diff(n) % (1 << int(bits))


def pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    """皮尔逊相关。任一序列为常量时返回 None（未定义 ≠ 0）。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = ~(np.isnan(x) | np.isnan(y))
    x, y = x[m], y[m]
    if x.size < 3:
        return None
    sx, sy = np.std(x), np.std(y)
    if sx <= EPS or sy <= EPS:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def phase_resample(t: np.ndarray, v: np.ndarray, n: int = 101) -> np.ndarray:
    """按归一化时间相位重采样到 n 点。丢包导致采样不均，必须按时间不能按索引。"""
    t = np.asarray(t, dtype=float)
    v = np.asarray(v, dtype=float)
    if t.size < 2:
        return np.full(n, np.nan)
    x = (t - t[0]) / max(t[-1] - t[0], EPS)
    return np.interp(np.linspace(0.0, 1.0, n), x, v)


def dtw_distance(a: np.ndarray, b: np.ndarray) -> float:
    """经典 DTW（O(L²)），返回按段长归一化的距离。仅用于可疑段，不做全量。"""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.size == 0 or b.size == 0:
        return float("nan")

    def norm(z: np.ndarray) -> np.ndarray:
        lo, hi = np.nanmin(z), np.nanmax(z)
        return (z - lo) / (hi - lo) if hi > lo else np.zeros_like(z)

    a, b = norm(a), norm(b)
    la, lb = a.size, b.size
    cost = np.full((la + 1, lb + 1), np.inf)
    cost[0, 0] = 0.0
    for i in range(1, la + 1):
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost[i, j] = abs(ai - b[j - 1]) + min(cost[i - 1, j], cost[i, j - 1], cost[i - 1, j - 1])
    return float(cost[la, lb] / max(la, lb))


# ===========================================================================
# 九、便捷构造与内部小工具
# ===========================================================================


def point(ctx: AlgorithmContext, level: Level | str, hit_type: HitType | str,
          t_start: int, t_end: int | None = None, **kw: Any) -> AnomalyPoint:
    """算法内构造异常点的唯一入口，自动挂上 unit 与测点代号。"""
    return AnomalyPoint(
        telemetry_code=ctx.series.code,
        t_start=int(t_start),
        t_end=int(t_end if t_end is not None else t_start),
        algorithm_id="",
        level=Level.parse(level),
        hit_type=HitType(hit_type),
        unit=kw.pop("unit", ctx.meta.unit),
        **kw,
    )


def _index_at(s: TelemetrySeries, ts: int) -> int:
    if not len(s):
        return -1
    return min(int(np.searchsorted(s.t, int(ts), side="left")), len(s) - 1)


def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if (np.isnan(f) or np.isinf(f)) else round(f, 6)


def _iso(ts_ms: int) -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S.%f")[:-3]


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        f = float(obj)
        return None if (np.isnan(f) or np.isinf(f)) else round(f, 6)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    return obj
