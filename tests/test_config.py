"""配置层测试：YAML 能否被正确加载、校验、并被环境变量覆盖。

这些测试守的是"配置即代码"这条底线：
- 改 YAML 不重启进程也能生效（reload_all）；
- YAML 写错时宁可启动失败，也不要带着坏配置跑出错误判据；
- 环境变量优先级高于 YAML。
"""
from __future__ import annotations

import textwrap

import pytest
import yaml

from app import yamls


@pytest.fixture(autouse=True)
def _clean():
    yamls.reload_all()
    yield
    yamls.reload_all()


def test_app_yaml_loaded():
    app = yamls.file("app.yaml")
    assert app["app"]["name"] == "VIGIL"
    assert app["simulate"]["max_points"] > 0
    # paths 已解析为绝对 Path，且 ${data} 引用正确展开
    assert app["paths"]["parquet"] == app["paths"]["data"] / "parquet"
    assert app["paths"]["data"].is_absolute()


def test_levels_and_colors():
    colors = yamls.level_colors()
    names = yamls.level_names()
    assert set(colors) == {"L1", "L2", "L3", "L4"}
    assert names["L4"] == "致命"
    assert yamls.level_order()["L4"] == 4


def test_telemetry_catalog():
    from app import meta
    assert "SPN1001" in meta.codes()
    d = meta.get_def("SPN1001")
    assert d.unit == "℃" and d.kind == "continuous"
    # 未登记代号走兜底，不能抛异常
    g = meta.get_def("TZZ9999")
    assert "未登记" in g.name


def test_presets_loaded_from_yaml():
    from app import presets, yamls
    ids = presets.preset_rule_ids()
    # 工业规则集：旋转机械 11 条 + 电气与PLC 11 条 = 22 条
    assert len(ids) == 22, ids
    assert ids[0] == "R-SPN-01" and ids[10] == "R-SPN-11"
    assert presets.SCENE_ID == "S-SPIN"
    scenes = yamls.file("presets.yaml")["scenes"]
    by_id = {s["id"]: s for s in scenes}
    assert len(by_id["S-SPIN"]["rules"]) == 11
    assert len(by_id["S-ELEC"]["rules"]) == 11
    rules = presets.build_preset_rules()
    # 算法参数必须来自 YAML，而不是代码里的默认值
    r01 = next(r for r in rules if r["rule_id"] == "R-SPN-01")
    assert r01["algorithms"][0]["algo_id"] == "A-01"
    assert r01["algorithms"][0]["params"]["high"] == 100.0
    # 复合条件规则（A-25）的 conditions 要正确展开
    r10 = next(r for r in rules if r["rule_id"] == "R-ELC-10")
    assert r10["algorithms"][0]["algo_id"] == "A-25"
    assert len(r10["algorithms"][0]["params"]["conditions"]) == 2


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("VIGIL_PORT", "9999")
    monkeypatch.setenv("VIGIL_LLM_MODEL", "test-model")
    yamls.reload_all()
    app = yamls.file("app.yaml")
    assert app["server"]["port"] == 9999
    assert app["llm"]["model"] == "test-model"


def test_env_bad_value_raises(monkeypatch):
    monkeypatch.setenv("VIGIL_PORT", "not-a-number")
    yamls.reload_all()
    with pytest.raises(yamls.ConfigError):
        yamls.file("app.yaml")


def test_presets_schema_validation(tmp_path, monkeypatch):
    """规则缺字段 / 引用不存在的规则 → 启动期直接失败。"""
    bad = tmp_path / "presets.yaml"
    bad.write_text(textwrap.dedent("""
        scenes:
          - id: S-X
            name: 测试
            rules: [R-NOT-EXIST]
        rules:
          - id: R-X-01
            code: SPN1001
            name: 缺 algorithms
    """).strip(), encoding="utf-8")
    data = yamls.load_yaml(bad)
    with pytest.raises(yamls.ConfigError, match="algorithms"):
        yamls._check_presets(data, bad)


def test_yaml_syntax_error_reported(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("a: [1, 2\nb: :\n", encoding="utf-8")
    with pytest.raises(yamls.ConfigError):
        yamls.load_yaml(bad)


def test_missing_file_uses_default(tmp_path):
    got = yamls.load_yaml(tmp_path / "nope.yaml", default={"k": 1})
    assert got == {"k": 1}
    assert any("nope.yaml" in w for w in yamls.warnings())


def test_theme_css_rendered():
    from app import theme
    assert ".sy-card" in theme.CSS
    # 等级色标由 levels 生成，而不是 CSS 里手写
    assert ".lvl-L4" in theme.CSS
    assert "${" not in theme.CSS          # 占位符必须全部替换掉
    assert "<svg" in theme.LOGO_SVG


def test_prompt_template_rendered():
    from app import llm
    from app.meta import get_def
    d = get_def("SPN1001")
    p = llm.build_user_prompt("SPN1001", {"name": d.name, "unit": d.unit, "kind": d.kind,
                                          "low": d.low, "high": d.high}, {"mean": 1}, 1.0)
    assert "SPN1001" in p and "可用算法清单" in p
    assert "{code}" not in p             # 变量都要被替换
    assert "A-01" in p                   # 算法清单确实拼进去了


def test_yaml_file_is_valid_mapping():
    """每个 YAML 文件本身都必须是合法映射（防止手工编辑写坏）。"""
    for name in ("app.yaml", "telemetry.yaml", "presets.yaml", "prompts.yaml", "theme.yaml"):
        path = yamls.CONFIG_DIR / name
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict), f"{name} 顶层必须是映射"
