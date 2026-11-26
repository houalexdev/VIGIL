"""命令行界面：Web 界面能做的事，这里全部能做，不需要启动前端。

设计原则
    * **单一事实来源**：所有命令都调用 ``app`` 里已有的业务函数（store / engine /
      llm / presets / custom_algo），不复制判断逻辑；结果的格式化统一走
      ``app.report``，与网页表格共用同一份转换。
    * **可脚本化**：默认输出人读表格；``--json`` / ``--csv`` 便于管道和二次处理。
      写操作默认要求 ``--yes`` 之外不加交互，适合放进批处理。
    * **退出码有意义**：0 成功；1 业务失败（如未找到规则）；2 参数错误；
      3 依赖不可达（如大模型离线）；4 校验未通过。

用法
    python -m app.cli --help
    ./vigil --help
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import config as C
from . import custom_algo, engine, llm, presets, report, store, yamls
import algorithm as alg

# 退出码
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_DEP = 3
EXIT_VALIDATE = 4

TZ = report.TZ


# ---------------------------------------------------------------------------
# 输出helpers
# ---------------------------------------------------------------------------
def _w(text: str = "") -> None:
    print(text)


def _width(s: str) -> int:
    """中文按 2 列宽计算，保证表格对齐。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(s))


def _pad(s: str, n: int, align: str = "left") -> str:
    s = str(s)
    gap = max(n - _width(s), 0)
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def _ellipsis(s: str, limit: int) -> str:
    """按显示宽度截断到 ``limit`` 列，末尾加省略号。中文按 2 列算。"""
    if limit <= 0 or _width(s) <= limit:
        return s
    out, used = [], 0
    budget = limit - 1  # 给省略号留 1 列
    for ch in s:
        w = 2 if ord(ch) > 0x2E80 else 1
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def table(rows: list[dict], columns: list[tuple[str, str]], *,
          max_width: int = 0, widths: dict[str, int] | None = None) -> None:
    """打印 ASCII 表。

    ``columns`` 为 [(字段, 表头), ...]。
    ``widths`` 可按字段指定该列最大显示宽度（中文按 2 列），超长截断加省略号；
    未指定的列不截断（终端会自动折行，比丢信息好）。``max_width`` > 0 时作为
    所有列的默认上限。
    """
    if not rows:
        _w("（无数据）")
        return
    widths = dict(widths or {})
    cells = []
    for r in rows:
        line = []
        for key, _ in columns:
            v = r.get(key, "")
            s = "" if v is None else str(v)
            lim = widths.get(key, max_width)
            if lim:
                s = _ellipsis(s, lim)
            line.append(s)
        cells.append(line)
    col_w = [_width(label) for _, label in columns]
    for line in cells:
        for i, s in enumerate(line):
            col_w[i] = max(col_w[i], _width(s))
    _w("  ".join(_pad(label, col_w[i]) for i, (_, label) in enumerate(columns)))
    _w("  ".join("-" * w for w in col_w))
    for line in cells:
        _w("  ".join(_pad(s, col_w[i]) for i, s in enumerate(line)))


def emit(rows: list[dict], columns: list[tuple[str, str]], fmt: str, *,
         out: str | None = None, widths: dict[str, int] | None = None) -> None:
    """按格式输出：table / json / csv。

    ``widths`` 只影响 table 输出；json/csv 始终保留完整字段，不做截断。
    """
    if fmt == "json":
        text = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
    elif fmt == "csv":
        text = report.csv_text(rows, columns)
    else:
        table(rows, columns, widths=widths)
        return
    if out:
        Path(out).write_text(text, encoding="utf-8")
        _w(f"已写入 {out}（{len(rows)} 行）")
    else:
        _w(text)


def ok(msg: str) -> None:
    _w(f"[OK] {msg}")


def warn(msg: str) -> None:
    _w(f"[!] {msg}")


