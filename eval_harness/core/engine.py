"""eval_harness · 执行引擎（Harness / Runtime）

Phase 2：asyncio 并发调度 + 令牌桶限流 + 指数退避 + 熔断(inconclusive) +
沙箱隔离（评分器层）+ Trial(k 次 → pass@k / pass^k)。
引擎与 provider / grader 通过注册表解耦；run_suite 同步入口兼容既有 pytest。

解决常见开源框架典型痛点：
- 单进程串行 → asyncio.Semaphore 并发
- 429 限流   → TokenBucket 速率控制
- 偶发抖动全红 → RetryPolicy 退避自愈
- provider 雪崩误判 → CircuitBreaker 熔断并标记 inconclusive
- 评测逻辑不可信 → sandbox 评分器子进程隔离
- 非确定性 SUT 单点误判 → Trial k 次 + pass@k / pass^k 一致性度量
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Optional

from .graders import GRADERS
from .loader import load_suite
from .models import CaseResult, Trial
from .providers import PROVIDERS
from .ratelimit import (
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    RetryableError,
    TokenBucket,
)
from .telemetry import get_tracer
from .version import HARNESS_VERSION
from .scaffold import run_three_way
from .gitinfo import collect_git_info
from . import scorers  # noqa: F401  注册 N1 预置评测器库（autoevals 平替）
from . import trajectory_eval  # noqa: F401  注册 trajectory_eval 评分器（轨迹级评测框架）


def _build_grader(name: str, judge_provider=None, human_labels=None):
    if name == "judge":
        return GRADERS.get("judge")(judge_provider=judge_provider)
    if name == "human":
        return GRADERS.get("human")(labels=human_labels)
    cls = GRADERS.get(name)
    # N1：新评测器库（如 factuality）需要 judge_provider；旧评测器不接受该参数 → 零参回退
    try:
        return cls(judge_provider=judge_provider, human_labels=human_labels)
    except TypeError:
        return cls()


def _combine(graders, case, combine) -> bool:
    strat = case.meta.get("combine", combine)
    if not graders:
        return False
    return all(g.passed for g in graders) if strat == "all" else any(g.passed for g in graders)


# 失败归类（环境类/缺陷分离用）。与被测方一次偶发 429 被写成「高危缺陷」是评测公信力的
# 致命伤，故必须把限流/超时/服务端/网络抖动等环境类失败与真实缺陷明确分开。
# 对齐 qe-platform engines.base.TRANSIENT_FAILURES 的口径。
import re as _re


def _classify_failure(error: str) -> tuple[str, Optional[int]]:
    """把 provider 异常文案解析为 (failure_class, http_status)。

    failure_class ∈ {rate_limited, server_error, timeout, network, circuit_open,
    client_error, unknown}。前者四类为环境类（不可判），client_error 属真实缺陷
    （接口的契约/鉴权不符）。
    """
    s = (error or "").lower()
    status = None
    m = _re.search(r"http[ ]?(\d{3})", s)
    if m:
        status = int(m.group(1))
    if "circuit_open" in s:
        return "circuit_open", status
    if status is not None:
        if status == 429:
            return "rate_limited", status
        if 500 <= status < 600:
            return "server_error", status
        if 400 <= status < 500:
            return "client_error", status
    if "429" in s or ("rate" in s and "limit" in s):
        return "rate_limited", status
    if "timeout" in s or "timed out" in s:
        return "timeout", status
    if "connection" in s or "urlerror" in s or "network" in s:
        return "network", status
    return "unknown", status


# ---------- 创新⑤：Judge 成本预算闸门（防止 judge 通道成本爆炸）----------
class ResultList(list):
    """结果列表（list 子类），支持附加元数据（如三方解耦 _three_way）。

    普通 list 无法设置任意属性；本类用于承载 run 级附属信息，语义与 list 完全兼容。
    """
    pass


class BudgetGate:
    """Judge 成本/Token 预算闸门（创新⑤ + O13）。

    budget_usd>0 时按估算成本累计；token_budget>0 时按估算 token 累计；
    任一越限即 exceeded，Judge 通道中止（防止 judge/远程评测成本爆炸）。
    """

    def __init__(self, budget_usd: float = 0.0, token_budget: int = 0):
        self.budget = budget_usd
        self.token_budget = token_budget
        self.used = 0.0
        self.tokens = 0
        self._lock = asyncio.Lock()

    @property
    def exceeded(self) -> bool:
        if self.budget > 0 and self.used >= self.budget:
            return True
        if self.token_budget > 0 and self.tokens >= self.token_budget:
            return True
        return False

    def charge(self, cost: float, tokens: int = 0):
        self.used += cost
        self.tokens += tokens

    async def charge_async(self, cost: float, tokens: int = 0):
        async with self._lock:
            self.used += cost
            self.tokens += tokens


# ---------- 异步适配：provider 可能只提供同步 ask ----------
async def _ask_async(provider, prompt: str, gold: str) -> str:
    if hasattr(provider, "ask_async"):
        return await provider.ask_async(prompt, gold)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: provider.ask(prompt, gold))


# ---------- 单题 k 次执行（含限流/退避/熔断）----------
async def _run_one(case, *, provider, judge_provider, human_labels, default_grader,
                  combine, trials, trial_policy, inconclusive_ratio,
                  sem, bucket, circuit, retry, budget=None):
    tracer = get_tracer()
    with tracer.start_span("eval.case", case_id=case.id, suite=case.suite,
                           category=case.category) as span:
        trials_out: list[Trial] = []
        for _ in range(trials):
            async with sem:
                if bucket:
                    await bucket.acquire()
                t0 = time.time()
                try:
                    resp = await _ask_with_retry(circuit, retry, provider, case.input, case.gold)
                except CircuitOpenError:
                    trials_out.append(Trial(response="", error="circuit_open",
                                            failure_class="circuit_open",
                                            duration_ms=(time.time() - t0) * 1000))
                    continue
                except RetryableError as e:
                    fc, sc = _classify_failure(f"retry_exhausted:{e}")
                    trials_out.append(Trial(response="", error=f"retry_exhausted:{e}",
                                            failure_class=fc, status_code=sc,
                                            duration_ms=(time.time() - t0) * 1000))
                    continue
                except Exception as e:  # pragma: no cover - 兜底
                    fc, sc = _classify_failure(f"{type(e).__name__}:{e}")
                    trials_out.append(Trial(response="", error=f"{type(e).__name__}:{e}",
                                            failure_class=fc, status_code=sc,
                                            duration_ms=(time.time() - t0) * 1000))
                    continue
                graders = await _grade_async(resp, case, judge_provider, human_labels, default_grader, budget)
                tp = _combine(graders, case, combine)
                trials_out.append(Trial(response=resp, graders=graders,
                                        duration_ms=(time.time() - t0) * 1000, passed=tp))

        passed_trials = sum(1 for t in trials_out if t.passed)
        err_count = sum(1 for t in trials_out if t.error)
        pass_rate = passed_trials / trials if trials else 0.0
        pass_at_k = 1 - (1 - pass_rate) ** trials if trials else 0.0
        pass_k = bool(trials) and all(t.passed for t in trials_out)
        inconclusive = (err_count / trials) >= inconclusive_ratio if trials else False

        if trial_policy == "all":
            passed = pass_k
        elif trial_policy == "majority":
            passed = pass_rate >= 0.5
        else:  # any（best-of-k）
            passed = passed_trials > 0
        if inconclusive:
            passed = False

        first = next((t for t in trials_out if not t.error), trials_out[0] if trials_out else None)
        span.set_attribute("passed", passed)
        span.set_attribute("inconclusive", inconclusive)
        span.set_attribute("k", trials)
        span.set_attribute("err_count", err_count)

        # 主导失败归类：取出现次数最多的 failure_class（环境类/缺陷分离用）
        from collections import Counter as _Counter
        trial_fcs = [t.failure_class for t in trials_out if t.failure_class]
        dominant_fc = _Counter(trial_fcs).most_common(1)[0][0] if trial_fcs else ""
        dom_status = next(
            (t.status_code for t in trials_out
             if t.failure_class == dominant_fc and t.status_code is not None),
            None,
        )
        return CaseResult(
            case=case, trials=trials_out,
            response=first.response if first else "",
            graders=first.graders if first else [],
            duration_ms=sum(t.duration_ms for t in trials_out),
            passed=passed, k=trials, pass_rate=pass_rate, pass_at_k=pass_at_k,
            pass_k=pass_k, inconclusive=inconclusive,
            error="circuit_open" if (trials and err_count == trials) else "",
            failure_class=dominant_fc,
            status_code=dom_status,
        )


async def _ask_with_retry(circuit, retry, provider, prompt, gold):
    tracer = get_tracer()
    async def attempt():
        return await _ask_async(provider, prompt, gold)
    with tracer.start_span("eval.provider.call", provider=getattr(provider, "name", "?")) as ps:
        for i in range(retry.max_retries + 1):
            try:
                r = await circuit.call(attempt)
                ps.set_attribute("chars", len(r))
                return r
            except CircuitOpenError:
                raise
            except RetryableError:
                if i < retry.max_retries:
                    await asyncio.sleep(retry.wait_for(i))
                else:
                    raise


async def _grade_async(response, case, judge_provider, human_labels, default_grader, budget=None):
    tracer = get_tracer()
    graders = []
    for gname in (case.grader or default_grader).split(","):
        gname = gname.strip()
        g = _build_grader(gname, judge_provider=judge_provider, human_labels=human_labels)
        if gname == "judge" and budget is not None and hasattr(g, "budget_gate"):
            g.budget_gate = budget  # 创新⑤：judge 通道接预算闸门
        with tracer.start_span(f"eval.grader.{gname}") as gs:
            if gname == "judge" and judge_provider is not None and hasattr(judge_provider, "ask_async"):
                res = await g.judge_async(response, case)
            else:
                res = g.judge(response, case)
            gs.set_attribute("score", res.score)
            gs.set_attribute("passed", res.passed)
            graders.append(res)
    return graders


# =================== 异步主入口 ===================
async def run_suite_async(
    suite_path: str,
    provider_name: str = "mock",
    provider_kwargs: Optional[dict] = None,
    judge_provider=None,
    judge_provider_name: Optional[str] = None,
    judge_provider_kwargs: Optional[dict] = None,
    human_labels: Optional[dict] = None,
    default_grader: str = "code",
    combine: str = "any",
    trials: int = 1,
    trial_policy: str = "any",
    inconclusive_ratio: float = 0.34,
    concurrency: int = 4,
    rate: float = 8.0,
    retry_policy: Optional[RetryPolicy] = None,
    circuit: Optional[CircuitBreaker] = None,
    output_path: Optional[str] = None,
    # —— Phase 4 / 创新点接入 —— #
    judge_budget: float = 0.0,
    three_way: bool = False,
    store=None,
    run_name: str = "",
    model_version: Optional[str] = None,
    dataset_version: Optional[str] = None,
    params: Optional[dict] = None,  # N8：附加评测参数（Parameters），随运行记录
    # —— O13：token/成本预算 —— #
    token_budget: int = 0,
    cost_budget: float = 0.0,
    # —— N12：RBAC 行级 ACL —— #
    owner: Optional[str] = None,
    visibility: str = "all",
    # —— 外部关联键（跨系统）：工作台传其评测运行 id，用于回灌关联 —— #
    external_id: Optional[str] = None,
    # —— 工作台回链 meta（O14 回链）：contract_id / callback_url 等，随运行记录 —— #
    wb_meta: Optional[dict] = None,
) -> list[CaseResult]:
    provider_kwargs = provider_kwargs or {}
    provider = PROVIDERS.get(provider_name)(**provider_kwargs)
    if judge_provider is None and judge_provider_name:
        judge_provider = PROVIDERS.get(judge_provider_name)(**(judge_provider_kwargs or {}))

    cases = load_suite(suite_path)
    sem = asyncio.Semaphore(max(1, concurrency))
    bucket = TokenBucket(rate) if rate and rate > 0 else None
    if circuit is None:
        circuit = CircuitBreaker()
    if retry_policy is None:
        retry_policy = RetryPolicy()
    # O13：token/成本预算闸门（与 judge_budget 兼容，取较大者）
    budget = BudgetGate(max(cost_budget, judge_budget), token_budget)

    tracer = get_tracer()
    with tracer.start_span("eval.run", suite=suite_path, cases=len(cases),
                           provider=provider_name) as run_span:
        results = await asyncio.gather(*[
            _run_one(
                case, provider=provider, judge_provider=judge_provider,
                human_labels=human_labels, default_grader=default_grader, combine=combine,
                trials=max(1, trials), trial_policy=trial_policy,
                inconclusive_ratio=inconclusive_ratio, sem=sem, bucket=bucket,
                circuit=circuit, retry=retry_policy, budget=budget,
            )
            for case in cases
        ])
        results = ResultList(results)
        run_span.set_attribute("passed", sum(1 for r in results if r.passed))
        run_span.set_attribute("inconclusive", sum(1 for r in results if r.inconclusive))

    # 创新③：脚手架三方解耦
    tw = None
    if three_way:
        tw = await run_three_way(
            suite_path, provider_name=provider_name, provider_kwargs=provider_kwargs,
            judge_provider_name=judge_provider_name, judge_provider_kwargs=judge_provider_kwargs,
            trials=max(1, trials), trial_policy=trial_policy, rate=rate, concurrency=concurrency,
        )

    # 版本钉（创新④）+ 代码版本钉（N3 并入增强）
    git_info = collect_git_info()
    meta = {
        "provider": provider_name, "judge_provider": judge_provider_name,
        "grader": default_grader, "trials": max(1, trials), "trial_policy": trial_policy,
        "combine": combine, "concurrency": concurrency, "rate": rate,
        "harness_version": HARNESS_VERSION,
        "model_version": model_version or provider_name,
        "dataset_version": dataset_version or "manual",
        "three_way": three_way,
    }
    for k, v in git_info.items():
        meta[f"git_{k}"] = v
    if params:
        meta["params"] = params  # N8：附加评测参数（Parameters）
    # O13：预算执行结果
    cost_used = sum(float(g.cost_usd or 0.0) for r in results for g in r.graders
                   if getattr(g, "grader", "") == "judge")
    budget_aborted = budget.exceeded
    meta["cost_used_usd"] = round(cost_used, 6)
    meta["token_budget"] = token_budget
    meta["cost_budget"] = cost_budget
    meta["budget_aborted"] = budget_aborted
    meta["owner"] = owner
    meta["visibility"] = visibility
    # 回链 meta：被工作台调用时携带合同 id / 回链 URL，随运行落库供双向回查
    if wb_meta:
        meta["wb_meta"] = wb_meta

    # Phase 4：持久化落库
    if store is not None:
        await store.save_run(
            run_name or suite_path, meta, results, three_way=tw,
            owner=owner, visibility=visibility, external_id=external_id,
            token_budget_usd=max(cost_budget, judge_budget),
            cost_used_usd=round(cost_used, 6), budget_aborted=budget_aborted,
        )

    if output_path:
        _dump(results, output_path)
    if tw is not None:
        results._three_way = tw  # type: ignore[attr-defined]
    return results


def run_suite(
    suite_path: str,
    provider_name: str = "mock",
    provider_kwargs: Optional[dict] = None,
    judge_provider=None,
    judge_provider_name: Optional[str] = None,
    judge_provider_kwargs: Optional[dict] = None,
    human_labels: Optional[dict] = None,
    default_grader: str = "code",
    combine: str = "any",
    trials: int = 1,
    trial_policy: str = "any",
    inconclusive_ratio: float = 0.34,
    concurrency: int = 4,
    rate: float = 8.0,
    output_path: Optional[str] = None,
    judge_budget: float = 0.0,
    three_way: bool = False,
    store=None,
    run_name: str = "",
    model_version: Optional[str] = None,
    dataset_version: Optional[str] = None,
    params: Optional[dict] = None,  # N8：附加评测参数（Parameters），随运行记录
    token_budget: int = 0,
    cost_budget: float = 0.0,
    owner: Optional[str] = None,
    visibility: str = "all",
    wb_meta: Optional[dict] = None,
) -> list[CaseResult]:
    """同步兼容入口：pytest / CLI 直接调用。内部委托 asyncio 引擎。"""
    return asyncio.run(run_suite_async(
        suite_path, provider_name=provider_name, provider_kwargs=provider_kwargs,
        judge_provider=judge_provider, judge_provider_name=judge_provider_name,
        judge_provider_kwargs=judge_provider_kwargs, human_labels=human_labels,
        default_grader=default_grader, combine=combine, trials=trials,
        trial_policy=trial_policy, inconclusive_ratio=inconclusive_ratio,
        concurrency=concurrency, rate=rate, output_path=output_path,
        judge_budget=judge_budget, three_way=three_way, store=store,
        run_name=run_name, model_version=model_version, dataset_version=dataset_version,
        params=params, token_budget=token_budget, cost_budget=cost_budget,
        owner=owner, visibility=visibility, wb_meta=wb_meta,
    ))


def result_to_row(r: CaseResult) -> dict:
    """把一条 CaseResult 序列化为可 JSON 化的行（供落盘 / 跨进程回写队列）。"""
    d = asdict(r.case)
    d["response"] = r.response
    d["graders"] = [asdict(g) for g in r.graders]
    d["trials"] = [asdict(t) for t in r.trials]
    d["passed"] = r.passed
    d["inconclusive"] = r.inconclusive
    d["failure_class"] = r.failure_class
    d["status_code"] = r.status_code
    d["k"] = r.k
    d["pass_rate"] = round(r.pass_rate, 4)
    d["pass_at_k"] = round(r.pass_at_k, 4)
    d["pass_k"] = r.pass_k
    d["primary_score"] = r.primary_score
    d["duration_ms"] = round(r.duration_ms, 1)
    return d


def _dump(results: list[CaseResult], path: str):
    rows = [result_to_row(r) for r in results]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def aggregate(results: list[CaseResult]) -> dict:
    n = len(results)
    passed = sum(1 for r in results if r.passed)
    inconclusive = sum(1 for r in results if r.inconclusive)
    avg_pass_at_k = (sum(r.pass_at_k for r in results) / n) if n else 0.0
    return {
        "total": n,
        "passed": passed,
        "pass_rate": passed / n if n else 0.0,
        "inconclusive": inconclusive,
        "avg_pass_at_k": avg_pass_at_k,
        "by_suite": _group_by(results, "suite"),
        "by_category": _group_by(results, "category"),
    }


def _group_by(results: list[CaseResult], attr: str) -> dict:
    out: dict = {}
    for r in results:
        key = getattr(r.case, attr)
        out.setdefault(key, {"total": 0, "passed": 0, "inconclusive": 0})
        out[key]["total"] += 1
        out[key]["passed"] += 1 if r.passed else 0
        out[key]["inconclusive"] += 1 if r.inconclusive else 0
    for k in out:
        t = out[k]["total"]
        out[k]["pass_rate"] = out[k]["passed"] / t if t else 0.0
    return out
