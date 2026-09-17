"""eval_harness · 沙箱隔离（Phase 2 专设）

用于执行「不可信评估代码 / 被测智能体的工具调用」时做资源隔离，避免：
- 无限循环 / 死循环拖垮评测进程     → RLIMIT_CPU + subprocess timeout
- 内存爆炸 OOM 拖垮宿主             → RLIMIT_AS（macOS/Linux best-effort）
- 异常栈污染评测结果               → 捕获 returncode / stderr

典型用法：交评器 `sandbox` 在受限子进程里跑 case.meta['check_code']
（RESP 变量 = 被测回答），返回是否通过。也可用于 eval 智能体的代码动作。
纯 stdlib（subprocess + resource），无外部依赖。
"""
from __future__ import annotations

import os
import subprocess
import sys
import resource
from dataclasses import dataclass
from typing import Optional


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


def run_in_sandbox(code: str, timeout: int = 5, mem_mb: int = 256,
                   env: Optional[dict] = None) -> SandboxResult:
    """在受限子进程中执行 code，返回 stdout/stderr/returncode。

    code 可直接使用变量 RESP（由调用方注入）。
    env：额外注入的环境变量（合并到当前环境，不替换）。
    """
    def _limit():
        # CPU 时间上限
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
        except (ValueError, OSError):
            pass
        # 地址空间上限（防 OOM）；部分平台 RLIMIT_AS 不生效，best-effort
        try:
            bytes_ = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (bytes_, bytes_))
        except (ValueError, OSError):
            pass

    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout,
            preexec_fn=_limit if sys.platform != "win32" else None,
            env=full_env,
        )
        return SandboxResult(proc.stdout, proc.stderr, proc.returncode, timed_out=False)
    except subprocess.TimeoutExpired as e:
        return SandboxResult(e.stdout or "", e.stderr or "", -1, timed_out=True)
    except Exception as e:  # pragma: no cover - 兜底
        return SandboxResult("", str(e), -1, timed_out=False)
