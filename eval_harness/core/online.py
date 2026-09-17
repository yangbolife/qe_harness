"""eval_harness · 在线评测器（O3 流式评分 + 告警驱动）

把「逐条到达的 (输入, 期望, SUT 回答)」实时评分，并维护滚动指标、触发阈值告警。
复用 graders 注册表（code/judge/sandbox/…），与离线 engine 同套评分语义。

典型用法：
    ev = OnlineEvaluator(default_grader="code", rules=[AlertRule("pass_rate","lt",0.6)])
    for item in stream:
        res = ev.feed(item["input"], item["gold"], item["response"])
        if res["alerts"]:
            notify(res["alerts"])
    print(ev.metrics.snapshot())
"""
from __future__ import annotations

from typing import Optional

from .graders import GRADERS
from .models import Case
from .alerting import AlertManager, AlertRule, RollingMetrics


def _build_grader(name: str, judge_provider=None):
    """与 engine._build_grader 同语义（judge/human 特殊构造，其余零参回退）。"""
    if name == "judge":
        return GRADERS.get("judge")(judge_provider=judge_provider)
    if name == "human":
        return GRADERS.get("human")()
    cls = GRADERS.get(name)
    try:
        return cls(judge_provider=judge_provider)
    except TypeError:
        return cls()


class OnlineEvaluator:
    """流式评测器：feed 一条 → 评分 + 累积指标 + 评估告警。"""

    def __init__(self, default_grader: str = "code", judge_provider=None,
                 combine: str = "any", rules: Optional[list] = None,
                 alert_manager: Optional[AlertManager] = None, window: int = 1000):
        self.default_grader = default_grader
        self.judge_provider = judge_provider
        self.combine = combine
        self.metrics = RollingMetrics(window=window)
        self.alerts = alert_manager or AlertManager(rules or [])
        self._count = 0

    def add_rule(self, rule: AlertRule) -> None:
        self.alerts.add_rule(rule)

    def _grade(self, response: str, case: Case, grader_name: str) -> list:
        graders = []
        for gname in (grader_name or self.default_grader).split(","):
            gname = gname.strip()
            if not gname:
                continue
            g = _build_grader(gname, judge_provider=self.judge_provider)
            graders.append(g.judge(response, case))
        return graders

    def _combine(self, graders) -> bool:
        if not graders:
            return False
        return all(g.passed for g in graders) if self.combine == "all" else any(g.passed for g in graders)

    def feed(self, input: str, gold: str, response: str, case_id: str = "",
              grader: Optional[str] = None, latency_ms: float = 0.0,
              cost_usd: float = 0.0, category: str = "general") -> dict:
        """喂入一条结果，返回评分 + 当前指标 + 触发的告警。"""
        self._count += 1
        cid = case_id or f"online-{self._count}"
        case = Case(id=cid, input=input, gold=gold, category=category)
        graders = self._grade(response, case, grader)
        passed = self._combine(graders)
        self.metrics.update(passed, latency_ms, cost_usd, inconclusive=False)
        fired = self.alerts.evaluate(self.metrics.snapshot())
        return {
            "case_id": cid,
            "passed": passed,
            "score": graders[0].score if graders else 0.0,
            "graders": [{"grader": g.grader, "score": g.score, "passed": g.passed,
                         "detail": g.detail} for g in graders],
            "metrics": self.metrics.snapshot(),
            "alerts": [{"rule_name": a.rule_name, "metric": a.metric, "value": a.value,
                        "threshold": a.threshold, "severity": a.severity,
                        "message": a.message} for a in fired],
        }

    def snapshot(self) -> dict:
        return self.metrics.snapshot()
