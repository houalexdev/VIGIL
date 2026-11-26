import pytest
from nicegui import ui
from nicegui.testing import User

from app import store

pytest_plugins = ["nicegui.testing.user_plugin"]


async def test_rule_editor_flow(user: User) -> None:
    before = {r["rule_id"] for r in store.list_rules()}
    await user.open("/rules")
    user.find(marker="btn-new-rule").click()
    await user.should_see("新建规则", retries=20)

    user.find(marker="in-rule-name").type("自动化测试规则")
    user.find(marker="in-rule-code").type("SPN1004")
    with user.client:
        sel = list(user.find(marker="sel-algo").elements)[0]
        sel.set_value("A-01")
    await user.should_see("持续确认", retries=20)
    user.find(marker="btn-add-algo").click()
    await user.should_see("已配置 1 个算法", retries=20)
    user.find(marker="btn-save-rule").click()
    await user.should_see("已保存", retries=20)

    after = [r for r in store.list_rules() if r["rule_id"] not in before]
    assert len(after) == 1, after
    rec = after[0]
    assert rec["code"] == "SPN1004"
    assert rec["algorithms"][0]["algo_id"] == "A-01"
    store.delete_rule(rec["rule_id"])
