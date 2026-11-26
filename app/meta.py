"""测点元数据目录（数据源：``config/telemetry.yaml``）。

算法上下文的 TelemetryMeta 全部由这里构造。目录里的代号按台账填写；
未登记的代号走 ``guess_by_prefix`` → ``unknown_default`` 兜底，
保证算法永远拿得到可用的值域与量纲，不会因为"没建目录"而跑不起来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from algorithm.base import RangeSpec, TelemetryMeta, ValueKind

from . import yamls

_CAT = yamls.file("telemetry.yaml")


@dataclass
class TelemetryDef:
    code: str
    name: str
    unit: str = ""
    kind: str = "continuous"
    low: float = 0.0
    high: float = 100.0
    bit_width: int = 16
    quantum: float = 0.0
    decimals: int = 3
    system: str = ""
    note: str = ""
    enum: list[tuple[int, str]] = field(default_factory=list)


def _to_def(row: dict) -> TelemetryDef:
    enum = [(int(v), str(lab)) for v, lab in (row.get("enum") or [])]
    return TelemetryDef(
        code=str(row["code"]),
        name=str(row.get("name") or row["code"]),
        unit=str(row.get("unit") or ""),
        kind=str(row.get("kind") or "continuous"),
        low=float(row.get("low", 0.0)),
        high=float(row.get("high", 100.0)),
        bit_width=int(row.get("bit_width", 16)),
        quantum=float(row.get("quantum", 0.0)),
        decimals=int(row.get("decimals", 3)),
        system=str(row.get("system") or ""),
        note=str(row.get("note") or ""),
        enum=enum,
    )


def _build_catalog() -> tuple[list[TelemetryDef], dict[str, TelemetryDef]]:
    rows = _CAT.get("items") or []
    if not isinstance(rows, list):
        raise yamls.ConfigError("telemetry.yaml: items 必须是列表")
    defs: list[TelemetryDef] = []
    index: dict[str, TelemetryDef] = {}
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not row.get("code"):
            raise yamls.ConfigError(f"telemetry.yaml: 第 {i} 项缺少 code")
        d = _to_def(row)
        if d.code in index:
            raise yamls.ConfigError(f"telemetry.yaml: 代号重复 {d.code}")
        if d.kind not in {k.value for k in ValueKind}:
            raise yamls.ConfigError(
                f"telemetry.yaml: {d.code} 的 kind={d.kind} 非法，"
                f"可选 {[k.value for k in ValueKind]}")
        defs.append(d)
        index[d.code] = d
    return defs, index


_L, CATALOG = _build_catalog()

_GUESS_PREFIX: dict[str, str] = dict(_CAT.get("guess_by_prefix") or {})
_UNKNOWN: dict = dict(_CAT.get("unknown_default") or {})


def guess_def(code: str) -> TelemetryDef:
    """未知代号的合理默认：按代号前缀估计子系统，值域取 unknown_default。"""
    prefix = code[:3].upper() if code else "TM?"
    system = _GUESS_PREFIX.get(prefix, "")
    suffix = str(_UNKNOWN.get("name_suffix", "（未登记测点）"))
    return TelemetryDef(
        code=code,
        name=f"{code}{suffix}",
        unit=str(_UNKNOWN.get("unit", "")),
        kind=str(_UNKNOWN.get("kind", "continuous")),
        low=float(_UNKNOWN.get("low", 0.0)),
        high=float(_UNKNOWN.get("high", 100.0)),
        bit_width=int(_UNKNOWN.get("bit_width", 16)),
        quantum=float(_UNKNOWN.get("quantum", 0.0)),
        decimals=int(_UNKNOWN.get("decimals", 2)),
        system=system,
        note=str(_UNKNOWN.get("note", "")),
    )


def get_def(code: str) -> TelemetryDef:
    d = CATALOG.get(code)
    return d if d is not None else guess_def(code)


@lru_cache(maxsize=512)
def _guess_cached(code: str) -> TelemetryDef:
    return guess_def(code)


def build_meta(code: str, *, kind: str | None = None, low: float | None = None,
               high: float | None = None, unit: str | None = None,
               name: str | None = None, bit_width: int | None = None,
               quantum: float | None = None, decimals: int | None = None,
               enum: list[tuple[int, str]] | None = None) -> TelemetryMeta:
    """把规则里的元数据描述（可能被用户覆盖）装配成算法需要的 TelemetryMeta。"""
    base = get_def(code)
    kind = kind or base.kind
    low = base.low if low is None else float(low)
    high = base.high if high is None else float(high)
    unit = base.unit if unit is None else unit
    bw = int(bit_width or base.bit_width)
    quantum = float(base.quantum if quantum is None else quantum)

    ranges: list[RangeSpec] = []
    if kind in ("enum", "flag", "bitfield") and enum:
        ranges = [RangeSpec(label=lab, low=float(val), high=float(val)) for val, lab in enum]
    elif kind in ("enum", "flag") and low == high:
        ranges = [RangeSpec(label="正常", low=low, high=high)]
    else:
        ranges = [RangeSpec(label="正常", low=min(low, high), high=max(low, high))]

    return TelemetryMeta(
        code=code,
        name=name or base.name,
        system=base.system,
        bit_width=bw,
        decimals=int(base.decimals if decimals is None else decimals),
        alarm_enabled=True,
        normal_ranges=ranges,
        quantum=quantum if quantum > 0 else None,
        kind=ValueKind(kind),
        unit=unit,
        raw={"def": base.__dict__},
    )


def catalog_rows() -> list[dict]:
    return [
        {"code": d.code, "name": d.name, "unit": d.unit, "kind": d.kind,
         "range": f"[{d.low:g}, {d.high:g}]", "system": d.system, "note": d.note}
        for d in _L
    ]


def codes() -> list[str]:
    return [d.code for d in _L]


def systems() -> list[str]:
    seen: list[str] = []
    for d in _L:
        if d.system and d.system not in seen:
            seen.append(d.system)
    return seen
