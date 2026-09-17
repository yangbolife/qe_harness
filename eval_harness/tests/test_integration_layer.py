"""eval_harness · 交界层四类原子评测测试（mock scenario 模式）

验证：原子评测器既能放行合规行为（compliant 应过），也能拦住违规行为（violate 应失败）。
真实 SUT 评测用 --provider deepseek 跑同一套用例集即可。
"""
import os

import pytest

from eval_harness.core.engine import aggregate, run_suite

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(HERE, "..", "examples")

SUITES = {
    "permission": os.path.join(EX, "integration_permission.jsonl"),
    "contract": os.path.join(EX, "integration_contract.jsonl"),
    "fallback": os.path.join(EX, "integration_fallback.jsonl"),
    "attribution": os.path.join(EX, "integration_attribution.jsonl"),
}


@pytest.mark.parametrize("name", list(SUITES))
def test_integration_compliant_passes_and_violate_blocked(name):
    path = SUITES[name]
    results = run_suite(path, provider_name="mock", provider_kwargs={"mode": "scenario"})
    assert results, f"{name}: 空结果"
    for r in results:
        policy = r.case.gold  # compliant / violate（mock 策略通道）
        detail = [g.detail for g in r.graders]
        if policy == "compliant":
            assert r.passed, f"{name}: 合规用例应过 {r.case.id} | {detail}"
        else:  # violate
            assert not r.passed, f"{name}: 违规用例应被拦 {r.case.id} | {detail}"
    agg = aggregate(results)
    print(f"\n[交界层/{name}] 通过率={agg['pass_rate']:.0%} ({agg['passed']}/{agg['total']})")
