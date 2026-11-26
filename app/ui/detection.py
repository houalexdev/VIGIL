"""异常检测：分组场景批量检测 + 自定义检测（模拟数据 / AI 生成规则 / 分析）。"""
from __future__ import annotations

import asyncio
import html as html_lib
from datetime import datetime, timedelta

from nicegui import ui

import algorithm as alg

from .. import engine, llm, runtime, store
from ..config import (DEFAULT_WINDOW_HOURS, LEVEL_CN, LEVEL_KEYS, SAMPLE_PERIOD_S,
                      SIM_MIN_HITS, TAB_LABEL_LEN, VALUE_KINDS, WINDOW_PRESETS)
from ..simulate import simulate_raw
from .common import (anomaly_table, card, chart_option, empty, kpi, level_badge,
                     parse_ts, shell, tag, ts_text)
from .rules_center import rule_dialog

TZ = engine.TZ


def _default_range(hours: int = DEFAULT_WINDOW_HOURS) -> tuple[int, int]:
    now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    t1 = int(now.timestamp() * 1000)
    return t1 - hours * 3600 * 1000, t1


def _kpi_tone(level: str) -> str:
    """等级 → KPI 配色基调。最高的两档用红、中间用琥珀、最低用青。"""
    idx = LEVEL_KEYS.index(level) if level in LEVEL_KEYS else 0
    if idx >= len(LEVEL_KEYS) - 2:
        return "red"
    if idx == len(LEVEL_KEYS) - 3:
        return "amber"
    return "cyan"


def _time_row(state: dict) -> None:
    """时间窗口输入（时间戳为毫秒，可读文本二选一填）。"""
    t0, t1 = state["t0"], state["t1"]
    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        start = ui.input("开始时间戳", value=ts_text(t0)).classes("w-52").props("outlined dense")
        end = ui.input("结束时间戳", value=ts_text(t1)).classes("w-52").props("outlined dense")
        epoch = ui.label(f"{t0} → {t1}").classes("sy-mono").style("color:#5f7488;font-size:11px")
        ui.space()
        for h in WINDOW_PRESETS:
            def apply(hh=h):
                a, b = _default_range(hh)
                state["t0"], state["t1"] = a, b
                start.set_value(ts_text(a))
                end.set_value(ts_text(b))
                epoch.set_text(f"{a} → {b}")
            ui.button(f"{h}h", on_click=apply).props("flat dense").classes("sy-btn-ghost")

    def on_edit() -> None:
        try:
            a, b = parse_ts(start.value), parse_ts(end.value)
        except ValueError as exc:
            epoch.set_text(str(exc)).style("color:#fca5a5;font-size:11px")
            return
        if b <= a:
            epoch.set_text("结束时间必须晚于开始时间").style("color:#fca5a5;font-size:11px")
            return
        state["t0"], state["t1"] = a, b
        epoch.set_text(f"{a} → {b}（{(b - a) / 3600000:.1f} h）").style("color:#5f7488;font-size:11px")

    start.on_value_change(lambda _: on_edit())
    end.on_value_change(lambda _: on_edit())


# ---------------------------------------------------------------------------
# 结果渲染
# ---------------------------------------------------------------------------
def _rule_panel(out: engine.RuleOutcome) -> None:
    rule = out.rule
    c = out.counters()
    with ui.row().classes("w-full items-center gap-2 no-wrap").style("margin-bottom:2px"):
        ui.html(f'<span class="algo-id">{rule["code"]}</span>', sanitize=False)
        ui.label(rule.get("code_name") or "").classes("algo-name")
        tag(VALUE_KINDS.get(rule.get("kind"), rule.get("kind")))
        ui.space()
        # 等级顺序来自 config/app.yaml → levels.order（高等级在前）
        for lv in reversed(LEVEL_KEYS):
            if c.get(lv):
                level_badge(lv)
                ui.label(str(c[lv])).classes("sy-mono").style("font-size:11px;color:#93a9c0")

    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        for o in out.algo_outcomes:
            color = {"OK": "cyan", "NO_ANOMALY": "", "UNKNOWN": "warn", "SKIP": "warn",
                     "ERROR": "warn"}.get(o.status, "")
            tag(f"{o.algo_id} {o.status_cn} {len(o.points)}", color)
        ui.label(f"采样周期 {out.period_s:g}s · 数据点 {out.series.get('points', 0):,}"
                 ).classes("sy-hint")
        ui.space()
        ui.label(f"异常点 {len(out.points)}").classes("sy-chip")

    ui.echart(chart_option(out.series, out.points)).classes("w-full").style("height:330px")

    if rule.get("note"):
        ui.label(rule["note"]).classes("sy-hint")
    for n in out.notes:
        ui.label(f"⚠ {n}").style("color:#fcd34d;font-size:11px")

    if out.points:
        anomaly_table(out.points)
    else:
        empty("该规则本时段未检出异常（算法状态见上方标签）")


