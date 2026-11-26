"""界面公共件：页面骨架、卡片、参数表单、图表配置、表格。"""
from __future__ import annotations

import html as html_lib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from nicegui import ui

import algorithm as alg
from algorithm.base import ParamSpec

from ..config import (ANOMALY_COLUMNS as _CFG_COLUMNS, CHART_MAX_POINTS, COLLAPSE_PARAMS,
                      LEVEL_COLOR, LEVEL_CN, LEVEL_FALLBACK_COLOR, LEVEL_ORDER,
                      PARAM_SUMMARY_MAX, TABLE_ROWS_PER_PAGE, VALUE_KINDS)
from ..report import point_rows
from ..theme import CSS, HEAD_HTML, LOGO_SVG

TZ = timezone(timedelta(hours=8))

NAV = [
    ("/rules", "01", "规则中心", "tune"),
    ("/detect", "02", "异常检测", "troubleshoot"),
]


def setup_page() -> None:
    ui.add_head_html(HEAD_HTML)
    ui.add_head_html(f"<style>{CSS}</style>")
    ui.dark_mode(True)
    ui.html('<div class="sy-bgfx"></div>', sanitize=False).classes("fixed").style("z-index:0")


def header(llm_status: tuple[bool, str] = (False, "未检测")) -> None:
    online, msg = llm_status
    with ui.row().classes("sy-header w-full no-wrap items-center"):
        with ui.row().classes("sy-logo items-center gap-3"):
            ui.html(LOGO_SVG, sanitize=False)
            with ui.column().classes("gap-0"):
                ui.html('VI<span>GIL</span>', sanitize=False).classes("sy-brand")
                ui.label("工业设备智能监测平台").classes("sy-sub")
        ui.html('<div class="sy-mod">异常数据检测</div>', sanitize=False)
        ui.space()
        with ui.row().classes("sy-status gap-2 items-center"):
            ui.html('<span class="sy-chip"><span class="sy-dot"></span>测点链路 <b>SIM</b></span>', sanitize=False)
            ui.html('<span class="sy-chip"><span class="sy-dot"></span>存储 <b>PARQUET / SNAPPY</b></span>', sanitize=False)
            dot = "sy-dot" if online else "sy-dot amber"
            label = "在线" if online else "离线"
            safe_msg = html_lib.escape(msg[:26])
            ui.html(f'<span class="sy-chip"><span class="{dot}"></span>本地大模型 <b>{label}</b>'
                    f'<span style="color:#4d6379">· {safe_msg}</span></span>', sanitize=False)


def rail(active: str) -> None:
    with ui.column().classes("sy-rail gap-0"):
        for href, idx, name, icon in NAV:
            cls = "sy-nav active" if active == href else "sy-nav"
            with ui.row().classes(cls + " no-wrap items-center").on("click", lambda h=href: ui.navigate.to(h)):
                ui.html(f'<span class="idx">{idx}</span>', sanitize=False)
                ui.icon(icon, size="16px")
                ui.label(name)
        with ui.column().classes("sy-rail-foot gap-1"):
            ui.label("ALGORITHM ENGINE").classes("sy-label")
            ui.label(f"{len(alg.all_algorithms())} 个算法已注册").style("color:#6f859c")
            ui.label("27 内置 · 支持自定义").style("color:#4d6379")
            ui.separator().style("margin:8px 0")
            clock = ui.label("").classes("sy-mono").style("color:#5f7488;font-size:11px")

            def tick() -> None:
                clock.set_text(datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))

            tick()
            ui.timer(1.0, tick)


@contextmanager
def shell(active: str, llm_status: tuple[bool, str] = (False, "未检测")):
    setup_page()
    with ui.column().classes("sy-shell w-full gap-0"):
        header(llm_status)
        with ui.row().classes("w-full no-wrap gap-3 px-4 items-start"):
            rail(active)
            with ui.column().classes("grow gap-3 pb-8").style("min-width:0"):
                yield


def card(title: str = "", sub: str = "", extra_style: str = ""):
    """返回一个带角标的卡片容器（作为 contextmanager 使用）。"""
    return _Card(title, sub, extra_style)


class _Card:
    def __init__(self, title: str, sub: str, extra_style: str):
        self.title, self.sub, self.extra_style = title, sub, extra_style

    def __enter__(self):
        self.ctx = ui.card().classes("sy-card w-full gap-3").style(
            "padding:14px 16px; " + self.extra_style)
        self.ctx.__enter__()
        if self.title:
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.html('<span class="bar"></span>', sanitize=False)
                ui.label(self.title).classes("sy-card-title")
                if self.sub:
                    ui.label(self.sub).classes("sy-card-sub")
        return self.ctx

    def __exit__(self, *exc):
        return self.ctx.__exit__(*exc)


