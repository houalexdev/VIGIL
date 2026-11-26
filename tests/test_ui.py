"""页面渲染测试（无头驱动元素树）。

注意：NiceGUI 的 ``should_see`` 会穿透隐藏的 tab_panel（隐藏元素也在元素树里），
所以断言尽量挑**当前页面唯一**的文案；关键交互按钮统一用 ``.mark()`` 定位。
"""
from nicegui.testing import User

pytest_plugins = ["nicegui.testing.user_plugin"]


async def test_rules_page(user: User) -> None:
    await user.open("/rules")
    await user.should_see("规则中心")
    user.find("规则库").click()
    await user.should_see("双击行可编辑")
    user.find("分组场景").click()
    await user.should_see("若干规则打包成一个可复用的测试场景", retries=10)


async def test_config_tab(user: User) -> None:
    """运行配置页：文件来源、生效参数、预置条目都要能读出来。"""
    await user.open("/rules")
    user.find("运行配置").click()
    await user.should_see("生效参数（app.yaml + 环境变量）", retries=10)
    await user.should_see("telemetry.yaml", retries=10)
    await user.should_see("prompts.yaml", retries=10)


async def test_detect_page(user: User) -> None:
    await user.open("/detect")
    await user.should_see("异常检测")
    await user.should_see("模拟数据")
    user.find("自定义检测").click()
    await user.should_see("01 数据准备")
    await user.should_see("AI 生成规则")


async def test_custom_code_is_text_input(user: User) -> None:
    """「01 数据准备」的测点代号是纯文本输入框（不带下拉候选），且带读取/保存规则按钮。"""
    await user.open("/detect")
    user.find("自定义检测").click()
    await user.should_see("01 数据准备")
    await user.should_see("读取规则", retries=10)
    await user.should_see("保存规则", retries=10)

    # find(marker=...) 找不到会抛 AssertionError
    box = user.find(marker="input-code")
    from nicegui.elements.input import Input
    assert isinstance(box.elements.pop(), Input), "测点代号应为文本输入框"

    # 旧实现是 with_input 的下拉选择框：确认整页没有标签为「测点代号」的 Select
    from nicegui.elements.select import Select
    stray = [el for el in user.client.elements.values()
             if isinstance(el, Select)
             and (el._props.get("label") == "测点代号"
                  or (el._props.get("input-debounce") is not None
                      and el._props.get("use-input")))]
    assert not stray, "测点代号不应再是下拉选择框"


async def test_custom_read_refreshes_02_list(user: User) -> None:
    """01「读取规则」不建自己的列表，而是把 02「检测规则」列表刷新成该代号的规则。

    端到端点真实按钮（不是只调 store），因为这条链路的坑都在 UI 侧
    （``label.style("red")`` 这种非 ``key:value`` 写法会直接把 refreshable 打挂）。
    """
    from app import store

    code, rid = "TME2E001", "R-E2E0001"
    other_code, other_rid = "TME2E002", "R-E2E0002"
    store.init_db()
    for c, r, name in ((code, rid, "端到端验证规则"), (other_code, other_rid, "另一代号规则")):
        store.upsert_rule({
            "rule_id": r, "name": name, "code": c, "code_name": "验证测点",
            "kind": "continuous", "unit": "mm", "low": 0.0, "high": 100.0, "bit_width": 16,
            "quantum": 0.001, "note": "", "source": "user",
            "algorithms": [{"algo_id": "A-01",
                            "params": {"mode": "outside", "low": 1.0, "high": 40.0, "k": 3},
                            "note": ""}],
        }, new=True)
    try:
        await user.open("/detect")
        user.find("自定义检测").click()
        await user.should_see("01 数据准备")

        # 01 卡片里不能再出现独立的「规则列表」标题（列表只在 02）
        try:
            user.find("规则列表")
        except AssertionError:
            pass
        else:
            raise AssertionError("01 不应再有独立的规则列表")

        user.find(marker="input-code").clear().type(code)
        user.find("读取规则").click()
        await user.should_see("端到端验证规则", retries=10)
        await user.should_see(f"{code} · 1 条", retries=10)
        # 只展示当前代号的规则
        try:
            user.find("另一代号规则")
        except AssertionError:
            pass
        else:
            raise AssertionError("读取规则应只展示当前测点代号的规则")

        # 切到另一个代号后点读取，列表应随之切换
        user.find(marker="input-code").clear().type(other_code)
        user.find("读取规则").click()
        await user.should_see("另一代号规则", retries=10)
    finally:
        for r in (rid, other_rid):
            if store.get_rule(r):
                store.delete_rule(r)