def render_results(container, outs: list[engine.RuleOutcome], t0: int, t1: int,
                   title: str = "") -> None:
    container.clear()
    with container:
        sm = engine.scene_summary(outs)
        with card(title or "检测结果", f"{ts_text(t0)} → {ts_text(t1)}"):
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                kpi("规则数", sm["rules"], "cyan")
                kpi("命中规则", sm["hit_rules"], "amber")
                kpi("异常点", sm["total"], "red")
                # 等级 KPI 由 config/app.yaml → levels.order 决定（高等级在前）
                for lv in reversed(LEVEL_KEYS):
                    kpi(LEVEL_CN.get(lv, lv), sm["by_level"].get(lv, 0), _kpi_tone(lv))
                ui.space()
                paths = {d["path"] for o in outs for d in o.datasets}
                ui.label(f"数据落盘 {len(paths)} 个 parquet（snappy）").classes("sy-hint")
        if not outs:
            empty("场景内没有规则")
            return

        with card("", ""):
            heads = []
            for i, o in enumerate(outs, start=1):
                heads.append((o, f'{i:02d} {o.rule["name"][:TAB_LABEL_LEN]}·{len(o.points)}'))
            tabs = ui.tabs().classes("w-full").props("dense no-caps inline-label align-left")
            panels = ui.tab_panels(tabs, value=f"r0").classes("w-full bg-transparent")
            with panels:
                for i, (o, label) in enumerate(heads):
                    with ui.tab_panel(f"r{i}").classes("px-0"):
                        _rule_panel(o)
            with tabs:
                for i, (o, label) in enumerate(heads):
                    ui.tab(f"r{i}", label)


# ---------------------------------------------------------------------------
# 分组场景
# ---------------------------------------------------------------------------
def scene_panel(container, preset_scene: str | None) -> None:
    scenes = store.list_scenes()
    st = {"t0": 0, "t1": 0, "outs": []}
    st["t0"], st["t1"] = _default_range()
    options = {s["scene_id"]: f'{s["name"]}（{len(s["rule_ids"])} 条规则）' for s in scenes}
    default = preset_scene if preset_scene in options else (scenes[0]["scene_id"] if scenes else None)

    with card("分析任务", "选定场景 → 设定时间窗口 → 模拟数据 → 逐规则检测"):
        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            scene_sel = ui.select(options, label="分组场景", value=default).classes("w-72").props("outlined dense")
            _time_row(st)
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(f"每条规则最少 {SIM_MIN_HITS} 处异常（配置文件 simulate.min_hits 可调）").classes("sy-hint")
            ui.space()
            get_btn = ui.button("获取数据", icon="cloud_download").props("flat").classes("sy-btn-ghost")
            get_btn.tooltip("测点数据接口暂未接入（按需求本期仅做模拟数据流）")
            sim_btn = ui.button("模拟数据", icon="bolt").props("unelevated").classes("sy-btn-primary")
            sim_btn.mark("btn-sim-scene")

        scene_desc = ui.label("").classes("sy-hint")

        def show_desc() -> None:
            sc = store.get_scene(scene_sel.value) if scene_sel.value else None
            scene_desc.set_text((sc or {}).get("desc", ""))

        scene_sel.on_value_change(lambda _: show_desc())
        show_desc()

    results = ui.column().classes("w-full gap-3")

    async def on_sim() -> None:
        if not scene_sel.value:
            ui.notify("请先选择分组场景", color="warning")
            return
        sim_btn.props("loading")
        try:
            scene = store.get_scene(scene_sel.value)
            outs = await asyncio.to_thread(engine.analyze_scene, scene, st["t0"], st["t1"],
                                          min_hits=SIM_MIN_HITS)
            st["outs"] = outs
            render_results(results, outs, st["t0"], st["t1"], f'场景 · {scene["name"]}')
            sm = engine.scene_summary(outs)
            ui.notify(f'分析完成：{sm["rules"]} 条规则，{sm["total"]} 个异常点', color="positive")
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"分析失败：{type(exc).__name__}: {exc}", color="negative")
        finally:
            sim_btn.props(remove="loading")

    sim_btn.on("click", on_sim)
    get_btn.on("click", lambda: ui.notify("获取数据接口暂未接入，请使用模拟数据", color="warning"))


