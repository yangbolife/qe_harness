"""eval_harness · 工具注册与托管执行（O10）

Braintrust 的 Tools（一等对象 + 托管执行）对应物。坚持「工具即资产、执行可隔离」：
- ToolRegistry：把工具（名称 + 描述 + 可执行体）注册成一等对象，供评测引用。
- execute_tool：托管执行——callable 直接运行；command（外部脚本）走受限沙箱子进程，
  与评分器沙箱（sandbox.py）同源，避免拖垮评测进程 / 越权。

落库：Tool 元信息（不含源码）持久化，调用记录可审计（O12 Patterns 可借此归因工具失败）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .sandbox import run_in_sandbox


@dataclass
class ToolResult:
    response: str
    ok: bool
    error: str = ""
    duration_ms: float = 0.0


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}   # name → {description, fn, command}

    def register(self, name: str, description: str, fn: Optional[Callable] = None,
                 command: Optional[str] = None):
        if fn is None and command is None:
            raise ValueError("工具需提供 fn 或 command 之一")
        self._tools[name] = {"description": description, "fn": fn, "command": command}

    def available(self) -> list[dict]:
        return [{"name": n, "description": t["description"], "kind":
                 "callable" if t["fn"] else "command"} for n, t in self._tools.items()]

    def execute(self, name: str, args: str, sandbox: bool = True) -> ToolResult:
        t0 = time.time()
        if name not in self._tools:
            return ToolResult("", False, f"未注册工具：{name}", (time.time() - t0) * 1000)
        tool = self._tools[name]
        try:
            if tool["fn"] is not None:
                out = tool["fn"](args)
                return ToolResult(str(out), True, "", (time.time() - t0) * 1000)
            # command 走沙箱
            script = f"{tool['command']}\n" if not sandbox else None
            res = run_in_sandbox(f"import sys\nsys.argv=['x']\nprint(({tool['command']!r})({args!r}))"
                                 if not tool["command"].strip().startswith("python")
                                 else tool["command"],
                                 timeout=30, mem_mb=256) if sandbox else \
                _run_local(tool["command"], args)
            if res.timed_out:
                return ToolResult("", False, "工具执行超时", (time.time() - t0) * 1000)
            out = (res.stdout or "").strip()
            return ToolResult(out, res.returncode == 0, res.stderr or "",
                              (time.time() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            return ToolResult("", False, f"{type(e).__name__}:{e}", (time.time() - t0) * 1000)


def _run_local(command: str, args: str) -> "object":
    """非沙箱本地执行（仅受信任工具使用）。"""
    import subprocess
    proc = subprocess.run(["python", "-c", command], input=args,
                          capture_output=True, text=True, timeout=30)
    class _R:
        timed_out = False
        returncode = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    return _R()


# 全局默认注册表（可被测试重置）
_default = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _default