def kpi(label: str, value, tone: str = "") -> None:
    with ui.column().classes("sy-kpi gap-0"):
        ui.label(label).classes("k")
        ui.label(str(value)).classes(f"v {tone}")


def tag(text: str, kind: str = "") -> None:
    ui.html(f'<span class="tag {kind}">{text}</span>', sanitize=False)


def level_badge(level: str) -> None:
    if level:
        ui.html(f'<span class="lvl lvl-{level}">{LEVEL_CN.get(level, level)}</span>', sanitize=False)
    else:
        ui.html('<span class="lvl lvl-blank">—</span>', sanitize=False)


def empty(text: str) -> None:
    ui.html(f'<div class="sy-empty">{text}</div>', sanitize=False)


# ---------------------------------------------------------------------------
# 参数表单
# ---------------------------------------------------------------------------
def _fmt_list(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def _parse_list(raw: str, cast) -> list:
    parts = [p for p in raw.replace("，", ",").replace(" ", ",").split(",") if p != ""]
    return [cast(p) for p in parts]


def param_fields(specs: list[ParamSpec], values: dict | None = None,
                 columns: int = 2) -> tuple[dict, callable]:
    """按 ParamSpec 生成表单控件，返回 (控件表, 取值函数)。取值函数抛 ValueError 表示非法。"""
    values = dict(values or {})
    widgets: dict[str, object] = {}
    errors: list[str] = []
    specs = [s for s in specs if s.name not in ("min_samples", "max_points")]
    with ui.grid(columns=columns).classes("w-full gap-3"):
        for spec in specs:
            cur = values.get(spec.name, spec.default)
            label = spec.label + (f"（{spec.unit}）" if spec.unit else "")
            if spec.type == "enum":
                w = ui.select(spec.choices or [], label=label, value=cur).classes("w-full").props("outlined dense")
            elif spec.type == "bool":
                w = ui.switch(spec.label, value=bool(cur)).props("dense")
            elif spec.type == "int":
                w = ui.number(label, value=None if cur is None else int(cur),
                              min=spec.minimum, max=spec.maximum, precision=0,
                              step=1).classes("w-full").props("outlined dense")
            elif spec.type in ("float", "seconds", "ms"):
                w = ui.number(label, value=None if cur is None else float(cur),
                              min=spec.minimum, max=spec.maximum).classes("w-full").props("outlined dense")
            elif spec.type in ("list[int]", "list[float]"):
                w = ui.input(label, value=_fmt_list(cur), placeholder="逗号分隔，如 1, 2, 3").classes("w-full").props("outlined dense")
            elif spec.type in ("list[str]", "code_list"):
                w = ui.input(label, value=_fmt_list(cur), placeholder="逗号分隔代号").classes("w-full").props("outlined dense")
            elif spec.type in ("dict", "list[dict]"):
                w = ui.textarea(label, value=json.dumps(cur, ensure_ascii=False) if cur else "",
                                placeholder='JSON，如 [{"code":"TRM0001","op":"ge","value":86.5}]'
                                ).classes("w-full").props("outlined dense")
            else:
                w = ui.input(label, value="" if cur is None else str(cur)).classes("w-full").props("outlined dense")
            if spec.help:
                w.tooltip(spec.help)
            widgets[spec.name] = w
            # 条件显示
            if spec.depends_on:
                for dep_name, allowed in spec.depends_on.items():
                    dep = widgets.get(dep_name)
                    if dep is None:
                        continue
                    w.bind_visibility_from(dep, "value", backward=lambda v, a=allowed: v in a)

    def getter() -> dict:
        out: dict = {}
        errors.clear()
        for spec in specs:
            w = widgets[spec.name]
            raw = w.value
            if spec.type == "bool":
                out[spec.name] = bool(raw)
                continue
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                if spec.required and spec.default is None:
                    errors.append(f"{spec.label} 为必填")
                continue
            try:
                if spec.type == "int":
                    out[spec.name] = int(raw)
                elif spec.type in ("float", "seconds", "ms"):
                    out[spec.name] = float(raw)
                elif spec.type == "list[int]":
                    out[spec.name] = _parse_list(str(raw), int)
                elif spec.type == "list[float]":
                    out[spec.name] = _parse_list(str(raw), float)
                elif spec.type in ("list[str]", "code_list"):
                    out[spec.name] = _parse_list(str(raw), str)
                elif spec.type in ("dict", "list[dict]"):
                    parsed = json.loads(raw)
                    out[spec.name] = parsed
                else:
                    out[spec.name] = str(raw)
            except (TypeError, ValueError) as exc:
                errors.append(f"{spec.label} 取值非法：{exc}")
        if errors:
            raise ValueError("；".join(errors))
        return out

    return widgets, getter


# ---------------------------------------------------------------------------
# 图表
# ---------------------------------------------------------------------------
def _fmt_val(v) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.1f}"
    return f"{v:.4g}" if abs(v) < 10 else f"{v:.3f}".rstrip("0").rstrip(".")


