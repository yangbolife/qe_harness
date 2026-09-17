"""eval_harness · 智化融合评测引擎

架构：core(模型/注册表/评分器/提供者/加载器/限流/沙箱/可观测/队列/Worker/持久化/脚手架/
轨迹归因/生成器/trace回灌/引擎) + cli + web + tests
设计目标：插件化解耦、三层评分器、声明式用例、本地 CLI/pytest 友好，
并向生产演进：asyncio 高并发 + 令牌桶 + 退避熔断 + 沙箱 + Trial(pass@k/pass^k)
+ OTel 可观测 + 跨进程 Worker 水平扩容 + PostgreSQL 持久化。
六创新点（对标 lm-eval / BrainTrust 空白）：交界层原子评测 / 轨迹级归因 /
脚手架三方解耦 / 动态产题+版本钉 / Judge三通道隔离+成本 / 生产trace回灌。
"""
from .cli import main
from .core.engine import aggregate, run_suite, run_suite_async, BudgetGate
from .core.worker import run_distributed
from .core.models import Case, CaseResult, Trial, GraderResult
from .core.ratelimit import CircuitBreaker, CircuitOpenError, RetryPolicy, RetryableError, TokenBucket
from .core.sandbox import SandboxResult, run_in_sandbox
from .core.telemetry import configure, get_tracer, finish
from .core.queue import SqliteJobQueue, RedisJobQueue, JobQueue
from .core.persistence import Database
from .core.scaffold import run_three_way
from .core.generator import generate_curated, version_of
from .core.trace_store import ingest_and_evaluate, evaluate_trace_payload, parse_trace_to_case
from .core.trajectory_eval import (
    Trajectory, TrajectoryStep, TrajectoryEvaluator, TrajectoryEvalResult,
    evaluate_trajectory, make_demo_trajectory_solid, make_demo_trajectory_lucky,
)
from .core.version import HARNESS_VERSION
from .web.app import create_app

__all__ = [
    "main", "aggregate", "run_suite", "run_suite_async", "run_distributed", "BudgetGate",
    "Case", "CaseResult", "Trial", "GraderResult",
    "CircuitBreaker", "CircuitOpenError", "RetryPolicy", "RetryableError", "TokenBucket",
    "SandboxResult", "run_in_sandbox",
    "configure", "get_tracer", "finish",
    "SqliteJobQueue", "RedisJobQueue", "JobQueue", "Database",
    "run_three_way", "generate_curated", "version_of",
    "ingest_and_evaluate", "evaluate_trace_payload", "parse_trace_to_case", "HARNESS_VERSION",
    "Trajectory", "TrajectoryStep", "TrajectoryEvaluator", "TrajectoryEvalResult",
    "evaluate_trajectory", "make_demo_trajectory_solid", "make_demo_trajectory_lucky",
    "create_app",
]
