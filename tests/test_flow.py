"""端到端流程测试（无头浏览器驱动 NiceGUI 元素树）。

关于重试：NiceGUI 的 ``retries`` 是**次数**不是秒数，每次间隔 0.1 s，
所以 ``retries=120`` 只有 12 秒。模拟数据 + 规则生成属于秒级任务，统一用
``WAIT`` 换算成"至少等 N 秒"。

关于大模型：测试绝不能依赖现场模型服务是否在线（在线时一次推理要十几秒，
离线时要等超时），所以这里强制走本地启发式路径，只验证「生成 → 入库 → 分析」
这条链路本身。
"""
from nicegui.testing import User

pytest_plugins = ["nicegui.testing.user_plugin"]


def wait(seconds: float) -> int:
    """把秒数换算成 should_see 需要的重试次数（每次 0.1 s）。"""
    return max(3, int(seconds * 10))


async def test_scene_analysis(user: User) -> None:
    await user.open("/detect")
    user.find(marker="btn-sim-scene").click()
    await user.should_see("场景 · 旋转机械", retries=wait(30))
    await user.should_see("数据落盘", retries=wait(3))


async def test_custom_flow(user: User, monkeypatch) -> None:
    from app import llm

    # 固定用本地启发式：真实模型一次推理要十几秒且依赖现场服务，测试不该赌这个。
    # 注意要保留原函数引用，否则包装函数会递归调用自己。
    real_generate = llm.generate

    def local_only(code, meta, stats, period_s, use_llm=True):
        return real_generate(code, meta, stats, period_s, use_llm=False)

    monkeypatch.setattr(llm, "generate", local_only)

    await user.open("/detect")
    user.find("自定义检测").click()
    await user.should_see("01 数据准备", retries=wait(3))
    user.find(marker="btn-sim-custom").click()
    await user.should_see("单拍差分 p99", retries=wait(20))
    user.find(marker="btn-gen-rules").click()
    await user.should_see("已保存", retries=wait(20))
    user.find(marker="btn-run").click()
    await user.should_see("自定义检测", retries=wait(30))
