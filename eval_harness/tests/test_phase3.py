"""eval_harness · Phase 3 测试（可观测性 OTel + 跨进程 Worker 水平扩容）

全部基于 mock provider（不烧 key、确定性），验证：
- telemetry：tracer 可配置、span 正确嵌套与计数、未装 otel 自动回退内置
- queue：SqliteJobQueue 多并发原子认领（不重不漏，模拟 SKIP LOCKED）
- distributed：N 进程 Worker 跑出的结果与单进程 async 引擎一致（正确性 + 水平扩容）
"""
from __future__ import annotations

import os
import threading

import pytest

from eval_harness.core.engine import run_suite
from eval_harness.core.worker import run_distributed
from eval_harness.core.queue import SqliteJobQueue
from eval_harness.core.telemetry import configure, get_tracer, finish, _OTEL, _OTLP

HERE = os.path.dirname(__file__)
SMOKE = os.path.join(HERE, "..", "examples", "practice3_smoke.jsonl")


@pytest.fixture(autouse=True)
def _reset_tracer():
    # 每个用例用全新 tracer，避免 span 累加干扰断言
    finish()
    yield
    finish()


def _count_by_name(summary, name):
    return summary["by_name"].get(name, {}).get("count", 0)


def test_telemetry_local_spans():
    """本地模式：run 之下嵌套 case / provider.call / grader 三层 span，计数正确。"""
    configure()  # 无 console/otlp → LocalTracer
    results = run_suite(SMOKE, provider_name="mock", provider_kwargs={"mode": "full"})
    summary = get_tracer().summary()
    assert summary["mode"] == "local"
    assert _count_by_name(summary, "eval.run") == 1
    n = len(results)
    assert _count_by_name(summary, "eval.case") == n
    # trials=1 时 provider.call 次数 = case 数
    assert _count_by_name(summary, "eval.provider.call") == n
    # 每题至少有一个 grader span
    assert _count_by_name(summary, "eval.grader.code") == n
    assert summary["total_dur_ms"] >= 0


def test_telemetry_otel_console_no_crash():
    """装了 otel 时，console 模式应配置成功且不抛异常；未装则回退内置。"""
    configure(console=True)  # 未装 otel 时打印回退提示但仍可用
    run_suite(SMOKE, provider_name="mock", provider_kwargs={"mode": "full"})
    s = get_tracer().summary()
    # 两种模式下都应能产出摘要
    assert s["spans"] >= 1


@pytest.mark.skipif(not _OTLP, reason="opentelemetry OTLP grpc 导出器未安装，跳过真实 OTLP 配置路径")
def test_telemetry_otlp_configure():
    """OTLP 端点配置不应在 setup 阶段抛异常（懒连接，不实际发送）。"""
    configure(otlp_endpoint="http://localhost:4317")
    s = get_tracer().summary()
    assert s["mode"] == "otel"


def test_queue_atomic_claim_no_dup():
    """SqliteJobQueue 多并发原子认领：不重不漏，模拟 FOR UPDATE SKIP LOCKED。"""
    import tempfile, json
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    q = SqliteJobQueue(path)
    q.enqueue([{"i": k} for k in range(20)])
    claimed = []
    lock = threading.Lock()

    def worker():
        while q.remaining() > 0:
            job = q.claim(timeout=0.2)
            if job is None:
                continue
            with lock:
                claimed.append(job["id"])
            q.complete(job["id"], json.dumps({"ok": True}))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 20
    assert len(set(claimed)) == 20  # 无重复认领
    assert sorted(claimed) == list(range(1, 21))
    assert q.done_count() == 20
    q.close()
    os.remove(path)


def test_distributed_matches_sequential():
    """分布式（2 Worker）跑出的结果与单进程 async 引擎一致：正确性 + 水平扩容。"""
    seq = run_suite(SMOKE, provider_name="mock", provider_kwargs={"mode": "full"})
    dist = run_distributed(SMOKE, workers=2, provider_name="mock",
                           provider_kwargs={"mode": "full"})

    assert len(dist) == len(seq) == 11
    seq_pass = sum(1 for r in seq if r.passed)
    dist_pass = sum(1 for r in dist if r.passed)
    assert dist_pass == seq_pass == 11  # mock full 确定性全过
    # 结果按 id 排序后应一一对应（passed 一致）
    seq_sorted = sorted(seq, key=lambda r: int(r.case.id))
    dist_sorted = sorted(dist, key=lambda r: int(r.case.id))
    for a, b in zip(seq_sorted, dist_sorted):
        assert a.case.id == b.case.id
        assert a.passed == b.passed
        assert a.response == b.response


def test_distributed_flaky_inconclusive():
    """分布式 + flaky provider：失败占比超阈值 → 标记 inconclusive（熔断语义跨进程仍成立）。"""
    dist = run_distributed(
        SMOKE, workers=2, provider_name="mock",
        provider_kwargs={"mode": "flaky", "fail_every": 2},
        trials=8, inconclusive_ratio=0.34,
    )
    # flaky 每 2 次失败 1 次 → 8 次里约 4 次失败 > 34% → 全部 inconclusive
    assert len(dist) == 11
    assert all(r.inconclusive for r in dist)
    assert all(not r.passed for r in dist)  # inconclusive 优先于 passed
