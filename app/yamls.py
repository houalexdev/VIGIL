"""YAML 配置加载层。

一切可配置项（应用参数、路径、等级色、时间窗、模拟参数、落盘格式、UI 渲染、
测点目录、预置规则/场景、提示词、主题样式）都在 ``config/*.yaml`` 里，
代码只负责读取与解析，不再硬编码具体数值。

加载规则
--------
1. 目录：默认 ``<项目根>/config``，可用环境变量 ``VIGIL_CONFIG_DIR`` 整体替换。
2. 单个文件：可用 ``VIGIL_CONFIG`` 指向另一份 ``app.yaml``（多环境部署）。
3. 环境变量覆盖：``VIGIL_*`` 优先于 YAML，例如 ``VIGIL_PORT`` / ``VIGIL_LLM_URL``。
4. 路径支持三种写法：
   - 相对路径 → 相对项目根；
   - ``${data}/parquet`` → 引用同一文件内已解析的键；
   - ``<项目根>`` 之外不设限制（部署时可用绝对路径）。

出错策略：文件缺失 → 用代码内最小默认值兜底并告警；
YAML 语法错误 / 类型不符 → 直接抛 :class:`ConfigError`，宁可启动失败也不要带着
错误配置跑出错误的判据。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent      # 项目根（含 algorithm/ docs/）
CONFIG_DIR = Path(os.environ.get("VIGIL_CONFIG_DIR", ROOT / "config"))
APP_YAML = Path(os.environ.get("VIGIL_CONFIG", CONFIG_DIR / "app.yaml"))

_WARNINGS: list[str] = []


class ConfigError(RuntimeError):
    """配置文件存在但不可用（语法/类型错误）。"""


# ---------------------------------------------------------------------------
# 基础读写
# ---------------------------------------------------------------------------
def load_yaml(path: Path, *, default: dict | None = None) -> dict:
    """读一个 YAML 文件；缺失时返回 default（并记一条告警）。"""
    if not path.exists():
        if default is None:
            raise ConfigError(f"配置文件不存在：{path}")
        warn(f"缺少 {path.name}，使用内置默认值")
        return copy.deepcopy(default)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} YAML 语法错误：{exc}") from exc
    if data is None:
        return copy.deepcopy(default) if default is not None else {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 顶层必须是映射（key: value），实际是 {type(data).__name__}")
    return data


def warn(msg: str) -> None:
    if msg not in _WARNINGS:
        _WARNINGS.append(msg)
        print(f"[config] {msg}")


def warnings() -> list[str]:
    return list(_WARNINGS)


def _get(d: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = d
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


# ---------------------------------------------------------------------------
# 环境变量覆盖
# ---------------------------------------------------------------------------
_ENV_MAP: dict[str, tuple[str, type]] = {
    # 环境变量            配置键                类型
    "VIGIL_HOST": ("server.host", str),
    "VIGIL_PORT": ("server.port", int),
    "VIGIL_DATA": ("paths.data", str),
    "VIGIL_DB": ("paths.db", str),
    "VIGIL_LLM_URL": ("llm.base_url", str),
    "VIGIL_LLM_KEY": ("llm.api_key", str),
    "VIGIL_LLM_MODEL": ("llm.model", str),
    "VIGIL_LLM_TIMEOUT": ("llm.timeout", float),
}


def _coerce(raw: str, typ: type) -> Any:
    if typ is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return typ(raw)


def _apply_env(cfg: dict) -> dict:
    for env, (key, typ) in _ENV_MAP.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        try:
            val = _coerce(raw, typ)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"环境变量 {env}={raw!r} 无法转换为 {typ.__name__}") from exc
        _set_dotted(cfg, key, val)
    return cfg


def _set_dotted(d: dict, dotted: str, value: Any) -> None:
    segs = dotted.split(".")
    cur = d
    for seg in segs[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[segs[-1]] = value


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def _resolve_paths(paths: dict) -> dict:
    """解析 ${key} 引用与相对路径，返回绝对 Path。"""
    out: dict[str, Path] = {}

    def resolve(key: str, depth: int = 0) -> Path:
        if key in out:
            return out[key]
        if depth > 8:
            raise ConfigError(f"paths 存在循环引用（{key}）")
        raw = str(paths.get(key, "")).strip()
        if not raw:
            raise ConfigError(f"paths.{key} 为空")
        for ref in _refs(raw):
            raw = raw.replace("${" + ref + "}", str(resolve(ref, depth + 1)))
        p = Path(raw)
        p = (ROOT / p) if not p.is_absolute() else p
        out[key] = p.resolve()
        return out[key]

    for k in paths:
        resolve(k)
    return out


def _refs(text: str) -> list[str]:
    found, i = [], 0
    while True:
        a = text.find("${", i)
        if a < 0:
            return found
        b = text.find("}", a)
        if b < 0:
            return found
        found.append(text[a + 2:b].strip())
        i = b + 1


# ---------------------------------------------------------------------------
# 汇总：一次加载、全程复用
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, dict] = {
    "app.yaml": {
        "app": {"name": "VIGIL", "subtitle": "工业设备智能监测平台", "module": "异常数据检测"},
        "server": {"host": "0.0.0.0", "port": 8080, "dark": True, "reload": False, "show": False},
        "paths": {"data": "data", "parquet": "${data}/parquet", "db": "${data}/vigil.db",
                  "user_algorithms": "user_algorithms", "logs": "${data}/runs"},
        "llm": {"base_url": "http://127.0.0.1:8000", "api_key": "", "model": "",
                "timeout": 60, "probe_timeout": 2.0, "temperature": 0.2, "top_p": 0.9,
                "max_tokens": 2048, "endpoints": ["/v1/chat/completions"]},
        "levels": {"order": ["L1", "L2", "L3", "L4"],
                   "items": {f"L{i}": {"cn": f"L{i}", "color": "#94a3b8",
                                       "css": "#cbd5e1", "bg": "rgba(148,163,184,.12)",
                                       "border": "rgba(148,163,184,.35)"} for i in range(1, 5)},
                   "fallback_color": "#94a3b8", "fallback_css": "#64748b"},
        "value_kinds": {"continuous": "连续量", "enum": "枚举量", "counter": "计数量",
                        "bitfield": "状态字", "flag": "布尔位"},
        "windows": {"default_hours": 12, "presets": [1, 4, 12, 24], "min_points": 120},
        "simulate": {"max_points": 9000, "min_period_s": 1.0, "max_period_s": 9.5,
                     "nominal_period_s": 1.0, "lane_lo": 0.28, "lane_hi": 0.97,
                     "max_attempts": 4, "min_hits": 5, "needs_external": {}},
        "storage": {"compression": "snappy", "date_dir_format": "%Y%m%d",
                    "filename": "{t_start}_{t_end}.parquet",
                    "columns": {"t": "t", "v": "v", "quality": "quality"}},
        "ui": {"chart_max_points": 1600, "table_rows_per_page": 10, "tab_label_len": 7,
               "collapse_params": ["min_samples", "max_points", "level"],
               "param_summary_max": 6, "anomaly_columns": []},
    },
    "telemetry.yaml": {"guess_by_prefix": {}, "unknown_default": {"kind": "continuous", "low": 0.0,
                                                                  "high": 100.0, "bit_width": 16},
                       "items": []},
    "presets.yaml": {"scenes": [], "rules": []},
    "prompts.yaml": {"system": "", "user": "", "catalog_hide_params": ["min_samples", "max_points"],
                     "min_rules": 2, "max_rules": 4, "request_defaults": {"stream": False}},
    "theme.yaml": {"variables": {}, "tints": {}, "css": "", "head_html": "", "logo_svg": ""},
}

_CACHE: dict[str, dict] = {}


def file(name: str) -> dict:
    """读 config/<name>，带缓存。"""
    if name not in _CACHE:
        path = CONFIG_DIR / name
        data = load_yaml(path, default=_DEFAULTS.get(name, {}))
        if name == "app.yaml":
            data = _apply_env(data)
            data["paths"] = _resolve_paths(data.get("paths") or {})
        elif name == "presets.yaml":
            _check_presets(data, path)
        _CACHE[name] = data
    return _CACHE[name]


def reload_all() -> None:
    """清缓存（测试或多环境热切换用）。"""
    _CACHE.clear()
    _WARNINGS.clear()


# ---------------------------------------------------------------------------
# 预置规则文件的结构校验：宁可启动失败，也不要写进去半条坏规则
# ---------------------------------------------------------------------------
_REQUIRED_RULE = ("id", "code", "name", "algorithms")


def _check_presets(data: dict, path: Path) -> None:
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        raise ConfigError(f"{path.name}: rules 必须是列表")
    seen: set[str] = set()
    for i, r in enumerate(rules, start=1):
        if not isinstance(r, dict):
            raise ConfigError(f"{path.name}: 第 {i} 条规则不是映射")
        miss = [k for k in _REQUIRED_RULE if not r.get(k)]
        if miss:
            raise ConfigError(f"{path.name}: 第 {i} 条规则缺少字段 {miss}")
        rid = str(r["id"])
        if rid in seen:
            raise ConfigError(f"{path.name}: 规则 ID 重复 {rid}")
        seen.add(rid)
        algos = r["algorithms"]
        if not isinstance(algos, list) or not algos:
            raise ConfigError(f"{path.name}: 规则 {rid} 的 algorithms 不能为空")
        for j, a in enumerate(algos, start=1):
            if not isinstance(a, dict) or not a.get("algo_id"):
                raise ConfigError(f"{path.name}: 规则 {rid} 第 {j} 个算法缺少 algo_id")

    scenes = data.get("scenes") or []
    if not isinstance(scenes, list):
        raise ConfigError(f"{path.name}: scenes 必须是列表")
    for i, s in enumerate(scenes, start=1):
        if not isinstance(s, dict) or not s.get("id"):
            raise ConfigError(f"{path.name}: 第 {i} 个场景缺少 id")
        for rid in s.get("rules") or []:
            if rid not in seen:
                raise ConfigError(f"{path.name}: 场景 {s['id']} 引用了不存在的规则 {rid}")


# ---------------------------------------------------------------------------
# 便捷取值
# ---------------------------------------------------------------------------
def get(dotted: str, default: Any = None) -> Any:
    """取 app.yaml 里的配置，如 get("simulate.max_points")。"""
    return _get(file("app.yaml"), dotted, default)


def level_colors() -> dict[str, str]:
    return {k: v.get("color", "#94a3b8") for k, v in file("app.yaml")["levels"]["items"].items()}


def level_names() -> dict[str, str]:
    return {k: v.get("cn", k) for k, v in file("app.yaml")["levels"]["items"].items()}


def level_order() -> dict[str, int]:
    order = file("app.yaml")["levels"]["order"]
    return {lv: i + 1 for i, lv in enumerate(order)}
