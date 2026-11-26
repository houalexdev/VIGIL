"""全局配置出口。

**这里不再硬编码任何业务数值** —— 所有配置来自 ``config/*.yaml``：
``app.yaml``（应用/路径/等级/模拟/落盘/UI）、``telemetry.yaml``（测点目录）、
``presets.yaml``（预置规则与场景）、``prompts.yaml``（大模型提示词）、
``theme.yaml``（视觉主题）。

本模块只做「YAML → 常用常量」的翻译，保持原有 import 路径不变，
所以业务代码依然写 ``from .config import LEVEL_CN``。
"""
from __future__ import annotations

from . import yamls

_APP = yamls.file("app.yaml")

# ---------------------------------------------------------------------------
# 应用标识
# ---------------------------------------------------------------------------
_meta = _APP.get("app", {})
APP_NAME = _meta.get("name", "VIGIL")
APP_SUB = _meta.get("subtitle", "")
APP_MODULE = _meta.get("module", "")
APP_VERSION = _meta.get("version", "")

# 顶栏标题模板：{name} {subtitle} {module} {version}
PAGE_TITLE = str(_APP.get("server", {}).get("title", "{name} · {subtitle} · {module}")).format(
    name=APP_NAME, subtitle=APP_SUB, module=APP_MODULE, version=APP_VERSION)

# ---------------------------------------------------------------------------
# 路径（已解析为绝对路径，目录已创建）
# ---------------------------------------------------------------------------
_PATHS = _APP["paths"]
DATA_DIR = _PATHS["data"]
PARQUET_DIR = _PATHS["parquet"]
DB_PATH = _PATHS["db"]
USER_ALGO_DIR = _PATHS["user_algorithms"]
LOG_DIR = _PATHS["logs"]

for _p in (DATA_DIR, PARQUET_DIR, USER_ALGO_DIR, LOG_DIR, DB_PATH.parent):
    _p.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 大模型
# ---------------------------------------------------------------------------
_LLM = _APP.get("llm", {})
LLM_BASE_URL = str(_LLM.get("base_url", ""))
LLM_API_KEY = str(_LLM.get("api_key", ""))
LLM_MODEL = str(_LLM.get("model", ""))
LLM_TIMEOUT = float(_LLM.get("timeout", 60))
LLM_PROBE_TIMEOUT = float(_LLM.get("probe_timeout", 2.0))
LLM_ENDPOINTS = list(_LLM.get("endpoints") or ["/v1/chat/completions"])
LLM_REQUEST = {
    "temperature": float(_LLM.get("temperature", 0.2)),
    "top_p": float(_LLM.get("top_p", 0.9)),
    "max_tokens": int(_LLM.get("max_tokens", 2048)),
}

# ---------------------------------------------------------------------------
# 告警等级：颜色 / 中文名 / 数值序
# ---------------------------------------------------------------------------
LEVEL_COLOR = yamls.level_colors()
LEVEL_CN = yamls.level_names()
LEVEL_ORDER = yamls.level_order()
LEVEL_FALLBACK_COLOR = _APP["levels"].get("fallback_color", "#94a3b8")
LEVEL_FALLBACK_CSS = _APP["levels"].get("fallback_css", "#64748b")
LEVEL_KEYS = list(_APP["levels"].get("order") or LEVEL_COLOR.keys())

VALUE_KINDS: dict[str, str] = dict(_APP.get("value_kinds") or {})

# ---------------------------------------------------------------------------
# 时间窗口
# ---------------------------------------------------------------------------
_WIN = _APP.get("windows", {})
DEFAULT_WINDOW_HOURS = int(_WIN.get("default_hours", 12))
WINDOW_PRESETS = [int(h) for h in (_WIN.get("presets") or [1, 4, 12, 24])]
MIN_POINTS = int(_WIN.get("min_points", 120))
DEFAULT_WINDOW_MS = DEFAULT_WINDOW_HOURS * 3600 * 1000

# ---------------------------------------------------------------------------
# 模拟
# ---------------------------------------------------------------------------
_SIM = _APP.get("simulate", {})
SIM_MAX_POINTS = int(_SIM.get("max_points", 9000))
SIM_MIN_PERIOD_S = float(_SIM.get("min_period_s", 1.0))
SIM_MAX_PERIOD_S = float(_SIM.get("max_period_s", 9.5))
SAMPLE_PERIOD_S = float(_SIM.get("nominal_period_s", 1.0))
SIM_LANE = (float(_SIM.get("lane_lo", 0.28)), float(_SIM.get("lane_hi", 0.97)))
SIM_MAX_ATTEMPTS = int(_SIM.get("max_attempts", 4))
SIM_MIN_HITS = int(_SIM.get("min_hits", 5))
NEEDS_EXTERNAL: dict[str, str] = dict(_SIM.get("needs_external") or {})

# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
_STORE = _APP.get("storage", {})
PARQUET_COMPRESSION = str(_STORE.get("compression", "snappy"))
DATE_DIR_FORMAT = str(_STORE.get("date_dir_format", "%Y%m%d"))
PARQUET_FILENAME = str(_STORE.get("filename", "{t_start}_{t_end}.parquet"))
COLUMNS = dict(_STORE.get("columns") or {"t": "t", "v": "v", "quality": "quality"})

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
_UI = _APP.get("ui", {})
CHART_MAX_POINTS = int(_UI.get("chart_max_points", 1600))
TABLE_ROWS_PER_PAGE = int(_UI.get("table_rows_per_page", 10))
TAB_LABEL_LEN = int(_UI.get("tab_label_len", 7))
COLLAPSE_PARAMS = set(_UI.get("collapse_params") or ["min_samples", "max_points", "level"])
PARAM_SUMMARY_MAX = int(_UI.get("param_summary_max", 6))
ANOMALY_COLUMNS: list[dict] = list(_UI.get("anomaly_columns") or [])

# 服务器
SERVER = dict(_APP.get("server", {}))
