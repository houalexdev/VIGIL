"""本地存储层：SQLite 存规则/场景/数据集索引；Parquet(snappy) 存测点数据。

数据落盘策略：``data/parquet/<测点代号>/<YYYYMMDD>/<起始ts>_<结束ts>.parquet``
— 以测点代号与时间戳分目录，同名覆盖（同窗口重复模拟只保留最后一份）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .config import (COLUMNS, DATE_DIR_FORMAT, DB_PATH, PARQUET_COMPRESSION,
                     PARQUET_DIR, PARQUET_FILENAME)

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL,
    code_name   TEXT DEFAULT '',
    unit        TEXT DEFAULT '',
    kind        TEXT DEFAULT 'continuous',
    low         REAL,
    high        REAL,
    bit_width   INTEGER DEFAULT 16,
    quantum     REAL DEFAULT 0,
    algorithms  TEXT NOT NULL DEFAULT '[]',
    note        TEXT DEFAULT '',
    source      TEXT DEFAULT 'user',
    sort_no     INTEGER DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS scenes (
    scene_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    desc        TEXT DEFAULT '',
    rule_ids    TEXT NOT NULL DEFAULT '[]',
    source      TEXT DEFAULT 'user',
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id  TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    t_start     INTEGER NOT NULL,
    t_end       INTEGER NOT NULL,
    path        TEXT NOT NULL,
    points      INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'sim',
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    scene_id    TEXT,
    mode        TEXT,
    t_start     INTEGER,
    t_end       INTEGER,
    summary     TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_datasets_code ON datasets(code);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _LOCK, _conn() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------
def _rule_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["algorithms"] = json.loads(d.get("algorithms") or "[]")
    return d


def list_rules() -> list[dict]:
    with _conn() as c:
        return [_rule_row(r) for r in c.execute("SELECT * FROM rules ORDER BY sort_no, created_at")]


def get_rule(rule_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
        return _rule_row(r) if r else None


def get_rules(rule_ids: Iterable[str]) -> list[dict]:
    ids = list(dict.fromkeys(rule_ids))
    if not ids:
        return []
    q = f"SELECT * FROM rules WHERE rule_id IN ({','.join('?' * len(ids))})"
    with _conn() as c:
        rows = {r["rule_id"]: _rule_row(r) for r in c.execute(q, ids)}
    return [rows[i] for i in ids if i in rows]


def upsert_rule(rule: dict, new: bool = False) -> dict:
    rule = dict(rule)
    rid = rule.get("rule_id") or (f"R-{uuid.uuid4().hex[:8]}" if new else "")
    if not rid:
        raise ValueError("缺少 rule_id")
    old = get_rule(rid)
    rule["rule_id"] = rid
    rule.setdefault("source", old["source"] if old else "user")
    rule.setdefault("sort_no", old["sort_no"] if old else int(time.time()))
    algo_json = json.dumps(rule.get("algorithms") or [], ensure_ascii=False)
    # 同代号 + 同名 + 同算法组合的规则不重复插入（AI 反复生成同一条判据时只更新参数）
    if not old:
        with _conn() as c:
            dup = c.execute(
                "SELECT * FROM rules WHERE code=? AND name=? AND algorithms=? LIMIT 1",
                (rule.get("code", ""), rule.get("name", ""), algo_json)).fetchone()
        if dup:
            rule["rule_id"] = rid = dup["rule_id"]
            old = _rule_row(dup)
    with _LOCK, _conn() as c:
        if old:
            c.execute(
                """UPDATE rules SET name=?, code=?, code_name=?, unit=?, kind=?, low=?, high=?,
                   bit_width=?, quantum=?, algorithms=?, note=?, source=?, sort_no=?, updated_at=?
                   WHERE rule_id=?""",
                (rule.get("name", ""), rule.get("code", ""), rule.get("code_name", ""),
                 rule.get("unit", ""), rule.get("kind", "continuous"), rule.get("low"),
                 rule.get("high"), int(rule.get("bit_width") or 16),
                 float(rule.get("quantum") or 0), algo_json, rule.get("note", ""),
                 rule["source"], int(rule["sort_no"]), _now(), rid),
            )
        else:
            c.execute(
                """INSERT INTO rules (rule_id, name, code, code_name, unit, kind, low, high,
                   bit_width, quantum, algorithms, note, source, sort_no, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, rule.get("name", ""), rule.get("code", ""), rule.get("code_name", ""),
                 rule.get("unit", ""), rule.get("kind", "continuous"), rule.get("low"),
                 rule.get("high"), int(rule.get("bit_width") or 16),
                 float(rule.get("quantum") or 0), algo_json, rule.get("note", ""),
                 rule["source"], int(rule["sort_no"]), _now(), _now()),
            )
    return get_rule(rid)


