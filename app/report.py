"""报告层：把检测结果整理成「可直接展示/导出」的纯数据。

这里**不导入任何 UI 框架**，Web 界面与命令行共用同一份转换逻辑，
避免两边各写一套导致字段、说明文字、统计口径漂移。

主要出口：
    point_rows()      异常点 → 表格行（含 note 说明，与界面表格列一一对应）
    outcome_rows()    单条规则的结果 → 摘要行
    summary_rows()    场景/规则集合 → 总览行
    to_json()         结果 → 可 JSON 序列化的字典
    csv_text()        表格行 → CSV 文本（纯标准库，不依赖 pandas）
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import LEVEL_CN, LEVEL_KEYS, LEVEL_ORDER

TZ = timezone(timedelta(hours=8))


def iso(ms: int | None) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'（东八区）。"""
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(text: str) -> int:
    """接受毫秒时间戳、秒时间戳或 'YYYY-MM-DD HH:MM:SS'，返回毫秒。"""
    s = (text or "").strip()
    if not s:
        raise ValueError("时间不能为空")
    if s.isdigit():
        v = int(s)
        return v * 1000 if v < 10**11 else v
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=TZ).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间：{text}")


def window(t0: str | None, t1: str | None, hours: float) -> tuple[int, int]:
    """把 (起点, 终点) 文本解析成毫秒区间；两者都给则按给定，否则按 hours 取最近窗口。"""
    if t0 and t1:
        a, b = parse_ts(t0), parse_ts(t1)
        if b <= a:
            raise ValueError("结束时间必须晚于起始时间")
        return a, b
    end = parse_ts(t1) if t1 else int(datetime.now(TZ).timestamp() * 1000)
    if t0:
        return parse_ts(t0), end
    return end - int(hours * 3600 * 1000), end


def _point_note(ev: dict) -> str:
    """把算法证据压成一句人读说明（界面表格与 CSV 共用）。"""
    if not ev:
        return ""
    if "nature" in ev:
        return {"spike": "野值已回落", "step": "台阶未回落"}.get(ev["nature"], "")
    if ev.get("zone"):
        return f"区间 {ev.get('zone')}，越限 {ev.get('breach_count', '')} 点"
    if "r" in ev:
        return f"r={ev['r']}，参照 r={ev.get('r_ref')}"
    if "dev" in ev:
        return f"偏差 {ev['dev']}，触发 {ev.get('trigger', '')}"
    if "delta" in ev:
        return f"Δ={ev['delta']}，模 {ev.get('modulus', '')}"
    if "bit_index" in ev and ev.get("bit_index") is not None:
        return f"第 {ev['bit_index']} 位，字值 {ev.get('word_value')}"
    if "frozen_s" in ev:
        return f"冻结 {ev['frozen_s']} s / {ev.get('frozen_points')} 点"
    if "gap_s" in ev and ev.get("gap_s"):
        return f"缺口 {ev['gap_s']} s，缺 {ev.get('missing_points')} 点"
    return ""


def _point_dimension(p: dict) -> str:
    """「定位」列：优先用算法给出的细分维度，否则退到子系统 + 测点代号。

    多数算法不产 ``dimension``（它是可选的细分维度），只留空会让这一列
    在表格里整列空白，看的人不知道异常发生在哪台设备的哪个量上。
    """
    dim = (p.get("dimension") or "").strip()
    if dim:
        return dim
    parts = [x for x in ((p.get("system") or "").strip(),
                         (p.get("telemetry_code") or "").strip()) if x]
    return " / ".join(parts)


def point_rows(points: Iterable[dict]) -> list[dict]:
    """异常点 → 表格行。字段名与 config/app.yaml → ui.anomaly_columns 对应。"""
    rows = []
    for i, p in enumerate(points, start=1):
        rows.append({
            "no": i,
            "time": p.get("time", ""),
            "level": p.get("level", ""),
            "level_cn": p.get("level_cn") or LEVEL_CN.get(p.get("level"), ""),
            "hit_type": p.get("hit_type", ""),
            "algo": p.get("algorithm_id", ""),
            "value_s": p.get("value_s", ""),
            "threshold_s": p.get("threshold_s", ""),
            "duration_s": p.get("duration_s", 0),
            "dimension": _point_dimension(p),
            "note": _point_note(p.get("evidence") or {}),
        })
    return rows


