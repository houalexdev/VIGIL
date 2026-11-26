from nicegui import ui
from nicegui.testing import User

from app import custom_algo

pytest_plugins = ["nicegui.testing.user_plugin"]


async def test_custom_algo_editor(user: User) -> None:
    """算法库：新增自定义算法 → 校验 → 保存 → 注册进注册表。"""
    await user.open("/rules")
    user.find("新增自定义算法").click()
    await user.should_see("继承 BaseAlgorithm", retries=20)

    user.find(kind=ui.button, content="校验").click()
    await user.should_see("试跑通过", retries=30)

    user.find(kind=ui.button, content="保存").click()
    await user.should_see("已保存", retries=20)

    assert any(k.startswith("U-") for k in custom_algo.META), custom_algo.META
    for aid in list(custom_algo.META):
        custom_algo.delete_algo(aid)
