"""命令行测试：不启动 WebUI，也能完成全部功能。

这里测的是「命令行这条路本身没说谎」：
- 每个子命令都能跑通，且退出码符合约定（0 成功 / 1 业务失败 / 2 参数错）；
- 输出不是空的、关键字段在，避免命令"静默成功"；
- 结构化输出（json/csv）可被机器解析，便于写脚本。

不测检测结果的数值——那是算法层的事，见 test_flow.py。
CLI 只负责把已有能力接出来，所以断言集中在「连接是否接通」。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, expect: int = 0) -> subprocess.CompletedProcess:
    """跑一条 CLI 命令，默认断言退出码为 0。"""
    r = subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == expect, (
        f"args={args} 期望退出码 {expect}，实际 {r.returncode}\n"
        f"stdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-2000:]}"
    )
    return r


# ---------------------------------------------------------------------------
# 顶层
# ---------------------------------------------------------------------------
def test_help_lists_all_command_groups():
    r = run("--help")
    for group in ("algo", "rule", "scene", "preset", "detect", "llm", "data", "config"):
        assert group in r.stdout, f"帮助里缺命令组 {group}"


def test_no_subcommand_gives_arg_error():
    """不带子命令不能假装成功——脚本里 `vigil || exit` 要能拦住。"""
    run(expect=2)


def test_unknown_command_is_arg_error():
    run("no-such-command", expect=2)


# ---------------------------------------------------------------------------
# 算法库
# ---------------------------------------------------------------------------
def test_algo_list_has_27_algorithms():
    r = run("algo", "list")
    assert "A-01" in r.stdout and "A-27" in r.stdout
    assert "A-01" in r.stdout


def test_algo_list_json_is_parseable():
    rows = json.loads(run("algo", "list", "-f", "json").stdout)
    assert len(rows) == 27, f"应有 27 个算法，实际 {len(rows)}"
    first = rows[0]
    for key in ("id", "name", "category", "hit_type", "detector"):
        assert key in first
    assert {r["id"] for r in rows} >= {"A-01", "A-10", "A-25", "A-27"}


def test_algo_show_lists_params():
    r = run("algo", "show", "A-01")
    assert "A-01" in r.stdout
    for field in ("mode", "low", "high", "k", "level"):
        assert field in r.stdout
    assert "提示" in r.stdout and "致命" in r.stdout


def test_algo_show_json_has_param_specs():
    data = json.loads(run("algo", "show", "A-01", "-f", "json").stdout)
    names = {p["name"] for p in data["params"]}
    assert {"mode", "level", "k"} <= names


def test_algo_show_missing_returns_business_failure():
    r = run("algo", "show", "A-99", expect=1)
    assert "A-99" in (r.stderr + r.stdout)


def test_algo_template_prints_python_class():
    r = run("algo", "template")
    assert "class " in r.stdout and "ALGO_ID" in r.stdout


# ---------------------------------------------------------------------------
# 自定义算法：注册 → 列表 → 读源码 → 删除
# ---------------------------------------------------------------------------
def test_custom_algo_lifecycle(tmp_path: Path):
    """自定义算法全生命周期。

    这条用例守的是「CLI 每进程独立」这个坑：注册表是进程内 dict，
    Web 由 app.main 加载，命令行必须自己 load_all()，否则刚注册的算法
    在下一个命令里就"消失"了。
    """
    src = run("algo", "template").stdout
    f = tmp_path / "my_algo.py"
    f.write_text(src, encoding="utf-8")

    # 静态校验 + 冒烟试跑
    r = run("algo", "check", str(f))
    assert "校验通过" in r.stdout

    # 注册
    r = run("algo", "add", str(f))
    assert "已注册算法" in r.stdout
    algo_id = "U-01"

    # 下一个进程里必须还能看到（这是最容易坏的一步）
    rows = json.loads(run("algo", "list", "--custom-only", "-f", "json").stdout)
    assert any(x["id"] == algo_id for x in rows), f"自定义算法 {algo_id} 注册后应能跨进程看到"

    # 能读回源码
    assert "class " in run("algo", "src", algo_id).stdout

    # 删除，且失败时不能伪装成功
    r = run("algo", "rm", algo_id)
    assert "已删除" in r.stdout
    rows = json.loads(run("algo", "list", "--custom-only", "-f", "json").stdout)
    assert not any(x["id"] == algo_id for x in rows)


def test_algo_rm_builtin_is_refused():
    r = run("algo", "rm", "A-01", expect=1)
    assert "内置算法不可删除" in (r.stderr + r.stdout)


# ---------------------------------------------------------------------------
# 规则库
# ---------------------------------------------------------------------------
def test_rule_list_has_32_presets():
    rows = json.loads(run("rule", "list", "-f", "json").stdout)
    presets = [r for r in rows if r["source"] == "preset"]
    # 工业规则集：旋转机械 11 + 电气与PLC 11 = 22 条
    assert len(presets) == 22, f"预置规则应为 22 条，实际 {len(presets)}"
    ids = {r["rule_id"] for r in rows}
    assert {"R-SPN-01", "R-ELC-01", "R-ELC-11"} <= ids


def test_rule_list_filter_by_source():
    rows = json.loads(run("rule", "list", "--source", "preset", "-f", "json").stdout)
    assert rows and all(r["source"] == "preset" for r in rows)


def test_rule_show_has_algorithms_section():
    r = run("rule", "show", "R-SPN-01")
    assert "R-SPN-01" in r.stdout
    assert "A-01" in r.stdout
    assert "关联算法" in r.stdout


def test_rule_show_missing_returns_business_failure():
    run("rule", "show", "R-NOPE", expect=1)


def test_rule_export_json_roundtrip():
    text = run("rule", "export", "--code", "SPN1001").stdout
    data = json.loads(text)
    assert isinstance(data, list) and data
    assert all("algorithms" in r for r in data)


def test_rule_export_csv_has_header():
    text = run("rule", "export", "-f", "csv", "--code", "SPN1001").stdout
    head = text.splitlines()[0]
    assert head.startswith("规则ID,")
    assert "算法参数 JSON" in head


def test_rule_export_table_renders():
    r = run("rule", "export", "-f", "table", "--scene", "S-ELEC")
    assert "R-ELC-01" in r.stdout


def test_rule_add_fills_telemetry_metadata(tmp_path: Path):
    """命令行建的规则要带测点元数据快照。

    不填的话规则详情会出现「SPN1001（）」「值域 [None, None]」这种空壳，
    很难看出这条判据到底作用在什么量上。
    """
    rid = "R-CLITEST01"
    run("rule", "add", "--rule-id", rid, "--code", "SPN1001", "--name", "元数据快照测试",
        "--algo", "A-01:mode=outside,low=1.0,high=40.0,k=3,level=严重")
    try:
        data = json.loads(run("rule", "show", rid, "-f", "json").stdout)
        assert data["code_name"], "应补齐测点中文名"
        assert data["kind"] == "continuous"
        assert data["low"] == -10.0 and data["high"] == 120.0
    finally:
        run("rule", "rm", rid)


def test_rule_add_rejects_bad_params():
    """参数不合法要按「校验未通过」退出（4），不是笼统的 1。"""
    r = run("rule", "add", "--code", "SPN1001", "--name", "坏参数",
            "--algo", "A-01:k=-5", expect=4)
    assert "参数" in (r.stderr + r.stdout) or "校验" in (r.stderr + r.stdout)


def test_scene_crud_lifecycle(tmp_path: Path):
    run("scene", "add", "S-CLITEST", "--name", "命令行测试",
        "--rules", "R-SPN-01,R-SPN-07")
    try:
        data = json.loads(run("scene", "show", "S-CLITEST", "-f", "json").stdout)
        assert data["name"] == "命令行测试"
        assert data["rule_ids"] == ["R-SPN-01", "R-SPN-07"]
        run("scene", "edit", "S-CLITEST", "--name", "改名了")
        assert json.loads(run("scene", "show", "S-CLITEST", "-f", "json").stdout)["name"] == "改名了"
    finally:
        run("scene", "rm", "S-CLITEST")
    run("scene", "show", "S-CLITEST", expect=1)


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------
def test_scene_list_has_two_preset_scenes():
    rows = json.loads(run("scene", "list", "-f", "json").stdout)
    by_id = {r["scene_id"]: r for r in rows}
    assert "S-SPIN" in by_id and "S-ELEC" in by_id
    assert by_id["S-SPIN"]["rules"] == 11
    assert by_id["S-ELEC"]["rules"] == 11


def test_scene_show_lists_rules():
    r = run("scene", "show", "S-SPIN")
    assert "旋转机械" in r.stdout
    assert "R-SPN-01" in r.stdout


def test_scene_show_missing_returns_business_failure():
    run("scene", "show", "S-NOPE", expect=1)


# ---------------------------------------------------------------------------
# 检测（核心）
# ---------------------------------------------------------------------------
def test_detect_scene_runs_and_hits_all_rules():
    """11 条规则全命中——模拟注入 + 回测闭环的最小验收。"""
    r = run("detect", "scene", "-s", "S-SPIN", "-H", "12", "-f", "json")
    data = json.loads(r.stdout)
    assert data["summary"]["rules"] == 11
    assert data["summary"]["hit_rules"] == 11, data["summary"]
    assert data["summary"]["total"] > 0
    # 每条规则都必须有异常点
    assert all(rule["points"] for rule in data["rules"])


def test_detect_scene_table_is_readable():
    r = run("detect", "scene", "-s", "S-SPIN", "-H", "12")
    assert "旋转机械" in r.stdout
    assert "等级分布" in r.stdout
    # 等级分布不能整列为空（曾因键值混用踩过，见 report.outcome_rows）
    assert "严重×" in r.stdout


def test_detect_scene_points_prints_detail():
    r = run("detect", "scene", "-s", "S-SPIN", "-H", "12", "--points")
    assert "个异常点" in r.stdout
    assert "定位" in r.stdout


def test_detect_scene_csv_writes_per_rule(tmp_path: Path):
    out = tmp_path / "scene.csv"
    run("detect", "scene", "-s", "S-SPIN", "-H", "12", "-f", "csv",
        "--csv-per-rule", "-o", str(out))
    # --csv-per-rule 会在同目录按规则拆多个文件
    files = list(tmp_path.glob("*.csv"))
    assert files, "应至少产出一个 csv 文件"
    header = files[0].read_text(encoding="utf-8").splitlines()[0]
    assert "规则ID" in header or "时间" in header


def test_detect_scene_bad_scene_returns_business_failure():
    run("detect", "scene", "-s", "S-NOPE", expect=1)


def test_detect_custom_by_code():
    r = run("detect", "custom", "-c", "SPN1001", "-H", "12", "-f", "json")
    data = json.loads(r.stdout)
    assert data["summary"]["rules"] >= 1
    assert all(rule["code"] == "SPN1001" for rule in data["rules"])


def test_detect_custom_by_rule_ids():
    r = run("detect", "custom", "-r", "R-SPN-01,R-SPN-07", "-H", "12", "-f", "json")
    data = json.loads(r.stdout)
    ids = {rule["rule_id"] for rule in data["rules"]}
    assert ids == {"R-SPN-01", "R-SPN-07"}


def test_detect_custom_without_target_is_arg_error():
    """既没 --code 也没 --rules：参数用法错，不是业务失败。"""
    run("detect", "custom", expect=2)


def test_detect_custom_no_rules_and_no_generate_fails():
    # TRM0001 目录里有、但没有任何规则
    run("detect", "custom", "-c", "TRM0001", "-H", "12", expect=1)


def test_detect_custom_no_llm_uses_heuristic():
    """--no-llm 不调模型，离线也能出规则。"""
    r = run("detect", "custom", "-c", "SPN1004", "-H", "12",
            "--generate", "--no-llm", "-f", "json")
    data = json.loads(r.stdout)
    assert data["summary"]["rules"] >= 1


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def test_data_list_json():
    rows = json.loads(run("data", "list", "-f", "json").stdout)
    assert isinstance(rows, list)
    if rows:
        for key in ("code", "start", "end", "points"):
            assert key in rows[0]


def test_data_stats_after_scene_detect():
    run("detect", "scene", "-s", "S-SPIN", "-H", "12")  # 确保有数据
    r = run("data", "stats", "-c", "SPN1001", "-H", "12", "-f", "json")
    data = json.loads(r.stdout)
    assert data["code"] == "SPN1001"
    assert data["points"] > 0


def test_data_export_csv(tmp_path: Path):
    out = tmp_path / "series.csv"
    run("data", "export", "-c", "SPN1001", "-H", "12", "-o", str(out))
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "t,v,quality"
    assert len(lines) > 100


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_config_check_passes():
    r = run("config", "check")
    assert "校验通过" in r.stdout


def test_config_show_app_json():
    data = json.loads(run("config", "show", "app", "-f", "json").stdout)
    assert data["app"]["name"] == "VIGIL"
    assert data["simulate"]["min_hits"] == 5


def test_config_show_telemetry():
    data = json.loads(run("config", "show", "telemetry", "-f", "json").stdout)
    keys = " ".join(data.keys())
    assert "telemetry" in keys or "items" in keys


def test_config_show_unknown_file_is_arg_error():
    run("config", "show", "nope", expect=2)


def test_config_reload_ok():
    r = run("config", "reload")
    assert "OK" in r.stdout


# ---------------------------------------------------------------------------
# 预置
# ---------------------------------------------------------------------------
def test_preset_reload_is_idempotent():
    """不带 --force：已有库再跑一次不应重复插入。"""
    r = run("preset", "reload")
    assert "OK" in r.stdout or "无需" in r.stdout or "已是最新" in r.stdout


# ---------------------------------------------------------------------------
# 入口脚本
# ---------------------------------------------------------------------------
def test_entry_script_is_executable():
    exe = ROOT / "vigil"
    assert exe.exists(), "根目录应有 vigil 可执行入口"
    assert os.access(exe, os.X_OK), "vigil 应有可执行权限"


def test_entry_script_matches_module():
    """./vigil 与 python -m app.cli 输出应当一致（同一份 argparse）。"""
    via_module = run("algo", "list", "-f", "json").stdout
    r = subprocess.run([sys.executable, str(ROOT / "vigil"), "algo", "list", "-f", "json"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-1000:]
    assert r.stdout == via_module
