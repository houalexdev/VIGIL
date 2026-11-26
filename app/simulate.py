"""模拟数据生成器。

目标：给定一条规则（测点代号 + 若干算法及其参数），生成一段既能被这些算法判出异常、
又不会离谱到全窗口都是异常的时间序列，并保证每条算法至少命中 ``min_hits`` 处。

实现策略
--------
1. 按值域形态生成一条"干净"基线（趋势 + 量化噪声分离保存，噪声注入时要用）。
2. 把可用时间轴切成若干"车道"，每个算法独占一条车道，在车道内等距布点注入异常，
   避免不同算法的异常互相污染（例如冻结窗落在跳变窗里）。
3. 注入后立刻用**真实算法**跑一遍，命中不足 min_hits 就加大剂量/增加窗口重来
   （最多 4 轮）。这是唯一能保证"真的被判出来"的做法，靠猜阈值不可靠。

单位与时间约定与算法框架一致：t 为 int64 毫秒，v 为工程值浮点，缺失用 NaN。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

import algorithm as alg
from algorithm.base import RunStatus, TelemetrySeries

from .config import (MIN_POINTS, NEEDS_EXTERNAL, SIM_LANE, SIM_MAX_ATTEMPTS,
                     SIM_MAX_POINTS, SIM_MAX_PERIOD_S, SIM_MIN_HITS, SIM_MIN_PERIOD_S)
from .meta import build_meta

# 以下数值全部来自 config/app.yaml → simulate，这里只是本地别名
MAX_POINTS = SIM_MAX_POINTS
MIN_PERIOD_S = SIM_MIN_PERIOD_S
MAX_PERIOD_S = SIM_MAX_PERIOD_S      # 必须小于 A-03 的 max_gap_s(10s)，否则跳变算法自己会跳过
LANE_LO, LANE_HI = SIM_LANE          # 异常注入车道范围：留出开头给基线/参照段
MAX_ATTEMPTS = SIM_MAX_ATTEMPTS
DEFAULT_MIN_HITS = SIM_MIN_HITS
__all__ = ["NEEDS_EXTERNAL"]


@dataclass
class SimCtx:
    meta: Any
    kind: str
    n: int
    period_s: float
    low: float
    high: float
    lsb: float
    t: np.ndarray
    trend: np.ndarray
    noise_sd: float
    rng: np.random.Generator
    peers: dict[str, np.ndarray] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    min_hits: int = 5          # 本轮的命中目标，注入器可据此决定要布几处异常

    @property
    def span(self) -> float:
        return max(self.high - self.low, self.lsb * 10, 1e-6)


@dataclass
class SimResult:
    series: TelemetrySeries
    peers: dict[str, TelemetrySeries]
    hits: dict[str, int]
    attempts: int
    notes: list[str]
    period_s: float
    status: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------
def _base_curve(S: SimCtx, algo_ids: list[str], params_list: list[dict]) -> np.ndarray:
    n, rng = S.n, S.rng
    i = np.arange(n, dtype=float)
    if S.kind == "continuous":
        p1, p2 = n / 3.2, n / 1.7
        trend = (S.low + S.high) / 2 + 0.16 * S.span * np.sin(2 * np.pi * i / p1) \
            + 0.06 * S.span * np.sin(2 * np.pi * i / p2 + 1.3)
        return trend

    if S.kind == "counter":
        bits = 16
        step_mode = False
        for aid, p in zip(algo_ids, params_list):
            if aid == "A-10":
                bits = int(p.get("bits") or 16)
                if p.get("mode") in ("no_increase", "delta_ne", "any_change"):
                    step_mode = True
                elif p.get("mode") == "delta_gt" and int(p.get("max_delta") or 0) >= 1:
                    step_mode = True
        mod = 1 << bits
        start = (1 << max(bits - 3, 1)) if step_mode else 0
        base = start + (np.floor(i).astype(np.int64) if step_mode else np.zeros(n, dtype=np.int64))
        return (base % mod).astype(float)

    if S.kind == "bitfield":
        width, fault = 8, 1
        for aid, p in zip(algo_ids, params_list):
            if aid == "A-11":
                width = int(p.get("bit_width") or 8)
                fault = int(p.get("fault_value") if p.get("fault_value") is not None else 1)
                if p.get("mode") == "equals":
                    fault = int(p.get("expect_value") or 0)
        if fault == 0:
            return np.full(n, float((1 << width) - 1))
        return np.zeros(n, dtype=float)

    if S.kind == "enum":
        val = S.low if S.low == S.high else float(S.meta.normal_ranges[0].low if S.meta.normal_ranges else 0)
        for aid, p in zip(algo_ids, params_list):
            if aid == "A-01" and p.get("mode") in ("eq", "ne") and p.get("target_value") is not None:
                tv = float(p["target_value"])
                if p.get("mode") == "ne":
                    val = tv
            if aid == "A-24" and p.get("target_values"):
                tv = p["target_values"]
                val = float(tv[0]) if isinstance(tv, (list, tuple)) and tv else float(tv)
        return np.full(n, val)

    # flag
    return np.zeros(n, dtype=float)


def _noise_sd(S: SimCtx) -> float:
    if S.kind == "continuous":
        return max(2.0 * S.lsb, 0.0008 * S.span)
    if S.kind == "counter":
        return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# 车道与窗口
# ---------------------------------------------------------------------------
def _lanes(n: int, m: int) -> list[tuple[int, int]]:
    lo, hi = int(n * LANE_LO), int(n * LANE_HI)
    span = max(hi - lo, m * 40)
    out = []
    for j in range(m):
        a = lo + int(span * j / m)
        b = lo + int(span * (j + 1) / m)
        out.append((a, max(b, a + 40)))
    return out


def _windows(lane: tuple[int, int], count: int, wlen: int) -> list[tuple[int, int]]:
    a, b = lane
    length = b - a
    wlen = min(max(wlen, 6), max(length // 2, 6))
    count = max(1, min(count, length // (wlen + 4)))
    gap = max(3, (length - count * wlen) // (count + 1))
    wins = []
    cur = a + gap
    for _ in range(count):
        if cur + wlen > b:
            break
        wins.append((cur, cur + wlen))
        cur += wlen + gap
    return wins


def _default_wlen(aid: str, p: dict, S: SimCtx) -> int:
    if aid == "A-02":
        return max(int(p.get("n_dead") or 64) + 12, 40)
    if aid == "A-04":
        return max(int(p.get("window") or 60) * 2, 60)
    if aid == "A-07":
        w = int(round(float(p.get("window_s") or 1800) / max(S.period_s, 1e-6)))
        return min(max(w + 40, 60), max(S.n // 6, 60))
    if aid == "A-09":
        return max(int(round(float(p.get("cusum_window_s") or 60) / max(S.period_s, 1e-6))) * 3, 45)
    if aid == "A-22":
        return max(int(round(float(p.get("window_s") or 60) / max(S.period_s, 1e-6))) * 2, 40)
    if aid == "A-24":
        mad = p.get("max_dwell_s")
        if mad:
            return int(round(float(mad) / max(S.period_s, 1e-6))) * 3
        return int(p.get("max_dwell_periods") or 2) * 3 + 6
    if aid == "A-27":
        return max(int(round(4 * S.period_s / S.period_s)) + 3, 8)
    return 24


# ---------------------------------------------------------------------------
# 注入器
# ---------------------------------------------------------------------------
def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _inj_a01(v, wins, p, S):
    mode = str(p.get("mode") or "outside")
    low = p.get("low")
    high = p.get("high")
    if low is None and mode in ("outside", "lt", "le"):
        low = S.low
    if high is None and mode in ("outside", "gt", "ge"):
        high = S.high
    low = float(low if low is not None else S.low)
    high = float(high if high is not None else S.high)
    span = max(high - low, S.lsb * 10)
    tv = float(p.get("target_value") or 0.0)
    for a, b in wins:
        if mode in ("outside", "gt", "ge"):
            target = high + 0.12 * span
        elif mode in ("lt", "le"):
            target = low - 0.12 * span
        elif mode == "eq":
            target = tv
        elif mode == "ne":
            target = tv + max(abs(span) * 0.2, 1.0)
        elif mode == "set":
            vs = p.get("value_set") or [0]
            target = float(vs[0]) if isinstance(vs, (list, tuple)) else float(vs)
        else:
            continue
        if mode in ("eq", "ne", "set"):
            # 定值类：正常段要避开 target
            far = tv + max(abs(span) * 0.2, 1.0)
            v[: a] = np.where(np.abs(v[: a] - target) < 1e-9, far, v[: a])
            v[b:] = np.where(np.abs(v[b:] - target) < 1e-9, far, v[b:])
        start_val = float(v[max(a - 1, 0)])
        for k in range(min(2, b - a)):
            v[a + k] = _lerp(start_val, target, (k + 1) / 2.0)
        v[a + 2: b] = target
        if b < S.n:
            for k, idx in enumerate(range(b, min(b + 2, S.n))):
                v[idx] = _lerp(target, S.trend[idx], (k + 1) / 2.0)
    return {}


def _inj_a02(v, wins, p, S):
    nd = int(p.get("n_dead") or 64)
    for a, b in wins:
        if b - a < nd - 1:
            continue
        val = float(np.round(v[a - 1], 6)) if a > 0 else float(np.round(v[a], 6))
        v[a:b] = val
    return {}


def _inj_a03(v, wins, p, S):
    a_abs = float(p.get("a_abs") or 0.0)
    delta = max(a_abs * 3.0, 8 * S.lsb, 8 * S.noise_sd)
    for j, (a, b) in enumerate(wins):
        i = (a + b) // 2
        if i + 2 >= S.n:
            continue
        sign = 1.0 if j % 2 == 0 else -1.0
        v[i] = float(v[i]) + sign * delta
        v[i + 1] = S.trend[i + 1]
    return {}


def _inj_a04(v, wins, p, S):
    k = max(float(p.get("ratio_up") or 3.0) * 2.5, 4.0)
    for a, b in wins:
        v[a:b] = S.trend[a:b] + S.rng.normal(0, S.noise_sd * k, b - a)
    return {}


def _inj_a05(v, wins, p, S):
    rate = float(p.get("max_rate") or 0.5)
    direction = str(p.get("direction") or "both")
    for j, (a, b) in enumerate(wins):
        w = max(6, min(b - a, 30))
        amp = rate * 3.0 * S.period_s * (w / 2.0)
        up = direction == "up" or (direction == "both" and j % 2 == 0)
        base = float(v[a - 1]) if a > 0 else float(v[a])
        for k in range(w):
            frac = (k + 1) / (w / 2.0)
            shape = frac if frac <= 1 else max(0.0, 2 - frac)
            v[a + k] = base + (1 if up else -1) * amp * shape
    return {}


def _inj_a07(v, wins, p, S):
    if not S.peers:
        peer_code = str(p.get("peer_code") or f"PEER{ S.meta.code }")
        S.peers[peer_code] = -S.trend + S.rng.normal(0, S.noise_sd * 2, S.n)
    for a, b in wins:
        walk = np.cumsum(S.rng.normal(0, S.noise_sd * 3, b - a)) + S.trend[a]
        v[a:b] = walk
    return {}


def _inj_a09(v, wins, p, S):
    if not S.peers:
        peer_code = str(p.get("peer_code") or f"PEER{S.meta.code}")
        S.peers[peer_code] = S.trend.copy()
    off = max(float(p.get("dev_abs") or 0.5), 1e-9) * 2.5
    for a, b in wins:
        v[a:b] = v[a:b] + off
    return {}


def _inj_a10(v, wins, p, S):
    mode = str(p.get("mode") or "no_increase")
    for a, b in wins:
        if mode == "no_increase":
            v[a:b] = float(v[a - 1]) if a > 0 else float(v[a])
        elif mode == "delta_gt":
            bump = int(p.get("max_delta") or 0) + 3
            v[a:] = v[a:] + bump
            v[a + 1:] = v[a + 1:] + bump
        elif mode == "delta_ne":
            bump = 3
            v[a:] = v[a:] + bump
        else:
            # any_change：在窗内插入若干次「离散增量台阶」。
            # 两个约束：
            #   1) 不能整段 +=2 —— 那只有一个阶跃，算法只报 1 处；
            #   2) 台阶之间必须留出「不变的间隙」—— A-10 用 runs() 把连续成立
            #      的拍合并成一段，若每拍都在涨，5 个台阶也只会输出 1 个异常点。
            need = max(int(getattr(S, "min_hits", 5) or 5), 5)
            span = max(b - a, need * 2)
            step_idx = np.linspace(a, a + span - 1, need).astype(int)
            for i, idx in enumerate(step_idx):
                nxt = step_idx[i + 1] if i + 1 < len(step_idx) else idx + 1
                v[idx:nxt] = v[idx:nxt] + 1
    return {}


def _inj_a11(v, wins, p, S):
    width = int(p.get("bit_width") or 8)
    off = int(p.get("bit_offset") or 0)
    fault = int(p.get("fault_value") if p.get("fault_value") is not None else 1)
    expand = bool(p.get("expand_bits", True))
    for j, (a, b) in enumerate(wins):
        bit = (j % width) + off
        seg = min(b - a, 8)
        word = int(v[a - 1]) if a > 0 else int(v[a])
        if fault:
            new = word | (1 << bit)
        else:
            new = word & ~(1 << bit) & ((1 << max(width, 32)) - 1)
        if expand:
            v[a:a + seg] = float(new)
        else:
            v[a:a + seg] = float(new)
    return {}


def _inj_a12(v, wins, p, S):
    op = str(p.get("op") or "gt")
    val = p.get("value")
    val = float(val) if val is not None else float(np.nanmedian(v) + 3 * np.nanstd(v))
    codes = [c for c in (p.get("peer_codes") or []) if c]
    for c in codes:
        if c not in S.peers:
            S.peers[c] = np.array(v, dtype=float, copy=True)
    for j, (a, b) in enumerate(wins):
        target = val + (abs(val) * 0.05 + 1.0) if op in ("gt", "ge") else val - (abs(val) * 0.05 + 1.0)
        v[a:b] = target
        for c in codes:
            S.peers[c][a:b] = target
    return {}


def _inj_a22(v, wins, p, S):
    step = float(p.get("step_thr") or 0.0)
    amp = max(step, 4 * S.noise_sd, 6 * S.lsb) * 2.0
    for a, b in wins:
        for k, i in enumerate(range(a, b)):
            v[i] = S.trend[i] + (amp if k % 2 == 0 else -amp)
    return {}


def _inj_a23(v, wins, p, S):
    bad = p.get("bad_values") or {}
    sig = p.get("signature_values") or {}
    legal = p.get("legal_values")
    if bad:
        illegal = float(sorted(int(k) for k in bad)[0])
    elif sig:
        illegal = float(sorted(int(k) for k in sig)[0])
    elif legal:
        illegal = float(max(int(x) for x in legal) + 1)
    else:
        illegal = float(S.high + 1)
    for a, b in wins:
        v[a:b] = illegal
    return {}


def _inj_a24(v, wins, p, S):
    tv = p.get("target_values") or [1]
    target = float(tv[0]) if isinstance(tv, (list, tuple)) and tv else float(tv)
    for a, b in wins:
        v[a:b] = target
    return {}


def _inj_a25(v, wins, p, S):
    conds = p.get("conditions") or []
    for c in conds:
        code = c.get("code")
        if code and code != S.meta.code and code not in S.peers:
            S.peers[str(code)] = np.array(v, dtype=float, copy=True)
    for a, b in wins:
        for c in conds:
            code, op = c.get("code"), str(c.get("op") or "gt")
            val = float(c.get("value") or 0.0)
            arr = v if code in (None, S.meta.code) else S.peers[str(code)]
            lo, hi = val - max(abs(val) * 0.1, 1.0), val + max(abs(val) * 0.1, 1.0)
            if op in ("gt", "ge"):
                arr[a:b] = hi + abs(val) * 0.1 + 1.0
            elif op in ("lt", "le"):
                arr[a:b] = lo - abs(val) * 0.1 - 1.0
            else:
                arr[a:b] = val
    return {}


def _inj_a26(v, wins, p, S):
    """设定点-响应关系：在窗口起点给一条指令，测点始终达不到期望值 → 未响应。"""
    if p.get("expect_value") is None:
        p = dict(p)
        p["expect_value"] = float(np.nanmax(S.trend) + 10.0)
        p["expect_op"] = "ge"
    cmds = S.extra.setdefault("commands", [])
    for j, (a, b) in enumerate(wins):
        cmds.append({"cmd_id": f"K{7000 + j}", "t": int(S.t[min(a, S.n - 1)])})
    return {}


def _inj_a27(v, wins, p, S):
    return {"drop": [(a, min(b, a + max(3, int(round((float(p.get('max_gap_s') or 2.5 * S.period_s)) / S.period_s)) + 2)))
                     for a, b in wins]}


def _inj_generic(v, wins, p, S):
    """未知/自定义算法：多形态混合注入（跳变、台阶、噪声、冻结、缺口），
    由上层用真实算法回测，命中不足会加重再来。"""
    for j, (a, b) in enumerate(wins):
        mode = j % 4
        seg = b - a
        delta = max(6 * S.lsb, 6 * S.noise_sd, 0.06 * S.span)
        if mode == 0:
            v[a:b] = float(v[a - 1] if a else v[a]) + delta * 2
        elif mode == 1:
            v[a:b] = float(np.round(v[a - 1], 6) if a else np.round(v[a], 6))
        elif mode == 2:
            v[a:b] = S.trend[a:b] + S.rng.normal(0, S.noise_sd * 8, seg)
        else:
            for k in range(3):
                idx = a + int(seg * (k + 1) / 4)
                if idx < S.n:
                    v[idx] = v[idx] + delta * 3 * (1 if k % 2 == 0 else -1)
    return {}


INJECTORS: dict[str, Callable] = {
    "A-01": _inj_a01, "A-02": _inj_a02, "A-03": _inj_a03, "A-04": _inj_a04,
    "A-05": _inj_a05, "A-07": _inj_a07, "A-09": _inj_a09, "A-10": _inj_a10,
    "A-11": _inj_a11, "A-12": _inj_a12, "A-22": _inj_a22, "A-23": _inj_a23,
    "A-24": _inj_a24, "A-25": _inj_a25, "A-26": _inj_a26, "A-27": _inj_a27,
}

# 需要外部输入（基线/原始帧/参考通道校验数据）的算法，模拟端无法伪造，明确告知而不是硬造。
# 清单在 config/app.yaml → simulate.needs_external，新增算法时改 YAML 即可。


# ---------------------------------------------------------------------------
# 参数兜底：模拟必须让算法真的能跑起来
# ---------------------------------------------------------------------------
def autofill_params(algo_id: str, params: dict, code: str) -> dict:
    p = dict(params or {})
    try:
        cls = alg.get(algo_id)
    except KeyError:
        return p
    # A-25：条件里留空或写着占位文本的 value（现场阈值未定）先填 0，
    # 否则 _op_apply 会因 float("待补…") 直接抛错，整条规则变成 ERROR。
    # 这只影响"模拟数据能否跑通"，不改变规则里保存的原始参数。
    if algo_id == "A-25" and isinstance(p.get("conditions"), list):
        for c in p["conditions"]:
            if not isinstance(c, dict):
                continue
            val = c.get("value")
            if val is None:
                c["value"] = 0.0
            elif isinstance(val, str):
                try:
                    c["value"] = float(val)
                except ValueError:
                    c["value"] = 0.0
    for spec in cls.param_specs():
        if p.get(spec.name) is not None:
            continue
        if not spec.required:
            continue
        if spec.type in ("code",) or spec.name == "peer_code":
            p[spec.name] = f"{code}-P"
        elif spec.type == "code_list":
            p[spec.name] = [f"{code}-P"]
    return p


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def simulate_rule(rule: dict, t0: int, t1: int, *, min_hits: int = DEFAULT_MIN_HITS,
                  seed: int | None = None) -> SimResult:
    t0, t1 = int(t0), int(t1)
    duration_s = max((t1 - t0) / 1000.0, 60.0)
    period_s = float(np.clip(duration_s / MAX_POINTS, MIN_PERIOD_S, MAX_PERIOD_S))
    n = max(int(duration_s / period_s) + 1, MIN_POINTS)
    t_axis = t0 + np.round(np.arange(n) * period_s * 1000.0).astype(np.int64)

    code = rule.get("code") or "TGT0000"
    meta = build_meta(code, kind=rule.get("kind"), low=rule.get("low"), high=rule.get("high"),
                      unit=rule.get("unit"), name=rule.get("code_name"),
                      bit_width=rule.get("bit_width"), quantum=rule.get("quantum"))
    algo_specs = [(a.get("algo_id"), dict(a.get("params") or {})) for a in (rule.get("algorithms") or [])]
    algo_specs = [(a, autofill_params(a, p, code)) for a, p in algo_specs if a]
    note: list[str] = []
    rng = np.random.default_rng(seed if seed is not None else hash(rule.get("rule_id") or code) % 2**31)

    best: tuple[TelemetrySeries, dict, dict, int] | None = None
    hits: dict[str, int] = {}
    attempts = 0
    for attempt in range(MAX_ATTEMPTS):
        attempts = attempt + 1
        S = SimCtx(meta=meta, kind=str(meta.kind.value), n=n, period_s=period_s,
                   low=float(rule.get("low") if rule.get("low") is not None else meta.primary_normal().low),
                   high=float(rule.get("high") if rule.get("high") is not None else meta.primary_normal().high),
                   lsb=meta.lsb(), t=t_axis, trend=np.zeros(n), noise_sd=0.0, rng=rng,
                   min_hits=min_hits)
        S.trend = _base_curve(S, [a for a, _ in algo_specs], [p for _, p in algo_specs])
        S.noise_sd = _noise_sd(S)
        v = S.trend + (S.rng.normal(0, S.noise_sd, n) if S.noise_sd > 0 else 0.0)

        drop: list[tuple[int, int]] = []
        lanes = _lanes(n, max(len(algo_specs), 1))
        for j, (aid, p) in enumerate(algo_specs):
            if aid in NEEDS_EXTERNAL:
                continue
            wlen = _default_wlen(aid, p, S)
            count = min_hits + attempt * 2
            wins = _windows(lanes[j], count, wlen)
            fn = INJECTORS.get(aid, _inj_generic)
            try:
                out = fn(v, wins, p, S) or {}
            except Exception as exc:  # noqa: BLE001
                note.append(f"{aid} 注入失败：{type(exc).__name__}: {exc}")
                continue
            drop.extend(out.get("drop") or [])

        # 缺口型注入：真删采样点，时间轴上留真空
        keep = np.ones(n, dtype=bool)
        for a, b in drop:
            keep[max(a, 1): min(b, n)] = False
        t_use, v_use = t_axis[keep], v[keep]

        peers = {
            c: TelemetrySeries(code=c, t=t_use,
                               v=np.asarray(pv)[keep] if len(pv) == n else np.asarray(pv),
                               meta=build_meta(c, kind="continuous"))
            for c, pv in S.peers.items()
        }
        series = TelemetrySeries(code=code, t=t_use, v=v_use, meta=meta, packet_period_ms=period_s * 1000)

        # 用真实算法回测
        hits, status = _probe(series, peers, meta, algo_specs, S.extra, rule)
        best = (series, peers, dict(hits), dict(status), S.extra)
        if hits and all(h >= min_hits for h in hits.values() if h is not None):
            break

    series, peers, hits, status, extra = best  # type: ignore[misc]
    for aid, h in hits.items():
        if h < min_hits:
            if aid in NEEDS_EXTERNAL:
                note.append(f"{aid} 需要外部数据，无法模拟（{NEEDS_EXTERNAL[aid]}）")
            else:
                note.append(f"{aid} 本次仅命中 {h} 处（<{min_hits}），受窗口长度或参数限制")
    return SimResult(series=series, peers=peers, hits=hits, attempts=attempts,
                     notes=list(dict.fromkeys(note)), period_s=period_s, status=status,
                     extra=dict(extra))


def _probe(series: TelemetrySeries, peers: dict, meta, algo_specs, extra: dict, rule: dict):
    hits, status = {}, {}
    for aid, p in algo_specs:
        if aid in NEEDS_EXTERNAL:
            hits[aid] = 0
            status[aid] = "SKIP"
            continue
        try:
            cls = alg.get(aid)
        except KeyError:
            hits[aid], status[aid] = 0, "ERROR"
            continue
        ctx = alg.AlgorithmContext(
            series=series, meta=meta, peers=peers, params=p, extra=extra,
            window=(int(series.t[0]), int(series.t[-1])) if len(series) else (None, None),
            rule_id=rule.get("rule_id", ""), scene_id=rule.get("scene_id", ""),
        )
        res = cls().run(ctx)
        hits[aid] = len(res.points)
        status[aid] = res.status.value
    return hits, status


def simulate_raw(code: str, t0: int, t1: int, *, kind: str | None = None,
                 low: float | None = None, high: float | None = None,
                 unit: str | None = None, seed: int | None = None) -> TelemetrySeries:
    """生成一条"干净"的测点序列（不含注入异常），用于自定义检测的第一步「模拟数据」。"""
    t0, t1 = int(t0), int(t1)
    duration_s = max((t1 - t0) / 1000.0, 60.0)
    period_s = float(np.clip(duration_s / MAX_POINTS, MIN_PERIOD_S, MAX_PERIOD_S))
    n = max(int(duration_s / period_s) + 1, MIN_POINTS)
    t_axis = t0 + np.round(np.arange(n) * period_s * 1000.0).astype(np.int64)
    meta = build_meta(code, kind=kind, low=low, high=high, unit=unit)
    rng = np.random.default_rng(seed if seed is not None else abs(hash(code)) % 2**31)
    S = SimCtx(meta=meta, kind=str(meta.kind.value), n=n, period_s=period_s,
               low=float(meta.primary_normal().low), high=float(meta.primary_normal().high),
               lsb=meta.lsb(), t=t_axis, trend=np.zeros(n), noise_sd=0.0, rng=rng)
    S.trend = _base_curve(S, [], [])
    S.noise_sd = _noise_sd(S)
    v = S.trend + (S.rng.normal(0, S.noise_sd, n) if S.noise_sd > 0 else 0.0)
    # 轻微的工况漂移，让曲线更像真实测点
    if meta.kind.value == "continuous":
        drift = np.cumsum(S.rng.normal(0, S.noise_sd * 0.05, n))
        v = v + drift - drift[0]
    return TelemetrySeries(code=code, t=t_axis, v=v, meta=meta, packet_period_ms=period_s * 1000)
