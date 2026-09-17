"""eval_harness · 远程 / 沙箱评测（O11）

Braintrust 的 Remote / Sandboxed evals 对应物。解决「SUT 不在本进程、或需在隔离环境跑」：
- SandboxScriptProvider（注册名 sandbox_script）：把外部脚本当作 SUT，
  每道题把 (prompt, gold) 以环境变量注入子进程，捕获 stdout 作为回答。隔离执行，崩了不影响主进程。
- RemoteProvider（注册名 remote）：把 (prompt, gold) POST 到远端评测服务，
  取回回答。适合 SUT 跑在容器/独立机器（零信任边界）。

二者都实现 ask(prompt, gold)，与 MockProvider/DeepSeekProvider 同一接口，引擎零改动即可切换。
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from .providers import PROVIDERS
from .sandbox import run_in_sandbox


@PROVIDERS.register("sandbox_script")
class SandboxScriptProvider:
    """外部脚本作为被测系统（SUT）。

    脚本约定：从环境变量 EVAL_PROMPT / EVAL_GOLD 读取输入，把回答写到 stdout。
    脚本退出码非 0 或超时 → 返回错误标记，由引擎判定为该题环境失败（inconclusive）。
    """

    name = "sandbox_script"

    def __init__(self, script: str = "", timeout: int = 30, mem_mb: int = 256, **_kw):
        self.script = script
        self.timeout = timeout
        self.mem_mb = mem_mb

    def ask(self, prompt: str, gold: str = "") -> str:
        env = dict(os.environ)
        env["EVAL_PROMPT"] = prompt
        env["EVAL_GOLD"] = gold
        res = run_in_sandbox(
            self.script, timeout=self.timeout, mem_mb=self.mem_mb,
            env=env,
        )
        if res.timed_out:
            return f"[sandbox_timeout] 脚本 {self.timeout}s 内未返回"
        if res.returncode != 0:
            return f"[sandbox_error rc={res.returncode}] {res.stderr or ''}".strip()
        return (res.stdout or "").strip()


@PROVIDERS.register("remote")
class RemoteProvider:
    """远端评测服务作为 SUT（HTTP）。回答取自 JSON 响应的 answer 字段。"""

    name = "remote"

    def __init__(self, url: str = "", api_key: str = "", timeout: int = 30, **_kw):
        if not url:
            raise ValueError("remote provider 需提供 url")
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    def ask(self, prompt: str, gold: str = "") -> str:
        try:
            import urllib.request
        except Exception:  # noqa: BLE001
            return "[remote_unavailable] 运行环境无 urllib"
        payload = json.dumps({"prompt": prompt, "gold": gold}).encode()
        req = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            return str(data.get("answer", data.get("response", "")))
        except Exception as e:  # noqa: BLE001
            return f"[remote_error] {type(e).__name__}:{e}"