def outcome_rows(outs: Iterable[Any]) -> list[dict]:
    """每条规则的检测结果 → 一行摘要。"""
    rows = []
    for o in outs:
        rule = o.rule
        # 按等级键计数，展示时再映射成中文。注意不能先映射成中文再 count(键)，
        # 那样「严重」和「L3」永远对不上，等级分布会恒为空。
        keys = [(p.get("level") or "") for p in o.points]
        rows.append({
            "rule_id": rule.get("rule_id", ""),
            "name": rule.get("name", ""),
            "code": rule.get("code", ""),
            "code_name": rule.get("code_name", ""),
            "algos": ",".join(sorted(o.status_summary)),
            "points": len(o.points),
            "hit_rules": 1 if o.points else 0,
            "levels": ",".join(f"{LEVEL_CN.get(lv, lv)}×{keys.count(lv)}"
                               for lv in reversed(list(LEVEL_ORDER))
                               if keys.count(lv)),
            "status": ",".join(sorted(set(o.status_summary.values()))) or "-",
            "notes": "｜".join(o.notes or []),
        })
    return rows


def summary_of(outs: Iterable[Any]) -> dict:
    """集合级统计：规则数/命中数/异常点数/等级分布/算法状态。"""
    outs = list(outs)
    total = sum(len(o.points) for o in outs)
    by_level = {lv: 0 for lv in LEVEL_KEYS}
    hit_rules = 0
    for o in outs:
        if o.points:
            hit_rules += 1
        for p in o.points:
            lv = p.get("level")
            by_level[lv] = by_level.get(lv, 0) + 1
    status: dict[str, int] = {}
    for o in outs:
        for s in o.status_summary.values():
            status[s] = status.get(s, 0) + 1
    return {
        "rules": len(outs),
        "hit_rules": hit_rules,
        "total": total,
        "by_level": by_level,
        "by_level_cn": {LEVEL_CN.get(k, k): v for k, v in by_level.items()},
        "status": status,
    }


def to_json(outs: list, t0: int, t1: int, title: str = "") -> dict:
    """完整结果（含每个异常点）→ 可 JSON 序列化的字典。"""
    payload: dict[str, Any] = {
        "title": title,
        "t0": int(t0),
        "t1": int(t1),
        "t0_text": iso(t0),
        "t1_text": iso(t1),
        "summary": summary_of(outs),
        "rules": [],
    }
    for o in outs:
        rule = o.rule
        payload["rules"].append({
            "rule_id": rule.get("rule_id", ""),
            "name": rule.get("name", ""),
            "code": rule.get("code", ""),
            "code_name": rule.get("code_name", ""),
            "unit": rule.get("unit", ""),
            "judge_note": rule.get("note", ""),
            "algorithms": [{"algo_id": a.get("algo_id"), "params": a.get("params") or {}}
                           for a in (rule.get("algorithms") or [])],
            "status": o.status_summary,
            "period_s": o.period_s,
            "sim_hits": o.sim_hits,
            "notes": o.notes,
            "points": [
                {
                    "time": p.get("time"),
                    "t_start": p.get("t_start"),
                    "t_end": p.get("t_end"),
                    "level": p.get("level"),
                    "level_cn": LEVEL_CN.get(p.get("level"), p.get("level")),
                    "hit_type": p.get("hit_type"),
                    "algo": p.get("algorithm_id"),
                    "value": p.get("value"),
                    "value_s": p.get("value_s"),
                    "threshold_s": p.get("threshold_s"),
                    "duration_s": p.get("duration_s"),
                    "dimension": p.get("dimension"),
                    "note": _point_note(p.get("evidence") or {}),
                    "evidence": p.get("evidence") or {},
                }
                for p in o.points
            ],
        })
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def csv_text(rows: list[dict], columns: list[tuple[str, str]] | None = None) -> str:
    """行数据 → CSV 文本。``columns`` 为 [(字段名, 表头), ...]，缺省用行内全部键。"""
    if not rows:
        return ""
    cols = columns or [(k, k) for k in rows[0]]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([label for _, label in cols])
    for r in rows:
        w.writerow([r.get(k, "") for k, _ in cols])
    return buf.getvalue()


# 异常点 CSV 的列（与界面表格列一致，去掉用于着色的 level_cn）
POINT_CSV_COLUMNS = [
    ("no", "序号"), ("time", "时间"), ("level_cn", "等级"), ("hit_type", "命中类型"),
    ("algo", "算法"), ("value_s", "触发值"), ("threshold_s", "阈值"),
    ("duration_s", "持续(s)"), ("dimension", "定位"), ("note", "说明"),
]

OUTCOME_CSV_COLUMNS = [
    ("rule_id", "规则ID"), ("name", "规则名"), ("code", "测点代号"),
    ("algos", "算法"), ("points", "异常点数"), ("levels", "等级分布"),
    ("status", "算法状态"), ("notes", "备注"),
]
