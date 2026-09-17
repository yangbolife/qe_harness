"""eval_harness · 在线评分告警（O3）

Braintrust 的「online eval / 实时指标 + alerting」对应物。坚持「阈值可配、触发可审计」：
- RollingMetrics：流式累积指标（通过率 / 平均延迟 / P95 延迟 / 总成本 / inconclusive 占比）。
- AlertRule：指标 + 比较算子 + 阈值 + 严重度 + 通道（纯声明，可序列化为 JSON 配置）。
- AlertManager：规则评估（每次 feed 后调用），命中即产生 Alert 并记入历史；
  阈值击穿即触发，绝不静默。

指标口径：
- pass_rate        = 通过数 / 总数
- inconclusive_rate= inconclusive 数 / 总数
- latency_p95      = 最近窗口延迟的 95 分位（ms）
- cost_usd         = 累计 judge/远程成本（USD）
- total_calls      = 累计调用数
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

VALID_METRICS = ("pass_rate", "inconclusive_rate", "latency_p95", "cost_usd", "total_calls")
VALID_OPS = ("lt", "le", "gt", "ge", "eq")


@dataclass
class AlertRule:
    """一条告警规则（声明式，可 JSON 序列化）。"""
    metric: str                 # pass_rate / inconclusive_rate / latency_p95 / cost_usd / total_calls
    op: str                     # lt / le / gt / ge / eq
    threshold: float
    name: str = ""
    severity: str = "warn"     # info / warn / critical
    channels: list = field(default_factory=list)   # 通知通道（日志/邮件/Webhook…）

    def to_dict(self) -> dict:
        return {"metric": self.metric, "op": self.op, "threshold": self.threshold,
                "name": self.name or f"{self.metric}_{self.op}_{self.threshold}",
                "severity": self.severity, "channels": list(self.channels)}

    @classmethod
    def from_dict(cls, d: dict) -> "AlertRule":
        m = d.get("metric")
        if m not in VALID_METRICS:
            raise ValueError(f"未知告警指标：{m}（可用 {VALID_METRICS}）")
        op = d.get("op")
        if op not in VALID_OPS:
            raise ValueError(f"未知比较算子：{op}（可用 {VALID_OPS}）")
        return cls(metric=m, op=op, threshold=float(d.get("threshold", 0.0)),
                   name=d.get("name", ""), severity=d.get("severity", "warn"),
                   channels=list(d.get("channels", [])))


@dataclass
class Alert:
    rule_name: str
    metric: str
    value: float
    threshold: float
    op: str
    severity: str
    message: str
    ts: float = field(default_factory=time.time)


def _cmp(op: str, value: float, threshold: float) -> bool:
    if op == "lt":
        return value < threshold
    if op == "le":
        return value <= threshold
    if op == "gt":
        return value > threshold
    if op == "ge":
        return value >= threshold
    if op == "eq":
        return abs(value - threshold) < 1e-9
    return False


class AlertManager:
    """告警规则管理与评估（命中即记历史，可审计）。"""

    def __init__(self, rules: Optional[list] = None):
        self.rules: list[AlertRule] = list(rules or [])
        self.history: list[Alert] = []

    def add_rule(self, rule: AlertRule) -> None:
        self.rules.append(rule)

    def add_rule_from_dict(self, d: dict) -> AlertRule:
        r = AlertRule.from_dict(d)
        if not r.name:
            r.name = f"{r.metric}_{r.op}_{r.threshold}"
        self.rules.append(r)
        return r

    def remove_rule(self, name: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if (r.name or f"{r.metric}_{r.op}_{r.threshold}") != name]
        return len(self.rules) < before

    def evaluate(self, metrics: dict) -> list[Alert]:
        fired: list[Alert] = []
        for r in self.rules:
            if r.metric not in metrics:
                continue
            value = float(metrics[r.metric])
            if _cmp(r.op, value, r.threshold):
                a = Alert(
                    rule_name=r.name or f"{r.metric}_{r.op}_{r.threshold}",
                    metric=r.metric, value=value, threshold=r.threshold,
                    op=r.op, severity=r.severity,
                    message=f"[{r.severity}] {r.metric}={value:.4g} {r.op} {r.threshold}",
                )
                fired.append(a)
                self.history.append(a)
        return fired

    def rules_dict(self) -> list:
        return [r.to_dict() for r in self.rules]

    def clear_history(self) -> None:
        self.history = []


class RollingMetrics:
    """流式指标累积器（在线评测每来一条就 update）。"""

    def __init__(self, window: int = 1000):
        self.n = 0
        self.passed = 0
        self.inconclusive = 0
        self.cost = 0.0
        self._latencies: list[float] = []
        self._window = window

    def update(self, passed: bool, latency_ms: float = 0.0, cost_usd: float = 0.0,
               inconclusive: bool = False) -> None:
        self.n += 1
        if passed:
            self.passed += 1
        if inconclusive:
            self.inconclusive += 1
        self.cost += cost_usd
        self._latencies.append(latency_ms)
        if len(self._latencies) > self._window:
            self._latencies.pop(0)

    def _p95(self) -> float:
        if not self._latencies:
            return 0.0
        s = sorted(self._latencies)
        k = max(0, int(round(0.95 * (len(s) - 1))))
        return s[k]

    def snapshot(self) -> dict:
        n = self.n or 1
        return {
            "total_calls": self.n,
            "pass_rate": self.passed / n,
            "inconclusive_rate": self.inconclusive / n,
            "cost_usd": round(self.cost, 6),
            "avg_latency_ms": round(sum(self._latencies) / max(1, len(self._latencies)), 2),
            "latency_p95": round(self._p95(), 2),
        }
