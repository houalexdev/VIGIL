"""VIGIL · 工业设备智能监测平台 —— 异常数据检测

启动：
    python main.py                 # 默认 0.0.0.0:8080
    VIGIL_PORT=8000 python main.py
    python -m app.main             # 等价
"""
from __future__ import annotations

import threading

from nicegui import ui

from . import custom_algo, presets, runtime, store, yamls
from .config import LLM_PROBE_TIMEOUT, PAGE_TITLE, SERVER
from .ui import detection, rules_center


def bootstrap() -> None:
    store.init_db()
    for msg in yamls.warnings():
        print(f"[config] {msg}")
    loaded = custom_algo.load_all()
    if loaded:
        print(f"[bootstrap] 自定义算法已加载: {', '.join(loaded)}")
    if presets.seed():
        print(f"[bootstrap] 预置判据已写入（来源 config/presets.yaml）")
    threading.Thread(target=runtime.probe_llm, kwargs={"timeout": LLM_PROBE_TIMEOUT},
                     daemon=True).start()


def register_pages() -> None:
    @ui.page("/")
    def index() -> None:
        ui.navigate.to("/rules")

    @ui.page("/rules")
    def rules_page() -> None:
        rules_center.page()

    @ui.page("/detect")
    def detect_page() -> None:
        detection.page()

    @ui.page("/about")
    def about_page() -> None:
        from .ui.common import card, shell

        with shell("/rules"):
            with ui.column().classes("w-full gap-3"):
                with card("算法框架约定", "algorithm/base.py —— 所有算法唯一依赖"):
                    ui.markdown("""
- 时间统一 **int64 毫秒**；缺失值用 `NaN` + `quality` 标记，**禁止填 0**。
- 「不知道」用 `TriState.UNKNOWN` / `RunStatus.UNKNOWN` 表达，不退化成 False。
- 算法只吃 `AlgorithmContext`、只吐 `AnomalyPoint`：不写库、不读文件、不打印。
- `run()` 是模板方法：校验参数 → precheck → `detect()` → 归一化盖章 → `AlgorithmResult`。
- 新增算法：在「规则中心 → 算法库 → 新增自定义算法」写一个继承 `BaseAlgorithm` 的类，
  保存即注册，可直接被规则引用；也可直接往 `algorithm/` 放 `aNN_xxx.py`。
                    """).classes("sy-hint")
                with card("数据落盘", "Parquet / snappy · 按时间戳与测点代号分目录"):
                    ui.markdown("""
```
data/parquet/<测点代号>/<YYYYMMDD>/<起始ms>_<结束ms>.parquet
  t        int64     epoch 毫秒
  v        float64   工程值
  quality  uint8     0 正常 / 1 缺失 / 2 无效
```
                    """).classes("sy-hint")


def main() -> None:
    bootstrap()
    register_pages()
    ui.run(
        host=str(SERVER.get("host", "0.0.0.0")),
        port=int(SERVER.get("port", 8080)),
        title=PAGE_TITLE,
        favicon=str(SERVER.get("favicon", "🛰️")),
        dark=bool(SERVER.get("dark", True)),
        reload=bool(SERVER.get("reload", False)),
        show=bool(SERVER.get("show", False)),
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
