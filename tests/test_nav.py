from nicegui.testing import User
from app import store

pytest_plugins = ["nicegui.testing.user_plugin"]


async def test_scene_preselect(user: User) -> None:
    await user.open("/detect?scene=S-SPIN")
    await user.should_see("分析任务", retries=10)
    with user.client:
        sels = [e for e in user.find(kind=__import__("nicegui").ui.select).elements]
        values = [s.value for s in sels]
    assert "S-SPIN" in values, values
