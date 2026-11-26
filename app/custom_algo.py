"""用户自定义算法：源码落盘 → 校验 → 注册进 algorithm.base 的全局注册表。

一个自定义算法 = ``user_algorithms/uaNN_xxx.py`` 一个文件，内容与内置算法完全同构
（继承 BaseAlgorithm + @register 可省，框架统一在加载时注册）。
"""
from __future__ import annotations

import ast
import re
import sys
import time
import types
import uuid
from pathlib import Path

import numpy as np

import algorithm.base as B
from .config import USER_ALGO_DIR

META: dict[str, dict] = {}
_BUILTIN_IDS: frozenset[str] = frozenset(B._REGISTRY)   # 内置算法编号（不可占用）


TEMPLATE = '''"""自定义算法模板：复制后修改 ALGO_ID / ALGO_NAME / PARAM_SPECS / detect 即可。

统一约定（来自 algorithm/base.py）：
- 时间是 int64 毫秒；缺失值是 NaN，禁止填 0。
- 只吃 ctx、只吐 AnomalyPoint，不写库、不读文件、不打印。
"""
from __future__ import annotations

import numpy as np

from algorithm.base import (
    BaseAlgorithm, ParamSpec, Params, point, register,
    AlgorithmContext, AnomalyPoint, HitType, Level, ValueKind, runs,
)


@register
class MyAlgorithm(BaseAlgorithm):
    ALGO_ID = "{algo_id}"
    ALGO_NAME = "{algo_name}"
    ALGO_NAME_EN = "My Algorithm"
    LAYER = "L1"
    CATEGORY = "自定义"
    HIT_TYPE = HitType.THRESHOLD
    SUPPORTED_KINDS = (ValueKind.CONTINUOUS,)
    RATIONALE = "说明这条判据为什么需要单独一个算法。"
    USE_CASES = "典型判据描述。"
    PARAM_SPECS = [
        ParamSpec("ratio", "偏离倍数", "float", 2.0, minimum=1.0, help="超过均值 + ratio·σ 判异常"),
        ParamSpec("k", "连续成立拍数", "int", 3, "拍", minimum=1),
    ]
    OUTPUT_FIELDS = [("mean", "float", "窗内均值"), ("sigma", "float", "窗内标准差")]

    def detect(self, ctx: AlgorithmContext, p: Params) -> list[AnomalyPoint]:
        s = ctx.series
        v = s.v
        ok = ~np.isnan(v)
        if ok.sum() < 10:
            return []
        mu, sd = float(np.nanmean(v)), float(np.nanstd(v, ddof=1))
        thr = mu + float(p.ratio) * sd
        hit = ok & (v > thr)
        from algorithm.base import confirm
        confirmed = confirm(hit, int(p.k)) if int(p.k) > 1 else hit
        return [
            point(ctx, p.level, HitType.THRESHOLD, int(s.t[a]), int(s.t[b]),
                  value=float(np.nanmax(v[a:b + 1])), expected=mu, threshold=thr,
                  deviation=float(np.nanmax(v[a:b + 1]) - mu),
                  evidence={{"mean": round(mu, 6), "sigma": round(sd, 6), "threshold": round(thr, 6)}})
            for a, b in runs(confirmed)
        ]
'''


def _ns_name(algo_id: str) -> str:
    return "user_algo_" + re.sub(r"[^0-9a-zA-Z_]", "_", algo_id.lower())


def _declared_id(src: str) -> str:
    """从源码里静态取出 ALGO_ID 字面量（用于注册表换出，避免重复注册）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            targets = []
            value = None
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            elif isinstance(stmt, ast.AnnAssign):
                targets, value = [stmt.target], stmt.value
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "ALGO_ID" and isinstance(value, ast.Constant):
                    return str(value.value)
    return ""


def _exec_source(src: str, algo_id: str) -> types.ModuleType:
    mod = types.ModuleType(_ns_name(algo_id))
    mod.__dict__["__builtins__"] = __builtins__
    exec(compile(src, f"<custom:{algo_id}>", "exec"), mod.__dict__)
    return mod


def _find_cls(mod: types.ModuleType) -> type[B.BaseAlgorithm] | None:
    for obj in vars(mod).values():
        if isinstance(obj, type) and issubclass(obj, B.BaseAlgorithm) and obj is not B.BaseAlgorithm:
            if getattr(obj, "ALGO_ID", ""):
                return obj
    return None


def validate_source(src: str) -> tuple[type[B.BaseAlgorithm] | None, str]:
    """语法 + 结构校验，返回 (类, 错误信息)。空错误串表示通过。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return None, f"语法错误：第 {exc.lineno} 行 {exc.msg}"
    if not any(isinstance(n, (ast.ClassDef,)) for n in tree.body):
        return None, "源码中未找到类定义"
    aid = _declared_id(src)
    saved = B._REGISTRY.pop(aid, None) if aid else None
    try:
        mod = _exec_source(src, "probe")
    except Exception as exc:  # noqa: BLE001
        if saved is not None and aid not in B._REGISTRY:
            B._REGISTRY[aid] = saved
        return None, f"导入失败：{type(exc).__name__}: {exc}"
    finally:
        if saved is not None and aid not in B._REGISTRY:
            B._REGISTRY[aid] = saved
    cls = _find_cls(mod)
    if cls is None:
        return None, "未找到继承 BaseAlgorithm 且带 ALGO_ID 的类"
    if not re.match(r"^[A-Za-z]{1,4}-\d{2,3}$", cls.ALGO_ID or ""):
        return None, f"ALGO_ID 需形如 U-01 / X-07，当前为 {cls.ALGO_ID!r}"
    try:
        cls.param_specs()
    except Exception as exc:  # noqa: BLE001
        return None, f"PARAM_SPECS 定义有误：{exc}"
    if not getattr(cls, "ALGO_NAME", ""):
        return None, "缺少 ALGO_NAME"
    return cls, ""