def _mark_points(points: list[dict]) -> dict:
    """等级标签钉：把最严重的若干异常点直接标在曲线上（超过 30 个不标，避免糊成一片）。"""
    if not points or len(points) > 30:
        return {"data": []}
    items = []
    for p in points:
        pv = p.get("plot", p.get("value"))
        if pv is None:
            continue
        lv = p.get("level", "L2")
        items.append({
            "coord": [int(p["t_start"]), float(pv)],
            "name": lv,
            "itemStyle": {"color": LEVEL_COLOR.get(lv, "#f87171")},
            "label": {"formatter": lv, "color": "#04080f", "fontSize": 9, "fontWeight": "bold"},
        })
    return {"symbol": "pin", "symbolSize": 26, "data": items[:30],
            "tooltip": {"trigger": "item", "formatter": "{b}"}}


def chart_option(view: dict, points: list[dict], height: int = 340) -> dict:
    unit = view.get("unit") or ""
    line_data = [[int(t), v] for t, v in zip(view.get("t", []), view.get("v", [])) if v is not None]
    groups: dict[str, list] = {}
    for p in points:
        pv = p.get("plot", p.get("value"))
        if pv is None:
            continue
        key = f'{LEVEL_CN.get(p["level"], p["level"])}·{p.get("hit_type", "")}'
        label = (f'{p.get("time", "")} · {LEVEL_CN.get(p["level"], p["level"])}'
                 f' · {p.get("hit_type", "")} · {p.get("algorithm_id", "")}'
                 f' · 值 {p.get("value_s") or pv}{(" " + (view.get("unit") or "")).rstrip()}')
        groups.setdefault(key, []).append({"value": [int(p["t_start"]), float(pv)], "name": label})

    series = [{
        "name": view.get("code", "测点"),
        "type": "line",
        "showSymbol": False,
        "sampling": "lttb",
        "lineStyle": {"width": 1.2, "color": "#22d3ee", "shadowBlur": 6, "shadowColor": "rgba(34,211,238,.35)"},
        "itemStyle": {"color": "#22d3ee"},
        "areaStyle": {"color": {
            "type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1,
            "colorStops": [{"offset": 0, "color": "rgba(34,211,238,.20)"},
                           {"offset": 1, "color": "rgba(34,211,238,0)"}],
        }},
        "data": line_data,
        "z": 2,
        "markPoint": _mark_points(points),
    }]
    for key, data in groups.items():
        lv = key[:2]
        color = _cn_to_color().get(lv, LEVEL_FALLBACK_COLOR)
        series.append({
            "name": key,
            "type": "scatter",
            "data": data,
            "symbolSize": 11,
            "z": 5,
            "itemStyle": {"color": color, "borderColor": "#04080f", "borderWidth": 1,
                          "shadowBlur": 10, "shadowColor": color},
            "tooltip": {"trigger": "item", "formatter": "{b}"},
        })

    return {
        "backgroundColor": "transparent",
        "animationDuration": 420,
        "grid": {"left": 58, "right": 22, "top": 34, "bottom": 52},
        "legend": {"top": 2, "right": 8, "textStyle": {"color": "#8aa1b8", "fontSize": 10},
                   "itemWidth": 10, "itemHeight": 8, "icon": "circle"},
        "tooltip": {
            "trigger": "axis",
            "backgroundColor": "rgba(9,16,26,.94)",
            "borderColor": "rgba(56,189,248,.35)",
            "textStyle": {"color": "#d6e4f2", "fontSize": 12},
            "axisPointer": {"type": "cross", "lineStyle": {"color": "rgba(34,211,238,.45)"},
                            "crossStyle": {"color": "rgba(34,211,238,.35)"}},
        },
        "xAxis": {
            "type": "time",
            "axisLine": {"lineStyle": {"color": "rgba(56,189,248,.28)"}},
            "axisLabel": {"color": "#7d90a8", "fontSize": 10, "hideOverlap": True},
            "splitLine": {"show": False},
        },
        "yAxis": {
            "type": "value", "scale": True,
            "name": unit, "nameTextStyle": {"color": "#6f859c", "fontSize": 10, "padding": [0, 0, 0, 6]},
            "axisLine": {"show": False},
            "axisLabel": {"color": "#7d90a8", "fontSize": 10},
            "splitLine": {"lineStyle": {"color": "rgba(56,189,248,.075)"}},
        },
        "dataZoom": [
            {"type": "inside", "filterMode": "none"},
            {"type": "slider", "height": 14, "bottom": 8, "borderColor": "rgba(56,189,248,.18)",
             "backgroundColor": "rgba(9,15,25,.6)", "fillerColor": "rgba(34,211,238,.10)",
             "handleStyle": {"color": "#22d3ee"}, "textStyle": {"color": "#5f7488", "fontSize": 9}},
        ],
        "series": series,
    }


