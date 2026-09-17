"""trajectory_eval 轨迹级评测框架 · 单元测试

覆盖：结构度量（覆盖/必做/效率/鲁棒）、捷径侥幸判定、冗余循环、越权动作、
自由文本解析、grader 注册与 engine 接入、与 trace_store 回灌轨迹的兼容。
"""
import pytest

from eval_harness.core.trajectory_eval import (
    Trajectory, TrajectoryEvaluator, TrajectoryEvalGrader,
    evaluate_trajectory, make_demo_trajectory_solid, make_demo_trajectory_lucky,
)
from eval_harness.core.registry import GRADERS
from eval_harness.core.models import Case, GraderResult
from eval_harness.core.trace_store import parse_trace_to_case, make_demo_trace


# ---------- 1. 规范轨迹：应判定通过 ----------
def test_solid_trajectory_passes():
    res = evaluate_trajectory(
        make_demo_trajectory_solid(),
        expected_steps=["检索", "核验", "确认"],
        required_steps=["核验"],
    )
    assert res.shortcut_risk is False
    assert res.required_coverage == 1.0
    assert res.robustness == 1.0
    assert res.passed is True
    assert res.first_failure_step is None


# ---------- 2. 蒙对轨迹：跳过必做「核验」→ 捷径风险、不通过 ----------
def test_lucky_trajectory_shortcut():
    res = evaluate_trajectory(
        make_demo_trajectory_lucky(),
        expected_steps=["检索", "确认"],
        required_steps=["核验"],
    )
    assert res.shortcut_risk is True
    assert res.required_coverage < 1.0
    assert res.passed is False
    assert "捷径" in res.detail


# ---------- 3. 冗余/循环：重复工具调用应被检出 ----------
def test_redundancy_detected():
    traj = [
        {"kind": "action", "tool": "search", "args": "'x'"},
        {"kind": "observation", "content": "r1"},
        {"kind": "action", "tool": "search", "args": "'x'"},   # 完全重复
        {"kind": "observation", "content": "r2"},
        {"kind": "answer", "content": "done"},
    ]
    res = evaluate_trajectory(traj, expected_steps=["search"])
    assert res.redundancy_ratio > 0.0
    assert 0.0 <= res.efficiency < 1.0


# ---------- 4. 越权动作：调用禁用工具 → 不通过 ----------
def test_forbidden_tool_flagged():
    traj = [
        {"kind": "action", "tool": "delete_all", "args": ""},
        {"kind": "answer", "content": "cleared"},
    ]
    res = evaluate_trajectory(traj, forbidden_tools=["delete_all"])
    assert any("delete_all" in i for i in res.action_issues)
    assert res.passed is False


# ---------- 5. 崩溃信号：鲁棒性归零、不通过 ----------
def test_crash_robustness_zero():
    traj = [
        {"kind": "action", "tool": "api", "args": ""},
        {"kind": "observation", "content": "Traceback: timeout"},
        {"kind": "answer", "content": "no result"},
    ]
    res = evaluate_trajectory(traj, expected_steps=["api"])
    assert res.robustness == 0.0
    assert res.first_failure_step is not None
    assert res.passed is False


# ---------- 6. 自由文本解析：编号步骤 + 工具提取 + answer 识别 ----------
def test_from_text_parsing():
    text = (
        "步骤1: 思考用户要查余额\n"
        "步骤2: 调用 query_balance('acc1')\n"
        "步骤3: 观察返回 100 元\n"
        "步骤4: 回答: 余额 100 元"
    )
    traj = Trajectory.from_text(text)
    kinds = [s.kind for s in traj.steps]
    assert "action" in kinds
    assert "answer" in kinds
    action = next(s for s in traj.steps if s.kind == "action")
    assert action.tool == "query_balance"
    assert action.args == "'acc1'"


# ---------- 7. grader 注册 + engine 接入 ----------
def test_grader_registered_and_judge():
    assert "trajectory_eval" in GRADERS.available()
    g = GRADERS.get("trajectory_eval")()
    case = Case(
        id="t1", input="x", grader="trajectory_eval",
        meta={"trajectory": make_demo_trajectory_solid(),
              "expected_steps": ["检索", "核验", "确认"], "required_steps": ["核验"]},
    )
    r = g.judge("", case)   # pred 为空也行：轨迹来自 meta.trajectory
    assert isinstance(r, __import__("eval_harness.core.models", fromlist=["GraderResult"]).GraderResult)
    assert r.passed is True


# ---------- 8. 与 trace_store 回灌轨迹兼容（结构化 meta.trajectory） ----------
def test_trace_store_compat():
    case = parse_trace_to_case(make_demo_trace(), trace_id="demo")
    # parse_trace_to_case 已写入结构化 trajectory
    assert case.meta.get("trajectory")
    g = GRADERS.get("trajectory_eval")()
    r = g.judge(case.meta.get("trajectory_text", ""), case)
    assert r.grader == "trajectory_eval"
    assert r.score >= 0.0
