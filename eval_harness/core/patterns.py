"""eval_harness · Patterns 式自动问题发现（O12）

Braintrust 的 Patterns（证据驱动、自动发现反复出现的问题）对应物。
- discover_patterns(db, run_id)：扫描一次运行里的失败用例，按
  「失败类别(classifier) × 业务类别(category) × 输入签名」聚类，
  输出 Top 问题模式 + 证据（示例 case_id + 失败类别 + 占比）。
- 直接复用 classifiers.classify_response（N6）给出的失败标签，无需人工标注。

与 dashboards（N7）/ SQL（N9）互补：SQL 是「你来问」，Patterns 是「我主动告诉你哪错了」。
"""
from __future__ import annotations

from collections import Counter
from typing import Optional


async def discover_patterns(db, run_id: str, top_n: int = 5) -> dict:
    """扫描运行失败用例，聚类成可行动的问题模式。"""
    run = await db.get_run(run_id)
    if run is None:
        return {"run_id": run_id, "error": "run not found", "patterns": []}
    cases = run.get("cases", [])
    fails = [c for c in cases if not c.get("passed")]
    if not fails:
        return {"run_id": run_id, "total": len(cases), "failed": 0, "patterns": [],
                "verdict": "无失败，无模式可发现"}

    # 聚类键：(失败类别, 业务类别)
    clusters: Counter = Counter()
    evidence: dict = {}
    for c in fails:
        cat = _category_of(c)
        key = (cat, c.get("category") or "general")
        clusters[key] += 1
        evidence.setdefault(key, []).append({
            "case_id": c.get("case_id"),
            "category": cat,
            "input_preview": (c.get("input") or "")[:60],
            "response_preview": (c.get("response") or "")[:60],
        })

    total_fail = len(fails)
    ranked = sorted(clusters.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    patterns = []
    for (cat, biz), n in ranked:
        pat = {
            "pattern": f"{cat} @ {biz}",
            "failure_category": cat,
            "business_category": biz,
            "count": n,
            "share": round(n / total_fail, 4),
            "examples": evidence[(cat, biz)][:3],
        }
        # 给一句可行动建议
        pat["suggestion"] = _suggest(cat)
        patterns.append(pat)

    return {
        "run_id": run_id,
        "total": len(cases),
        "failed": total_fail,
        "patterns": patterns,
        "verdict": f"发现 {len(patterns)} 个高发问题模式，首要：{patterns[0]['pattern']}",
    }


def _category_of(c: dict) -> str:
    """从用例的评分器中取 classifier 失败标签；没有则用 inconclusive。"""
    for g in c.get("graders", []):
        if g.get("grader") == "classifier":
            detail = g.get("detail", "")
            if detail.startswith("category="):
                return detail.split("=", 1)[1]
    # 退而用 error / 全局
    if c.get("error"):
        return "tool_error"
    return "inconclusive"


def _suggest(cat: str) -> str:
    return {
        "timeout": "检查 SUT 超时与重试；考虑熔断阈值与下游可用性。",
        "format": "强化输出契约（contract grader）；约束 JSON schema。",
        "hallucination": "引入检索/归因约束（attribution grader）；接入 judge 复核。",
        "safety": "加 permission 边界 grader；补充拒绝对齐语料。",
        "tool_error": "用 Tools 托管执行（O10）隔离工具调用；加失效兜底（fallback）。",
        "refusal": "区分真越权与误拒；校准 permission 边界 marker。",
        "inconclusive": "排查环境失败占比；提升稳定性后再评。",
        "ok": "无需处理。",
    }.get(cat, "人工复核该类别失败样本。")
