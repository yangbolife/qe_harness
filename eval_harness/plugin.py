"""eval_harness · pytest 集成（N11 · 测试框架集成）

把「跑评测」变成 pytest 里的一等公民，让评测能直接进既有 CI 测试套件：

用法（项目根放 conftest.py 写 ``pytest_plugins = ["eval_harness.plugin"]``，或 pip install -e .）：

    def test_my_agent(eval_harness):
        results = eval_harness.run("examples/practice3_smoke.jsonl",
                                   provider="mock", provider_kwargs={"mode": "full"},
                                   grader="code", trials=3)
        # 断言整体通过率
        eval_harness.assert_pass_rate(results, 0.8)
        # 断言逐类阈值 / 全过
        eval_harness.assert_category_min(results, "safety", 0.9)
        eval_harness.assert_all_pass(results)

非 pytest 场景也可直接 ``from eval_harness.plugin import run_eval``。
"""
from __future__ import annotations

from typing import Optional

import pytest

from .core.engine import aggregate, run_suite
from .core.models import CaseResult


class HarnessRunner:
    """pytest fixture 暴露的评测运行器。"""

    def run(self, suite: str, *, provider: str = "mock", grader: str = "code",
            **kwargs) -> list[CaseResult]:
        """跑一次评测，返回 list[CaseResult]。

        为贴合 CLI 习惯，``provider``/``grader`` 是 ``provider_name``/``default_grader`` 的友好别名；
        其余 kwargs（trials/policy/provider_kwargs/judge_provider/...）原样透传 engine.run_suite。
        """
        kwargs.setdefault("provider_name", provider)
        kwargs.setdefault("default_grader", grader)
        return run_suite(suite, **kwargs)

    def assert_pass_rate(self, results: list[CaseResult], min_rate: float, msg: Optional[str] = None):
        agg = aggregate(results)
        rate = agg["pass_rate"]
        assert rate >= min_rate, msg or (
            f"通过率 {rate * 100:.1f}% 低于门槛 {min_rate * 100:.1f}%"
            f"（{agg['passed']}/{agg['total']} 通过）"
        )

    def assert_all_pass(self, results: list[CaseResult], msg: Optional[str] = None):
        agg = aggregate(results)
        assert agg["passed"] == agg["total"], msg or (
            f"有 {agg['total'] - agg['passed']} 题未通过（共 {agg['total']}）"
        )

    def assert_category_min(self, results: list[CaseResult], category: str, min_rate: float,
                            msg: Optional[str] = None):
        agg = aggregate(results)
        cr = agg.get("by_category", {}).get(category, {}).get("pass_rate", 0.0)
        n = agg.get("by_category", {}).get(category, {}).get("total", 0)
        assert cr >= min_rate, msg or (
            f"类别[{category}] 通过率 {cr * 100:.1f}% 低于门槛 {min_rate * 100:.1f}%"
            f"（{n} 题）"
        )


@pytest.fixture
def eval_harness() -> HarnessRunner:
    return HarnessRunner()


def run_eval(suite: str, **kwargs) -> list[CaseResult]:
    """非 pytest 便捷入口：跑一次评测并返回结果。"""
    return run_suite(suite, **kwargs)