def delete_rule(rule_id: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM rules WHERE rule_id=?", (rule_id,))
        for r in c.execute("SELECT scene_id, rule_ids FROM scenes").fetchall():
            ids = json.loads(r["rule_ids"] or "[]")
            if rule_id in ids:
                ids.remove(rule_id)
                c.execute("UPDATE scenes SET rule_ids=?, updated_at=? WHERE scene_id=?",
                          (json.dumps(ids, ensure_ascii=False), _now(), r["scene_id"]))


# ---------------------------------------------------------------------------
# 分组场景
# ---------------------------------------------------------------------------
def _scene_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["rule_ids"] = json.loads(d.get("rule_ids") or "[]")
    return d


def list_scenes() -> list[dict]:
    with _conn() as c:
        return [_scene_row(r) for r in c.execute("SELECT * FROM scenes ORDER BY created_at")]


def get_scene(scene_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM scenes WHERE scene_id=?", (scene_id,)).fetchone()
        return _scene_row(r) if r else None


def upsert_scene(scene: dict, new: bool = False) -> dict:
    scene = dict(scene)
    sid = scene.get("scene_id") or (f"S-{uuid.uuid4().hex[:8]}" if new else "")
    if not sid:
        raise ValueError("缺少 scene_id")
    old = get_scene(sid)
    ids = json.dumps(scene.get("rule_ids") or [], ensure_ascii=False)
    with _LOCK, _conn() as c:
        if old:
            c.execute("UPDATE scenes SET name=?, desc=?, rule_ids=?, updated_at=? WHERE scene_id=?",
                      (scene.get("name", ""), scene.get("desc", ""), ids, _now(), sid))
        else:
            c.execute("""INSERT INTO scenes (scene_id, name, desc, rule_ids, source, created_at, updated_at)
                         VALUES (?,?,?,?,?,?,?)""",
                      (sid, scene.get("name", ""), scene.get("desc", ""), ids,
                       scene.get("source", "user"), _now(), _now()))
    return get_scene(sid)


def delete_scene(scene_id: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM scenes WHERE scene_id=?", (scene_id,))


# ---------------------------------------------------------------------------
# 数据集（Parquet）
# ---------------------------------------------------------------------------
def dataset_path(code: str, t0: int, t1: int) -> Path:
    """目录与文件名格式来自 config/app.yaml → storage，改配置即可换命名规则。"""
    day = datetime.fromtimestamp(t0 / 1000).strftime(DATE_DIR_FORMAT)
    d = PARQUET_DIR / code / day
    d.mkdir(parents=True, exist_ok=True)
    return d / PARQUET_FILENAME.format(t_start=int(t0), t_end=int(t1), code=code, day=day)


def save_series(code: str, t: np.ndarray, v: np.ndarray, t0: int, t1: int,
                quality: np.ndarray | None = None, source: str = "sim") -> dict:
    """单代号时序落盘。返回数据集记录。"""
    df = pd.DataFrame({
        COLUMNS["t"]: np.asarray(t, dtype="int64"),
        COLUMNS["v"]: np.asarray(v, dtype="float64"),
        COLUMNS["quality"]: (np.asarray(quality, dtype="uint8") if quality is not None
                             else np.where(np.isnan(np.asarray(v, dtype=float)), 1, 0).astype("uint8")),
    })
    path = dataset_path(code, t0, t1)
    df.to_parquet(path, compression=PARQUET_COMPRESSION, index=False)
    rec = {
        "dataset_id": f"D-{uuid.uuid4().hex[:8]}",
        "code": code, "t_start": int(t0), "t_end": int(t1),
        "path": str(path), "points": int(len(df)), "source": source, "created_at": _now(),
    }
    with _LOCK, _conn() as c:
        old = c.execute("SELECT dataset_id, created_at FROM datasets WHERE path=?", (str(path),)).fetchone()
        if old:
            rec["dataset_id"] = old["dataset_id"]
            c.execute("""UPDATE datasets SET points=?, source=?, created_at=? WHERE dataset_id=?""",
                      (rec["points"], rec["source"], rec["created_at"], old["dataset_id"]))
        else:
            c.execute("""INSERT INTO datasets (dataset_id, code, t_start, t_end, path, points, source, created_at)
                         VALUES (:dataset_id,:code,:t_start,:t_end,:path,:points,:source,:created_at)""", rec)
    return rec


def load_series(code: str, t0: int, t1: int) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """按代号与窗口读取数据（用于「获取数据」，当前以模拟数据源为后端）。

    匹配规则是**有交集**而不是「完整覆盖」：
    用户说「最近 12 小时」时，窗口起点由当前时刻反推，往往比数据文件写盘时刻
    晚几秒、窗口终点又略超出数据末点。若按「完整覆盖」匹配，差这几秒就查不到
    数据，表现为"明明刚跑过检测却说没有落盘数据"。
    命中后按请求区间做裁剪，超出窗口的部分不返回。
    """
    with _conn() as c:
        r = c.execute("""SELECT path FROM datasets
                         WHERE code=? AND t_end>=? AND t_start<=?
                         ORDER BY created_at DESC LIMIT 1""",
                      (code, int(t0), int(t1))).fetchone()
    if not r:
        return None
    df = pd.read_parquet(r["path"])
    t = df["t"].to_numpy(np.int64)
    sel = (t >= int(t0)) & (t <= int(t1))
    if not sel.any():
        # 交集窗口里一个点都没有（例如只擦到边界），退回整份数据，
        # 让上层按自己的口径判断，比直接报"没有数据"更可解释
        sel = np.ones(len(t), dtype=bool)
    return (t[sel], df["v"].to_numpy(float)[sel],
            df["quality"].to_numpy(np.uint8)[sel])


def list_datasets(code: str | None = None, limit: int = 200) -> list[dict]:
    with _conn() as c:
        if code:
            rows = c.execute("SELECT * FROM datasets WHERE code=? ORDER BY created_at DESC LIMIT ?",
                             (code, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM datasets ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        p = Path(d["path"])
        d["exists"] = p.exists()
        d["size_kb"] = round(p.stat().st_size / 1024, 1) if p.exists() else 0
        out.append(d)
    return out


def dataset_stats(code: str, t0: int, t1: int) -> dict[str, Any] | None:
    got = load_series(code, t0, t1)
    if got is None:
        return None
    t, v, _ = got
    ok = v[~np.isnan(v)]
    if ok.size == 0:
        return None
    qs = np.quantile(ok, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
    d = np.abs(np.diff(ok))
    return {
        "points": int(len(v)), "valid": int(ok.size),
        "min": round(float(ok.min()), 6), "max": round(float(ok.max()), 6),
        "mean": round(float(ok.mean()), 6), "std": round(float(ok.std(ddof=1)) if ok.size > 1 else 0.0, 6),
        "q01": round(float(qs[0]), 6), "q05": round(float(qs[1]), 6), "q25": round(float(qs[2]), 6),
        "p50": round(float(qs[3]), 6), "q75": round(float(qs[4]), 6),
        "q95": round(float(qs[5]), 6), "q99": round(float(qs[6]), 6),
        "diff_med": round(float(np.median(d)), 6) if d.size else 0.0,
        "diff_p99": round(float(np.quantile(d, 0.99)), 6) if d.size else 0.0,
    }


def log_run(scene_id: str, mode: str, t0: int, t1: int, summary: dict) -> str:
    rid = f"RUN-{uuid.uuid4().hex[:8]}"
    with _LOCK, _conn() as c:
        c.execute("""INSERT INTO runs (run_id, scene_id, mode, t_start, t_end, summary, created_at)
                     VALUES (?,?,?,?,?,?,?)""",
                  (rid, scene_id, mode, int(t0), int(t1),
                   json.dumps(summary, ensure_ascii=False, default=str), _now()))
    return rid
