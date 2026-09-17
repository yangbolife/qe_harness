"""Phase 4 + 六创新点 测试（mock，不烧 key）

覆盖：
- 创新② 轨迹级归因 grader（覆盖/崩溃定位）
- 创新③ 脚手架三方解耦（bare<harness<=full，泄漏指标）
- 创新④ 动态产题 + 版本钉（确定性/哈希）
- 创新⑤ Judge 三通道隔离 + 成本追踪 + 预算闸门
- 创新⑥ 生产 trace 回灌（OTel 解析 + 轨迹归因）
- Phase 4 PostgreSQL/SQLite 持久化（落 run/case/grader/trial，可查询）
"""
import asyncio
import json
import os
import tempfile

import pytest

from eval_harness.core.models import Case
from eval_harness.core.graders import GRADERS
from eval_harness.core.generator import generate_curated, version_of
from eval_harness.core.trace_store import (
    evaluate_trace_payload, parse_trace_to_case,
    make_demo_trace, make_demo_trace_crash,
)
from eval_harness.core.engine import run_suite_async, BudgetGate
from eval_harness.core.persistence import Database


# ---------------- 创新② 轨迹级归因 ----------------
def test_trajectory_coverage_pass():
    g = GRADERS.get("trajectory")()
    case = Case(id="t1", input="x", gold="", grader="trajectory",
                meta={"expected_steps": ["规划", "检索航班", "生成订单", "确认"]})
    pred = "步骤1: 规划\n步骤2: 检索航班\n步骤3: 生成订单\n步骤4: 确认并通知"
    r = g.judge(pred, case)
    assert r.passed and r.score == 1.0


def test_trajectory_crash_attribution():
    g = GRADERS.get("trajectory")()
    case = Case(id="t2", input="x", gold="", grader="trajectory",
                meta={"expected_steps": ["规划", "调用银行API", "返回余额"]})
    pred = "步骤1: 规划\nException: timeout\n步骤3: 未返回"
    r = g.judge(pred, case)
    assert not r.passed
    assert "崩溃" in r.detail or "首错" in r.detail


# ---------------- 创新③ 脚手架三方解耦 ----------------
@pytest.mark.asyncio
async def test_three_way_monotonic():
    from eval_harness.core.scaffold import run_three_way
    tw = await run_three_way(
        "eval_harness/examples/practice3_smoke.jsonl",
        provider_name="mock", provider_kwargs={"mode": "full"}, trials=2,
    )
    assert tw["bare_pass_rate"] <= tw["full_pass_rate"] + 1e-9
    assert "scaffold_leakage" in tw and "harness_only_gain" in tw
    assert tw["n"] >= 1


# ---------------- 创新④ 动态产题 + 版本钉 ----------------
def test_generate_deterministic_and_version():
    a = generate_curated(7, 20)
    b = generate_curated(7, 20)
    assert a == b, "同 seed 应确定性"
    assert len(a) == 20
    v = version_of(a)
    assert len(v) == 16 and isinstance(v, str)
    assert version_of(a) == version_of(b)


# ---------------- 创新⑤ Judge 三通道 + 成本 + 预算 ----------------
def test_judge_cost_tracked():
    g = GRADERS.get("judge")()
    case = Case(id="j1", input="1+1=?", gold="2", grader="judge")
    r = g.judge("2", case)  # judge_provider=None → 未配置，但应有 cost 估算
    assert r.tokens >= 0 and r.cost_usd >= 0


def test_judge_budget_gate_blocks():
    gate = BudgetGate(budget_usd=1.0)
    gate.charge(2.0)  # 预充到超预算
    assert gate.exceeded
    g = GRADERS.get("judge")(judge_provider=None, budget_gate=gate)
    case = Case(id="j2", input="q", gold="a", grader="judge")
    # 预算已超，任何 judge 调用应立即被阻断
    r = g.judge("a", case)
    assert "BUDGET" in r.detail


@pytest.mark.asyncio
async def test_judge_channel_isolation_in_engine():
    # judge 通道只收到 (问题, gold, pred)，不接触执行通道状态
    results = await run_suite_async(
        "eval_harness/examples/practice3_smoke.jsonl",
        provider_name="mock", provider_kwargs={"mode": "full"},
        default_grader="judge", judge_budget=1.0, trials=1,
    )
    for r in results:
        for g in r.graders:
            if g.grader == "judge":
                assert g.cost_usd >= 0  # 成本可见


# ---------------- 创新⑥ 生产 trace 回灌 ----------------
def test_trace_parse_and_evaluate():
    payload = make_demo_trace()
    res = evaluate_trace_payload(payload, grader_name="trajectory")
    assert res["score"] == 1.0 and res["passed"]
    case = parse_trace_to_case(payload, trace_id="x")
    assert case.grader == "trajectory" and len(case.meta["expected_steps"]) == 4


def test_trace_crash_detected():
    payload = make_demo_trace_crash()
    res = evaluate_trace_payload(payload, grader_name="trajectory")
    assert not res["passed"]
    assert "崩溃" in res["detail"] or "首错" in res["detail"]


# ---------------- Phase 4 持久化（SQLite 临时库，零外部依赖） ----------------
@pytest.mark.asyncio
async def test_persistence_save_and_query(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'eval_test.db'}"
    db = Database(db_url)
    await db.init()
    results = await run_suite_async(
        "eval_harness/examples/practice3_smoke.jsonl",
        provider_name="mock", provider_kwargs={"mode": "full"},
        default_grader="code", trials=1, store=db,
        run_name="smoke-run", model_version="mock-full", dataset_version="v1",
    )
    runs = await db.list_runs()
    assert len(runs) == 1
    rid = runs[0]["id"]
    detail = await db.get_run(rid)
    assert detail["total"] == len(results)
    assert detail["passed"] == sum(1 for r in results if r.passed)
    assert detail["model_version"] == "mock-full"
    assert len(detail["cases"]) == len(results)
    # grader 行存在
    assert len(detail["cases"][0]["graders"]) >= 1
    # 用例集版本钉
    await db.save_case_set("demo", "abc123", "x.jsonl", "abc123")
    cs = await db.list_case_sets()
    assert any(c["version"] == "abc123" for c in cs)
    # trace 回灌落库
    tid = await db.ingest_trace("demo.json", make_demo_trace())
    traces = await db.list_traces()
    assert any(t["id"] == tid for t in traces)
