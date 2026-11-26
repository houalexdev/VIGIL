"""规则中心：算法库 / 规则库 / 分组场景。"""
from __future__ import annotations

import json

from nicegui import ui

import algorithm as alg
from algorithm.base import COMMON_SPECS

from .. import custom_algo, presets, store, yamls
from ..config import LEVEL_CN, VALUE_KINDS
from ..meta import catalog_rows, get_def
from .common import (algo_options, algo_summary, card, empty, kind_options,
                     param_fields, shell, tag)

SOURCE_LABEL = {"preset": "预置", "user": "用户", "llm": "AI", "custom": "自定义"}


# ===========================================================================
# 算法库
# ===========================================================================
@ui.refreshable
def algo_grid(query: str = "", scope: str = "全部") -> None:
    rows = []
    for cls in alg.all_algorithms():
        if query and query.lower() not in (cls.ALGO_ID + cls.ALGO_NAME + cls.CATEGORY).lower():
            continue
        is_user = custom_algo.is_custom(cls.ALGO_ID)
        if scope == "内置" and is_user:
            continue
        if scope == "自定义" and not is_user:
            continue
        rows.append(cls)

    if not rows:
        empty("没有匹配的算法")
        return
    with ui.grid(columns=3).classes("w-full gap-3"):
        for cls in rows:
            is_user = custom_algo.is_custom(cls.ALGO_ID)
            with ui.column().classes("algo-card gap-2"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.html(f'<span class="algo-id">{cls.ALGO_ID}</span>', sanitize=False)
                    ui.label(cls.ALGO_NAME).classes("algo-name")
                    ui.space()
                    if is_user:
                        tag("自定义", "user")
                    elif not cls.IS_DETECTOR:
                        tag("基础设施", "warn")
                with ui.row().classes("gap-1 flex-wrap"):
                    tag(f"L{cls.LAYER[1]}", "cyan")
                    tag(cls.CATEGORY)
                    tag(cls.HIT_TYPE.value)
                ui.label(" / ".join(VALUE_KINDS[k.value] for k in cls.SUPPORTED_KINDS)
                         or "—").classes("sy-hint")
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(f"{len(cls.param_specs(include_common=False))} 个参数").classes("sy-hint")
                    ui.space()
                    ui.button("详情", on_click=lambda c=cls: algo_detail(c)).props("flat dense").classes("sy-btn-ghost")
                    if is_user:
                        ui.button(icon="edit", on_click=lambda c=cls: algo_editor(c.ALGO_ID)).props("flat dense round size=sm")
                        ui.button(icon="delete_outline",
                                  on_click=lambda c=cls: confirm_delete_algo(c)).props("flat dense round size=sm color=negative")


def algo_detail(cls) -> None:
    with ui.dialog() as dlg, ui.card().classes("w-[820px] max-w-[94vw] gap-3").style(
            "background:#0a1220;border:1px solid var(--sy-line-2)"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.html(f'<span class="algo-id">{cls.ALGO_ID}</span>', sanitize=False)
            ui.label(cls.ALGO_NAME).classes("algo-name")
            ui.space()
            tag(f"v{cls.VERSION}")
            tag(cls.CATEGORY, "cyan")
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")
        ui.label(cls.RATIONALE or "—").classes("sy-hint")
        ui.label(f"典型判据：{cls.USE_CASES or '—'}").classes("sy-hint")
        cols = [
            {"name": "name", "label": "参数", "field": "name", "align": "left"},
            {"name": "label", "label": "含义", "field": "label", "align": "left"},
            {"name": "type", "label": "类型", "field": "type", "align": "left"},
            {"name": "default", "label": "默认", "field": "default", "align": "left"},
            {"name": "range", "label": "范围", "field": "range", "align": "left"},
            {"name": "help", "label": "说明", "field": "help", "align": "left"},
        ]
        rows = []
        for s in cls.param_specs():
            rng = ""
            if s.choices:
                rng = " / ".join(str(c) for c in s.choices)
            elif s.minimum is not None or s.maximum is not None:
                rng = f"[{s.minimum if s.minimum is not None else '-∞'}, {s.maximum if s.maximum is not None else '+∞'}]"
            rows.append({"name": s.name, "label": s.label, "type": s.type,
                         "default": "—" if s.default is None else str(s.default),
                         "range": rng, "help": s.help})
        ui.table(columns=cols, rows=rows, row_key="name",
                 pagination={"rowsPerPage": 12}).classes("w-full").props("flat dense")
        ui.button("关闭", on_click=dlg.close).props("flat").classes("sy-btn-ghost self-end")
    dlg.open()


def confirm_delete_algo(cls) -> None:
    with ui.dialog() as dlg, ui.card().classes("gap-3").style("background:#0a1220;border:1px solid var(--sy-line-2)"):
        ui.label(f"删除自定义算法 {cls.ALGO_ID} {cls.ALGO_NAME}？").classes("sy-card-title")
        ui.label("源码文件会一并删除；已引用该算法的规则将变为「未注册算法」。").classes("sy-hint")

        def do() -> None:
            err = custom_algo.delete_algo(cls.ALGO_ID)
            dlg.close()
            if err:
                ui.notify(err, color="negative")
            else:
                ui.notify(f"{cls.ALGO_ID} 已删除", color="positive")
            algo_grid.refresh()

        with ui.row().classes("self-end gap-2"):
            ui.button("取消", on_click=dlg.close).props("flat")
            ui.button("删除", on_click=do).props("unelevated color=negative")
    dlg.open()


def algo_editor(algo_id: str | None = None) -> None:
    is_new = algo_id is None
    src = custom_algo.new_template() if is_new else custom_algo.read_source(algo_id)
    with ui.dialog() as dlg, ui.card().classes(
            "w-[980px] max-w-[96vw] max-h-[94vh] gap-3 overflow-auto").style(
            "background:#0a1220;border:1px solid var(--sy-line-2)"):
        with ui.row().classes("w-full items-center gap-2 shrink-0"):
            ui.label("新增自定义算法" if is_new else f"编辑 {algo_id}").classes("sy-card-title")
            ui.space()
            ui.label("继承 BaseAlgorithm，实现 detect(ctx, p) 即可").classes("sy-hint")
        editor = ui.textarea(value=src).classes("w-full").props("outlined").style(
            "width:100%;font-family:'JetBrains Mono',ui-monospace;font-size:12px;"
            "line-height:1.55;height:min(64vh,720px);min-height:420px;resize:vertical")
        msg = ui.label("").classes("sy-hint shrink-0")

        def check() -> None:
            cls, err = custom_algo.validate_source(editor.value)
            if err:
                msg.set_text(f"✗ {err}")
                msg.style("color:#fca5a5")
                return
            ok, note = custom_algo.smoke_test(cls)
            msg.set_text(("✓ " if ok else "△ ") + f"{cls.ALGO_ID} {cls.ALGO_NAME} · {note}")
            msg.style("color:#86efac" if ok else "#fcd34d")

        def save() -> None:
            aid, err = custom_algo.save_algo(algo_id or "", "", editor.value)
            if err and not aid:
                msg.set_text(f"✗ {err}")
                msg.style("color:#fca5a5")
                return
            dlg.close()
            ui.notify(f"{aid} 已保存" + (f"（{err}）" if err else ""),
                      color="positive" if not err else "warning")
            algo_grid.refresh()

        with ui.row().classes("w-full items-center gap-2 self-end shrink-0"):
            ui.button("校验", on_click=check).props("flat").classes("sy-btn-ghost")
            ui.button("模板重置", on_click=lambda: editor.set_value(custom_algo.new_template())).props("flat")
            ui.space()
            ui.button("取消", on_click=dlg.close).props("flat")
            ui.button("保存", icon="save", on_click=save).props("unelevated").classes("sy-btn-primary")
    dlg.open()


# ===========================================================================
# 规则库
# ===========================================================================
@ui.refreshable
def rule_table(query: str = "") -> None:
    rules = store.list_rules()
    if query:
        q = query.lower()
        rules = [r for r in rules if q in (r["name"] + r["code"] + (r["code_name"] or "")).lower()]
    if not rules:
        empty("规则库为空，点击「新建规则」开始配置")
        return
    rows = [{
        "rule_id": r["rule_id"], "name": r["name"], "code": r["code"],
        "code_name": r["code_name"] or "",
        "kind": VALUE_KINDS.get(r["kind"], r["kind"]),
        "algos": " + ".join(a.get("algo_id", "?") for a in r["algorithms"]),
        "algo_names": " / ".join(a.get("algo_id", "") for a in r["algorithms"]),
        "source": SOURCE_LABEL.get(r["source"], r["source"]),
        "updated_at": (r["updated_at"] or "")[5:16],
        "rule": r,
    } for r in rules]
    cols = [
        {"name": "name", "label": "规则名称", "field": "name", "align": "left", "sortable": True},
        {"name": "code", "label": "测点代号", "field": "code", "align": "left", "sortable": True},
        {"name": "code_name", "label": "测点名称", "field": "code_name", "align": "left"},
        {"name": "kind", "label": "值域", "field": "kind", "align": "left"},
        {"name": "algos", "label": "算法组合", "field": "algos", "align": "left"},
        {"name": "source", "label": "来源", "field": "source", "align": "left"},
        {"name": "updated_at", "label": "更新", "field": "updated_at", "align": "left"},
        {"name": "ops", "label": "操作", "field": "rule_id", "align": "right"},
    ]
    table = ui.table(columns=cols, rows=rows, row_key="rule_id",
                     pagination={"rowsPerPage": 12, "sortBy": "name"}).classes("w-full").props("flat dense")
    table.add_slot("body-cell-algos", """
        <q-td :props="props">
          <span v-for="a in props.row.algos.split(' + ')" :key="a"
                class="algo-id" style="margin-right:4px">{{ a }}</span>
        </q-td>""")
    # 按钮不直接在模板里绑事件：表格插槽是 Vue 模板，作用域里拿不到 NiceGUI 的
    # emitEvent，而 $parent.$emit 又只走 Vue 组件树、到不了 table.on() 监听的事件总线。
    # 所以给按钮打 data 标记 + 唯一的 class，由 page() 用原生 JS 事件委托转发到服务端
    # （see bind_rule_actions）。这样点击一定会有响应。
    table.add_slot("body-cell-ops", """
        <q-td :props="props" class="text-right">
          <q-btn flat dense round size="sm" icon="edit"
                 class="rc-edit" :data-rid="props.row.rule_id" />
          <q-btn flat dense round size="sm" icon="delete_outline" color="negative"
                 class="rc-del" :data-rid="props.row.rule_id" />
        </q-td>""")
    # 双击行打开编辑（保持原有便利）
    table.on("rowDblclick", lambda e: open_rule_by_id(_row_id(e.args)), ["row"])


def _row_id(args) -> str:
    """从 rowDblclick 的参数里取出 rule_id。"""
    if isinstance(args, list):
        args = args[0] if args else {}
    if not isinstance(args, dict):
        return ""
    row = args.get("row") if isinstance(args.get("row"), dict) else args
    return str((row or {}).get("rule_id") or "")


def open_rule_by_id(rule_id: str) -> None:
    if not rule_id:
        return
    rule = next((r for r in store.list_rules() if r["rule_id"] == rule_id), None)
    if rule is None:
        ui.notify("规则不存在，可能已被删除", color="warning")
        rule_table.refresh()
        return
    rule_dialog(rule)


def delete_rule_by_id(rule_id: str) -> None:
    if not rule_id:
        return
    rule = next((r for r in store.list_rules() if r["rule_id"] == rule_id), None)
    if rule is None:
        ui.notify("规则不存在，可能已被删除", color="warning")
        rule_table.refresh()
        return
    confirm_delete_rule(rule)


# 事件委托：在 document 上捕获一次点击，命中 .rc-edit / .rc-del 就上报 rule_id。
# 用 document 级委托是因为表格翻页 / 搜索刷新后按钮会被重建，逐按钮绑定会失效。
_RULE_ACTIONS_JS = """
(() => {
  if (window.__syRuleActionsBound) return;
  window.__syRuleActionsBound = true;
  document.addEventListener('click', (ev) => {
    const btn = ev.target && ev.target.closest && ev.target.closest('.rc-edit, .rc-del');
    if (!btn) return;
    const rid = btn.dataset ? btn.dataset.rid : null;
    if (!rid) return;
    const action = btn.classList.contains('rc-del') ? 'rule-delete' : 'rule-edit';
    if (window.emitEvent) { window.emitEvent(action, rid); return; }
    const root = document.getElementById('app') || document.querySelector('#app');
    const c = (window.Vue && window.Vue.getCurrentInstance && window.Vue.getCurrentInstance())
           || (root && root.__vue_app__ && root.__vue_app__._instance);
    if (c && c.proxy && c.proxy.$emit) c.proxy.$emit(action, rid);
  }, true);
})();
"""


def bind_rule_actions() -> None:
    """把规则库行内按钮的点击接到服务端（页面级只需绑定一次）。"""
    ui.run_javascript(_RULE_ACTIONS_JS)
    ui.on("rule-edit", lambda e: open_rule_by_id(str(e.args)))
    ui.on("rule-delete", lambda e: delete_rule_by_id(str(e.args)))


def confirm_delete_rule(rule: dict) -> None:
    with ui.dialog() as dlg, ui.card().classes("gap-3").style("background:#0a1220;border:1px solid var(--sy-line-2)"):
        ui.label(f"删除规则「{rule['name']}」？").classes("sy-card-title")
        ui.label("分组场景中对该规则的引用会同时移除（不会删除数据集）。").classes("sy-hint")

        def do() -> None:
            store.delete_rule(rule["rule_id"])
            dlg.close()
            ui.notify("规则已删除", color="positive")
            _refresh_all()

        with ui.row().classes("self-end gap-2"):
            ui.button("取消", on_click=dlg.close).props("flat")
            ui.button("删除", on_click=do).props("unelevated color=negative")
    dlg.open()


def _refresh_rules() -> None:
    try:
        rule_table.refresh()
    except Exception:  # noqa: BLE001  — 该 refreshable 还没渲染过就忽略
        pass


def _refresh_scenes() -> None:
    try:
        scene_grid.refresh()
    except Exception:  # noqa: BLE001
        pass


def _refresh_all() -> None:
    """刷新规则表与场景网格：任一 refreshable 未渲染时不能连带吞掉整段回调。"""
    _refresh_rules()
    _refresh_scenes()


def rule_dialog(rule: dict | None = None, on_saved=None) -> None:
    """规则编辑器：元数据 + 若干算法（每个算法一套参数）。"""
    if isinstance(rule, list):  # 兜底：历史上事件参数曾被当成列表传进来
        rule = rule[0] if rule else None
    if rule is not None and not isinstance(rule, dict):
        rule = None
    is_new = rule is None
    base = dict(rule or {})
    state: dict = {"algos": [dict(a) for a in (base.get("algorithms") or [])],
                   "editing": None, "getter": None}

    with ui.dialog() as dlg, ui.card().classes("w-[980px] max-w-[96vw] gap-0").style(
            "background:#0a1220;border:1px solid var(--sy-line-2);padding:18px 20px"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("新建规则" if is_new else f"编辑规则 · {rule['rule_id']}").classes("sy-card-title")
            ui.space()
            src = SOURCE_LABEL.get(base.get("source", "user"), "用户")
            tag(f"来源 {src}")
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")

        ui.label("规则 = 一个测点代号 + 若干算法（每个算法独立配参）。分析时按算法逐条执行，异常点合并展示。"
                 ).classes("sy-hint").style("margin:6px 0 10px")

        with ui.grid(columns=4).classes("w-full gap-3"):
            name_in = ui.input("规则名称", value=base.get("name", "")).classes("w-full").props("outlined dense")
            name_in.mark("in-rule-name")
            code_in = ui.input("测点代号", value=base.get("code", ""),
                               placeholder="如 SPN1001").classes("w-full").props("outlined dense")
            code_in.mark("in-rule-code")
            kind_in = ui.select(kind_options(), label="值域形态",
                                value=base.get("kind", "continuous")).classes("w-full").props("outlined dense")
            unit_in = ui.input("单位", value=base.get("unit", "")).classes("w-full").props("outlined dense")
            low_in = ui.number("正常下限", value=base.get("low")).classes("w-full").props("outlined dense")
            high_in = ui.number("正常上限", value=base.get("high")).classes("w-full").props("outlined dense")
            bw_in = ui.number("位宽(bit)", value=base.get("bit_width", 16), precision=0).classes("w-full").props("outlined dense")
            q_in = ui.number("1 LSB", value=base.get("quantum", 0)).classes("w-full").props("outlined dense")
            note_in = ui.input("规则说明", value=base.get("note", "")).classes("w-full col-span-4").props("outlined dense")

        code_in.props('hint="代号决定测点元数据；未登记代号按连续量默认值域"')
        name_auto = ui.label("").classes("sy-hint")

        def on_code() -> None:
            d = get_def((code_in.value or "").strip())
            if not is_new:
                return
            if code_in.value and not name_in.value:
                name_in.set_value("")
            if kind_in.value == "continuous" and d.kind != "continuous":
                kind_in.set_value(d.kind)
            low_in.set_value(d.low)
            high_in.set_value(d.high)
            bw_in.set_value(d.bit_width)
            q_in.set_value(d.quantum)
            unit_in.set_value(d.unit)
            name_auto.set_text(f"{d.name}｜{d.note}")

        code_in.on_value_change(lambda _: on_code())

        ui.separator()
        ui.label("算法组合").classes("sy-label").style("margin-top:8px")
        algo_list = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def render_algos() -> None:
            algo_list.clear()
            if state.get("count_el") is not None:
                state["count_el"].set_text(f"已配置 {len(state['algos'])} 个算法")
            with algo_list:
                if not state["algos"]:
                    ui.label("尚未添加算法 —— 一条规则至少要有一个算法才能真正执行检测").classes("sy-hint")
                for i, a in enumerate(state["algos"]):
                    with ui.row().classes("w-full items-center gap-2 no-wrap").style(
                            "border:1px solid rgba(56,189,248,.14);border-radius:3px;padding:7px 10px;"
                            "background:rgba(10,18,30,.6)"):
                        ui.html(f'<span class="algo-id">{a["algo_id"]}</span>', sanitize=False)
                        try:
                            nm = alg.get(a["algo_id"]).ALGO_NAME
                        except KeyError:
                            nm = "未注册算法"
                        ui.label(nm).classes("algo-name")
                        ui.label(algo_summary(a.get("params") or {}, a["algo_id"])).classes("sy-hint").style(
                            "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:420px")
                        ui.space()
                        ui.button(icon="edit", on_click=lambda i=i: start_edit(i)).props("flat dense round size=sm")
                        ui.button(icon="close", on_click=lambda i=i: remove_algo(i)).props(
                            "flat dense round size=sm color=negative")

        def remove_algo(i: int) -> None:
            state["algos"].pop(i)
            render_algos.refresh()

        add_box = ui.column().classes("w-full gap-2").style(
            "border:1px dashed rgba(56,189,248,.22);border-radius:3px;padding:10px")
        with add_box:
            with ui.row().classes("w-full items-center gap-2"):
                sel = ui.select({}, label="选择算法", value=None).classes("grow").props("outlined dense")
                sel.mark("sel-algo")
                ui.label("仅列出适配当前值域的算法").classes("sy-hint")
            form_holder = ui.column().classes("w-full")
            with ui.row().classes("w-full items-center gap-2"):
                add_btn = ui.button("添加算法", icon="add").props("unelevated dense").classes("sy-btn-primary")
                add_btn.mark("btn-add-algo")
                cancel_edit = ui.button("取消编辑", on_click=lambda: reset_edit()).props("flat dense").classes("sy-btn-ghost")
                cancel_edit.set_visibility(False)

        def start_edit(i: int) -> None:
            state["editing"] = i
            a = state["algos"][i]
            sel.set_value(a["algo_id"])
            build_form(a["algo_id"], a.get("params") or {})
            add_btn.set_text("更新该算法")
            cancel_edit.set_visibility(True)

        def reset_edit() -> None:
            state["editing"] = None
            form_holder.clear()
            state["getter"] = None
            add_btn.set_text("添加算法")
            cancel_edit.set_visibility(False)

        def build_form(algo_id: str, values: dict) -> None:
            form_holder.clear()
            try:
                cls = alg.get(algo_id)
            except KeyError:
                state["getter"] = None
                return
            with form_holder:
                ui.label(f"{cls.ALGO_NAME} · {cls.HIT_TYPE.value}").classes("sy-hint").style("margin-bottom:4px")
                widgets, getter = param_fields(cls.param_specs(), values)
            state["getter"] = getter

        def on_algo_change() -> None:
            if state["editing"] is not None:
                reset_edit()
            if sel.value:
                build_form(sel.value, {})

        sel.on_value_change(lambda _: on_algo_change())
        add_btn.on("click", lambda: commit_algo())

        def commit_algo() -> None:
            if not sel.value:
                ui.notify("先选择算法", color="warning")
                return
            if state["getter"] is None:
                build_form(sel.value, {})
            try:
                params = state["getter"]() if state["getter"] else {}
            except ValueError as exc:
                ui.notify(f"参数有误：{exc}", color="negative")
                return
            if state["editing"] is not None:
                state["algos"][state["editing"]] = {"algo_id": sel.value, "params": params}
            else:
                state["algos"].append({"algo_id": sel.value, "params": params})
            reset_edit()
            render_algos.refresh()

        def save() -> None:
            if not name_in.value.strip():
                ui.notify("请填写规则名称", color="warning")
                return
            if not code_in.value.strip():
                ui.notify("请填写测点代号", color="warning")
                return
            if not state["algos"]:
                ui.notify("至少添加一个算法", color="warning")
                return
            payload = {
                "rule_id": base.get("rule_id"),
                "name": name_in.value.strip(),
                "code": code_in.value.strip().upper(),
                "code_name": get_def(code_in.value.strip().upper()).name,
                "unit": unit_in.value or "",
                "kind": kind_in.value or "continuous",
                "low": low_in.value, "high": high_in.value,
                "bit_width": int(bw_in.value or 16),
                "quantum": float(q_in.value or 0),
                "note": note_in.value or "",
                "algorithms": state["algos"],
                "source": base.get("source", "user") if not is_new else "user",
            }
            try:
                rec = store.upsert_rule(payload, new=is_new)
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"保存失败：{exc}", color="negative")
                return
            dlg.close()
            ui.notify(f"规则「{rec['name']}」已保存", color="positive")
            _refresh_all()
            if on_saved:
                on_saved(rec)

        with ui.row().classes("w-full items-center gap-2").style("margin-top:14px"):
            state["count_el"] = ui.label(f"已配置 {len(state['algos'])} 个算法").classes("sy-hint")
            ui.space()
            ui.button("取消", on_click=dlg.close).props("flat")
            save_btn = ui.button("保存规则", icon="save", on_click=save).props("unelevated").classes("sy-btn-primary")
            save_btn.mark("btn-save-rule")

    def post_open() -> None:
        # 编辑已有规则时预填算法列表
        sel.set_options(algo_options(kind_in.value))
        render_algos()
        on_code()

    kind_in.on_value_change(lambda _: sel.set_options(algo_options(kind_in.value)))
    dlg.open()
    post_open()


# ===========================================================================
# 分组场景
# ===========================================================================
@ui.refreshable
def scene_grid() -> None:
    scenes = store.list_scenes()
    if not scenes:
        empty("还没有分组场景。场景 = 若干规则的打包，用于一次跑完一批判据")
        return
    with ui.grid(columns=2).classes("w-full gap-3"):
        for s in scenes:
            rules = store.get_rules(s["rule_ids"])
            with ui.column().classes("algo-card gap-2"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.icon("layers", size="16px").style("color:#22d3ee")
                    ui.label(s["name"]).classes("algo-name").style("font-size:14px")
                    if s.get("source") == "preset":
                        tag("预置", "cyan")
                    ui.space()
                    ui.label(f"{len(rules)} 条规则").classes("sy-chip")
                    ui.button(icon="edit", on_click=lambda x=s: scene_dialog(x)).props("flat dense round size=sm")
                    ui.button(icon="delete_outline", on_click=lambda x=s: confirm_delete_scene(x)).props(
                        "flat dense round size=sm color=negative")
                ui.label(s.get("desc") or "—").classes("sy-hint")
                with ui.row().classes("gap-1 flex-wrap"):
                    for r in rules[:8]:
                        tag(f"{r['code']}", "cyan")
                    if len(rules) > 8:
                        tag(f"+{len(rules) - 8}")
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(", ".join(r["name"] for r in rules[:4])[:58] + ("…" if len(rules) > 4 else "")
                             ).classes("sy-hint")
                    ui.space()
                    ui.button("去检测", icon="play_arrow",
                              on_click=lambda x=s: ui.navigate.to(f"/detect?scene={x['scene_id']}")
                              ).props("flat dense").classes("sy-btn-ghost")


def confirm_delete_scene(scene: dict) -> None:
    with ui.dialog() as dlg, ui.card().classes("gap-3").style("background:#0a1220;border:1px solid var(--sy-line-2)"):
        ui.label(f"删除分组场景「{scene['name']}」？").classes("sy-card-title")
        ui.label("场景内的规则不会被删除，仍保留在规则库中。").classes("sy-hint")

        def do() -> None:
            store.delete_scene(scene["scene_id"])
            dlg.close()
            ui.notify("场景已删除", color="positive")
            scene_grid.refresh()

        with ui.row().classes("self-end gap-2"):
            ui.button("取消", on_click=dlg.close).props("flat")
            ui.button("删除", on_click=do).props("unelevated color=negative")
    dlg.open()


def scene_dialog(scene: dict | None = None) -> None:
    is_new = scene is None
    base = dict(scene or {})
    rules = store.list_rules()
    options = {r["rule_id"]: f"{r['code']} · {r['name']}" for r in rules}
    with ui.dialog() as dlg, ui.card().classes("w-[720px] max-w-[94vw] gap-3").style(
            "background:#0a1220;border:1px solid var(--sy-line-2)"):
        ui.label("新建分组场景" if is_new else f"编辑场景 · {base['name']}").classes("sy-card-title")
        name_in = ui.input("场景名称", value=base.get("name", "")).classes("w-full").props("outlined dense")
        desc_in = ui.textarea("场景说明", value=base.get("desc", "")).classes("w-full").props("outlined dense")
        pick = ui.select(options, label="包含规则", multiple=True,
                         value=base.get("rule_ids") or []).classes("w-full").props(
            "outlined dense use-chips options-dense")
        ui.label("提示：规则顺序决定检测与分页顺序；场景可包含任意多条规则").classes("sy-hint")

        def save() -> None:
            if not name_in.value.strip():
                ui.notify("请填写场景名称", color="warning")
                return
            rec = store.upsert_scene({
                "scene_id": base.get("scene_id") or None,
                "name": name_in.value.strip(),
                "desc": desc_in.value or "",
                "rule_ids": pick.value or [],
                "source": base.get("source", "user"),
            }, new=is_new)
            dlg.close()
            ui.notify(f"场景「{rec['name']}」已保存", color="positive")
            scene_grid.refresh()

        with ui.row().classes("self-end gap-2"):
            ui.button("取消", on_click=dlg.close).props("flat")
            ui.button("保存场景", icon="save", on_click=save).props("unelevated").classes("sy-btn-primary")
    dlg.open()


# ===========================================================================
# 页面
# ===========================================================================
def page() -> None:
    from ..runtime import llm_status

    with shell("/rules", llm_status=llm_status()):
        with ui.row().classes("w-full items-end gap-3"):
            with ui.column().classes("gap-0"):
                ui.label("规则中心").style("font-size:20px;font-weight:700;letter-spacing:2px;color:#eaf6ff")
                ui.label("算法 → 规则 → 分组场景：三级资产在这里装配成可执行的判据体系").classes("sy-hint")
            ui.space()
            with ui.row().classes("gap-2"):
                def reload_presets() -> None:
                    created = presets.seed(force=True)
                    ui.notify("预置判据已重建" if created else "预置已存在", color="positive")
                    _refresh_all()

                def reload_config() -> None:
                    """重新读 config/*.yaml（改完配置不用重启进程）。"""
                    yamls.reload_all()
                    ui.notify("配置已重新加载：" + ("；".join(yamls.warnings()) or "无告警"),
                              color="warning" if yamls.warnings() else "positive")
                    alg_grid_refresh()

                def alg_grid_refresh() -> None:
                    try:
                        algo_grid.refresh()
                    except Exception:  # noqa: BLE001
                        pass

                ui.button("重载配置", icon="settings_backup_restore", on_click=reload_config).props(
                    "flat").classes("sy-btn-ghost").tooltip("重新读取 config/*.yaml，无需重启")
                ui.button("重载预置判据", icon="restart_alt", on_click=reload_presets).props("flat").classes("sy-btn-ghost")
                new_btn = ui.button("新建规则", icon="add", on_click=lambda: rule_dialog()).props("unelevated").classes("sy-btn-primary")
                new_btn.mark("btn-new-rule")

        with ui.tabs().classes("w-full").style("margin-top:4px") as tabs:
            t1 = ui.tab("算法库", icon="functions")
            t2 = ui.tab("规则库", icon="rule")
            t3 = ui.tab("分组场景", icon="layers")
            t4 = ui.tab("运行配置", icon="tune")
        with ui.tab_panels(tabs, value=t1).classes("w-full bg-transparent"):
            with ui.tab_panel(t1).classes("px-0"):
                with card("算法库", f"{len(alg.all_algorithms())} 个算法 · 支持自定义扩展"):
                    with ui.row().classes("w-full items-center gap-2"):
                        q = ui.input(placeholder="搜索算法编号 / 名称 / 类别").classes("w-64").props(
                            "outlined dense clearable")
                        scope = ui.select(["全部", "内置", "自定义"], value="全部").classes("w-28").props(
                            "outlined dense")
                        ui.space()
                        ui.button("新增自定义算法", icon="code",
                                  on_click=lambda: algo_editor()).props("unelevated").classes("sy-btn-primary")
                    algo_grid()
                    ui.label("自定义算法保存为 user_algorithms/*.py，重新加载后仍生效；"
                             "运行时注册进算法注册表，可直接被规则引用。").classes("sy-hint")
                q.on_value_change(lambda e: algo_grid.refresh(query=e.value or "", scope=scope.value))
                scope.on_value_change(lambda e: algo_grid.refresh(query=q.value or "", scope=e.value))

            with ui.tab_panel(t2).classes("px-0"):
                with card("规则库", "一条规则 = 一个测点代号 + 若干算法（含参数）"):
                    with ui.row().classes("w-full items-center gap-2"):
                        q2 = ui.input(placeholder="搜索规则 / 代号").classes("w-64").props("outlined dense clearable")
                        ui.space()
                        ui.label("双击行可编辑").classes("sy-hint")
                        new_btn = ui.button("新建规则", icon="add", on_click=lambda: rule_dialog()).props("unelevated").classes("sy-btn-primary")
                        new_btn.mark("btn-new-rule")
                    rule_table()
                q2.on_value_change(lambda e: rule_table.refresh(query=e.value or ""))

            with ui.tab_panel(t3).classes("px-0"):
                with card("分组场景", "若干规则打包成一个可复用的测试场景"):
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label("场景可整批执行，输出按规则分页的异常表与曲线").classes("sy-hint")
                        ui.space()
                        ui.button("新建场景", icon="add", on_click=lambda: scene_dialog()).props("unelevated").classes("sy-btn-primary")
                    scene_grid()

            with ui.tab_panel(t4).classes("px-0"):
                config_panel()

        # 规则库行内「编辑 / 删除」按钮的点击通道（页面级事件委托，只需绑一次）
        bind_rule_actions()


# ---------------------------------------------------------------------------
# 运行配置
# ---------------------------------------------------------------------------
def _flatten(d: dict, prefix: str = "") -> list[tuple[str, str]]:
    """把嵌套配置拍平成 (键路径, 值) 便于表格展示。"""
    rows: list[tuple[str, str]] = []
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            rows.extend(_flatten(v, key))
        elif isinstance(v, list):
            rows.append((key, ", ".join(str(x) for x in v) if v else "（空）"))
        else:
            rows.append((key, "" if v is None else str(v)))
    return rows


def config_panel() -> None:
    """展示当前生效的配置来源与关键项，方便现场核对 / 排查。"""
    files = [
        ("app.yaml", "应用参数、路径、等级色、时间窗、模拟、落盘格式、界面渲染"),
        ("telemetry.yaml", "测点代号目录（值域 / 量纲 / 位宽）与未登记代号兜底规则"),
        ("presets.yaml", "预置规则与分组场景（规则集 V1.0）"),
        ("prompts.yaml", "大模型系统提示词与用户提示词模板"),
        ("theme.yaml", "配色变量、CSS、Logo（等级色标由 app.yaml 生成）"),
    ]

    with card("运行配置", f"来源 {yamls.CONFIG_DIR}"):
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            for name, desc in files:
                path = yamls.CONFIG_DIR / name
                ok = path.exists()
                ui.html(
                    f'<span class="sy-chip"><span class="sy-dot{" ok" if ok else " amber"}"></span>'
                    f'<b>{name}</b> · {"已加载" if ok else "缺失"}</span>', sanitize=False).tooltip(desc)
            ui.space()
            ui.label("环境变量优先于 YAML：VIGIL_PORT / DATA / DB / LLM_URL / LLM_KEY / LLM_MODEL"
                     ).classes("sy-hint")

    app_cfg = yamls.file("app.yaml")
    rows = [{"k": k, "v": v} for k, v in _flatten(app_cfg)]
    with card("生效参数（app.yaml + 环境变量）", f"{len(rows)} 项"):
        ui.table(
            columns=[{"name": "k", "label": "配置项", "field": "k", "align": "left"},
                     {"name": "v", "label": "生效值", "field": "v", "align": "left"}],
            rows=rows, row_key="k",
            pagination={"rowsPerPage": 20},
        ).classes("w-full").props("flat dense").style("max-height:520px")

    with card("预置规则与场景（presets.yaml）", "改 YAML 后点「重载预置判据」即可生效"):
        pre = yamls.file("presets.yaml")
        scenes = pre.get("scenes") or []
        rules = pre.get("rules") or []
        with ui.row().classes("gap-2 flex-wrap"):
            for s in scenes:
                ui.html(f'<span class="sy-chip"><b>{s.get("name") or s.get("id")}</b> · '
                        f'{len(s.get("rules") or [])} 条规则</span>', sanitize=False)
        ui.label(f"共 {len(rules)} 条预置规则："
                 + "、".join(str(r.get("id")) for r in rules)).classes("sy-hint")

    if yamls.warnings():
        with card("配置告警", "以下项使用了内置默认值"):
            for w in yamls.warnings():
                ui.label(f"⚠ {w}").style("color:#fcd34d;font-size:11px")

