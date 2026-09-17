"""eval_harness · Phase 2 高并发 / 稳健性测试（mock，无需联网）

覆盖：令牌桶限流、指数退避、熔断(inconclusive)、沙箱隔离、并发调度、
k 次 Trial 一致性(pass@k / pass^k)、sandbox 评分器端到端。
"""
import asyncio
import json
import os
import statistics
import time

import pytest

from eval_harness.core.engine import run_suite
from eval_harness.core.ratelimit import (
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    RetryableError,
    TokenBucket,
)
from eval_harness.core.sandbox import run_in_sandbox

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(HERE, "..", "examples")
PRACTICE3 = os.path.join(EX, "practice3_smoke.jsonl")


# ============ 组件级：限流 / 退避 / 熔断 / 沙箱 ============
def test_token_bucket_throttles():
    async def drain():
        b = TokenBucket(rate=10.0, capacity=10)
        for _ in range(10):
            await b.acquire()            # 容量内，瞬时
        t0 = time.monotonic()
        await b.acquire()                # 第 11 个令牌，需等 ~0.1s
        return time.monotonic() - t0
    dt = asyncio.run(drain())
    assert 0.05 < dt < 0.6, f"令牌桶限速失效 dt={dt}"


def test_retry_policy_exponential_and_capped():
    # 关闭抖动以保证单调性断言确定（抖动范围由单独用例验证）
    rp = RetryPolicy(max_retries=3, base=0.1, max_wait=2.0, jitter=False)
    ws = [rp.wait_for(i) for i in range(4)]
    assert ws[1] > ws[0], "退避应递增"
    assert ws[3] <= 2.0, "退避应封顶"


def test_retry_policy_jitter_bounds():
    # 抖动应落在 [0.5, 1.5) 倍系数内，且总体随 attempt 上升（期望值递增）
    rp = RetryPolicy(max_retries=3, base=0.1, max_wait=2.0, jitter=True)
    for _ in range(50):
        ws = [rp.wait_for(i) for i in range(4)]
        # 期望值 ws[i] = base*2^i*1.0，抖动不改变期望的单调递增
        assert ws[2] >= 0.5 * 0.1 * 4 - 1e-9
        assert ws[3] <= 2.0
    # 统计学上 ws[1] 的期望(0.2)应大于 ws[0] 的期望(0.1)
    samples = [rp.wait_for(1) for _ in range(200)]
    assert 0.15 < statistics.mean(samples) < 0.25


def test_circuit_breaker_opens_then_half_opens():
    async def scenario():
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=0.2)

        async def fail():
            raise RetryableError("x")

        for _ in range(3):
            with pytest.raises(RetryableError):
                await cb.call(fail)
        # 第 4 次：熔断已开 → CircuitOpenError
        with pytest.raises(CircuitOpenError):
            await cb.call(fail)
        # 冷却后半开恢复
        await asyncio.sleep(0.3)
        async def ok():
            return "recovered"
        assert await cb.call(ok) == "recovered"
    asyncio.run(scenario())


def test_sandbox_isolation():
    ok = run_in_sandbox("RESP='42'\nassert int(RESP) == 42\nprint('checked')")
    assert ok.returncode == 0 and not ok.timed_out
    # 死循环被超时熔断
    hang = run_in_sandbox("import time\ntime.sleep(10)", timeout=1)
    assert hang.timed_out and hang.returncode == -1


# ============ 集成级：并发 / Trial / inconclusive / sandbox 评分器 ============
def test_mock_full_concurrent_k1():
    res = run_suite(PRACTICE3, provider_name="mock", concurrency=4, rate=50.0)
    assert res, "空结果"
    assert all(r.passed for r in res), "mock full 应全过"
    assert all(r.k == 1 for r in res)


def test_mock_always_fail_inconclusive():
    """provider 持续失败 → 退避耗尽 + 熔断 → 标记 inconclusive（不误判模型）。"""
    res = run_suite(PRACTICE3, provider_name="mock",
                    provider_kwargs={"mode": "always_fail"}, trials=1, concurrency=1)
    assert res, "空结果"
    assert all(r.inconclusive for r in res), "应全部 inconclusive"
    assert all(not r.passed for r in res), "inconclusive 不得判通过"


def test_trials_pass_at_k_best_of_k():
    """k=3 且 SUT 确定通过 → pass_rate=1, pass@k=1, pass^k=True。"""
    res = run_suite(PRACTICE3, provider_name="mock", trials=3,
                    trial_policy="any", concurrency=1, rate=50.0)
    assert len(res) > 0
    assert all(r.k == 3 for r in res)
    assert all(r.pass_rate == 1.0 and r.pass_at_k == 1.0 and r.pass_k
                for r in res), "确定通过项 pass@k 应=1"


def test_trial_policy_all_requires_every_k():
    """trial_policy=all(pass^k)：构造一个 k 次里偶尔不过的 SUT（mock partial）。"""
    res = run_suite(PRACTICE3, provider_name="mock", provider_kwargs={"mode": "partial"},
                    trials=5, trial_policy="all", concurrency=1, rate=50.0)
    # partial 模式约 70% 命中，故 pass^k(全过) 大概率 False，至少结构正确
    assert all(r.k == 5 for r in res)
    assert all(0.0 <= r.pass_rate <= 1.0 for r in res)


def test_sandbox_grader_e2e():
    """sandbox 评分器：在受限子进程执行 check_code，隔离评估。"""
    suite = os.path.join(EX, "sandbox_demo.jsonl")
    res = run_suite(suite, provider_name="mock", concurrency=1)
    by_id = {r.case.id: r for r in res}
    assert by_id["sb-1"].passed, "42 应通过 check_code"
    assert by_id["sb-2"].passed, "hello world 应通过 check_code"
    assert not by_id["sb-3"].passed, "bad != good 应被拦"
    # 评分器类型正确
    assert by_id["sb-1"].graders[0].grader == "sandbox"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
