"""eval_harness · 生产 trace 回灌闭环（创新⑥）

痛点（BrainTrust production-to-eval 闭源锁定；多数框架离线/在线割裂，生产漂移看不见）：
- 评测只在离线 dataset 上跑，线上真实轨迹（含工具调用、失败恢复）无法回灌复评。
- 生产 drift 不可见，模型升级后线上表现无法用同一把尺子回溯。

本模块：
- parse_otel_spans(payload)：兼容 OTel JSON 导出格式，抽取 span 列表。
- parse_trace_to_case(payload)：从轨迹里还原 输入 / 各步 / 最终输出，构造可复评的 Case
  （expected_steps = span 名序列，pred = 最终输出），供轨迹级归因（trajectory grader）复用。
- ingest_and_evaluate(db, source, payload, run_id)：落库 + 对轨迹重跑 grader，返回离线/在线一致性 delta。
"""
from __future__ import annotations

import json
from typing import Optional

from .graders import GRADERS
from .models import Case


def parse_otel_spans(payload: dict) -> list[dict]:
    """兼容 OTel JSON 导出（resourceSpans -> scopeSpans -> spans）。"""
    spans: list[dict] = []
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                spans.append(sp)
    # 也兼容裸 list
    if not spans and isinstance(payload.get("spans"), list):
        spans = payload["spans"]
    return spans


def parse_trace_to_case(payload: dict, trace_id: str = "trace") -> Case:
    """从生产轨迹还原一个可复评 Case（供 trajectory grader 使用）。"""
    spans = parse_otel_spans(payload)
    # 按 start time 排序，取首个作为输入、末个为最终输出
    spans_sorted = sorted(spans, key=lambda s: s.get("startTime", s.get("startTimeUnixNano", 0)))
    inputs, outputs, step_names = [], [], []
    final_output = ""
    for s in spans_sorted:
        name = s.get("name", "step")
        step_names.append(name)
        attrs = s.get("attributes", {})
        # 兼容 OTel key/value 列表形式
        if isinstance(attrs, list):
            attrs = {a.get("key"): _unwrap(a.get("value")) for a in attrs}
        inp = attrs.get("input") or attrs.get("llm.input_messages") or attrs.get("prompt") or ""
        out = attrs.get("output") or attrs.get("llm.output_text") or attrs.get("completion") or ""
        if inp:
            inputs.append(inp)
        if out:
            final_output = out
            outputs.append(out)
    # 完整执行轨迹文本：trajectory grader 需要对照全部步骤计算覆盖率
    trajectory_lines = []
    trajectory_struct = []  # 结构化轨迹：供 trajectory_eval 评分器消费（创新②扩展）
    for s in spans_sorted:
        name = s.get("name", "step")
        attrs = s.get("attributes", {})
        if isinstance(attrs, list):
            attrs = {a.get("key"): _unwrap(a.get("value")) for a in attrs}
        inp = attrs.get("input") or attrs.get("llm.input_messages") or attrs.get("prompt") or ""
        out = attrs.get("output") or attrs.get("llm.output_text") or attrs.get("completion") or ""
        kind = "action" if (out and inp) else ("observation" if out else "step")
        trajectory_lines.append(f"{name}: {out}" if out else name)
        trajectory_struct.append({
            "kind": kind,
            "content": (f"{name}: {out}" if out else name),
            "tool": name if kind == "action" else "",
            "args": str(inp or ""),
        })
    trajectory_text = "\n".join(trajectory_lines)
    first_input = inputs[0] if inputs else (spans_sorted[0].get("name", "") if spans_sorted else "")
    return Case(
        id=trace_id,
        input=first_input,
        gold="",
        category="production_trace",
        suite="behavior",
        grader="trajectory",
        meta={"expected_steps": step_names or ["step"],
              "trajectory_text": trajectory_text,
              "trajectory": trajectory_struct,
              "production_trace": True, "raw_output": final_output},
    )


def _unwrap(v):
    if isinstance(v, dict):
        return v.get("stringValue") or v.get("intValue") or v.get("boolValue") or ""
    return v


async def ingest_and_evaluate(db, source: str, payload: dict,
                               run_id: Optional[str] = None,
                               grader_name: str = "trajectory") -> dict:
    """落库 + 对轨迹重跑 grader，返回离线/在线一致性 delta。"""
    tid = await db.ingest_trace(source, payload, run_id=run_id)
    case = parse_trace_to_case(payload, trace_id=f"trace:{tid}")
    grader = GRADERS.get(grader_name)()
    pred = case.meta.get("trajectory_text", case.meta.get("raw_output", ""))
    res = grader.judge(pred, case)
    return {
        "trace_id": tid,
        "source": source,
        "steps": case.meta.get("expected_steps"),
        "grader": grader_name,
        "score": res.score,
        "passed": res.passed,
        "detail": res.detail,
        "online_output_preview": (case.meta.get("raw_output", "") or "")[:200],
    }


def evaluate_trace_payload(payload: dict, grader_name: str = "trajectory") -> dict:
    """无 DB 模式：直接评估一份 trace payload（CLI/测试用）。"""
    case = parse_trace_to_case(payload, trace_id="inline")
    grader = GRADERS.get(grader_name)()
    pred = case.meta.get("trajectory_text", case.meta.get("raw_output", ""))
    res = grader.judge(pred, case)
    return {
        "steps": case.meta.get("expected_steps"),
        "grader": grader_name, "score": res.score,
        "passed": res.passed, "detail": res.detail,
    }


def make_demo_trace() -> dict:
    """构造一份示例生产轨迹（OTel 形状），用于演示/测试。"""
    return {
        "resourceSpans": [{
            "scopeSpans": [{"spans": [
                {"name": "规划", "startTime": 1, "attributes": [
                    {"key": "input", "value": {"stringValue": "帮我订明天北京到上海的机票"}}]},
                {"name": "检索航班", "startTime": 2, "attributes": [
                    {"key": "output", "value": {"stringValue": "找到 3 个航班"}}]},
                {"name": "生成订单", "startTime": 3, "attributes": [
                    {"key": "output", "value": {"stringValue": "已生成订单 CA1234"}}]},
                {"name": "确认", "startTime": 4, "attributes": [
                    {"key": "output", "value": {"stringValue": "订单已确认，依据检索结果"}}]},
            ]}]
        }]
    }


def make_demo_trace_crash() -> dict:
    """构造一份含崩溃信号的生产轨迹（演示归因失败定位）。"""
    return {
        "resourceSpans": [{
            "scopeSpans": [{"spans": [
                {"name": "规划", "startTime": 1, "attributes": [
                    {"key": "input", "value": {"stringValue": "查询账户余额"}}]},
                {"name": "调用银行API", "startTime": 2, "attributes": [
                    {"key": "output", "value": {"stringValue": "Exception: timeout"}}]},
                {"name": "返回", "startTime": 3, "attributes": [
                    {"key": "output", "value": {"stringValue": "未返回余额"}}]},
            ]}]
        }]
    }
