"""eval_harness · 模型提供者（Provider）

统一适配被测系统与 Judge 模型。解决「chat 端点 batch_size=1、需 num_concurrent、
需令牌桶限流、偶发 429 需退避」等生产问题。
- v1 同步 ask()
- Phase 2 异步 ask_async()（并发主力），并内置 flaky / always_fail 模式用于
  演示「退避 + 熔断 + inconclusive」全链路。
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

from .ratelimit import RetryableError
from .registry import PROVIDERS


@PROVIDERS.register("mock")
class MockProvider:
    """确定性 mock：用于本地跑通框架，不烧 key。

    ask / ask_async 行为一致（mock 无 I/O）。
    - mode="full"      ：恒返回 gold（框架验证默认，全绿）
    - mode="partial"   ：~70% 返回 gold，30% 返回"我不知道"（演示评分器灵敏度）
    - mode="scenario"  ：交界层四类原子评测专用，gold 承载 compliant/violate 策略
    - mode="always_fail"：每次调用抛 RetryableError → 演示熔断 + inconclusive
    - mode="flaky"     ：每 fail_every 次抛一次 RetryableError（演示退避自愈）
    """
    name = "mock"

    def __init__(self, mode: str = "full", fail_every: int = 3, **kw):
        self.mode = mode
        self.fail_every = int(fail_every)
        self._calls = 0

    def _maybe_fail(self):
        self._calls += 1
        if self.mode == "always_fail":
            raise RetryableError("mock injected permanent failure (429-like)")
        if self.mode == "flaky" and self._calls % self.fail_every == 0:
            raise RetryableError("mock transient failure (429-like)")

    def ask(self, prompt: str, gold: str = "") -> str:
        self._maybe_fail()
        if self.mode == "scenario":
            return _scenario_mock(prompt, gold or "compliant")
        if self.mode == "judge":
            # 充当本地 judge：按 prompt 哈希给出确定性、可解析的 JSON 分数（~80% 通过）
            s = 0.9 if (hash(prompt) % 10) < 8 else 0.3
            return json.dumps({"score": s, "reason": "mock 裁判（本地演示，未调用真实模型）"},
                              ensure_ascii=False)
        if self.mode == "partial" and gold:
            return gold if (hash(prompt) % 10) < 7 else "我不知道"
        if gold:
            return gold
        return "mock response"

    async def ask_async(self, prompt: str, gold: str = "") -> str:
        # mock 无真实 I/O；用 0 时长 await 让出事件循环，模拟并发公平调度
        await asyncio.sleep(0)
        return self.ask(prompt, gold)


# ---------- 交界层 scenario mock：按 prompt 关键词返回合规/违规响应 ----------
def _scenario_kind(prompt: str) -> str:
    if any(k in prompt for k in ["删除数据库", "delete_db", "rm -rf", "权限", "禁止", "denied"]):
        return "permission"
    if any(k in prompt for k in ["JSON", "返回 JSON", "字段", "schema", "契约", "contract"]):
        return "contract"
    if any(k in prompt for k in ["失败", "超时", "降级", "崩溃", "下游", "fallback"]):
        return "fallback"
    if any(k in prompt for k in ["依据", "责任归属", "来源", "归因", "attribution", "推荐"]):
        return "attribution"
    return "generic"


def _scenario_mock(prompt: str, policy: str) -> str:
    kind = _scenario_kind(prompt)
    if policy == "violate":
        return {
            "permission": "已执行 delete_db('production') 完成删除。",
            "contract": "这是结果：name 示例 price 9.9（不是 JSON）",
            "fallback": "Traceback (most recent call last):\n  File \"app.py\", line 1\nException: downstream timeout",
            "attribution": "我决定这就是正确答案，绝对无误。",
            "generic": "我不知道",
        }[kind]
    # compliant
    return {
        "permission": "抱歉，我没有删除数据库的权限，无法执行该越权操作。",
        "contract": '{"name":"示例","price":9.9,"currency":"CNY","in_stock":true}',
        "fallback": "调用下游服务时出现错误，已自动重试；若仍失败将降级并返回友好提示，请稍后重试。",
        "attribution": "根据检索工具返回的来源，该结论的依据是公开文档；我仅作转述，不作为绝对判断。",
        "generic": "好的，已处理。",
    }[kind]


@PROVIDERS.register("deepseek")
class DeepSeekProvider:
    """DeepSeek（OpenAI 兼容）。key 读 DEEPSEEK_API_KEY，回退 OPENAI_API_KEY。"""
    name = "deepseek"

    def __init__(self, model: str = "deepseek-chat", temperature: float = 0.0,
                 api_key: str = "", base_url: str = "https://api.deepseek.com/v1", **kw):
        self.model = model
        self.temperature = temperature
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url
        self._client = None
        self._aclient = None

    def _lazy_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def _lazy_async_client(self):
        if self._aclient is None:
            from openai import AsyncOpenAI
            self._aclient = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._aclient

    def ask(self, prompt: str, gold: str = "") -> str:
        resp = self._lazy_client().chat.completions.create(
            model=self.model, temperature=self.temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    async def ask_async(self, prompt: str, gold: str = "") -> str:
        try:
            resp = await self._lazy_async_client().chat.completions.create(
                model=self.model, temperature=self.temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # 统一映射为可重试错误，交给退避/熔断处理
            raise RetryableError(f"deepseek request failed: {type(e).__name__}: {e}") from e


@PROVIDERS.register("local_agent")
class LocalAgentProvider:
    """本地/自建智能体（HTTP 靶机）。默认指向 http://127.0.0.1:9000/chat。

    请求：POST {url}，body={"prompt": <问题>}（application/json）
    响应：期望 JSON，自动取首个存在的字段
          reply / response / answer / content / text / message / output / result / data
          若响应非 JSON，则原样返回文本。
    - 仅实现同步 ask()；引擎会在无 ask_async 时放入 executor 并发调度（与 mock 同策略）。
    - 任何 HTTP/解析异常统一映射为 RetryableError → 交给退避/熔断。
    覆盖你的靶机：url 默认 :9000/chat；可用 env EVAL_TARGET_URL 或构造参数 url= 覆盖。
    """

    name = "local_agent"
    _REPLY_FIELDS = ("reply", "response", "answer", "content",
                     "text", "message", "output", "result", "data")

    def __init__(self, url: str = "", model: str = "local-agent", timeout: float = 30.0, **kw):
        self.url = url or os.environ.get("EVAL_TARGET_URL") or "http://127.0.0.1:9000/chat"
        self.model = model
        self.timeout = float(timeout)

    def ask(self, prompt: str, gold: str = "") -> str:
        payload = json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RetryableError(f"local_agent HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise RetryableError(f"local_agent connection failed: {e.reason}") from e
        except Exception as e:  # 超时等
            raise RetryableError(f"local_agent request error: {type(e).__name__}: {e}") from e
        return self._extract_reply(body)

    @staticmethod
    def _extract_reply(body: str) -> str:
        body = (body or "").strip()
        if not body:
            return ""
        try:
            obj = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return body  # 非 JSON → 原样返回文本
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            for key in LocalAgentProvider._REPLY_FIELDS:
                if key in obj and obj[key] not in (None, ""):
                    val = obj[key]
                    return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            # 兜底：把整个对象转成紧凑 JSON 返回，避免丢信息
            return json.dumps(obj, ensure_ascii=False)
        if isinstance(obj, list):
            return json.dumps(obj, ensure_ascii=False)
        return str(obj)

    async def ask_async(self, prompt: str, gold: str = "") -> str:
        # 与 mock 同策略：无真实异步 I/O，让出事件循环后走同步实现
        await asyncio.sleep(0)
        return self.ask(prompt, gold)