def err(msg: str) -> None:
    print(f"[ERR] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 算法库
# ---------------------------------------------------------------------------
def _algo_row(spec: dict) -> dict:
    aid = spec["algorithm_id"]
    # requires_peers / requires_baseline 是「依赖别的测点或基线」，needs_external 是
    # 「模拟端给不了输入」。三者对使用者的意义都是「这个算法单靠模拟数据测不了」，
    # 合并成一列，避免表格里出现三个语义相近的短列。
    lacks: list[str] = []
    if aid in C.NEEDS_EXTERNAL:
        lacks.append("需外部输入")
    if spec.get("requires_peers"):
        lacks.append("需配对测点")
    if spec.get("requires_baseline"):
        lacks.append("需基线")
    return {
        "id": aid,
        "name": spec["name"],
        "category": spec["category"],
        "hit_type": spec["hit_type"],
        "kinds": ",".join(spec.get("supported_kinds") or []) or "-",
        "detector": "是" if spec.get("is_detector") else "否",
        "limits": " / ".join(lacks) or "-",
        "custom": "自定义" if custom_algo.is_custom(aid) else "",
    }


ALGO_COLS = [("id", "编号"), ("name", "算法名"), ("category", "类别"), ("hit_type", "命中类型"),
             ("kinds", "支持形态"), ("detector", "检测器"), ("limits", "适用限制"), ("custom", "来源")]


def cmd_algo_list(a: argparse.Namespace) -> int:
    rows = [_algo_row(s) for s in alg.catalog()]
    if a.detector_only:
        rows = [r for r in rows if r["detector"] == "是"]
    if a.custom_only:
        rows = [r for r in rows if r["custom"]]
    emit(rows, ALGO_COLS, a.format, out=a.out)
    return EXIT_OK


def cmd_algo_show(a: argparse.Namespace) -> int:
    try:
        cls = alg.get(a.algo_id)
    except KeyError:
        err(f"算法不存在：{a.algo_id}")
        return EXIT_FAIL
    spec = next((s for s in alg.catalog() if s["algorithm_id"] == a.algo_id), {})
    info = {
        "编号": cls.ALGO_ID, "名称": cls.ALGO_NAME, "类别": getattr(cls, "CATEGORY", ""),
        "命中类型": getattr(cls.HIT_TYPE, "value", cls.HIT_TYPE),
        "支持形态": ",".join(k.value for k in cls.SUPPORTED_KINDS),
        "检测器": "是" if cls.IS_DETECTOR else "否",
        "说明": getattr(cls, "DESCRIPTION", "") or spec.get("description", ""),
        "适用场景": getattr(cls, "USE_CASES", ""),
    }
    if a.format == "json":
        params = [{"name": p.name, "label": p.label, "type": p.type, "default": p.default,
                   "choices": getattr(p, "choices", None), "required": p.required,
                   "help": getattr(p, "help", "")} for p in cls.param_specs()]
        _w(json.dumps({"info": info, "params": params}, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    for k, v in info.items():
        if v:
            _w(f"{_pad(k, 10)}: {v}")
    _w("")
    _w("参数：")
    rows = [{"name": p.name, "type": p.type, "default": repr(p.default),
             "choices": ",".join(map(str, getattr(p, "choices", None) or [])),
             "req": "是" if p.required else "",
             "help": getattr(p, "help", "")}
            for p in cls.param_specs()]
    table(rows, [("name", "参数名"), ("type", "类型"), ("default", "默认值"),
                 ("choices", "可选值"), ("req", "必填"), ("help", "说明")],
          widths={"choices": 30, "help": 60})
    return EXIT_OK


def cmd_algo_template(a: argparse.Namespace) -> int:
    text = custom_algo.new_template()
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        ok(f"模板已写入 {a.out}")
    else:
        _w(text)
    return EXIT_OK


def cmd_algo_add(a: argparse.Namespace) -> int:
    p = Path(a.file)
    if not p.exists():
        err(f"文件不存在：{a.file}")
        return EXIT_FAIL
    src = p.read_text(encoding="utf-8")
    cls, msg = custom_algo.validate_source(src)
    if cls is None:
        err(f"校验未通过：{msg}")
        return EXIT_VALIDATE
    passed, detail = custom_algo.smoke_test(cls)
    if not passed and not a.skip_smoke:
        err(f"冒烟测试未通过：{detail}")
        return EXIT_VALIDATE
    # save_algo 第二个返回值同样是「错误信息」，空串才是干净成功
    aid, err_msg = custom_algo.save_algo(a.id or "", a.name or cls.ALGO_NAME, src)
    if not aid:
        err(f"注册失败：{err_msg}")
        return EXIT_VALIDATE
    ok(f"已注册算法 {aid}（{cls.ALGO_NAME}）→ user_algorithms/")
    if err_msg:
        warn(err_msg)
    if passed:
        _w(f"     冒烟测试：{detail}")
    return EXIT_OK


def cmd_algo_rm(a: argparse.Namespace) -> int:
    if not custom_algo.is_custom(a.algo_id):
        err(f"{a.algo_id} 不是自定义算法，内置算法不可删除")
        return EXIT_FAIL
    # delete_algo 的返回值语义是「错误信息」，空串代表成功，
    # 不能直接当成功提示打印（否则失败时会把错误写成 [OK]）
    msg = custom_algo.delete_algo(a.algo_id)
    if msg:
        err(msg)
        return EXIT_FAIL
    ok(f"已删除自定义算法 {a.algo_id}")
    return EXIT_OK


def cmd_algo_check(a: argparse.Namespace) -> int:
    p = Path(a.file)
    if not p.exists():
        err(f"文件不存在：{a.file}")
        return EXIT_FAIL
    src = p.read_text(encoding="utf-8")
    cls, msg = custom_algo.validate_source(src)
    if cls is None:
        err(f"校验未通过：{msg}")
        return EXIT_VALIDATE
    ok(f"静态校验通过：{cls.ALGO_ID} {cls.ALGO_NAME}")
    passed, detail = custom_algo.smoke_test(cls)
    (_w if passed else err)(f"冒烟测试{'通过' if passed else '未通过'}：{detail}")
    return EXIT_OK if passed else EXIT_VALIDATE


def cmd_algo_src(a: argparse.Namespace) -> int:
    if not custom_algo.is_custom(a.algo_id):
        err(f"{a.algo_id} 不是自定义算法")
        return EXIT_FAIL
    _w(custom_algo.read_source(a.algo_id))
    return EXIT_OK


# ---------------------------------------------------------------------------
# 规则库
# ---------------------------------------------------------------------------
RULE_COLS = [("rule_id", "规则ID"), ("name", "规则名"), ("code", "代号"),
             ("code_name", "测点名称"), ("algos", "算法"), ("source", "来源")]

# CSV 导出用：比表格多带等级与说明，少带测点名称（导出后按需自取）
RULE_CSV_COLUMNS = [("rule_id", "规则ID"), ("name", "规则名"), ("code", "代号"),
                    ("algos", "算法"), ("level", "等级"), ("source", "来源"),
                    ("note", "说明"), ("params", "算法参数 JSON")]


def _rule_row(r: dict) -> dict:
    return {
        "rule_id": r.get("rule_id", ""),
        "name": r.get("name", ""),
        "code": r.get("code", ""),
        "code_name": r.get("code_name", ""),
        "algos": ",".join(a.get("algo_id", "") for a in (r.get("algorithms") or [])),
        "source": r.get("source", ""),
        "level": ",".join(sorted({str((a.get("params") or {}).get("level", ""))
                                  for a in (r.get("algorithms") or [])})),
        "note": r.get("note", ""),
    }


def cmd_rule_list(a: argparse.Namespace) -> int:
    rows = [_rule_row(r) for r in store.list_rules()]
    if a.code:
        rows = [r for r in rows if r["code"] == a.code]
    if a.source:
        rows = [r for r in rows if r["source"] == a.source]
    if a.scene:
        sc = store.get_scene(a.scene)
        if not sc:
            err(f"场景不存在：{a.scene}")
            return EXIT_FAIL
        ids = set(sc["rule_ids"])
        rows = [r for r in rows if r["rule_id"] in ids]
    cols = RULE_COLS if not a.verbose else RULE_COLS + [("level", "等级"), ("note", "判据/说明")]
    emit(rows, cols, a.format, out=a.out)
    return EXIT_OK


def cmd_rule_show(a: argparse.Namespace) -> int:
    r = store.get_rule(a.rule_id)
    if not r:
        err(f"规则不存在：{a.rule_id}")
        return EXIT_FAIL
    if a.format == "json":
        _w(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    _w(f"规则ID   : {r['rule_id']}")
    _w(f"规则名   : {r['name']}")
    _w(f"测点代号 : {r['code']}（{r.get('code_name', '')}）")
    _w(f"值域形态 : {r.get('kind', '')}  单位: {r.get('unit') or '-'}  "
       f"值域: [{r.get('low')}, {r.get('high')}]  位宽: {r.get('bit_width')}")
    _w(f"来源     : {r.get('source', '')}")
    _w(f"说明     : {r.get('note') or '-'}")
    _w("")
    _w("关联算法：")
    rows = []
    for x in r.get("algorithms") or []:
        params = x.get("params") or {}
        rows.append({
            "algo": x.get("algo_id", ""),
            "level": params.get("level", ""),
            "params": ", ".join(f"{k}={v}" for k, v in params.items() if k != "level"),
            "note": x.get("note", ""),
        })
    table(rows, [("algo", "算法"), ("level", "等级"), ("params", "参数"), ("note", "配置说明")],
          widths={"params": 70, "note": 44})
    return EXIT_OK


def _parse_pairs(items: list[str] | None) -> dict:
    """'k=v' 列表 → dict，值自动转数字/布尔，其余保持字符串。"""
    out: dict[str, Any] = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"参数格式应为 key=value：{it}")
        k, v = it.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def _meta_snapshot(code: str) -> dict:
    """取测点元数据快照，随规则一起入库。

    规则表里的 code_name/kind/unit/low/high/bit_width 是**建规则时的快照**，
    不是每次都去查目录：这样以后 telemetry.yaml 改了值域，老规则的判据口径
    不会跟着悄悄变。命令行建规则时如果不填这些，规则详情页会出现
    「SPN1001（）」「值域 [None, None]」这种空壳，所以要在这里补齐。
    """
    try:
        d = engine.build_meta(code)
    except Exception:  # noqa: BLE001  未登记代号时按兜底值走，不阻断建规则
        return {}
    raw = (d.raw or {}).get("def", {})
    return {
        "code_name": raw.get("name") or d.name or "",
        "kind": d.kind.value,
        "unit": raw.get("unit") or d.unit or "",
        "low": raw.get("low", d.primary_normal().low),
        "high": raw.get("high", d.primary_normal().high),
        "bit_width": raw.get("bit_width", d.bit_width),
        "quantum": raw.get("quantum", d.quantum),
    }


def _build_rule_args(a: argparse.Namespace) -> dict:
    """从命令行参数组装规则 dict（不含 rule_id 时由 store 生成）。"""
    algos = []
    for spec in a.algo or []:
        # 形如 A-01:k=3,low=0.5,level=严重
        if ":" in spec:
            aid, rest = spec.split(":", 1)
            params = _parse_pairs([x for x in rest.split(",") if x.strip()])
        else:
            aid, params = spec, {}
        algos.append({"algo_id": aid.strip(), "params": params, "note": ""})
    if not algos:
        raise ValueError("至少需要一个 --algo")
    # 参数校验（与 UI 同一套 ParamSpec）
    from algorithm.base import ParamError, validate_params
    for x in algos:
        cls = alg.get(x["algo_id"])
        try:
            validate_params(cls.param_specs(), x["params"])
        except ParamError as exc:
            raise ValueError(f"{x['algo_id']} 参数不合法：{exc}") from exc
    return {
        "rule_id": a.rule_id or "",
        "name": a.name,
        "code": a.code,
        "note": a.note or "",
        "source": a.source,
        "algorithms": algos,
        **_meta_snapshot(a.code),
    }


def cmd_rule_add(a: argparse.Namespace) -> int:
    try:
        rule = _build_rule_args(a)
    except (ValueError, KeyError) as exc:
        err(str(exc))
        return EXIT_VALIDATE
    if not rule["rule_id"]:
        import uuid
        rule["rule_id"] = f"R-{uuid.uuid4().hex[:8]}"
    saved = store.upsert_rule(rule, new=True)
    ok(f"已保存规则 {saved['rule_id']}（{saved['name']}）")
    if a.scene:
        _append_to_scene(a.scene, [saved["rule_id"]])
    return EXIT_OK


def cmd_rule_edit(a: argparse.Namespace) -> int:
    old = store.get_rule(a.rule_id)
    if not old:
        err(f"规则不存在：{a.rule_id}")
        return EXIT_FAIL
    upd = dict(old)
    if a.name:
        upd["name"] = a.name
    if a.note is not None:
        upd["note"] = a.note
    if a.algo:
        try:
            built = _build_rule_args(argparse.Namespace(
                algo=a.algo, code=old["code"], name=upd["name"], note=upd["note"],
                rule_id=old["rule_id"], source=old.get("source", "user")))
        except (ValueError, KeyError, AttributeError) as exc:
            err(str(exc))
            return EXIT_VALIDATE
        upd["algorithms"] = built["algorithms"]
    store.upsert_rule(upd, new=False)
    ok(f"已更新规则 {a.rule_id}")
    return EXIT_OK


def cmd_rule_rm(a: argparse.Namespace) -> int:
    if not store.get_rule(a.rule_id):
        err(f"规则不存在：{a.rule_id}")
        return EXIT_FAIL
    store.delete_rule(a.rule_id)
    ok(f"已删除规则 {a.rule_id}")
    return EXIT_OK


def cmd_rule_export(a: argparse.Namespace) -> int:
    rules = store.list_rules()
    if a.code:
        rules = [r for r in rules if r["code"] == a.code]
    if a.scene:
        sc = store.get_scene(a.scene)
        if not sc:
            err(f"场景不存在：{a.scene}")
            return EXIT_FAIL
        ids = set(sc["rule_ids"])
        rules = [r for r in rules if r["rule_id"] in ids]
    if a.format == "csv":
        # CSV 是给人看/给 Excel 吃的，拍平算法列表；JSON 保留完整结构以便回导
        rows = [_rule_row(r) | {"params": json.dumps(r.get("algorithms") or [],
                                                    ensure_ascii=False, default=str)}
                for r in rules]
        text = report.csv_text(rows, RULE_CSV_COLUMNS)
    elif a.format == "table":
        table([_rule_row(r) for r in rules], RULE_COLS,
              widths={"name": 24, "code_name": 26, "note": 40})
        return EXIT_OK
    else:
        text = json.dumps(rules, ensure_ascii=False, indent=2, default=str)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        ok(f"已导出 {len(rules)} 条规则 → {a.out}")
    else:
        _w(text)
    return EXIT_OK


def cmd_rule_import(a: argparse.Namespace) -> int:
    p = Path(a.file)
    if not p.exists():
        err(f"文件不存在：{a.file}")
        return EXIT_FAIL
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err(f"JSON 解析失败：{exc}")
        return EXIT_FAIL
    items = data if isinstance(data, list) else [data]
    n = 0
    for r in items:
        if not isinstance(r, dict) or not r.get("code"):
            warn(f"跳过无效条目：{r}")
            continue
        store.upsert_rule(r, new=not r.get("rule_id"))
        n += 1
    ok(f"已导入 {n} 条规则")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 分组场景
# ---------------------------------------------------------------------------
def _append_to_scene(scene_id: str, rule_ids: list[str]) -> None:
    sc = store.get_scene(scene_id)
    if not sc:
        warn(f"场景不存在，未加入：{scene_id}")
        return
    merged = list(sc["rule_ids"])
    for rid in rule_ids:
        if rid not in merged:
            merged.append(rid)
    store.upsert_scene({"scene_id": scene_id, "name": sc["name"], "desc": sc["desc"],
                        "rule_ids": merged, "source": sc.get("source", "user")})
    ok(f"已加入场景「{sc['name']}」，现共 {len(merged)} 条规则")


def cmd_scene_list(a: argparse.Namespace) -> int:
    rows = [{"scene_id": s["scene_id"], "name": s["name"], "rules": len(s["rule_ids"]),
             "source": s.get("source", ""), "desc": s.get("desc", "")}
            for s in store.list_scenes()]
    emit(rows, [("scene_id", "场景ID"), ("name", "场景名"), ("rules", "规则数"),
                ("source", "来源"), ("desc", "说明")], a.format, out=a.out,
         widths={"desc": 60})
    return EXIT_OK


def cmd_scene_show(a: argparse.Namespace) -> int:
    sc = store.get_scene(a.scene_id)
    if not sc:
        err(f"场景不存在：{a.scene_id}")
        return EXIT_FAIL
    if a.format == "json":
        _w(json.dumps(sc, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    _w(f"场景ID : {sc['scene_id']}")
    _w(f"名称   : {sc['name']}")
    _w(f"说明   : {sc.get('desc') or '-'}")
    _w(f"规则数 : {len(sc['rule_ids'])}")
    _w("")
    rows = [_rule_row(r) for r in store.get_rules(sc["rule_ids"])]
    table(rows, RULE_COLS)
    return EXIT_OK


def cmd_scene_add(a: argparse.Namespace) -> int:
    if store.get_scene(a.scene_id):
        err(f"场景已存在：{a.scene_id}")
        return EXIT_FAIL
    ids = [x.strip() for x in (a.rules or "").split(",") if x.strip()]
    missing = [x for x in ids if not store.get_rule(x)]
    if missing:
        err(f"规则不存在：{missing}")
        return EXIT_FAIL
    store.upsert_scene({"scene_id": a.scene_id, "name": a.name or a.scene_id,
                        "desc": a.desc or "", "rule_ids": ids, "source": "user"})
    ok(f"已创建场景 {a.scene_id}（{len(ids)} 条规则）")
    return EXIT_OK


def cmd_scene_edit(a: argparse.Namespace) -> int:
    sc = store.get_scene(a.scene_id)
    if not sc:
        err(f"场景不存在：{a.scene_id}")
        return EXIT_FAIL
    upd = dict(sc)
    if a.name:
        upd["name"] = a.name
    if a.desc is not None:
        upd["desc"] = a.desc
    if a.add:
        for rid in [x.strip() for x in a.add.split(",") if x.strip()]:
            if not store.get_rule(rid):
                err(f"规则不存在：{rid}")
                return EXIT_FAIL
            if rid not in upd["rule_ids"]:
                upd["rule_ids"].append(rid)
    if a.remove:
        for rid in [x.strip() for x in a.remove.split(",") if x.strip()]:
            if rid in upd["rule_ids"]:
                upd["rule_ids"].remove(rid)
    if a.rules is not None:
        ids = [x.strip() for x in a.rules.split(",") if x.strip()]
        missing = [x for x in ids if not store.get_rule(x)]
        if missing:
            err(f"规则不存在：{missing}")
            return EXIT_FAIL
        upd["rule_ids"] = ids
    store.upsert_scene(upd, new=False)
    ok(f"已更新场景 {a.scene_id}（{len(upd['rule_ids'])} 条规则）")
    return EXIT_OK


def cmd_scene_rm(a: argparse.Namespace) -> int:
    if not store.get_scene(a.scene_id):
        err(f"场景不存在：{a.scene_id}")
        return EXIT_FAIL
    store.delete_scene(a.scene_id)
    ok(f"已删除场景 {a.scene_id}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 预置
# ---------------------------------------------------------------------------
def cmd_preset_reload(a: argparse.Namespace) -> int:
    changed = presets.seed(force=a.force)
    total = len(presets.preset_rule_ids())
    if changed:
        ok(f"预置已{'重建' if a.force else '补齐'}，文件中共 {total} 条规则")
    else:
        ok(f"预置已是最新（{total} 条规则），无需写入")
    for s in store.list_scenes():
        if s.get("source") == "preset":
            _w(f"     场景 {s['name']}：{len(s['rule_ids'])} 条规则")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------
def _print_outcome_summary(outs: list, title: str, t0: int, t1: int) -> None:
    sm = report.summary_of(outs)
    _w(f"=== {title} ===")
    _w(f"时间窗口: {report.iso(t0)} ~ {report.iso(t1)}")
    _w(f"规则 {sm['rules']} 条 | 命中 {sm['hit_rules']} 条 | 异常点 {sm['total']} 个")
    lv = " ".join(f"{k}×{v}" for k, v in sm["by_level_cn"].items() if v)
    _w(f"等级分布: {lv or '无'}")
    st = " ".join(f"{k}×{v}" for k, v in sm["status"].items())
    _w(f"算法状态: {st or '无'}")
    _w("")


def _deliver(outs: list, a: argparse.Namespace, title: str, t0: int, t1: int) -> int:
    """按 --format 输出；csv 时逐规则分节。"""
    if a.format == "json":
        payload = report.to_json(outs, t0, t1, title)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
            ok(f"已写入 {a.out}")
        else:
            _w(text)
        return EXIT_OK

    lines: list[str] = []
    if a.format == "csv":
        for o in outs:
            rows = report.point_rows(o.points)
            if not rows:
                continue
            if a.csv_per_rule:
                name = f"{o.rule['rule_id']}_{o.rule['name']}".replace("/", "_")
                path = Path(a.out).with_name(f"{Path(a.out).stem}.{name}.csv") if a.out else None
                text = report.csv_text(rows, report.POINT_CSV_COLUMNS)
                if path:
                    path.write_text(text, encoding="utf-8")
                    _w(f"  写入 {path}（{len(rows)} 行）")
                else:
                    _w(f"# {o.rule['rule_id']} {o.rule['name']}")
                    _w(text)
            else:
                lines.append(f"# {o.rule['rule_id']} {o.rule['name']}")
                lines.append(report.csv_text(rows, report.POINT_CSV_COLUMNS))
        if not a.csv_per_rule:
            text = "\n".join(lines)
            if a.out:
                Path(a.out).write_text(text, encoding="utf-8")
                ok(f"已写入 {a.out}")
            else:
                _w(text)
        elif a.out:
            ok(f"已按规则拆分写入 {Path(a.out).parent}")
        return EXIT_OK

    # 默认：人读表格
    if not a.quiet:
        _print_outcome_summary(outs, title, t0, t1)
    table(report.outcome_rows(outs),
          [("rule_id", "规则ID"), ("name", "规则名"), ("code", "代号"),
           ("algos", "算法"), ("points", "异常点"), ("levels", "等级分布"),
           ("status", "状态"), ("notes", "备注")],
          widths={"name": 26, "algos": 18, "notes": 34})
    if a.points:
        for o in outs:
            rows = report.point_rows(o.points)
            if not rows:
                continue
            _w("")
            _w(f"── {o.rule['rule_id']} {o.rule['name']}（{len(rows)} 个异常点）")
            table(rows, [("no", "#"), ("time", "时间"), ("level_cn", "等级"),
                         ("hit_type", "命中类型"), ("algo", "算法"),
                         ("value_s", "触发值"), ("threshold_s", "阈值"),
                         ("duration_s", "持续(s)"), ("dimension", "定位"), ("note", "说明")],
                  widths={"algo": 18, "threshold_s": 20, "dimension": 26, "note": 40})
    return EXIT_OK


def cmd_detect_scene(a: argparse.Namespace) -> int:
    sc = store.get_scene(a.scene)
    if not sc:
        err(f"场景不存在：{a.scene}。可用 --scene 指定，先跑 `scene list` 看有哪些")
        return EXIT_FAIL
    try:
        t0, t1 = report.window(a.start, a.end, a.hours)
    except ValueError as exc:
        err(str(exc))
        return EXIT_ARGS
    min_hits = a.min_hits if a.min_hits is not None else C.SIM_MIN_HITS
    outs = engine.analyze_scene(sc, t0, t1, min_hits=min_hits)
    store.log_run(sc["scene_id"], "scene", t0, t1, report.summary_of(outs))
    return _deliver(outs, a, f"场景 · {sc['name']}", t0, t1)


def _resolve_rules(a: argparse.Namespace) -> list[dict] | None:
    if a.rules:
        ids = [x.strip() for x in a.rules.split(",") if x.strip()]
        return store.get_rules(ids)
    if a.code:
        return [r for r in store.list_rules() if r["code"] == a.code]
    return None


def cmd_detect_custom(a: argparse.Namespace) -> int:
    if not a.code and not a.rules:
        err("需要 --code 或 --rules 指定要检测的对象")
        return EXIT_ARGS
    try:
        t0, t1 = report.window(a.start, a.end, a.hours)
    except ValueError as exc:
        err(str(exc))
        return EXIT_ARGS
    rules = _resolve_rules(a)
    if rules is None:
        err("未找到规则")
        return EXIT_FAIL
    # --code 模式：该代号下没有规则时，按需自动生成，生成结果直接用于本次分析
    if not rules and a.code and a.generate:
        generated: list[dict] = []
        rc = _generate_rules(a, t0, t1, sink=generated)
        if rc != EXIT_OK:
            return rc
        # 加了 --save 时规则已入库（带正式 rule_id），优先用库里的；
        # 没加 --save 就用内存里这份，本次分析照样能跑。
        rules = [r for r in store.list_rules() if r["code"] == a.code] or generated
        if rules:
            _w("")
    if not rules:
        err(f"代号 {a.code} 下没有可用规则；加 --generate 让模型现场生成")
        return EXIT_FAIL
    min_hits = a.min_hits if a.min_hits is not None else C.SIM_MIN_HITS
    outs = engine.analyze_rules(rules, t0, t1, min_hits=min_hits)
    title = f"自定义 · {a.code or '选定规则'}"
    if a.code:
        store.log_run(f"code:{a.code}", "custom", t0, t1, report.summary_of(outs))
    return _deliver(outs, a, title, t0, t1)


# ---------------------------------------------------------------------------
# 大模型
# ---------------------------------------------------------------------------
def _meta_row(code: str) -> dict:
    d = engine.build_meta(code)
    raw = (d.raw or {}).get("def", {})
    return {
        "name": raw.get("name") or d.name,
        "unit": raw.get("unit") or d.unit,
        "kind": d.kind.value,
        "low": raw.get("low", d.primary_normal().low),
        "high": raw.get("high", d.primary_normal().high),
        "bit_width": raw.get("bit_width", d.bit_width),
        "quantum": raw.get("quantum", d.quantum),
    }


def _stats_for(code: str, t0: int, t1: int) -> dict:
    """从已落盘数据算画像；没有数据就现造一段干净的。"""
    import numpy as np
    loaded = store.load_series(code, t0, t1)
    if loaded:
        _, v, _ = loaded
    else:
        from .simulate import simulate_rule
        dummy = {"rule_id": f"__stats_{code}", "code": code,
                 "algorithms": [{"algo_id": "A-27", "params": {"gap_k": 2.5}}]}
        sim = simulate_rule(dummy, t0, t1, min_hits=C.SIM_MIN_HITS)
        v = sim.series.v
    v = np.asarray(v, dtype=float)
    v = v[~np.isnan(v)]
    if v.size == 0:
        return {}
    d = np.abs(np.diff(v))
    return {
        "n": int(v.size), "mean": float(np.mean(v)), "std": float(np.std(v)),
        "min": float(np.min(v)), "max": float(np.max(v)),
        "p01": float(np.percentile(v, 1)), "p99": float(np.percentile(v, 99)),
        "nunique": int(np.unique(v).size),
        "max_run_s": 0.0,
        "diff_p99": float(np.percentile(d, 99)) if d.size else 0.0,
    }


def cmd_llm_ping(a: argparse.Namespace) -> int:
    t0 = time.time()
    online, msg = llm.check_connection(timeout=a.timeout)
    dt = time.time() - t0
    _w(f"地址 : {C.LLM_BASE_URL}")
    _w(f"模型 : {C.LLM_MODEL}")
    _w(f"密钥 : {'（未配置，不发送 Authorization）' if not C.LLM_API_KEY else '已配置'}")
    if online:
        ok(f"{msg}（{dt:.1f}s）")
        return EXIT_OK
    err(f"不可达：{msg}（{dt:.1f}s）")
    return EXIT_DEP


def _generate_rules(a: argparse.Namespace, t0: int, t1: int,
                    sink: list[dict] | None = None) -> int:
    """生成规则。``sink`` 非空时把生成的规则塞进去，供调用方继续分析。

    ``_generate_rules`` 本身不负责把规则返回，因为它有两个出口（打印 / 入库），
    前者不加 --save 时规则不会进数据库。用 sink 传出，避免调用方只能靠查库拿规则
    ——那样 --save 没加时会查不到，误判成失败。
    """
    row = _meta_row(a.code)
    stats = _stats_for(a.code, t0, t1)
    res = llm.generate(a.code, row, stats, 1.0, use_llm=not a.no_llm)
    # sink 非空表示「生成完还要接着分析」，此时 stdout 留给最终结果，
    # 过程信息走 stderr——否则 -f json 的输出前面会挂几行中文，管道里解析不了。
    chatty = _w if sink is None else (lambda text="": print(text, file=sys.stderr))
    chatty(f"模式: {res.mode} | {res.message}")
    if res.analysis:
        chatty(f"分析: {res.analysis}")
    if not res.rules:
        err("未生成任何规则")
        return EXIT_DEP if res.mode != "fallback" else EXIT_FAIL
    # 生成时就把 rule_id 定下来：不写库的规则也要在表格与本次分析里有标识，
    # 否则「规则ID」列整列空白，出问题不知道是哪条。
    import uuid
    produced = [dict(r, rule_id=r.get("rule_id") or f"R-{uuid.uuid4().hex[:8]}")
                for r in res.rules]
    if sink is not None:
        sink.extend(produced)
    if not a.save:
        # 加了 sink 时规则马上要送去分析，这里只列一句清单，不铺表格
        if sink is None or a.format == "table":
            table([_rule_row(r) for r in produced], RULE_COLS)
        chatty("")
        chatty("（未入库；加 --save 写入规则库）" if sink is None
               else "（未入库；本次分析已使用，加 --save 可写入规则库）")
        return EXIT_OK
    saved = []
    for r in produced:
        saved.append(store.upsert_rule(r, new=True)["rule_id"])
    chatty(f"[OK] 已入库 {len(saved)} 条规则：{', '.join(saved)}")
    if a.scene:
        _append_to_scene(a.scene, saved)
    return EXIT_OK


def cmd_llm_generate(a: argparse.Namespace) -> int:
    try:
        t0, t1 = report.window(a.start, a.end, a.hours)
    except ValueError as exc:
        err(str(exc))
        return EXIT_ARGS
    return _generate_rules(a, t0, t1)


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def cmd_data_list(a: argparse.Namespace) -> int:
    rows = []
    for d in store.list_datasets(a.code, limit=a.limit):
        rows.append({
            "code": d.get("code", ""), "start": report.iso(d.get("t_start")),
            "end": report.iso(d.get("t_end")), "points": d.get("points", ""),
            "source": d.get("source", ""), "path": d.get("path", ""),
        })
    emit(rows, [("code", "代号"), ("start", "起始"), ("end", "结束"),
                ("points", "点数"), ("source", "来源"), ("path", "文件")],
         a.format, out=a.out)
    return EXIT_OK


def cmd_data_stats(a: argparse.Namespace) -> int:
    try:
        t0, t1 = report.window(a.start, a.end, a.hours)
    except ValueError as exc:
        err(str(exc))
        return EXIT_ARGS
    st = store.dataset_stats(a.code, t0, t1)
    if not st:
        err(f"区间内无 {a.code} 的落盘数据")
        return EXIT_FAIL
    # 带上代号与窗口，脚本里一次拿全，不用自己记上下文
    st = {"code": a.code, "start": report.iso(t0), "end": report.iso(t1), **st}
    if a.format == "json":
        _w(json.dumps(st, ensure_ascii=False, indent=2, default=str))
    else:
        for k, v in st.items():
            _w(f"{_pad(k, 12)}: {v}")
    return EXIT_OK


def cmd_data_export(a: argparse.Namespace) -> int:
    try:
        t0, t1 = report.window(a.start, a.end, a.hours)
    except ValueError as exc:
        err(str(exc))
        return EXIT_ARGS
    loaded = store.load_series(a.code, t0, t1)
    if not loaded:
        err(f"区间内无 {a.code} 的落盘数据")
        return EXIT_FAIL
    t, v, q = loaded
    buf = ["t,v,quality"]
    for i in range(len(t)):
        buf.append(f"{int(t[i])},{float(v[i])},{int(q[i]) if q is not None else 0}")
    text = "\n".join(buf) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        ok(f"已导出 {len(t)} 点到 {a.out}")
    else:
        _w(text)
    return EXIT_OK


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _flatten(d: dict, prefix: str = "") -> list[dict]:
    rows = []
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            rows.extend(_flatten(v, key))
        else:
            rows.append({"key": key, "value": v if not isinstance(v, str) else v})
    return rows


def cmd_config_show(a: argparse.Namespace) -> int:
    if a.file == "app":
        data = yamls.file("app.yaml")
    elif a.file == "telemetry":
        data = yamls.file("telemetry.yaml")
    elif a.file == "presets":
        data = yamls.file("presets.yaml")
    elif a.file == "prompts":
        data = yamls.file("prompts.yaml")
    else:
        data = yamls.file("theme.yaml")
    # 去掉超长与不需要展示的部分
    if a.file == "theme":
        data = {k: v for k, v in data.items() if k != "css"}
    if a.file == "prompts":
        data = {k: (v if k not in ("system", "user") else f"<{len(str(v))} 字符>")
                for k, v in data.items()}
    if a.file == "presets":
        data = {"scenes": [
            {"id": s["id"], "name": s["name"], "rules": len(s.get("rules") or [])}
            for s in data.get("scenes") or []],
            "rules_count": len(data.get("rules") or [])}
    if a.format == "json":
        _w(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    rows = _flatten(data)
    if a.grep:
        rows = [r for r in rows if a.grep.lower() in str(r["key"]).lower()
                or a.grep.lower() in str(r["value"]).lower()]
    table(rows, [("key", "配置项"), ("value", "值")], widths={"value": 90})
    _w("")
    _w(f"生效路径: {C.DATA_DIR} | 数据库: {C.DB_PATH}")
    return EXIT_OK


def cmd_config_reload(a: argparse.Namespace) -> int:
    yamls.reload_all()
    ws = yamls.warnings()
    ok("配置已重载")
    if ws:
        for w in ws:
            warn(w)
    else:
        _w("     无告警")
    return EXIT_OK


def cmd_config_check(a: argparse.Namespace) -> int:
    """校验全部 YAML：解析、结构、预置引用、算法参数。"""
    bad = 0
    for name in ("app", "telemetry", "presets", "prompts", "theme"):
        try:
            yamls.file(f"{name}.yaml")
            ok(f"{name}.yaml 解析正常")
        except Exception as exc:  # noqa: BLE001
            err(f"{name}.yaml 解析失败：{exc}")
            bad += 1
    for w in yamls.warnings():
        warn(w)
    # 预置规则参数逐条校验
    from algorithm.base import ParamError, validate_params
    for r in presets.build_preset_rules():
        for x in r["algorithms"]:
            try:
                validate_params(alg.get(x["algo_id"]).param_specs(), x["params"])
            except (ParamError, KeyError) as exc:
                err(f"{r['rule_id']} / {x['algo_id']}: {exc}")
                bad += 1
    if bad:
        err(f"共 {bad} 处问题")
        return EXIT_VALIDATE
    ok("全部配置与预置规则校验通过")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def _add_format(p: argparse.ArgumentParser, *, out: bool = True) -> None:
    p.add_argument("--format", "-f", choices=["table", "json", "csv"], default="table",
                   help="输出格式（默认 table）")
    if out:
        p.add_argument("--out", "-o", default=None, help="写入文件而不是打印（json/csv 适用）")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vigil",
        description="VIGIL 工业设备智能监测平台 · 异常数据检测 · 命令行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  vigil algo list                              列出全部算法
  vigil algo show A-01                         查看算法参数规格
  vigil rule list --code SPN1001               按代号筛规则
  vigil scene list                             列出分组场景
  vigil detect scene -s S-SPIN -H 12          跑「旋转机械」场景最近 12 小时
  vigil detect scene -s S-ELEC -H 12 --points  连异常点明细一起打印
  vigil detect custom -c SPN1001 -H 12 --generate --save
  vigil detect custom -c SPN1001 -H 12 -f json -o result.json
  vigil llm ping                               探测本地大模型
  vigil config check                           校验全部 YAML
  vigil config show app --grep llm             查 llm 相关配置
""")
    sub = ap.add_subparsers(dest="group", metavar="<命令组>")

    # --- algo ---
    pa = sub.add_parser("algo", help="算法库：查看 / 新增 / 删除自定义算法")
    sa = pa.add_subparsers(dest="cmd", metavar="<动作>")

    q = sa.add_parser("list", help="列出算法")
    q.add_argument("--detector-only", action="store_true", help="只看检测器")
    q.add_argument("--custom-only", action="store_true", help="只看自定义算法")
    _add_format(q)
    q.set_defaults(func=cmd_algo_list)

    q = sa.add_parser("show", help="查看算法详情与参数规格")
    q.add_argument("algo_id")
    q.add_argument("--format", "-f", choices=["table", "json"], default="table")
    q.set_defaults(func=cmd_algo_show)

    q = sa.add_parser("template", help="输出自定义算法模板")
    q.add_argument("--out", "-o")
    q.set_defaults(func=cmd_algo_template)

    q = sa.add_parser("add", help="注册自定义算法（.py 文件）")
    q.add_argument("file")
    q.add_argument("--id", default=None, help="算法编号，缺省取源码里的 ALGO_ID")
    q.add_argument("--name", default=None, help="算法名，缺省取源码里的 ALGO_NAME")
    q.add_argument("--skip-smoke", action="store_true", help="跳过冒烟测试")
    q.set_defaults(func=cmd_algo_add)

    q = sa.add_parser("rm", help="删除自定义算法")
    q.add_argument("algo_id")
    q.set_defaults(func=cmd_algo_rm)

    q = sa.add_parser("check", help="静态校验 + 冒烟测试（不入库）")
    q.add_argument("file")
    q.set_defaults(func=cmd_algo_check)

    q = sa.add_parser("src", help="查看自定义算法源码")
    q.add_argument("algo_id")
    q.set_defaults(func=cmd_algo_src)

    # --- rule ---
    pr = sub.add_parser("rule", help="规则库：增删改查 / 导入导出")
    sr = pr.add_subparsers(dest="cmd", metavar="<动作>")

    q = sr.add_parser("list", help="列出规则")
    q.add_argument("--code", help="按测点代号筛选")
    q.add_argument("--source", help="按来源筛选：preset / llm / user")
    q.add_argument("--scene", help="只看某场景下的规则")
    q.add_argument("--verbose", "-v", action="store_true", help="显示等级与说明")
    _add_format(q)
    q.set_defaults(func=cmd_rule_list)

    q = sr.add_parser("show", help="查看单条规则详情")
    q.add_argument("rule_id")
    q.add_argument("--format", "-f", choices=["table", "json"], default="table")
    q.set_defaults(func=cmd_rule_show)

    q = sr.add_parser("add", help="新增规则")
    q.add_argument("--code", required=True, help="测点代号")
    q.add_argument("--name", required=True, help="规则名")
    q.add_argument("--algo", action="append", required=True,
                   help="算法与参数，形如 A-01:k=3,low=0.5,level=严重；可重复")
    q.add_argument("--note", default="", help="判据说明")
    q.add_argument("--rule-id", default=None)
    q.add_argument("--source", default="user")
    q.add_argument("--scene", default=None, help="同时加入该场景")
    q.set_defaults(func=cmd_rule_add)

    q = sr.add_parser("edit", help="修改规则（改算法参数用 --algo 覆盖全部）")
    q.add_argument("rule_id")
    q.add_argument("--name")
    q.add_argument("--note")
    q.add_argument("--algo", action="append", help="重设算法列表，形如 A-01:k=3")
    q.set_defaults(func=cmd_rule_edit)

    q = sr.add_parser("rm", help="删除规则")
    q.add_argument("rule_id")
    q.set_defaults(func=cmd_rule_rm)

    q = sr.add_parser("export", help="导出规则为 JSON / CSV")
    q.add_argument("--code")
    q.add_argument("--scene")
    q.add_argument("--out", "-o")
    q.add_argument("--format", "-f", choices=["json", "csv", "table"], default="json",
                   help="json 可回导；csv 给 Excel；table 直接看")
    q.set_defaults(func=cmd_rule_export)

    q = sr.add_parser("import", help="从 JSON 导入规则")
    q.add_argument("file")
    q.set_defaults(func=cmd_rule_import)

    # --- scene ---
    ps = sub.add_parser("scene", help="分组场景：增删改查")
    ss = ps.add_subparsers(dest="cmd", metavar="<动作>")

    q = ss.add_parser("list", help="列出场景")
    _add_format(q)
    q.set_defaults(func=cmd_scene_list)

    q = ss.add_parser("show", help="查看场景及其规则")
    q.add_argument("scene_id")
    q.add_argument("--format", "-f", choices=["table", "json"], default="table")
    q.set_defaults(func=cmd_scene_show)

    q = ss.add_parser("add", help="新建场景")
    q.add_argument("scene_id")
    q.add_argument("--name")
    q.add_argument("--desc")
    q.add_argument("--rules", default="", help="逗号分隔的规则 ID")
    q.set_defaults(func=cmd_scene_add)

    q = ss.add_parser("edit", help="修改场景")
    q.add_argument("scene_id")
    q.add_argument("--name")
    q.add_argument("--desc")
    q.add_argument("--add", help="追加规则 ID，逗号分隔")
    q.add_argument("--remove", help="移除规则 ID，逗号分隔")
    q.add_argument("--rules", help="整体重设规则列表，逗号分隔")
    q.set_defaults(func=cmd_scene_edit)

    q = ss.add_parser("rm", help="删除场景")
    q.add_argument("scene_id")
    q.set_defaults(func=cmd_scene_rm)

    # --- preset ---
    pp = sub.add_parser("preset", help="预置规则/场景")
    sp = pp.add_subparsers(dest="cmd", metavar="<动作>")
    q = sp.add_parser("reload", help="从 presets.yaml 补齐预置（--force 全量重建）")
    q.add_argument("--force", action="store_true", help="按 YAML 全量覆盖，会丢弃对预置规则的改动")
    q.set_defaults(func=cmd_preset_reload)

    # --- detect ---
    pd = sub.add_parser("detect", help="执行异常检测")
    sd = pd.add_subparsers(dest="cmd", metavar="<模式>")

    def _detect_opts(q: argparse.ArgumentParser) -> None:
        q.add_argument("--start", "-a", default=None, help="起始时间，如 '2026-11-17 00:00:00'")
        q.add_argument("--end", "-b", default=None, help="结束时间")
        q.add_argument("--hours", "-H", type=float, default=C.DEFAULT_WINDOW_HOURS,
                       help=f"未给起止时间时，取最近 N 小时（默认 {C.DEFAULT_WINDOW_HOURS:g}）")
        q.add_argument("--min-hits", type=int, default=None,
                       help=f"每条规则最少异常数（默认取配置 {C.SIM_MIN_HITS}）")
        q.add_argument("--points", "-p", action="store_true", help="同时打印每个异常点明细")
        q.add_argument("--quiet", "-q", action="store_true", help="不打印总览，只列规则结果")
        q.add_argument("--csv-per-rule", action="store_true", help="csv 模式下按规则拆成多个文件")
        _add_format(q)
        q.add_argument("--out-all", dest="out_all", action="store_true", help=argparse.SUPPRESS)

    q = sd.add_parser("scene", help="按分组场景分析")
    q.add_argument("--scene", "-s", default=None, help="场景 ID；缺省用第一个预置场景")
    _detect_opts(q)
    q.set_defaults(func=cmd_detect_scene)

    q = sd.add_parser("custom", help="按代号或指定规则分析")
    q.add_argument("--code", "-c", default=None, help="测点代号")
    q.add_argument("--rules", "-r", default=None, help="逗号分隔的规则 ID")
    q.add_argument("--generate", "-g", action="store_true",
                   help="该代号没有规则时，让模型现场生成")
    q.add_argument("--save", action="store_true", help="与 --generate 配合：生成的规则入库")
    q.add_argument("--no-llm", action="store_true", help="只用本地启发式，不调模型")
    _detect_opts(q)
    q.set_defaults(func=cmd_detect_custom)

    # --- llm ---
    pl = sub.add_parser("llm", help="本地大模型")
    sl = pl.add_subparsers(dest="cmd", metavar="<动作>")
    q = sl.add_parser("ping", help="连通性探测")
    q.add_argument("--timeout", type=float, default=C.LLM_PROBE_TIMEOUT)
    q.set_defaults(func=cmd_llm_ping)

    q = sl.add_parser("generate", help="按代号生成检测规则")
    q.add_argument("--code", "-c", required=True)
    q.add_argument("--start", "-a", default=None)
    q.add_argument("--end", "-b", default=None)
    q.add_argument("--hours", "-H", type=float, default=C.DEFAULT_WINDOW_HOURS)
    q.add_argument("--save", action="store_true", help="写入规则库")
    q.add_argument("--scene", "-s", default=None, help="保存后同时加入该场景")
    q.add_argument("--no-llm", action="store_true", help="只用本地启发式兜底")
    q.set_defaults(func=cmd_llm_generate)

    # --- data ---
    pt = sub.add_parser("data", help="落盘数据")
    st_ = pt.add_subparsers(dest="cmd", metavar="<动作>")
    q = st_.add_parser("list", help="列出已落盘的 Parquet")
    q.add_argument("--code")
    q.add_argument("--limit", type=int, default=200)
    _add_format(q)
    q.set_defaults(func=cmd_data_list)

    q = st_.add_parser("stats", help="数据统计")
    q.add_argument("--code", "-c", required=True)
    q.add_argument("--start", "-a", default=None)
    q.add_argument("--end", "-b", default=None)
    q.add_argument("--hours", "-H", type=float, default=C.DEFAULT_WINDOW_HOURS)
    q.add_argument("--format", "-f", choices=["table", "json"], default="table")
    q.set_defaults(func=cmd_data_stats)

    q = st_.add_parser("export", help="导出为 CSV（t,v,quality）")
    q.add_argument("--code", "-c", required=True)
    q.add_argument("--start", "-a", default=None)
    q.add_argument("--end", "-b", default=None)
    q.add_argument("--hours", "-H", type=float, default=C.DEFAULT_WINDOW_HOURS)
    q.add_argument("--out", "-o")
    q.set_defaults(func=cmd_data_export)

    # --- config ---
    pc = sub.add_parser("config", help="配置查看与校验")
    sc_ = pc.add_subparsers(dest="cmd", metavar="<动作>")
    q = sc_.add_parser("show", help="查看生效配置")
    q.add_argument("file", nargs="?", default="app",
                   choices=["app", "telemetry", "presets", "prompts", "theme"])
    q.add_argument("--grep", default=None, help="只显示键或值匹配的项")
    q.add_argument("--format", "-f", choices=["table", "json"], default="table")
    q.set_defaults(func=cmd_config_show)

    q = sc_.add_parser("reload", help="重新加载 YAML（热重载）")
    q.set_defaults(func=cmd_config_reload)

    q = sc_.add_parser("check", help="校验 YAML 与预置规则")
    q.set_defaults(func=cmd_config_check)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "func", None):
        # 没有子命令：打印帮助并给非零码，便于脚本判断
        ap.print_help()
        return EXIT_ARGS
    # 数据库可能还没建（命令行首次运行）
    store.init_db()
    # 自定义算法的注册表在进程内存里，Web 由 app.main 加载；
    # 命令行每次都是新进程，必须自己加载一遍，否则 user_algorithms/ 下的算法
    # 在 list/rule add 里看不到，会误以为注册失败。
    try:
        custom_algo.load_all()
    except Exception as exc:  # noqa: BLE001  坏文件不该拖垮整条命令
        warn(f"自定义算法加载有问题：{exc}")
    try:
        return int(a.func(a))
    except KeyboardInterrupt:
        err("已中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        err(f"{type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
