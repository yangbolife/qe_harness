"""eval_harness · 跨进程 Worker（Phase 3 · 水平扩容）

Coordinator 把用例集入队 → 起 N 个独立进程 Worker → 各自从队列原子认领任务、
在本进程内构造 provider/judge（避免跨进程 pickle 客户端对象）、跑异步引擎、回写结果。

- 零外部依赖：本地用 SqliteJobQueue（单文件），多进程安全。
- 生产替换点：把 queue 换成 RedisJobQueue 即可跨机扩容，Worker/引擎代码不变。
- 每个 Worker 进程独立：天然规避 GIL，CPU/IO 密集评测可水平铺开。
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sys
import tempfile
from typing import Optional

# 保证子进程（spawn）能 import 本包
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def worker_main(db_path: str, params_json: str, worker_id: int):
    """Worker 进程入口（顶层函数，供 multiprocessing spawn）。

    注意：provider / judge 的真实客户端对象在进程内重建，绝不跨进程 pickle。
    """
    from .engine import _run_one, result_to_row
    from .models import Case
    from .providers import PROVIDERS
    from .ratelimit import TokenBucket, CircuitBreaker, RetryPolicy
    from .queue import SqliteJobQueue
    from .telemetry import configure, finish

    params = json.loads(params_json)
    otlp = os.environ.get("HARNESS_OTEL_ENDPOINT")
    if otlp:
        configure(otlp_endpoint=otlp)

    provider = PROVIDERS.get(params["provider_name"])(**(params.get("provider_kwargs") or {}))
    judge_provider = None
    if params.get("judge_provider_name"):
        judge_provider = PROVIDERS.get(params["judge_provider_name"])(
            **(params.get("judge_provider_kwargs") or {})
        )

    q = SqliteJobQueue(db_path)
    sem = asyncio.Semaphore(1)  # 单 Worker 内串行即可（并发由多 Worker 进程体现）
    bucket = TokenBucket(params["rate"]) if params.get("rate") else None
    circuit = CircuitBreaker()
    retry = RetryPolicy()

    processed = 0
    while q.remaining() > 0:
        job = q.claim(timeout=0.5)
        if job is None:
            continue
        try:
            case = Case.from_dict(json.loads(job["payload"]))
            result = _run_one_sync(
                case, provider=provider, judge_provider=judge_provider,
                human_labels=None,
                default_grader=params.get("default_grader", "code"),
                combine=params.get("combine", "any"),
                trials=params.get("trials", 1),
                trial_policy=params.get("trial_policy", "any"),
                inconclusive_ratio=params.get("inconclusive_ratio", 0.34),
                sem=sem, bucket=bucket, circuit=circuit, retry=retry,
            )
            q.complete(job["id"], json.dumps(result_to_row(result), ensure_ascii=False))
            processed += 1
        except Exception as e:  # pragma: no cover - Worker 容错：单题失败不影响其余
            q.fail(job["id"], f"{type(e).__name__}: {e}")
    finish()
    return processed


def _run_one_sync(case, **kw):
    """Worker 内同步驱动单题异步执行（进程内 event loop）。"""
    import asyncio

    return asyncio.run(_run_one_awaitable(case, **kw))


async def _run_one_awaitable(case, **kw):
    # 直接复用 engine 的 _run_one（已含限流/退避/熔断/Trial 逻辑）
    from .engine import _run_one

    return await _run_one(case, **kw)


def run_distributed(
    suite_path: str,
    workers: int = 2,
    queue_path: Optional[str] = None,
    provider_name: str = "mock",
    provider_kwargs: Optional[dict] = None,
    judge_provider_name: Optional[str] = None,
    judge_provider_kwargs: Optional[dict] = None,
    default_grader: str = "code",
    combine: str = "any",
    trials: int = 1,
    trial_policy: str = "any",
    inconclusive_ratio: float = 0.34,
    rate: float = 0.0,
    output_path: Optional[str] = None,
) -> list:
    """Coordinator：入队 + 起 N 个 Worker 进程 + 回收结果。返回 list[CaseResult]。"""
    from dataclasses import asdict

    from .engine import _dump
    from .loader import load_suite
    from .models import CaseResult
    from .queue import SqliteJobQueue

    cases = load_suite(suite_path)
    if not queue_path:
        fd, queue_path = tempfile.mkstemp(suffix=".db", prefix="evalq_")
        os.close(fd)

    q = SqliteJobQueue(queue_path)
    q.reset_running()
    q.enqueue([asdict(c) for c in cases])

    params = json.dumps({
        "provider_name": provider_name,
        "provider_kwargs": provider_kwargs or {},
        "judge_provider_name": judge_provider_name,
        "judge_provider_kwargs": judge_provider_kwargs or {},
        "default_grader": default_grader,
        "combine": combine,
        "trials": trials,
        "trial_policy": trial_policy,
        "inconclusive_ratio": inconclusive_ratio,
        "rate": rate,
    }, ensure_ascii=False)

    procs = [
        multiprocessing.Process(target=worker_main, args=(queue_path, params, i), daemon=True)
        for i in range(max(1, workers))
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    rows = q.iter_results()
    q.close()
    try:
        os.remove(queue_path)
    except OSError:  # pragma: no cover
        pass

    results = [CaseResult.from_dict(r) for r in rows]
    # 还原原始顺序（按 id 数值）
    results.sort(key=lambda r: int(r.case.id) if str(r.case.id).isdigit() else 0)
    if output_path:
        _dump(results, output_path)
    return results
