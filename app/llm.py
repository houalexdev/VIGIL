"""本地大模型接入：把数据画像丢给模型，让它挑算法、配参数、出规则。

约束：
- 模型可能不可达／返回非 JSON／编造参数名。所有输出都必须过 ``validate_rules``，
  非法参数按 ParamSpec 剔除，非法算法整条丢弃，绝不把半成品塞进规则库。
- 模型不可达时退化为本地启发式规则（基于分位数与差分统计），保证功能可用。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import httpx

import algorithm as alg
from algorithm.base import ParamError, RunStatus, ValueKind, validate_params

from . import yamls
from .config import LLM_API_KEY, LLM_BASE_URL, LLM_ENDPOINTS, LLM_MODEL, LLM_REQUEST, LLM_TIMEOUT
from .meta import get_def
from .simulate import NEEDS_EXTERNAL

# 提示词与请求参数全部来自 config/prompts.yaml，改提示词不需要动代码
_PROMPT = yamls.file("prompts.yaml")
SYSTEM_PROMPT = str(_PROMPT.get("system") or "")
USER_PROMPT = str(_PROMPT.get("user") or "")
_HIDE_PARAMS = set(_PROMPT.get("catalog_hide_params") or [])
_MIN_RULES = int(_PROMPT.get("min_rules", 2))
_MAX_RULES = int(_PROMPT.get("max_rules", 4))
_REQUEST_EXTRA = dict(_PROMPT.get("request_defaults") or {})
# 模型服务专属参数（如 Qwen3 的 enable_thinking）。与 request_defaults 平铺合并进请求体。
# 单独放一个键是为了让「vLLM/Qwen3 专属」这类开关在 YAML 里自成一节，不和通用请求参数混在一起。
_EXTRA_KEYS = {k: v for k, v in _PROMPT.items()
               if k not in ("system", "user", "catalog_hide_params", "min_rules",
                            "max_rules", "request_defaults", "fallback")}
_REQUEST_EXTRA.update(_EXTRA_KEYS)
_FALLBACK = dict(_PROMPT.get("fallback") or {})
_LEVEL_CN = {k: v.get("cn", k) for k, v in yamls.file("app.yaml")["levels"]["items"].items()}



@dataclass
class LLMResult:
    mode: str                     # llm / fallback / error
    analysis: str = ""
    rules: list[dict] = field(default_factory=list)
    raw: str = ""
    message: str = ""


def _catalog_text() -> str:
    lines = []
    for spec in alg.catalog():
        if not spec["is_detector"]:
            continue
        if spec["algorithm_id"] in NEEDS_EXTERNAL:
            continue
        lines.append(f"- {spec['algorithm_id']} {spec['name']}｜类别={spec['category']}"
                     f"｜命中类型={spec['hit_type']}｜supported_kinds={','.join(spec['supported_kinds'])}")
        params = []
        for p in spec["params"]:
            if p["name"] in _HIDE_PARAMS:
                continue
            seg = f"{p['name']}({p['type']},默认={p['default']!r}"
            if p.get("choices"):
                seg += f",可选={p['choices']}"
            if p.get("minimum") is not None:
                seg += f",min={p['minimum']}"
            if p.get("maximum") is not None:
                seg += f",max={p['maximum']}"
            if p.get("unit"):
                seg += f",单位={p['unit']}"
            if p.get("depends_on"):
                seg += f",仅当{p['depends_on']}"
            seg += ")"
            params.append(seg)
        lines.append("   参数: " + "；".join(params))
    return "\n".join(lines)


def build_user_prompt(code: str, meta_row: dict, stats: dict, period_s: float) -> str:
    """按 config/prompts.yaml → user 模板渲染。缺变量原样保留，不会抛 KeyError。"""
    d = get_def(code)
    lo, hi = meta_row.get("low"), meta_row.get("high")
    return _render(USER_PROMPT, {
        "code": code,
        "name": meta_row.get("name") or d.name,
        "kind": d.kind,
        "unit": meta_row.get("unit") or d.unit or "无",
        "range": f"[{lo}, {hi}]",
        "low": lo,
        "high": hi,
        "bit_width": meta_row.get("bit_width") or d.bit_width,
        "quantum": meta_row.get("quantum") or d.quantum,
        "period_s": f"{period_s:g}",
        "stats": json.dumps(stats, ensure_ascii=False),
        "catalog": _catalog_text(),
        "min_rules": _MIN_RULES,
        "max_rules": _MAX_RULES,
    })


def _render(tpl: str, values: dict) -> str:
    out = tpl
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate_rules(items: list[dict], code: str, meta: dict) -> tuple[list[dict], list[str]]:
    rules, errs = [], []
    for i, it in enumerate(items):
        aid = str(it.get("algo_id") or "").strip()
        try:
            cls = alg.get(aid)
        except KeyError:
            errs.append(f"第 {i + 1} 条：算法 {aid or '(空)'} 不存在")
            continue
        if not cls.IS_DETECTOR:
            errs.append(f"第 {i + 1} 条：{aid} 是基础设施算法，不能进规则")
            continue
        kind = ValueKind(meta.get("kind") or "continuous")
        if kind not in cls.SUPPORTED_KINDS:
            errs.append(f"第 {i + 1} 条：{aid} 不支持值域 {kind.value}")
            continue
        params = dict(it.get("params") or {})
        try:
            clean = validate_params(cls.param_specs(), params)
        except ParamError as exc:
            errs.append(f"第 {i + 1} 条（{aid}）：{exc}")
            continue
        merged = {k: v for k, v in clean.items() if v is not None}
        rules.append({
            "name": (it.get("name") or f"{aid} 判据")[:24],
            "code": code,
            "code_name": meta.get("name") or get_def(code).name,
            "unit": meta.get("unit", ""),
            "kind": meta.get("kind") or "continuous",
            "low": meta.get("low"),
            "high": meta.get("high"),
            "bit_width": meta.get("bit_width"),
            "quantum": meta.get("quantum"),
            "algorithms": [{"algo_id": aid, "params": merged, "note": it.get("reason", "")}],
            "note": it.get("reason", ""),
            "source": "llm",
        })
    return rules, errs


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", text[start:end + 1]))
        except json.JSONDecodeError:
            return None


def _call_llm(prompt: str, timeout: float = LLM_TIMEOUT) -> tuple[str, str]:
    """返回 (内容, 错误)。接口挂载点与采样参数来自 config/app.yaml → llm。

    ``timeout`` 是**整个调用**的预算，不是每个 endpoint 各给一份：
    否则端点不可达时会退化成 N × timeout（N 个挂载点），界面会卡到用户以为死机。
    """
    base = LLM_BASE_URL.rstrip("/")
    urls = [f"{base}{ep}" for ep in LLM_ENDPOINTS]
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
        **LLM_REQUEST,
        **_REQUEST_EXTRA,
    }
    last = ""
    deadline = time.monotonic() + max(float(timeout), 1.0)
    # Key 为空（vLLM 内网免鉴权）时不发 Authorization，避免 "Bearer " 被网关判定为无效凭证
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY.strip():
        headers["Authorization"] = f"Bearer {LLM_API_KEY.strip()}"
    for i, url in enumerate(urls):
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            last = last or "总超时预算已用完"
            break
        # 留给后续挂载点至少 30% 的预算，避免第一个端点吃光全部时间
        share = remaining if i == len(urls) - 1 else remaining * 0.7
        try:
            with httpx.Client(timeout=min(remaining, max(share, 1.0))) as cli:
                r = cli.post(url, json=body, headers=headers)
            if r.status_code >= 400:
                last = f"HTTP {r.status_code} @ {url}: {r.text[:200]}"
                continue
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or choice.get("text") or ""
            if content:
                return content, ""
            last = f"空响应 @ {url}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__} @ {url}: {exc}"
    return "", last or "无法连接模型服务"


def check_connection(timeout: float = 3.0) -> tuple[bool, str]:
    content, err = _call_llm("ping", timeout=timeout)
    if content:
        return True, f"在线 · {LLM_MODEL}"
    return False, err[:160]


# ---------------------------------------------------------------------------
# 启发式兜底
# ---------------------------------------------------------------------------
def _num(v) -> float | int:
    """模板渲染出来的是字符串，能转数字就转回来（参数需要数值类型）。"""
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(str(v))
    except (TypeError, ValueError):
        return v
    return int(f) if f.is_integer() and "." not in str(v) else f


def fallback_rules(code: str, meta: dict, stats: dict, period_s: float) -> tuple[str, list[dict]]:
    """本地启发式兜底。规则与系数全部来自 config/prompts.yaml → fallback。

    模型不可达时不能什么都不给，但也不该把口径悄悄写死在代码里 ——
    现场对兜底判据的松紧有不同意见时，改 YAML 就能调整，不需要改代码。
    """
    kind = meta.get("kind") or "continuous"
    low, high = meta.get("low"), meta.get("high")
    span = (float(high) - float(low)) if (low is not None and high is not None) else 10.0
    span = max(span, 1e-6)

    conf = _FALLBACK or {}
    q01, q99 = stats.get("q01", 0.0), stats.get("q99", 100.0)
    a_abs = max(stats.get("diff_p99", 0.0) * float(conf.get("jump_scale", 3.0)),
                span * float(conf.get("jitter_span_ratio", 0.02)), 1e-6)
    vals = {
        "low": float(low) if low is not None else q01,
        "high": float(high) if high is not None else q99,
        "q01": q01, "q99": q99,
        "diff_p99": stats.get("diff_p99", 0.0),
        "span": span,
        "a_abs": round(a_abs, 6),
        "step_thr": round(max(a_abs / float(conf.get("jitter_div", 4.0)), 1e-6), 6),
        "n_dead": max(10, int(round(float(conf.get("freeze_seconds", 120))
                                    / max(period_s, 1e-6)))),
        "period_s": period_s,
    }

    items: list[dict] = []
    for rule in (conf.get("rules") or []):
        when = rule.get("when")
        if when and kind not in when:
            continue
        # severity 可以写中文等级名（严重）或等级键（L3），算法 ParamSpec 要中文名
        sev = str(rule.get("severity") or "")
        vals["level"] = _LEVEL_CN.get(sev, sev) or "一般"
        params = {k: _num(_render_value(v, vals))
                  for k, v in (rule.get("params") or {}).items()}
        items.append({
            "name": str(rule.get("name") or rule.get("algo_id")),
            "algo_id": str(rule.get("algo_id")),
            "params": params,
            "reason": _render_value(rule.get("reason") or "", vals),
        })

    tpl = str(conf.get("analysis_template")
              or "模型不可达，已用本地统计口径生成 {count} 条规则")
    analysis = _render(tpl.strip(), {"count": len(items),
                                     "mean": stats.get("mean"), "std": stats.get("std"),
                                     "diff_p99": stats.get("diff_p99")})
    return analysis, items


def _render_value(v, vals: dict):
    """params 里的值可能是模板字符串，也可能已经是数字/布尔。"""
    if isinstance(v, str):
        out = v
        for k, val in vals.items():
            out = out.replace("{" + k + "}", str(val))
        return out
    return v


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def generate(code: str, meta: dict, stats: dict, period_s: float,
             use_llm: bool = True) -> LLMResult:
    prompt = build_user_prompt(code, meta, stats, period_s)
    if use_llm:
        content, err = _call_llm(prompt)
        if content:
            data = _extract_json(content)
            if data and isinstance(data.get("rules"), list):
                rules, errs = validate_rules(data["rules"], code, meta)
                if rules:
                    msg = f"模型返回 {len(data['rules'])} 条，采纳 {len(rules)} 条"
                    if errs:
                        msg += "；剔除：" + "；".join(errs[:3])
                    return LLMResult(mode="llm", analysis=str(data.get("analysis") or ""),
                                     rules=rules, raw=content, message=msg)
                err = "模型输出的规则全部未通过校验：" + "；".join(errs[:3])
            else:
                err = "模型返回内容无法解析为 JSON"
        analysis, items = fallback_rules(code, meta, stats, period_s)
        rules, errs = validate_rules(items, code, meta)
        return LLMResult(mode="fallback", analysis=analysis, rules=rules,
                         raw="", message=f"{err}；已退化为本地启发式规则")

    analysis, items = fallback_rules(code, meta, stats, period_s)
    rules, errs = validate_rules(items, code, meta)
    return LLMResult(mode="fallback", analysis=analysis, rules=rules, message="本地启发式规则")