def smoke_test(cls: type[B.BaseAlgorithm], kind: str = "continuous") -> tuple[bool, str]:
    """在合成数据上试跑一次，尽量把运行期错误挡在保存之前。"""
    from .meta import build_meta
    try:
        rng = np.random.default_rng(7)
        t = np.arange(0, 600, 1, dtype=np.int64) * 1000
        v = rng.normal(50, 1.0, t.size)
        v[[100, 200, 400]] += 12.0
        meta = build_meta("TEST_CODE", kind=kind, low=0, high=100)
        series = B.TelemetrySeries(code="TEST_CODE", t=t, v=v, meta=meta)
        ctx = B.AlgorithmContext(series=series, meta=meta)
        res = cls().run(ctx)
        if res.status is B.RunStatus.ERROR:
            return False, f"试跑报错：{res.reason}"
        return True, f"试跑通过：{res.status.value}，命中 {len(res.points)} 点"
    except Exception as exc:  # noqa: BLE001
        return False, f"试跑异常：{type(exc).__name__}: {exc}"


def register_cls(cls: type[B.BaseAlgorithm], file_path: Path, *, replace: str = "") -> None:
    aid = cls.ALGO_ID
    if replace and replace != aid and replace in B._REGISTRY:
        B._REGISTRY.pop(replace, None)
        META.pop(replace, None)
    B._REGISTRY[aid] = cls
    META[aid] = {
        "algo_id": aid,
        "name": cls.ALGO_NAME,
        "file": str(file_path),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def load_all() -> list[str]:
    """启动时加载 user_algorithms 下全部自定义算法。单个文件坏掉不影响其余。"""
    loaded, errors = [], []
    for f in sorted(USER_ALGO_DIR.glob("*.py")):
        try:
            src = f.read_text(encoding="utf-8")
            cls, err = validate_source(src)
            if err:
                errors.append(f"{f.name}: {err}")
                continue
            register_cls(cls, f)
            loaded.append(cls.ALGO_ID)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{f.name}: {exc}")
    for e in errors:
        print(f"[custom_algo] 加载失败 {e}")
    return loaded


def save_algo(algo_id_hint: str, name: str, src: str) -> tuple[str, str]:
    """新建/更新自定义算法。返回 (algo_id, 错误信息)。"""
    cls, err = validate_source(src)
    if err:
        return "", err
    aid = cls.ALGO_ID
    if aid in _BUILTIN_IDS:
        return "", f"算法编号 {aid} 与内置算法冲突，请换一个编号"
    ok, msg = smoke_test(cls)
    old = META.get(aid)
    if old:
        path = Path(old["file"])
    else:
        slug = re.sub(r"[^0-9a-zA-Z]+", "_", algo_id_hint or name or aid).strip("_").lower()
        path = USER_ALGO_DIR / f"{aid.replace('-', '').lower()}_{slug or uuid.uuid4().hex[:6]}.py"
    path.write_text(src, encoding="utf-8")
    register_cls(cls, path, replace=aid)
    return aid, ("" if ok else f"已保存，但{msg}")


def delete_algo(aid: str) -> str:
    info = META.get(aid)
    if not info:
        return "仅支持删除自定义算法"
    try:
        p = Path(info["file"])
        if p.exists() and p.parent == USER_ALGO_DIR:
            p.unlink()
    except OSError as exc:
        return f"删除文件失败：{exc}"
    B._REGISTRY.pop(aid, None)
    META.pop(aid, None)
    return ""


def read_source(aid: str) -> str:
    info = META.get(aid)
    if not info:
        return ""
    p = Path(info["file"])
    return p.read_text(encoding="utf-8") if p.exists() else ""


def is_custom(aid: str) -> bool:
    return aid in META


def next_id() -> str:
    n = 1
    while f"U-{n:02d}" in B._REGISTRY:
        n += 1
    return f"U-{n:02d}"


def new_template() -> str:
    return TEMPLATE.format(algo_id=next_id(), algo_name="自定义判据")