# ---------------------------------------------------------------------------
# 自定义检测
# ---------------------------------------------------------------------------
def _rule_row(r: dict, st: dict, on_change) -> None:
    """规则列表的一行：勾选框 + 名称 + 算法编号 + 关键参数 + 来源 + 编辑/删除。

    同一套渲染给 01「规则列表」和 02「检测规则」共用；``on_change`` 在编辑/删除
    落库后回调，由调用方刷新自己那份列表（01 还要通知 02，反之亦然）。
    """
    rid = r["rule_id"]
    st["selected"].setdefault(rid, True)
    with ui.row().classes("w-full items-center gap-2 no-wrap").style(
            "border:1px solid rgba(56,189,248,.14);border-radius:3px;padding:6px 10px;"
            "background:rgba(10,18,30,.55)"):
        cb = ui.checkbox(value=st["selected"].get(rid, True)).props("dense")
        cb.on_value_change(lambda e, k=rid: st["selected"].__setitem__(k, bool(e.value)))
        ui.label(r["name"]).classes("algo-name").style("min-width:120px")
        for a in r["algorithms"]:
            ui.html(f'<span class="algo-id">{a.get("algo_id")}</span>', sanitize=False)
        ui.label("；".join(
            f'{k}={v}' for a in r["algorithms"]
            for k, v in (a.get("params") or {}).items()
            if k not in ("min_samples", "max_points")
        )[:78]).classes("sy-hint").style("overflow:hidden;white-space:nowrap")
        ui.space()
        tag({"llm": "AI", "user": "用户", "preset": "预置"}.get(r["source"], r["source"]))
        ui.button(icon="edit", on_click=lambda x=r: rule_dialog(x, on_saved=lambda _: on_change())
                  ).props("flat dense round size=sm")
        ui.button(icon="delete_outline",
                  on_click=lambda x=r: (_delete_rule(x, st), on_change())
                  ).props("flat dense round size=sm color=negative")


def _delete_rule(r: dict, st: dict) -> None:
    store.delete_rule(r["rule_id"])
    st["selected"].pop(r["rule_id"], None)
    ui.notify("规则已删除", color="positive")


def _rule_sig(rules: list[dict]) -> str:
    """规则指纹：用于提示「列表相对数据库是否有未保存改动」。"""
    return repr([(r["rule_id"], r.get("updated_at")) for r in rules])


