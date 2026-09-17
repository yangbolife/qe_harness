"""eval_harness · 可观测性（Phase 3 · OTel tracing）

设计目标：
- 零硬依赖：未安装 opentelemetry 时，回退内置轻量 tracer，仍可产出 trace 摘要。
- 生产就绪：安装 opentelemetry-sdk 后，configure(console=True | otlp_endpoint=URL)
  即把 span 导出到控制台 / OTLP collector（Jaeger / Tempo / 自建网关）。
- 接口统一：tracer.start_span(name, **attrs) 返回上下文管理器，支持
  .set_attribute / .add_event / .record_exception，引擎代码对两种后端无感。

解决常见开源框架痛点：评测过程不可观测（黑盒长跑、难定位慢/错环节）。
"""
from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field
from typing import Optional

try:  # pragma: no cover - 依赖可选
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource

    try:  # grpc OTLP 导出器（可选，缺时仅 console）
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        _OTLP = True
    except Exception:  # pragma: no cover
        _OTLP = False
    _OTEL = True
except Exception:  # pragma: no cover
    _OTEL = False
    _OTLP = False

_CURRENT = contextvars.ContextVar("harness_span", default=None)


@dataclass
class Span:
    """纯 Python span（始终可用，亦作为 OTel 的本地镜像用于摘要）。"""
    name: str
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    start: float = field(default_factory=time.time)
    end: float = 0.0
    status_ok: bool = True
    exception: Optional[str] = None
    parent: Optional["Span"] = None
    _otel: Optional[object] = field(default=None, repr=False)

    def set_attribute(self, k, v):
        self.attributes[k] = v
        if self._otel is not None:
            try:
                self._otel.set_attribute(k, v)
            except Exception:  # pragma: no cover
                pass

    def add_event(self, name: str, attrs: Optional[dict] = None):
        self.events.append((name, attrs or {}))
        if self._otel is not None:
            try:
                self._otel.add_event(name, attrs or {})
            except Exception:  # pragma: no cover
                pass

    def record_exception(self, e: Exception):
        self.exception = f"{type(e).__name__}: {e}"
        self.status_ok = False
        if self._otel is not None:
            try:
                self._otel.record_exception(e)
            except Exception:  # pragma: no cover
                pass

    def finish(self):
        if self.end == 0.0:
            self.end = time.time()

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start) * 1000


class _LocalGuard:
    def __init__(self, tracer: "LocalTracer", span: Span):
        self._t = tracer
        self._s = span
        self._tok = None

    def __enter__(self) -> Span:
        self._tok = _CURRENT.set(self._s)
        return self._s

    def __exit__(self, et, ev, tb):
        if et is not None:
            self._s.record_exception(ev)
        self._s.finish()
        _CURRENT.reset(self._tok)
        self._t._record(self._s)
        return False


class LocalTracer:
    """内置轻量 tracer：内存记录 span，可打印摘要；无外部依赖。"""

    def __init__(self, service: str = "eval_harness"):
        self.service = service
        self._roots: list[Span] = []
        self._all: list[Span] = []

    def start_span(self, name: str, **attrs) -> _LocalGuard:
        parent = _CURRENT.get()
        s = Span(name=name, attributes=attrs, parent=parent)
        return _LocalGuard(self, s)

    def _record(self, s: Span):
        self._all.append(s)
        if s.parent is None:
            self._roots.append(s)

    def summary(self) -> dict:
        spans = self._all
        total = len(spans)
        dur = sum(s.duration_ms for s in spans)
        by_name: dict = {}
        for s in spans:
            d = by_name.setdefault(s.name, {"count": 0, "dur_ms": 0.0, "err": 0})
            d["count"] += 1
            d["dur_ms"] += s.duration_ms
            d["err"] += 0 if s.status_ok else 1
        slowest = sorted(spans, key=lambda s: -s.duration_ms)[:5]
        return {
            "service": self.service,
            "mode": "local",
            "spans": total,
            "total_dur_ms": round(dur, 1),
            "by_name": {
                k: {"count": v["count"], "avg_ms": round(v["dur_ms"] / v["count"], 1),
                    "err": v["err"]}
                for k, v in by_name.items()
            },
            "slowest": [
                {"name": s.name, "dur_ms": round(s.duration_ms, 1), "attrs": s.attributes}
                for s in slowest
            ],
        }


class OtelTracer:
    """OTel 后端 tracer：本地镜像 + 真实导出到 console / OTLP collector。"""

    def __init__(self, service: str = "eval_harness"):
        self._local = LocalTracer(service)
        self._provider = TracerProvider(resource=Resource.create({"service.name": service}))
        self._tracer = self._provider.get_tracer(service)
        self._service = service

    def add_console(self):
        self._provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    def add_otlp(self, endpoint: str):
        if not _OTLP:
            raise RuntimeError("opentelemetry OTLP grpc 导出器不可用，请用 console 模式")
        self._provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

    def start_span(self, name: str, **attrs) -> "_OtelGuard":
        parent = _CURRENT.get()
        local_span = Span(name=name, attributes=attrs, parent=parent)
        otel_span = self._tracer.start_span(name, attributes=attrs or None)
        local_span._otel = otel_span
        otel_ctx = _otel_trace.set_span_in_context(otel_span)
        return _OtelGuard(self, local_span, otel_span, otel_ctx)

    def summary(self) -> dict:
        d = self._local.summary()
        d["mode"] = "otel"
        return d

    def finish(self):
        try:
            self._provider.shutdown()
        except Exception:  # pragma: no cover
            pass


class _OtelGuard:
    def __init__(self, tracer: OtelTracer, local_span: Span, otel_span, otel_ctx):
        self._t = tracer
        self._ls = local_span
        self._os = otel_span
        self._ctx = otel_ctx
        self._tok = None

    def __enter__(self) -> Span:
        self._tok = _CURRENT.set(self._ls)
        return self._ls

    def __exit__(self, et, ev, tb):
        if et is not None:
            self._ls.record_exception(ev)
        self._ls.finish()
        self._os.end()
        self._t._local._record(self._ls)
        _CURRENT.reset(self._tok)
        try:
            _otel_trace.detach(self._ctx)
        except Exception:  # pragma: no cover
            pass
        return False


# ---------------- 全局配置入口 ----------------
_TRACER: Optional[object] = None
_CONFIGURED = False


def configure(service: str = "eval_harness", console: bool = False,
              otlp_endpoint: Optional[str] = None) -> object:
    """配置全局 tracer。console/otlp 任一为真且装了 otel → OtelTracer；否则 LocalTracer。"""
    global _TRACER, _CONFIGURED
    if _OTEL and (console or otlp_endpoint):
        t = OtelTracer(service)
        if console:
            t.add_console()
        if otlp_endpoint:
            t.add_otlp(otlp_endpoint)
    else:
        if (console or otlp_endpoint) and not _OTEL:
            print("[telemetry] opentelemetry 未安装，回退内置轻量 tracer（仍产出 trace 摘要）")
        t = LocalTracer(service)
    _TRACER = t
    _CONFIGURED = True
    return t


def get_tracer() -> object:
    global _TRACER
    if _TRACER is None:
        _TRACER = LocalTracer()
    return _TRACER


def finish():
    global _TRACER
    if _TRACER is not None and hasattr(_TRACER, "finish"):
        try:
            _TRACER.finish()
        except Exception:  # pragma: no cover
            pass
    _TRACER = None


def available() -> bool:
    return _OTEL
