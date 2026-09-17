"""eval_harness · 核心数据模型

对齐《AI评测双览》六要素：
- Case       ~ Task（用例：输入 + 成功标准 + 套件类型 + 评分器指定）
- Trial      ~ Trial（同一 Case 的一次执行，因随机性常跑 k 次）
- GraderResult ~ Grader 的单次打分
- CaseResult ~ Outcome（以环境真实状态/评分为准的结果）

Phase 2 扩展：CaseResult 承载 k 次 Trial + 一致性指标(pass_rate / pass@k / pass^k)
+ inconclusive（熔断标记，对齐 qe-platform「环境失败占比>=34% 熔断」语义）。

Phase 3 扩展：新增各类型的 from_dict，用于跨进程 Worker 的结果序列化重建
（队列只传 JSON，Worker 子进程重建对象后跑异步引擎再回写）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Case:
    """一条评测用例（Task）。声明式：可从 JSONL / YAML 加载。"""
    id: str
    input: str                     # 输入（问题 / 任务指令）
    gold: str = ""                 # 期望答案（可选，开放题可空）
    category: str = "general"      # 业务类别（用于 BadCase 归因聚合）
    difficulty: str = "medium"     # 难度梯度：easy/medium/hard
    suite: str = "smoke"           # 离线四件套：smoke/regression/behavior/safety
    grader: str = ""               # 指定评分器：code / judge / human / sandbox / 逗号组合
                                    # 留空=未指定 → 回退到本次运行的 default_grader（CLI --grader）
    meta: dict = field(default_factory=dict)  # 任意扩展字段（env/rubric/schema/check_code…）

    @classmethod
    def from_dict(cls, d: dict, idx: int = 0) -> "Case":
        # 兼容练习3 的 {question, gold} 写法
        return cls(
            id=str(d.get("id", idx)),
            input=d.get("question", d.get("input", "")),
            gold=d.get("gold", d.get("answer", "")),
            category=d.get("category", "general"),
            difficulty=d.get("difficulty", "medium"),
            suite=d.get("suite", d.get("type", "smoke")),
            grader=d.get("grader", ""),
            meta=d.get("meta", {}),
        )


@dataclass
class GraderResult:
    """评分器对一次回答的判定。"""
    grader: str
    score: float                   # 0.0 ~ 1.0
    passed: bool
    detail: str = ""
    duration_ms: float = 0.0
    tokens: int = 0                # judge 通道消耗的 token（创新⑤ 成本追踪）
    cost_usd: float = 0.0          # judge 通道估算成本（创新⑤）

    @classmethod
    def from_dict(cls, d: dict) -> "GraderResult":
        return cls(
            grader=d.get("grader", ""),
            score=float(d.get("score", 0.0)),
            passed=bool(d.get("passed", False)),
            detail=d.get("detail", ""),
            duration_ms=float(d.get("duration_ms", 0.0)),
            tokens=int(d.get("tokens", 0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
        )


@dataclass
class Trial:
    """同一 Case 的一次执行（因随机性常跑 k 次）。"""
    response: str = ""             # 被测系统原始回答
    graders: list[GraderResult] = field(default_factory=list)
    duration_ms: float = 0.0
    passed: bool = False           # 本次执行按组合策略判定是否通过
    error: str = ""                # 执行异常（如熔断/网络失败）；空=正常
    # 失败归类（环境类/缺陷分离用）：rate_limited / server_error / timeout /
    # network / circuit_open / client_error / unknown。空=未失败或无归类。
    failure_class: str = ""
    status_code: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Trial":
        return cls(
            response=d.get("response", ""),
            graders=[GraderResult.from_dict(g) for g in d.get("graders", [])],
            duration_ms=float(d.get("duration_ms", 0.0)),
            passed=bool(d.get("passed", False)),
            error=d.get("error", ""),
            failure_class=d.get("failure_class", ""),
            status_code=d.get("status_code"),
        )


@dataclass
class CaseResult:
    """一条用例的最终结果（Outcome，含 k 次 Trial 的聚合）。"""
    case: Case
    trials: list[Trial] = field(default_factory=list)
    # —— 兼容字段（取首个有效 Trial）—— #
    response: str = ""             # 首个非错误 Trial 的回答
    graders: list[GraderResult] = field(default_factory=list)
    duration_ms: float = 0.0
    passed: bool = False           # 由 engine 按 trial_policy 计算后写入

    # —— Phase 2 一致性指标 —— #
    k: int = 1
    pass_rate: float = 0.0         # k 次中通过的比例（一致性估计 p）
    pass_at_k: float = 0.0         # 1 - (1 - pass_rate)^k（至少一次通过的概率估计）
    pass_k: bool = False            # 是否 k 次全过（严格 pass^k）
    inconclusive: bool = False      # 熔断标记：环境失败占比超阈值，不计入通过
    error: str = ""                # 全局错误（如全部 Trial 失败）
    # 主导失败归类 + HTTP 状态码（环境类/缺陷分离用，对齐 qe-platform 桥接层口径）
    failure_class: str = ""
    status_code: Optional[int] = None

    @property
    def primary_score(self) -> float:
        return self.graders[0].score if self.graders else 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "CaseResult":
        return cls(
            case=Case.from_dict(d),
            trials=[Trial.from_dict(t) for t in d.get("trials", [])],
            response=d.get("response", ""),
            graders=[GraderResult.from_dict(g) for g in d.get("graders", [])],
            duration_ms=float(d.get("duration_ms", 0.0)),
            passed=bool(d.get("passed", False)),
            k=int(d.get("k", 1)),
            pass_rate=float(d.get("pass_rate", 0.0)),
            pass_at_k=float(d.get("pass_at_k", 0.0)),
            pass_k=bool(d.get("pass_k", False)),
            inconclusive=bool(d.get("inconclusive", False)),
            error=d.get("error", ""),
            failure_class=d.get("failure_class", ""),
            status_code=d.get("status_code"),
        )