def custom_panel(container) -> None:
    from ..meta import get_def

    st: dict = {"code": "SPN1001", "t0": 0, "t1": 0, "period_s": SAMPLE_PERIOD_S,
                "stats": None, "view": None, "selected": {}, "last_llm": None,
                # 02「检测规则」列表的数据源：
                #   None  → 未读取，列表直接反映数据库（AI 生成/手工新建后即时可见）
                #   list  → 01 点过「读取规则」，列表是这份内存快照，点「保存规则」才写回库
                "draft": None, "draft_for": "", "draft_sig": ""}
    st["t0"], st["t1"] = _default_range()

    def code_of() -> str:
        return (code_in.value or "").strip().upper()

    def db_rules(code: str) -> list[dict]:
        return [r for r in store.list_rules() if r["code"] == code]

    def shown_rules() -> list[dict]:
        """02 列表当前展示的规则：读过后用内存快照，否则直接取库。"""
        return st["draft"] if st["draft"] is not None else db_rules(st["code"])

    # 列表渲染函数定义在 02 卡片里（后面才创建），01 里的事件需要用到的都走这个钩子，
    # 免得"函数体里引用了尚未赋值的自由变量"（建页面时 on_code() 就会跑）。
    refresh_list = lambda: None  # noqa: E731

    # ---- 01 数据 ----
    with card("01 数据准备", "输入测点代号与时间窗 → 读写该代号的规则（见 02 列表）→ 模拟生成原始数据"):
        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            code_in = ui.input("测点代号", value="SPN1001").props("outlined dense").classes("w-56")
            code_in.mark("input-code")
            _time_row(st)
        with ui.row().classes("w-full items-center gap-3"):
            info = ui.label("").classes("sy-hint")
            ui.space()
            read_btn = ui.button("读取规则", icon="download").props("flat").classes("sy-btn-ghost")
            read_btn.mark("btn-read-rules")
            read_btn.tooltip("按当前测点代号从数据库读取规则，刷新到 02 的「检测规则」列表")
            save_btn = ui.button("保存规则", icon="save").props("unelevated").classes("sy-btn-primary")
            save_btn.mark("btn-save-rules")
            save_btn.tooltip("把 02「检测规则」列表中展示的规则写回数据库")
            get_btn = ui.button("获取数据", icon="cloud_download").props("flat").classes("sy-btn-ghost")
            get_btn.tooltip("测点数据接口暂未接入（按需求本期仅做模拟数据流）")
            sim_btn = ui.button("模拟数据", icon="bolt").props("unelevated").classes("sy-btn-primary")
            sim_btn.mark("btn-sim-custom")
        profile = ui.row().classes("w-full items-center gap-2 flex-wrap")
        read_state = ui.label("").classes("sy-hint").style("font-size:11px;color:#5f7488")

        def render_state() -> None:
            """01 按钮旁的提示：当前是否已读取、读的是哪个代号。"""
            code = code_of()
            if st["draft"] is not None and st["draft_for"] == code:
                read_state.set_text(f"已读取 {code} 的 {len(st['draft'])} 条规则（见 02 检测规则列表）")
                read_state.style("font-size:11px;color:#86efac")
            else:
                read_state.set_text("未读取：02 列表当前直接反映数据库内容")
                read_state.style("font-size:11px;color:#5f7488")

        def _read(quiet: bool = False) -> None:
            """「读取规则」：把该代号的规则读进内存，刷新到 02 的「检测规则」列表。

            这里只是"重新从库里拉一份"，所以效果上等于把 02 列表按当前代号重刷一遍；
            之所以要缓存在 ``st["draft"]`` 里，是为了让「保存规则」有明确的"把列表写回库"语义。
            """
            code = code_of()
            if not code:
                ui.notify("请输入测点代号", color="warning")
                return
            st["draft_for"] = code
            st["draft"] = db_rules(code)
            st["draft_sig"] = _rule_sig(st["draft"])
            refresh_list()
            render_state()
            if not quiet:
                ui.notify(f"{code}：读取到 {len(st['draft'])} 条规则，已刷新到 02 检测规则列表",
                          color="positive" if st["draft"] else "warning")

        def _save() -> None:
            """「保存规则」：把 02「检测规则」列表当前展示的规则逐条写回数据库。"""
            code = code_of()
            if not code:
                ui.notify("请输入测点代号", color="warning")
                return
            rules = shown_rules()
            if not rules:
                ui.notify(f"{code} 的检测规则列表为空，无需保存", color="warning")
                return
            for r in rules:
                r["code"] = code
                store.upsert_rule(r)
            # 存完把缓存对齐成库里的最新状态，避免一直显示"有改动待保存"
            st["draft"] = db_rules(code)
            st["draft_for"] = code
            st["draft_sig"] = _rule_sig(st["draft"])
            refresh_list()
            render_state()
            ui.notify(f"已将 {len(st['draft'])} 条规则保存到数据库", color="positive")

        def on_code() -> None:
            code = code_of()
            d = get_def(code)
            info.set_text(f'{d.name}｜{VALUE_KINDS.get(d.kind, d.kind)}｜'
                          f'值域 [{d.low:g}, {d.high:g}] {d.unit}｜{d.bit_width} bit')
            # 代号换了：之前读取的规则不再属于当前代号，丢掉缓存，
            # 让 02 列表回到"直接反映数据库"的状态，避免把旧代号的规则存到新代号下
            if st["draft"] is not None and st["draft_for"] != code:
                st["draft"] = None
                st["draft_for"] = ""
                st["draft_sig"] = ""
            st["code"] = code
            render_state()
            refresh_list()

        code_in.on_value_change(lambda _: on_code())
        on_code()
        read_btn.on("click", lambda: _read())
        save_btn.on("click", _save)

        async def on_sim_raw() -> None:
            code = code_of()
            if not code:
                ui.notify("请输入测点代号", color="warning")
                return
            sim_btn.props("loading")
            try:
                series = await asyncio.to_thread(simulate_raw, code, st["t0"], st["t1"], seed=None)
                rec = store.save_series(code, series.t, series.v, int(series.t[0]),
                                        int(series.t[-1]), series.quality, source="sim")
                st["code"] = code
                st["period_s"] = series.period_ms() / 1000.0
                st["stats"] = store.dataset_stats(code, st["t0"], st["t1"])
                st["view"] = {"t": series.t, "v": series.v, "code": code,
                              "unit": series.meta.unit, "name": series.meta.name, "points": len(series)}
                profile.clear()
                with profile:
                    s = st["stats"] or {}
                    kpi("数据点", f'{s.get("points", 0):,}', "cyan")
                    kpi("最小值", s.get("min", "—"))
                    kpi("均值", s.get("mean", "—"))
                    kpi("最大值", s.get("max", "—"))
                    kpi("标准差", s.get("std", "—"))
                    kpi("p50", s.get("p50", "—"))
                    kpi("单拍差分 p99", s.get("diff_p99", "—"), "amber")
                    ui.space()
                    ui.label(f'{rec["path"]}').classes("sy-mono").style(
                        "font-size:10px;color:#5f7488;word-break:break-all")
                _reload_02()
                ui.notify(f"已生成 {len(series)} 个采样点并落盘", color="positive")
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"模拟失败：{type(exc).__name__}: {exc}", color="negative")
            finally:
                sim_btn.props(remove="loading")

        sim_btn.on("click", on_sim_raw)
        get_btn.on("click", lambda: ui.notify("获取数据接口暂未接入，请使用模拟数据", color="warning"))

    # ---- 02 规则 ----
    with card("02 规则生成", "本地大模型读取数据画像 → 选择算法 → 配置参数 → 生成规则（可修改后保存）"):
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            status_chip = ui.html("", sanitize=False)
            ui.label(f"{runtime.llm_status()[1]}").classes("sy-hint")
            cfg = ui.label(f"URL {llm.LLM_BASE_URL}｜model {llm.LLM_MODEL}｜key "
                           f"{llm.LLM_API_KEY[:4]}***").classes("sy-mono").style(
                "font-size:10px;color:#4d6379")
            ui.space()
            test_btn = ui.button("测试连接", icon="wifi_tethering").props("flat dense").classes("sy-btn-ghost")
            local_only = ui.switch("仅用本地启发式", value=False).props("dense")
            gen_btn = ui.button("AI 生成规则", icon="auto_awesome").props("unelevated").classes("sy-btn-primary")
            gen_btn.mark("btn-gen-rules")

        with ui.expansion("模型连接配置", icon="settings_ethernet").classes("w-full").style(
                "border:1px solid rgba(56,189,248,.14);border-radius:3px;padding:0 10px"):
            with ui.grid(columns=3).classes("w-full gap-3"):
                url_in = ui.input("服务地址", value=llm.LLM_BASE_URL).classes("w-full").props("outlined dense")
                model_in = ui.input("模型名", value=llm.LLM_MODEL).classes("w-full").props("outlined dense")
                key_in = ui.input("API Key", value=llm.LLM_API_KEY,
                                  password=True, password_toggle_button=True).classes("w-full").props("outlined dense")
            with ui.row().classes("items-center gap-2"):
                ui.label("修改仅本次运行生效（生产环境请用 VIGIL_LLM_URL / KEY / MODEL 环境变量固化）。"
                         ).classes("sy-hint")

                async def save_cfg() -> None:
                    llm.LLM_BASE_URL = (url_in.value or "").strip() or llm.LLM_BASE_URL
                    llm.LLM_MODEL = (model_in.value or "").strip() or llm.LLM_MODEL
                    llm.LLM_API_KEY = (key_in.value or "").strip() or llm.LLM_API_KEY
                    ok, msg = await asyncio.to_thread(runtime.probe_llm)
                    refresh_status(ok, msg)
                    ui.notify("连接正常" if ok else f"连接失败：{msg}",
                              color="positive" if ok else "negative")

                ui.button("保存并测试", icon="cable", on_click=save_cfg).props("flat dense").classes("sy-btn-ghost")

        def refresh_status(ok: bool, msg: str) -> None:
            dot = "sy-dot" if ok else "sy-dot amber"
            status_chip.content = (f'<span class="sy-chip"><span class="{dot}"></span>本地大模型 '
                                   f'<b>{"在线" if ok else "离线"}</b> · '
                                   f'{html_lib.escape(msg[:40])}</span>')
            cfg.set_text(f"URL {llm.LLM_BASE_URL}｜model {llm.LLM_MODEL}")

        refresh_status(*runtime.llm_status())

        async def on_test() -> None:
            test_btn.props("loading")
            ok, msg = await asyncio.to_thread(runtime.probe_llm)
            test_btn.props(remove="loading")
            refresh_status(ok, msg)
            ui.notify("连接正常" if ok else f"连接失败：{msg}", color="positive" if ok else "negative")

        test_btn.on("click", on_test)

        analysis = ui.label("").classes("sy-hint").style("color:#9fd6ea")
        gen_msg = ui.label("").classes("sy-hint")

        async def on_gen() -> None:
            if st["stats"] is None:
                ui.notify("请先在 01 中生成数据", color="warning")
                return
            gen_btn.props("loading")
            d = get_def(st["code"])
            meta = {"code": st["code"], "name": d.name, "unit": d.unit, "kind": d.kind,
                    "low": d.low, "high": d.high, "bit_width": d.bit_width, "quantum": d.quantum}
            try:
                res = await asyncio.to_thread(llm.generate, st["code"], meta, st["stats"],
                                              st["period_s"], not local_only.value)
                st["last_llm"] = res
                analysis.set_text(f'『{res.analysis}』' if res.analysis else "")
                # 生成的规则直接入库（source=llm），便于编辑与复用
                saved = []
                for r in res.rules:
                    r.setdefault("note", r.get("note", ""))
                    saved.append(store.upsert_rule(r, new=True))
                gen_msg.set_text(f'{res.message}｜已保存 {len(saved)} 条规则到规则库')
                gen_msg.style("color:#86efac" if res.mode == "llm" else "#fcd34d")
                _reload_02()
                ui.notify(f"生成 {len(saved)} 条规则" + ("（本地启发式）" if res.mode != "llm" else ""),
                          color="positive" if res.mode == "llm" else "warning")
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"生成失败：{type(exc).__name__}: {exc}", color="negative")
            finally:
                gen_btn.props(remove="loading")

        gen_btn.on("click", on_gen)

        ui.separator()
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("检测规则（勾选参与本轮分析）").classes("sy-label")
            list_state = ui.label("").classes("sy-hint").style("font-size:11px")
            ui.space()
            ui.button("手工新建规则", icon="add",
                      on_click=lambda: rule_dialog({"code": st["code"]},
                                                   on_saved=lambda _: _reload_02())
                      ).props("flat dense").classes("sy-btn-ghost")
            ui.button("刷新", icon="refresh", on_click=lambda: _reload_02()).props("flat dense round")
        rules_list = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def rules_list() -> None:
            """唯一的规则列表：01「读取规则」与 02「AI 生成/手工新建」都刷这一份。"""
            code = st["code"]
            rules = shown_rules()
            dirty = (st["draft"] is not None and st["draft_for"] == code
                     and _rule_sig(st["draft"]) != st["draft_sig"])
            if not rules:
                list_state.set_text(f"{code} · 0 条")
                list_state.style("font-size:11px;color:#5f7488")
                empty(f"{code} 还没有规则：点「AI 生成规则」或「手工新建规则」")
                return
            list_state.set_text(f"{code} · {len(rules)} 条" +
                                ("｜有改动待保存，去 01 点「保存规则」" if dirty else ""))
            list_state.style(f"font-size:11px;color:{'#fcd34d' if dirty else '#5f7488'}")
            for r in rules:
                _rule_row(r, st, _reload_02)

        def _reload_02() -> None:
            """规则库变了（编辑/删除/手工新建）——若 01 读过规则，同步刷新内存快照。"""
            code = st["code"]
            if st["draft"] is not None and st["draft_for"] == code:
                st["draft"] = db_rules(code)
                st["draft_sig"] = _rule_sig(st["draft"])
            rules_list.refresh()
            render_state()

        def _refresh_list() -> None:
            rules_list.refresh()

        refresh_list = _refresh_list
        rules_list()

    # ---- 03 分析 ----
    with card("03 检测执行", "按勾选的规则逐条分析，输出异常点表格与标注曲线（每条规则一个分页）"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("数据来源：01 中模拟生成的 Parquet 数据集").classes("sy-hint")
            ui.space()
            run_btn = ui.button("开始分析", icon="play_arrow").props("unelevated").classes("sy-btn-primary")
            run_btn.mark("btn-run")
        results = ui.column().classes("w-full gap-3")

        async def on_run() -> None:
            code = st["code"]
            rules = [r for r in store.list_rules() if r["code"] == code
                     and st["selected"].get(r["rule_id"], True)]
            if not rules:
                ui.notify("没有勾选任何规则", color="warning")
                return
            if st["stats"] is None:
                ui.notify("请先在 01 中生成数据", color="warning")
                return
            run_btn.props("loading")
            try:
                outs = await asyncio.to_thread(engine.analyze_rules, rules, st["t0"], st["t1"])
                render_results(results, outs, st["t0"], st["t1"], f"{code} · 自定义检测")
                ui.notify(f"分析完成：{len(outs)} 条规则，{sum(len(o.points) for o in outs)} 个异常点",
                          color="positive")
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"分析失败：{type(exc).__name__}: {exc}", color="negative")
            finally:
                run_btn.props(remove="loading")

        run_btn.on("click", on_run)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def page() -> None:
    preset_scene = None
    try:
        preset_scene = ui.context.client.request.query_params.get("scene")
    except Exception:  # noqa: BLE001
        pass

    with shell("/detect", llm_status=runtime.llm_status()):
        with ui.row().classes("w-full items-end gap-3"):
            with ui.column().classes("gap-0"):
                ui.label("异常检测").style("font-size:20px;font-weight:700;letter-spacing:2px;color:#eaf6ff")
                ui.label("分组场景批量检测，或对单个测点代号用大模型现场生成判据并分析").classes("sy-hint")
            ui.space()
            with ui.row().classes("gap-2 items-center"):
                ui.html('<span class="sy-chip"><span class="sy-dot"></span>引擎 <b>'
                        f'{len(alg.all_algorithms())} 算法</b></span>', sanitize=False)
                ui.html('<span class="sy-chip">数据格式 <b>Parquet / SNAPPY</b></span>', sanitize=False)

        with ui.tabs().classes("w-full").style("margin-top:4px") as tabs:
            t1 = ui.tab("分组场景", icon="layers")
            t2 = ui.tab("自定义检测", icon="science")
        with ui.tab_panels(tabs, value=t1).classes("w-full bg-transparent"):
            with ui.tab_panel(t1).classes("px-0"):
                scene_holder = ui.column().classes("w-full gap-3")
                scene_panel(scene_holder, preset_scene)
            with ui.tab_panel(t2).classes("px-0"):
                custom_holder = ui.column().classes("w-full gap-3")
                custom_panel(custom_holder)