# 异常点表格列来自 config/app.yaml → ui.anomaly_columns
ANOMALY_COLUMNS = _CFG_COLUMNS or [
    {"name": "time", "label": "时间", "field": "time", "align": "left", "sortable": True},
    {"name": "level", "label": "等级", "field": "level", "align": "left"},
    {"name": "hit_type", "label": "命中类型", "field": "hit_type", "align": "left"},
    {"name": "algo", "label": "算法", "field": "algo", "align": "left"},
    {"name": "value_s", "label": "触发值", "field": "value_s", "align": "right"},
    {"name": "threshold_s", "label": "阈值", "field": "threshold_s", "align": "right"},
    {"name": "duration_s", "label": "持续(s)", "field": "duration_s", "align": "right", "sortable": True},
    {"name": "dimension", "label": "定位", "field": "dimension", "align": "left"},
    {"name": "note", "label": "说明", "field": "note", "align": "left"},
]


def _cn_to_color() -> dict[str, str]:
    """中文等级名 → 颜色，用于图表散点分组着色（分组键是中文名）。"""
    return {LEVEL_CN.get(k, k): v for k, v in LEVEL_COLOR.items()}


def anomaly_table(points: list[dict]) -> None:
    # 行拼装逻辑在 app/report.py，与命令行导出共用同一份，避免说明文字漂移
    rows = point_rows(points)
    table = ui.table(columns=ANOMALY_COLUMNS, rows=rows, row_key="no",
                     pagination={"rowsPerPage": TABLE_ROWS_PER_PAGE, "sortBy": "time"}).classes("w-full").props("flat dense")
    table.add_slot("body-cell-level", """
        <q-td :props="props">
          <span :class="'lvl lvl-' + props.value">{{ props.row.level_cn }}</span>
        </q-td>""")
    table.add_slot("body-cell-no", '<q-td :props="props" class="sy-mono">{{ props.value }}</q-td>')


def parse_ts(text: str) -> int:
    """接受毫秒时间戳或 'YYYY-MM-DD HH:MM:SS'。"""
    s = (text or "").strip()
    if not s:
        raise ValueError("时间不能为空")
    if s.isdigit():
        v = int(s)
        return v * 1000 if v < 10**11 else v
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=TZ).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间：{text}")


def ts_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")


def algo_options(kind: str | None = None) -> dict[str, str]:
    from ..simulate import NEEDS_EXTERNAL
    out = {}
    for cls in alg.all_algorithms():
        if not cls.IS_DETECTOR:
            continue
        if kind and kind not in [k.value for k in cls.SUPPORTED_KINDS]:
            continue
        suffix = "（需外部数据，模拟端不提供）" if cls.ALGO_ID in NEEDS_EXTERNAL else ""
        out[cls.ALGO_ID] = f"{cls.ALGO_ID} · {cls.ALGO_NAME}{suffix}"
    return out


def kind_options() -> dict[str, str]:
    return {k: f"{v}（{k}）" for k, v in VALUE_KINDS.items()}


def algo_summary(params: dict, algo_id: str) -> str:
    try:
        cls = alg.get(algo_id)
    except KeyError:
        return ""
    parts = []
    for spec in cls.param_specs():
        if spec.name in COLLAPSE_PARAMS:
            continue
        if spec.name in params and params[spec.name] is not None:
            v = params[spec.name]
            if isinstance(v, list):
                v = ",".join(str(x) for x in v)
            parts.append(f"{spec.name}={v}")
    return "；".join(parts[:PARAM_SUMMARY_MAX])
