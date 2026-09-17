"""eval_harness · 轨迹级评测框架（扩展：把「做对了」与「蒙对了」区分开）

现状：已有 ``TrajectoryAttributionGrader``（grader="trajectory"，标为创新②）只做
「步骤关键词覆盖率 + 崩溃信号」两件事，对真实 Agent 轨迹（工具调用、循环/冗余、
效率、捷径侥幸）几乎无覆盖——这正是「只看终分、失败无归因 / 蒙对与做对分不清」的根源。

本模块提供一个真正的轨迹级评测框架：

- ``TrajectoryStep`` / ``Trajectory``：结构化轨迹（thought / action / observation / answer），
  支持从三种来源构建——结构化 list（推荐）、自由文本（CoT 日志）、OTel spans。
- ``TrajectoryEvaluator``：多维度度量并给出归因：
    * coverage          期望步骤覆盖率
    * required_coverage 关键必做步骤覆盖率
    * shortcut_risk    捷径/侥幸风险（到达答案但跳过必做验证步骤 → 「蒙对」嫌疑）
    * redundancy_ratio 冗余/循环比（重复工具调用、原地打转）
    * efficiency       效率分（步数相对期望的浪费程度）
    * robustness       鲁棒性（崩溃/兜底信号；1=无，0=有）
    * action_issues    动作问题（越权工具调用、缺失期望工具）
    * first_failure_step 最早出错/缺步定位（归因用）
    * trajectory_score 加权聚合分（0~1）
    * passed / detail  判定与可读明细
- ``TrajectoryEvalGrader``：注册为 grader="trajectory_eval"，零侵入接入 engine：
  轨迹取自 ``case.meta['trajectory']``（结构化）→ ``meta['trajectory_text']`` → ``pred``；
  期望/必做/禁用作 ``meta['expected_steps']`` / ``meta['required_steps']`` / ``meta['forbidden_tools']``。
- ``evaluate_trajectory(...)``：独立便捷入口（单智能体轨迹评测，无需跑完整 suite）。

设计原则：代码确定性优先（快·客观·可复现），不依赖 LLM-Judge；可与 judge/code 评分器
在 ``case.grader`` 中逗号组合使用（如 ``trajectory_eval,judge``）。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .models import Case, GraderResult
from .registry import GRADERS


# ===================== 结构化轨迹模型 =====================
@dataclass
class TrajectoryStep:
    """轨迹中的一步。kind 决定语义权重（answer 用于捷径判定，action 用于工具审计）。"""
    idx: int
    kind: str                       # thought | action | observation | answer | step
    content: str
    tool: str = ""
    args: str = ""


@dataclass
class Trajectory:
    steps: list[TrajectoryStep] = field(default_factory=list)
    raw: str = ""

    @property
    def n_steps(self) -> int:
        return len(self.steps)

    # ---- 来源一：结构化 list（推荐）----
    @classmethod
    def from_structured(cls, data: list) -> "Trajectory":
        steps: list[TrajectoryStep] = []
        for i, s in enumerate(data or []):
            if not isinstance(s, dict):
                s = {"content": str(s)}
            steps.append(TrajectoryStep(
                idx=i,
                kind=str(s.get("kind", "step")).lower(),
                content=str(s.get("content", s.get("text", "")) or ""),
                tool=str(s.get("tool", "") or ""),
                args=str(s.get("args", "") or ""),
            ))
        return cls(steps=steps)

    # ---- 来源二：自由文本（CoT 日志）----
    _KIND_MAP = [
        (re.compile(r"思考|thought|推理|分析"), "thought"),
        (re.compile(r"调用|执行|工具|tool|action|function"), "action"),
        (re.compile(r"观察|observation|结果|返回|retr|检索"), "observation"),
        (re.compile(r"回答|答案|结论|answer|final|输出"), "answer"),
    ]
    _STEP_NUM = re.compile(r"(?:^|\n)\s*(?:步骤|step|第)?\s*(\d+)[.、:：)\]]\s*(.*)")
    _TOOL = re.compile(r"(?:调用|执行|工具|tool|action)\s*[:：]?\s*([A-Za-z_][\w\-]*)")
    _ARGS = re.compile(r"[\(（]\s*(.*?)\s*[\)）]")

    @classmethod
    def from_text(cls, text: str) -> "Trajectory":
        if not text:
            return cls(steps=[])
        # 优先按编号步骤切分（步骤1: / Step 2 / 1. / 1)）
        numbered = cls._STEP_NUM.findall("\n" + text)
        if numbered:
            chunks = [c.strip() for _, c in numbered if c.strip()]
        else:
            chunks = [ln.strip() for ln in text.splitlines() if ln.strip()]
        steps: list[TrajectoryStep] = []
        for i, chunk in enumerate(chunks):
            kind = "step"
            for pat, k in cls._KIND_MAP:
                if pat.search(chunk):
                    kind = k
                    break
            tool = ""
            args = ""
            if kind == "action":
                m = cls._TOOL.search(chunk)
                if m:
                    tool = m.group(1)
                    am = cls._ARGS.search(chunk)
                    if am:
                        args = am.group(1)
            steps.append(TrajectoryStep(idx=i, kind=kind, content=chunk, tool=tool, args=args))
        # 无显式 answer 标记时，若存在 action/observation，则末步视作 answer（用于捷径判定）
        return cls(steps=steps)

    # ---- 来源三：OTel spans（复用 trace_store 的解析口径）----
    @classmethod
    def from_otel(cls, payload: dict) -> "Trajectory":
        spans: list[dict] = []
        for rs in payload.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                spans.extend(ss.get("spans", []))
        if not spans and isinstance(payload.get("spans"), list):
            spans = payload["spans"]
        spans = sorted(spans, key=lambda s: s.get("startTime", s.get("startTimeUnixNano", 0)))
        steps: list[TrajectoryStep] = []
        for i, s in enumerate(spans):
            name = s.get("name", "step")
            attrs = s.get("attributes", {})
            if isinstance(attrs, list):
                attrs = {a.get("key"): _unwrap(a.get("value")) for a in attrs}
            inp = attrs.get("input") or attrs.get("llm.input_messages") or attrs.get("prompt") or ""
            out = attrs.get("output") or attrs.get("llm.output_text") or attrs.get("completion") or ""
            kind = "action" if (out and inp) else ("observation" if out else "step")
            steps.append(TrajectoryStep(
                idx=i, kind=kind,
                content=(f"{name}: {out}" if out else name),
                tool=name if kind == "action" else "",
                args=str(inp or ""),
            ))
        return cls(steps=steps)


def _unwrap(v):
    if isinstance(v, dict):
        return v.get("stringValue") or v.get("intValue") or v.get("boolValue") or ""
    return v


# ===================== 评测结果 =====================
@dataclass
class TrajectoryEvalResult:
    coverage: float
    required_coverage: float
    shortcut_risk: bool
    redundancy_ratio: float
    efficiency: float
    robustness: float
    action_issues: list = field(default_factory=list)
    first_failure_step: Optional[int] = None
    trajectory_score: float = 0.0
    passed: bool = False
    detail: str = ""


# ===================== 评测器 =====================
_CRASH_RE = re.compile(r"traceback|exception|panic|fatal|stack trace|error:", re.I)


class TrajectoryEvaluator:
    """对一条结构化/半结构化轨迹做多维度量。纯代码确定性，无 LLM 依赖。"""

    def __init__(self, expected_steps: Optional[list] = None,
                 required_steps: Optional[list] = None,
                 forbidden_tools: Optional[list] = None,
                 threshold: float = 0.6):
        self.expected_steps = [str(x) for x in (expected_steps or [])]
        self.required_steps = [str(x) for x in (required_steps or [])]
        self.forbidden_tools = [str(x).lower() for x in (forbidden_tools or [])]
        self.threshold = threshold

    @staticmethod
    def _coverage(expected: list, steps: list) -> tuple[float, int]:
        if not expected:
            return 1.0, 0
        covered = 0
        first_fail = None
        for i, estep in enumerate(expected):
            hit = any(estep.lower() in s.content.lower() or estep.lower() in s.tool.lower()
                      for s in steps)
            if hit:
                covered += 1
            elif first_fail is None:
                first_fail = i + 1
        return covered / len(expected), first_fail

    @staticmethod
    def _redundancy(steps: list) -> float:
        if not steps:
            return 0.0
        keys = []
        for s in steps:
            if s.kind == "action":
                keys.append(("A", s.tool.lower(), s.args.strip().lower()))
            else:
                keys.append(("T", s.content.strip().lower()))
        cnt = Counter(keys)
        dup = sum(c - 1 for c in cnt.values() if c > 1)
        return dup / len(steps)

    @staticmethod
    def _has_answer(steps: list) -> bool:
        if any(s.kind == "answer" for s in steps):
            return True
        if not steps:
            return False
        last = steps[-1]
        if last.content and any(s.kind in ("action", "observation") for s in steps):
            return True
        return False

    def _action_issues(self, steps: list) -> list:
        issues: list = []
        for s in steps:
            if s.kind == "action" and s.tool:
                if s.tool.lower() in self.forbidden_tools:
                    issues.append(f"越权调用禁用工具:{s.tool}@步骤{s.idx + 1}")
        return issues

    def evaluate(self, traj: Trajectory) -> TrajectoryEvalResult:
        steps = traj.steps
        if not steps:
            return TrajectoryEvalResult(
                coverage=0.0, required_coverage=0.0, shortcut_risk=False,
                redundancy_ratio=0.0, efficiency=0.0, robustness=1.0,
                action_issues=[], first_failure_step=None,
                trajectory_score=0.0, passed=False,
                detail="无可解析轨迹（trajectory_text/pred 为空）",
            )

        coverage, first_cov_fail = self._coverage(self.expected_steps, steps)
        req_cov, first_req_fail = self._coverage(self.required_steps, steps)
        redundancy = self._redundancy(steps)
        action_issues = self._action_issues(steps)

        # 鲁棒性：任意步骤含崩溃信号 → 0
        crash_step = None
        for s in steps:
            if _CRASH_RE.search(s.content):
                crash_step = s.idx + 1
                break
        robustness = 0.0 if crash_step else 1.0

        # 效率：相对期望步数的浪费（冗余 + 超步数）
        expected_len = max(len(self.expected_steps), 1)
        excess = max(0, len(steps) - expected_len)
        efficiency = max(0.0, 1.0 - 0.5 * redundancy - 0.3 * (excess / expected_len))

        # 捷径/侥幸：到达答案但跳过必做步骤（「蒙对」嫌疑）
        has_answer = self._has_answer(steps)
        missing_required = [r for r in self.required_steps
                            if not any(r.lower() in s.content.lower() or r.lower() in s.tool.lower()
                                       for s in steps)]
        shortcut_risk = bool(has_answer) and bool(missing_required)

        # 最早出错/缺步归因（优先崩溃，其次必做缺步，再次期望缺步）
        first_failure = crash_step or first_req_fail or first_cov_fail

        # 聚合分（权重：覆盖0.3 / 必做0.3 / 效率0.15 / 鲁棒0.15 / 低冗余0.1）
        raw = (0.30 * coverage + 0.30 * req_cov + 0.15 * efficiency
               + 0.15 * robustness + 0.10 * (1.0 - redundancy))
        if shortcut_risk:
            raw *= 0.5          # 捷径侥幸直接腰斩
        if action_issues:
            raw *= 0.7          # 越权动作再打折
        trajectory_score = round(raw, 4)

        passed = (trajectory_score >= self.threshold) and (not shortcut_risk) \
            and (not action_issues) and (robustness == 1.0)

        parts = [f"覆盖={coverage:.0%}", f"必做={req_cov:.0%}",
                 f"冗余={redundancy:.0%}", f"效率={efficiency:.0%}",
                 f"鲁棒={robustness:.0%}"]
        if shortcut_risk:
            parts.append(f"捷径风险(缺必做:{missing_required})")
        if crash_step:
            parts.append(f"崩溃@步骤{crash_step}")
        if action_issues:
            parts.append(";".join(action_issues))
        if first_failure:
            parts.append(f"首错@步骤{first_failure}")
        if not self.expected_steps and not self.required_steps:
            parts.append("未提供expected/required，仅结构度量")

        return TrajectoryEvalResult(
            coverage=round(coverage, 4), required_coverage=round(req_cov, 4),
            shortcut_risk=shortcut_risk, redundancy_ratio=round(redundancy, 4),
            efficiency=round(efficiency, 4), robustness=robustness,
            action_issues=action_issues, first_failure_step=first_failure,
            trajectory_score=trajectory_score, passed=passed,
            detail="; ".join(parts),
        )


# ===================== 注册 grader（接入 engine） =====================
@GRADERS.register("trajectory_eval")
class TrajectoryEvalGrader:
    """轨迹级评测评分器。接入 engine：``case.grader="trajectory_eval"``（可逗号组合）。

    轨迹来源优先级：``meta['trajectory']``（结构化 list）→ ``meta['trajectory_text']`` → ``pred``。
    评测口径取自 meta：``expected_steps`` / ``required_steps`` / ``forbidden_tools``。
    """
    name = "trajectory_eval"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        self.threshold = threshold

    @staticmethod
    def _build_trajectory(case: Case, pred: str) -> Trajectory:
        meta = case.meta or {}
        if meta.get("trajectory"):
            return Trajectory.from_structured(meta["trajectory"])
        text = meta.get("trajectory_text") or pred or ""
        return Trajectory.from_text(text)

    def judge(self, pred: str, case: Case) -> GraderResult:
        meta = case.meta or {}
        ev = TrajectoryEvaluator(
            expected_steps=meta.get("expected_steps"),
            required_steps=meta.get("required_steps"),
            forbidden_tools=meta.get("forbidden_tools"),
            threshold=self.threshold,
        )
        traj = self._build_trajectory(case, pred)
        res = ev.evaluate(traj)
        return GraderResult(
            grader="trajectory_eval", score=res.trajectory_score, passed=res.passed,
            detail=res.detail,
        )


# ===================== 独立便捷入口（单智能体轨迹评测） =====================
def evaluate_trajectory(text_or_list, *, expected_steps=None, required_steps=None,
                        forbidden_tools=None, threshold: float = 0.6) -> TrajectoryEvalResult:
    """不依赖 engine/Case，直接评测一条轨迹（文本或结构化 list）。

    返回 ``TrajectoryEvalResult``，含各维度分与 ``shortcut_risk``（蒙对嫌疑）。
    """
    if isinstance(text_or_list, (list, tuple)):
        traj = Trajectory.from_structured(list(text_or_list))
    else:
        traj = Trajectory.from_text(str(text_or_list or ""))
    return TrajectoryEvaluator(
        expected_steps=expected_steps, required_steps=required_steps,
        forbidden_tools=forbidden_tools, threshold=threshold,
    ).evaluate(traj)


# ===================== 演示构造 =====================
def make_demo_trajectory_solid() -> list:
    """规范轨迹：规划→检索→核验→回答（含必做「核验」步骤）。"""
    return [
        {"kind": "thought", "content": "用户要订北京→上海的机票，需先检索再核验库存后确认"},
        {"kind": "action", "tool": "search_flights", "args": "'BJS','SHA','2026-09-18'"},
        {"kind": "observation", "content": "找到 3 个航班，CA1831 余票 9"},
        {"kind": "action", "tool": "verify_inventory", "args": "'CA1831'"},
        {"kind": "observation", "content": "核验通过，CA1831 可售"},
        {"kind": "answer", "content": "已为您预订 CA1831，依据检索与核验结果"},
    ]


def make_demo_trajectory_lucky() -> list:
    """蒙对轨迹：跳过「核验」必做步直接给答案。"""
    return [
        {"kind": "thought", "content": "用户要订北京→上海的机票"},
        {"kind": "action", "tool": "search_flights", "args": "'BJS','SHA','2026-09-18'"},
        {"kind": "observation", "content": "找到 3 个航班，CA1831 余票 9"},
        {"kind": "answer", "content": "已为您预订 CA1831"},
    ]
