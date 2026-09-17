"""限流抖动分离（Item3）+ 工作台回链（Item2）的 harness 侧端到端验证。

覆盖：
- _classify_failure：把 provider 异常文案解析为 failure_class / status_code；
- 失败归类随运行落库：cases / trials 的 failure_class、status_code 正确写入并能读回；
- 环境类失败（429/5xx/超时/网络）与真实缺陷分离：always_fail 模式被判为 rate_limited 而非缺陷；
- 回链 meta：external_id + wb_meta（contract_id / callback_url）随运行记录并经 get_run 回显。
"""
from __future__ import annotations

import asyncio
import tempfile

from eval_harness.core.persistence import Database
from eval_harness.core.engine import run_suite_async, _classify_failure
from eval_harness.core.ratelimit import CircuitBreaker, RetryPolicy


def _tmp_db():
    d = tempfile.mkdtemp(prefix="fcbl_")
    return f"sqlite+aiosqlite:///{d}/t.db"


async def _init(url):
    db = Database(url)
    await db.init()
    return db


async def _last_run_id(db):
    runs = await db.list_runs(limit=1)
    return runs[0]["id"]


SUITE = "eval_harness/examples/practice3_smoke.jsonl"


# ---------------- Item3：失败归类（单元级）----------------
def test_classify_failure_categories():
    # 429 → rate_limited
    fc, sc = _classify_failure("HTTP 429 Too Many Requests")
    assert fc == "rate_limited" and sc == 429
    # 5xx → server_error
    fc, sc = _classify_failure("deepseek HTTP 503 Service Unavailable")
    assert fc == "server_error" and sc == 503
    # 4xx → client_error（契约/鉴权不符，属真实缺陷）
    fc, sc = _classify_failure("local_agent HTTP 400 Bad Request")
    assert fc == "client_error" and sc == 400
    # 超时（无 http 状态）
    fc, sc = _classify_failure("Request timeout after 30s")
    assert fc == "timeout" and sc is None
    # 网络异常
    fc, sc = _classify_failure("Connection refused / URLError")
    assert fc == "network"
    # 熔断
    fc, sc = _classify_failure("circuit_open: breaker is open")
    assert fc == "circuit_open"
    # 偶发 429-like（无 HTTP 前缀，靠关键字）
    fc, sc = _classify_failure("mock injected permanent failure (429-like)")
    assert fc == "rate_limited"
    # 兜底
    fc, sc = _classify_failure("something weird happened")
    assert fc == "unknown"


# ---------------- Item3：失败归类随运行落库 ----------------
def test_failure_class_persisted_end_to_end():
    db = asyncio.run(_init(_tmp_db()))
    try:
        results = asyncio.run(run_suite_async(
            SUITE, provider_name="mock", provider_kwargs={"mode": "always_fail"},
            default_grader="code", trials=1, store=db, run_name="fc-test",
            # 关掉熔断/退避，让单次失败直接落到 RetryableError → rate_limited 归类
            retry_policy=RetryPolicy(max_retries=0),
            circuit=CircuitBreaker(failure_threshold=100000),
        ))
        # 引擎层：未通过且被判为环境类（rate_limited），而非缺陷
        assert all(not r.passed for r in results)
        assert all(r.inconclusive for r in results)
        assert all(r.failure_class == "rate_limited" for r in results)

        rid = asyncio.run(_last_run_id(db))
        run = asyncio.run(db.get_run(rid))
        # 落库后读回：cases / trials 都带 failure_class
        assert run["inconclusive"] == run["total"]
        for c in run["cases"]:
            assert c["failure_class"] == "rate_limited", c
            assert c["passed"] is False
            assert c["inconclusive"] is True
            for t in c["trials"]:
                assert t["failure_class"] == "rate_limited", t
    finally:
        asyncio.run(db.close())


# ---------------- Item2：回链 meta 随运行记录并回显 ----------------
def test_wb_meta_round_trip():
    db = asyncio.run(_init(_tmp_db()))
    try:
        wb_meta = {"callback_url": "http://wb.local/eval/C-7", "contract_id": "C-7"}
        asyncio.run(run_suite_async(
            SUITE, provider_name="mock", provider_kwargs={"mode": "full"},
            default_grader="code", trials=1, store=db, run_name="wb-test",
            external_id="wb-run-C-7", wb_meta=wb_meta,
        ))
        rid = asyncio.run(_last_run_id(db))
        run = asyncio.run(db.get_run(rid))
        # 外部关联键
        assert run["external_id"] == "wb-run-C-7"
        # 回链 meta 经 get_run 回显（_run_to_dict 从 config_json 提取 wb_meta）
        assert run["meta"]["wb_meta"] == wb_meta
    finally:
        asyncio.run(db.close())
