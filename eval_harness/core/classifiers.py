"""eval_harness · 分类器 + Span/Trace 级评分（N6）

Braintrust 的 Topics（宽泛分类）缺「深度归因」——本模块补齐：
- ClassifierGrader（注册名 classifier）：把每条失败归因到一个失败类别
  （timeout/format/hallucination/safety/tool_error/refusal/ok），
  用于下游 Patterns 自动问题发现（O12）聚类。
- score_spans(trace_payload)：对生产轨迹（OTel 形状）做 Span 级评分，
  逐 span 判定「是否有产出 / 是否崩溃 / 是否降级」，支撑 Trace 调试器（O15）。

与 trajectory grader（创新②）的分工：trajectory 看「步骤覆盖率」，本模块看
「逐 span 健康度 + 失败类别标签」，二者互补。
"""
from __future__ import annotations

from .graders import GRADERS
from .models import Case, GraderResult
from .trace_store import parse_otel_spans


# 失败类别（ok 表示通过，不计入失败归因）
FAILURE_CATEGORIES = [
    "timeout", "format", "hallucination", "safety", "tool_error",
    "refusal", "inconclusive", "ok",
]

_CRASH = ("traceback", "exception", "panic", "fatal", "stack trace", "timeout", "timed out")
_REFUSAL = ("无法", "不能", "不允许", "抱歉", "没有权限", "拒绝", "cannot", "unable", "i'm sorry")
_HALLU = ("根据我的知识", "作为ai", "我不确定", "可能", "推测", "没有依据")


def classify_response(pred: str, case: Case = None) -> str:
    """启发式失败分类（确定性、无需 LLM，适合大批量自动标注）。"""
    p = (pred or "").lower()
    if any(s in p for s in _CRASH):
        if "tool" in p or "调用" in (pred or ""):
            return "tool_error"
        return "timeout"  # 崩溃/超时归 timeout 簇
    if any(m in (pred or "") for m in _REFUSAL):
        return "refusal"
    if any(h in (pred or "") for h in _HALLU):
        return "hallucination"
    # 无合法 JSON 但声明要 JSON → 格式问题
    if case is not None and case.grader and "json" in (case.grader or ""):
        if "{" not in (pred or "") and "}" not in (pred or ""):
            return "format"
    if not (pred or "").strip():
        return "inconclusive"
    return "ok"


@GRADERS.register("classifier")
class ClassifierGrader:
    """把响应归因到失败类别；通过当且仅当类别为 ok。"""
    name = "classifier"

    def judge(self, pred: str, case: Case) -> GraderResult:
        cat = classify_response(pred, case)
        return GraderResult(
            grader="classifier",
            score=1.0 if cat == "ok" else 0.0,
            passed=cat == "ok",
            detail=f"category={cat}",
        )


def _unwrap(v):
    if isinstance(v, dict):
        return v.get("stringValue") or v.get("intValue") or v.get("boolValue") or ""
    return v


def score_spans(trace_payload: dict) -> list[dict]:
    """对生产轨迹逐 span 评分（Span 级健康度）。

    返回每个 span：{name, has_output, crash, degraded, score}；
    score = 有产出且未崩溃且未降级 → 1.0，否则按缺失程度扣分。
    """
    spans = parse_otel_spans(trace_payload)
    spans = sorted(spans, key=lambda s: s.get("startTime", s.get("startTimeUnixNano", 0)))
    out: list[dict] = []
    for s in spans:
        name = s.get("name", "step")
        attrs = s.get("attributes", {})
        if isinstance(attrs, list):
            attrs = {a.get("key"): _unwrap(a.get("value")) for a in attrs}
        out_text = attrs.get("output") or attrs.get("llm.output_text") or attrs.get("completion") or ""
        has_output = bool(out_text.strip())
        crash = any(c in (out_text or "").lower() for c in _CRASH)
        degraded = any(m in (out_text or "") for m in ("失败", "错误", "重试", "降级", "稍后"))
        score = 1.0
        if crash:
            score = 0.0
        elif not has_output:
            score = 0.3
        elif degraded:
            score = 0.6
        out.append({
            "name": name, "has_output": has_output, "crash": crash,
            "degraded": degraded, "score": score,
        })
    return out
