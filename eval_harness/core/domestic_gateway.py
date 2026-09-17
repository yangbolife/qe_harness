"""eval_harness · 国内模型 AI 网关（O1）

Braintrust 的「AI 网关 / 多 provider 统一接入」对应物。坚持「国产模型一等公民、可离线测试」：
- QwenProvider（通义千问）/ ZhipuProvider（智谱 GLM）：均为 OpenAI 兼容端点，零额外 SDK 依赖。
- DomesticGatewayProvider：配置驱动的多后端网关，支持
  * 加权轮询（按 weight 分摊流量，灰度/多模型 A/B）；
  * 故障转移（某后端抛错自动切下一个，全部失败才上抛 RetryableError）；
  * 后端可注入（测试用 fake，无需真实网络，离线确定性验证）。

所有后端均复用 providers 注册表（含已有的 deepseek），网关只是「编排层」，
不重复实现各家的请求逻辑。
"""
from __future__ import annotations

import asyncio
import os
from typing import Callable, List, Optional

from .ratelimit import RetryableError
from .registry import PROVIDERS


# ----------------------------------------------------------------------------
# 国内 OpenAI 兼容 Provider：仅端点/密钥来源不同，逻辑与 deepseek 一致
# ----------------------------------------------------------------------------
class _OpenAICompat:
    """OpenAI 兼容端点的公共基类（避免与 deepseek 重复实现请求逻辑）。"""

    def __init__(self, model: str, temperature: float = 0.0,
                 api_key: str = "", base_url: str = "", **kw):
        self.model = model
        self.temperature = temperature
        self.api_key = api_key
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
        except Exception as e:  # 统一映射为可重试错误
            raise RetryableError(f"{self.name} request failed: {type(e).__name__}: {e}") from e


@PROVIDERS.register("qwen")
class QwenProvider(_OpenAICompat):
    """通义千问（阿里云 DashScope，OpenAI 兼容模式）。

    key 读 DASHSCOPE_API_KEY；默认模型 qwen-plus。
    """
    name = "qwen"

    def __init__(self, model: str = "qwen-plus", temperature: float = 0.0,
                 api_key: str = "", base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 **kw):
        api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        super().__init__(model=model, temperature=temperature, api_key=api_key, base_url=base_url)


@PROVIDERS.register("zhipu")
class ZhipuProvider(_OpenAICompat):
    """智谱 GLM（BigModel，OpenAI 兼容端点）。

    key 读 ZHIPU_API_KEY；默认模型 glm-4。
    """
    name = "zhipu"

    def __init__(self, model: str = "glm-4", temperature: float = 0.0,
                 api_key: str = "", base_url: str = "https://open.bigmodel.cn/api/paas/v4",
                 **kw):
        api_key = api_key or os.environ.get("ZHIPU_API_KEY", "")
        super().__init__(model=model, temperature=temperature, api_key=api_key, base_url=base_url)


# ----------------------------------------------------------------------------
# 网关：加权轮询 + 故障转移（编排层）
# ----------------------------------------------------------------------------
@PROVIDERS.register("domestic_gateway")
class DomesticGatewayProvider:
    """国内模型统一网关：多后端、加权轮询、故障转移。

    backends 注入（测试用，离线）：直接传 provider 实例列表，如
        DomesticGatewayProvider(backends=[fake_a, fake_b])
    config 驱动（生产）：传后端配置列表，网关代为实例化，如
        DomesticGatewayProvider(config=[
            {"provider": "deepseek", "weight": 2},
            {"provider": "qwen", "model": "qwen-plus", "weight": 1},
        ])
    """
    name = "domestic_gateway"

    def __init__(self, backends: Optional[List] = None, config: Optional[List[dict]] = None,
                 failover: bool = True, **kw):
        self.failover = failover
        if backends is not None:
            self._backends = list(backends)
        else:
            self._backends = self._build_from_config(config or [])
        # 加权轮询：按 weight 展开候选序列
        self._ring: List[int] = []
        for i, b in enumerate(self._backends):
            w = getattr(b, "weight", 1) or 1
            self._ring.extend([i] * max(1, int(w)))
        self._pos = 0
        self._calls = 0
        self._errors = 0

    @staticmethod
    def _build_from_config(config: List[dict]) -> List:
        backends = []
        for item in config:
            name = item.get("provider")
            if name is None:
                raise ValueError("gateway 配置项需含 provider 字段")
            cls = PROVIDERS.get(name)
            kwargs = {k: v for k, v in item.items() if k not in ("provider", "weight")}
            kwargs.setdefault("weight", item.get("weight", 1))
            backends.append(cls(**kwargs))
        return backends

    def _next_index(self) -> int:
        if not self._ring:
            raise RetryableError("网关无可用后端")
        idx = self._ring[self._pos % len(self._ring)]
        self._pos += 1
        return idx

    async def ask_async(self, prompt: str, gold: str = "") -> str:
        await asyncio.sleep(0)  # 让出事件循环
        last_err = None
        tried = set()
        # 依次尝试：先按轮询挑一个，失败则故障转移到其余后端（按加权环长度覆盖全部）
        for _ in range(max(1, len(self._ring))):
            idx = self._next_index()
            if idx in tried:
                continue
            tried.add(idx)
            backend = self._backends[idx]
            try:
                self._calls += 1
                if hasattr(backend, "ask_async"):
                    return await backend.ask_async(prompt, gold)
                return backend.ask(prompt, gold)
            except Exception as e:  # 故障转移
                self._errors += 1
                last_err = e
                if not self.failover:
                    raise
                continue
        raise RetryableError(f"网关所有后端均失败：{last_err}") from last_err

    def ask(self, prompt: str, gold: str = "") -> str:
        """同步版：在单后端上直接跑（同步路径不做故障转移编排，复用首个可用）。"""
        tried = set()
        for _ in range(max(1, len(self._ring))):
            idx = self._next_index()
            if idx in tried:
                continue
            tried.add(idx)
            backend = self._backends[idx]
            try:
                self._calls += 1
                return backend.ask(prompt, gold)
            except Exception as e:
                self._errors += 1
                last_err = e
                if not self.failover:
                    raise
                continue
        raise RetryableError(f"网关所有后端均失败：{last_err}") from last_err

    def stats(self) -> dict:
        return {"backends": len(self._backends), "calls": self._calls, "errors": self._errors}