async def test_custom_save_writes_02_list(user: User) -> None:
    """01「保存规则」把 02「检测规则」列表里展示的规则写回数据库，且不产生重复记录。"""
    from app import store

    code, rid = "TME2E003", "R-E2E0003"
    store.init_db()
    store.upsert_rule({
        "rule_id": rid, "name": "保存链路规则", "code": code, "code_name": "验证测点",
        "kind": "continuous", "unit": "mm", "low": 0.0, "high": 100.0, "bit_width": 16,
        "quantum": 0.001, "note": "", "source": "user",
        "algorithms": [{"algo_id": "A-01",
                        "params": {"mode": "outside", "low": 1.0, "high": 40.0, "k": 3},
                        "note": ""}],
    }, new=True)
    try:
        await user.open("/detect")
        user.find("自定义检测").click()
        await user.should_see("01 数据准备")
        user.find(marker="input-code").clear().type(code)
        user.find("读取规则").click()
        await user.should_see("保存链路规则", retries=10)

        user.find("保存规则").click()
        await user.should_see("保存到数据库", retries=10)
        assert store.get_rule(rid), "保存后规则应仍在库中"
        assert len([r for r in store.list_rules() if r["code"] == code]) == 1, "不应重复插入"
    finally:
        if store.get_rule(rid):
            store.delete_rule(rid)


async def test_custom_save_on_empty_list_warns(user: User) -> None:
    """列表为空时点保存，应提示无需保存，而不是静默成功或报错。"""
    await user.open("/detect")
    user.find("自定义检测").click()
    await user.should_see("01 数据准备")
    user.find(marker="input-code").clear().type("TMNORULE9")
    user.find("保存规则").click()
    await user.should_see("无需保存", retries=10)


async def test_min_hits_not_editable(user: User) -> None:
    """「每条规则最少异常数」只做说明展示，不给用户输入框（值由 YAML 决定）。"""
    from app.config import SIM_MIN_HITS

    await user.open("/detect")
    await user.should_see(f"每条规则最少 {SIM_MIN_HITS} 处异常", retries=10)
    # find() 找不到元素时会抛 AssertionError；这里断言它确实找不到
    try:
        user.find("每条规则最少异常数")
    except AssertionError:
        return
    raise AssertionError("「每条规则最少异常数」输入框应已移除，由配置文件接管")

async def test_rule_table_ops_wired_to_event_bus(user: User) -> None:
    """规则库「操作」列（编辑/删除）按钮必须能被服务端收到。

    历史坑：曾用 ``@click="$parent.$emit('edit', props.row.rule)"``（Vue 组件树内部事件，
    ``table.on()`` 监听的是 NiceGUI 页面事件总线，两者不通）和模板内 ``emitEvent``
    （严格模式下裸标识符查找不可靠，失败还是静默的）。
    现行方案：模板里只放 class + data-rid 标记，由 ``bind_rule_actions()`` 注入 document 级
    事件委托，再用 ``ui.on("rule-edit"/"rule-delete")`` 收一个 rule_id。
    因此这里断言模板**不再**在 Vue 里绑事件。
    """
    await user.open("/rules")
    user.find("规则库").click()

    tables = [el for el in user.client.elements.values()
              if el.__class__.__name__ == "Table"]
    assert tables, "规则库应有表格"
    # 插槽模板存在 table.slots[name].template 里（不是 _props）
    ops = ""
    for t in tables:
        slot = getattr(t, "slots", {}).get("body-cell-ops")
        if slot is not None and slot.template:
            ops = slot.template
            break
    assert ops, "表格应有操作列插槽"
    assert "data-rid" in ops, f"行内按钮应带 data-rid 供事件委托取 rule_id，实际：{ops[:300]}"
    assert "$parent.$emit" not in ops, "不能再用 Vue 的 $parent.$emit，服务端收不到"


def test_delete_rule_syncs_scene_references() -> None:
    """删除规则时要同时把它从场景里摘掉，否则场景会引用一个不存在的规则。"""
    from app import store

    store.init_db()
    rid = "R-UITEST01"
    store.upsert_rule({
        "rule_id": rid, "name": "UI删除测试", "code": "SPN1001",
        "code_name": "主轴前端轴承温度", "kind": "continuous",
        "unit": "mm", "low": 0.5, "high": 44.5, "bit_width": 16, "quantum": 0.001,
        "note": "", "source": "user",
        "algorithms": [{"algo_id": "A-01", "params": {"mode": "outside", "low": 1.0,
                                                      "high": 40.0, "k": 3}, "note": ""}],
    }, new=True)
    try:
        assert store.get_rule(rid)
        store.delete_rule(rid)
        assert not store.get_rule(rid), "删除后不应还能查到"
        for s in store.list_scenes():
            assert rid not in s["rule_ids"], f"场景 {s['scene_id']} 仍引用已删规则"
    finally:
        if store.get_rule(rid):
            store.delete_rule(rid)
