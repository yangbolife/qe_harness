"""eval_harness · pytest 入口（演进自练习3）

展示「逐题门禁 + 聚合门禁 + 结构化结果」的插件化形态。
mock 模式本地即可跑通；real 模式用 --provider deepseek 并设 DEEPSEEK_API_KEY。
"""
import os
import json
import pytest

from eval_harness.core.engine import aggregate, run_suite
from eval_harness.core.loader import load_suite

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = os.path.join(HERE, "..", "examples", "practice3_smoke.jsonl")
PASS_THRESHOLD = 0.6
HAL_THRESHOLD = 0.3

PROVIDER = os.environ.get("EVAL_PROVIDER", "mock")
USE_MOCK = os.environ.get("EVAL_MOCK", "1") == "1"

cases = load_suite(SUITE)
print(f"[harness] 载入 {len(cases)} 题 | provider={PROVIDER} | mock={USE_MOCK}")


@pytest.mark.parametrize("case", cases, ids=[f"q{c.id}" for c in cases])
def test_not_empty(case):
    """逐题门禁1：回答非空。"""
    resp = _ask(case.input)
    assert resp.strip(), f"空回答: {case.input[:30]}"


@pytest.mark.parametrize("case", cases, ids=[f"q{c.id}" for c in cases])
def test_answer_matches(case):
    """逐题门禁2：代码评分器命中（精确/包含/数值兜底）。"""
    resp = _ask(case.input, case.gold)
    from eval_harness.core.graders import contains
    assert contains(resp, case.gold), f"未命中 gold={case.gold} | resp={resp[:40]}"


def test_overall_accuracy():
    """聚合门禁1：整体通过率 >= 阈值。"""
    results = run_suite(SUITE, provider_name=PROVIDER)
    agg = aggregate(results)
    print(f"\n[聚合] 通过率={agg['pass_rate']:.2%} 阈值={PASS_THRESHOLD:.2%}")
    assert agg["pass_rate"] >= PASS_THRESHOLD


_cache = {}


def _ask(prompt, gold=""):
    key = (prompt, gold)
    if key in _cache:
        return _cache[key]
    from eval_harness.core.providers import PROVIDERS
    prov = PROVIDERS.get(PROVIDER)()
    out = prov.ask(prompt, gold)
    _cache[key] = out
    return out
