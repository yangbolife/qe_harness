"""eval_harness · 生产级并发控制（Phase 2 专设）

解决常见开源框架的典型生产痛点：
1. 单进程串行 → 吞吐低、烧钱。            → asyncio 并发 + Semaphore
2. 无速率控制 → 触发 provider 429 限流。   → TokenBucket（令牌桶）
3. 429/5xx 直接失败 → 偶发抖动全红。      → RetryPolicy（指数退避 + 抖动）
4. provider 已挂还猛打 → 雪崩 + 误判。    → CircuitBreaker（熔断 → 标记 inconclusive）

设计原则：组件可独立使用、可注入 engine；纯 stdlib，无外部依赖。
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable


# ---------- 异常分类（区分可重试 vs 致命 vs 熔断）----------
class SUTError(Exception):
    """被测系统/评测相关错误的基类。"""


class RetryableError(SUTError):
    """可重试错误：429 限流 / 5xx / 超时 / 网络抖动。"""


class CircuitOpenError(SUTError):
    """熔断已开启：直接放弃本次调用，交由上层标记 inconclusive。"""


# =================== 令牌桶 ===================
class TokenBucket:
    """异步令牌桶：限制平均速率 <= rate (令牌/秒)，允许突发至 capacity。"""

    def __init__(self, rate: float, capacity: int | None = None):
        self.rate = float(rate)
        self.capacity = int(capacity if capacity is not None else max(1, int(rate)))
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1):
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate if self.rate > 0 else 0.0
            await asyncio.sleep(wait)


# =================== 指数退避 ===================
class RetryPolicy:
    """指数退避：base * 2**attempt，可选抖动，封顶 max_wait。"""

    def __init__(self, max_retries: int = 5, base: float = 0.5,
                 max_wait: float = 30.0, jitter: bool = True):
        self.max_retries = max_retries
        self.base = base
        self.max_wait = max_wait
        self.jitter = jitter

    def wait_for(self, attempt: int) -> float:
        w = self.base * (2 ** attempt)
        if self.jitter:
            w *= (0.5 + random.random())
        return min(w, self.max_wait)


# =================== 熔断器 ===================
class CircuitBreaker:
    """连续失败达阈值即熔断；冷却期内所有调用直接抛 CircuitOpenError。

    语义对齐 qe-platform：环境/上游持续失败时，不反复重试、不误判模型，
    而是标记 inconclusive（交由验收门禁仲裁）。
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    async def call(self, fn: Callable[[], Awaitable]):
        async with self._lock:
            if self._opened_at is not None:
                if time.monotonic() - self._opened_at < self.reset_timeout:
                    raise CircuitOpenError("circuit open")
                # 冷却结束，半开恢复
                self._opened_at = None
                self._failures = 0
        try:
            return await fn()
        except RetryableError:
            async with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._opened_at = time.monotonic()
            raise
