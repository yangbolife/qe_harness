"""eval_harness · 脚手架三方解耦协议（创新③）

痛点（2026 AgentAtlas 等实证）：同一 Agent 在 GPT-5 上 best-of-10 vs 单次 +4.3pp；
Claude Code 跨版本波动 50.8pp —— 说明「harness 框架带来的分」常被算作「模型能力分」，
交付验收时无法区分「模型不行」还是「脚手架不行」。

本模块把一次评测拆成三列独立出分：
- bare   : 裸模型——仅单次调用 provider，业务 code 评分器判定（不含任何 harness 增益）
- harness: 纯 harness——保留调度/重试/Trial(k 次 best-of-k)/judge 一致性增益，但
           不叠加业务 grader 的「业务正确性」加成口径
- full   : 全系统——harness + 业务 grader 组合（交付验收用的真实口径）

导出指标：
- harness_only_gain = harness - bare      （纯属 harness 基础设施带来的增益）
- scaffold_leakage  = full   - bare      （把 harness 增益 + 业务加成一起算进模型分的混淆量）
交付验收应报 full，但需同时披露 harness_only_gain，避免把脚手架分算成模型分。
"""
from __future__ import annotations

from typing import Optional

from .models import CaseResult


def _rate(results: list[CaseResult]) -> float:
    return sum(1 for r in results if r.passed) / len(results) if results else 0.0


async def run_three_way(
    suite_path: str,
    provider_name: str = "mock",
    provider_kwargs: Optional[dict] = None,
    judge_provider_name: Optional[str] = None,
    judge_provider_kwargs: Optional[dict] = None,
    trials: int = 3,
    trial_policy: str = "any",
    rate: float = 8.0,
    concurrency: int = 4,
) -> dict:
    """三方解耦：跑三遍不同口径，返回三列分与泄漏指标。"""
    # 延迟导入，打破 engine <-> scaffold 的循环依赖
    from .engine import run_suite_async

    bare = await run_suite_async(
        suite_path, provider_name=provider_name, provider_kwargs=provider_kwargs,
        default_grader="code", trials=1, trial_policy="any",
        concurrency=concurrency, rate=rate,
    )
    harness = await run_suite_async(
        suite_path, provider_name=provider_name, provider_kwargs=provider_kwargs,
        judge_provider_name=judge_provider_name, judge_provider_kwargs=judge_provider_kwargs,
        default_grader="judge", trials=trials, trial_policy=trial_policy,
        concurrency=concurrency, rate=rate,
    )
    full = await run_suite_async(
        suite_path, provider_name=provider_name, provider_kwargs=provider_kwargs,
        judge_provider_name=judge_provider_name, judge_provider_kwargs=judge_provider_kwargs,
        default_grader="code,judge", trials=trials, trial_policy=trial_policy,
        concurrency=concurrency, rate=rate,
    )

    bare_r, harness_r, full_r = _rate(bare), _rate(harness), _rate(full)
    per_case = []
    for b, h, f in zip(bare, harness, full):
        per_case.append({
            "case_id": b.case.id,
            "bare": b.passed, "harness": h.passed, "full": f.passed,
        })

    return {
        "bare_pass_rate": round(bare_r, 4),
        "harness_pass_rate": round(harness_r, 4),
        "full_pass_rate": round(full_r, 4),
        "harness_only_gain": round(harness_r - bare_r, 4),
        "scaffold_leakage": round(full_r - bare_r, 4),
        "per_case": per_case,
        "n": len(bare),
    }
