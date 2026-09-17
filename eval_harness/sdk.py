"""eval_harness · 多语言/OTel 采集 SDK（N5）

Braintrust 的 SDK（一行接入、自动采集 spans/inputs/outputs/scores）对应物。
坚持「轻量、可脱离主进程」：

- HarnessTracer：上下文管理器，记录评测过程的 span（输入/输出/耗时/错误），
  产出与 trace_store 兼容的 OTel 形状 payload，可直接回灌（创新⑥）。
- record_eval(...)：一行记录「一次评测调用」为 trace。
- push_trace(db_url, payload)：把本地采集的 trace 推送到评测库（回灌复评）。

v1 为 Python SDK；多语言可在各自运行时产出相同 OTel JSON 后调用 push_trace。
"""
from __future__ import annotations

import time
import uuid
from typing import Optional


class HarnessTracer:
    """采集一次评测的 span 序列，产出 OTel 形状 trace 供回灌。"""

    def __init__(self, source: str = "sdk"):
        self.source = source
        self._spans: list[dict] = []
        self._t0 = time.time()

    def span(self, name: str, input: str = "", output: str = "",
             error: str = "", duration_ms: float = 0.0):
        """记录一个 span（步骤）。"""
        self._spans.append({
            "name": name,
            "startTime": round(time.time() - self._t0, 4),
            "attributes": [
                {"key": "input", "value": {"stringValue": input}},
                {"key": "output", "value": {"stringValue": output}},
                {"key": "error", "value": {"stringValue": error}},
                {"key": "duration_ms", "value": {"doubleValue": duration_ms}},
            ],
        })
        return self

    def to_payload(self) -> dict:
        return {"resourceSpans": [{"scopeSpans": [{"spans": self._spans}]}]}

    def record_eval(self, case_id: str, prompt: str, response: str,
                    score: float = 0.0, passed: bool = False, error: str = ""):
        """便捷：把一次评测调用记录为「调用」+「评分」两个 span。"""
        self.span(f"eval:{case_id}", input=prompt, output=response, error=error)
        self.span(f"score:{case_id}", input=prompt,
                  output=f"score={score} passed={passed}")
        return self


def record_eval(case_id: str, prompt: str, response: str,
                score: float = 0.0, passed: bool = False, error: str = "",
                source: str = "sdk") -> dict:
    """一行记录一次评测为 OTel trace payload（不落库，返回 payload）。"""
    t = HarnessTracer(source=source)
    t.record_eval(case_id, prompt, response, score, passed, error)
    return t.to_payload()


def push_trace(db_url: str, payload: dict, source: str = "sdk") -> int:
    """把采集到的 trace payload 推送到评测库（回灌复评，创新⑥）。"""
    from .core.persistence import Database
    from .core.trace_store import ingest_and_evaluate

    async def _go():
        db = Database(db_url)
        await db.init()
        try:
            res = await ingest_and_evaluate(db, source=source, payload=payload,
                                            grader_name="trajectory")
            return res.get("trace_id")
        finally:
            await db.close()
    import asyncio
    return asyncio.run(_go())
