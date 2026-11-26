"""深色科技风主题（数据源：``config/theme.yaml``）。

CSS 文本、配色变量、Logo 全部放在 YAML 里；等级色标（.lvl-L1 …）由
``config/app.yaml → levels`` 动态生成，新增告警等级只需要改那一处。
"""
from __future__ import annotations

from . import yamls
from .config import LEVEL_FALLBACK_CSS, LEVEL_KEYS

_T = yamls.file("theme.yaml")
_APP = yamls.file("app.yaml")


def _root_vars() -> str:
    var_map = {"bg": "--sy-bg", "panel": "--sy-panel", "panel_2": "--sy-panel-2",
               "line": "--sy-line", "line_2": "--sy-line-2", "cyan": "--sy-cyan",
               "blue": "--sy-blue", "text": "--sy-text", "dim": "--sy-dim",
               "amber": "--sy-amber", "red": "--sy-red"}
    vars_ = _T.get("variables") or {}
    lines = ["{"]
    for key, css_var in var_map.items():
        if key in vars_:
            lines.append(f"  {css_var}: {vars_[key]};")
    return "\n".join(lines) + "\n}"


def _level_css() -> str:
    """由 levels 配置生成 .lvl-Lx 色标。"""
    items = _APP["levels"]["items"]
    out = []
    for lv in LEVEL_KEYS:
        conf = items.get(lv, {})
        out.append(
            f".lvl-{lv} {{ color: {conf.get('css', '#cbd5e1')};"
            f" background: {conf.get('bg', 'rgba(148,163,184,.12)')};"
            f" border: 1px solid {conf.get('border', 'rgba(148,163,184,.35)')}; }}")
    return "\n".join(out)


def _render(tpl: str) -> str:
    tints = dict(_T.get("tints") or {})
    values = {
        "root_vars": _root_vars(),
        "level_css": _level_css(),
        "fallback_css": LEVEL_FALLBACK_CSS,
        "fallback_border": "rgba(100, 116, 139, .3)",
        **{k: str(v) for k, v in tints.items()},
    }
    out = tpl
    for k, v in values.items():
        out = out.replace("${" + k + "}", v)
    return out


CSS = _render(str(_T.get("css") or ""))
HEAD_HTML = str(_T.get("head_html") or "")
LOGO_SVG = str(_T.get("logo_svg") or "")
