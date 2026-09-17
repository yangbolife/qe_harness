"""eval_harness · 项目级 Rubric + 盲评（N2）

Braintrust 的 Topics / Review 对应物，但更强调「可复用、可版本钉的项目级评分标准」：
- Rubric：一组带权重与引用评分器的评测准则（criterion），用于人工/半自动评审。
- apply_rubric_to_run：把 Rubric 套到一次运行，逐用例产出加权 rubric 分，并聚合。
- blind_anonymize：盲评时把 provider / model_version / git 来源替换成匿名标签，
  评审者看不到「这是哪个模型」从而消除偏见（对应 Braintrust Blind review）。

与 JudgeGrader 的差异：Rubric 是多准则加权（业务正确性/鲁棒性/四类集成风险面），
而 JudgeGrader 是单条 rubric 打分。Rubric 是「项目级」可沉淀资产。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from .graders import GRADERS
from .models import Case, GraderResult


@dataclass
class Criterion:
    key: str
    label: str
    weight: float = 1.0
    grader: str = "code"   # 引用已注册评分器名（code/judge/contract/...）

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label,
                "weight": self.weight, "grader": self.grader}

    @classmethod
    def from_dict(cls, d: dict) -> "Criterion":
        return cls(key=d["key"], label=d.get("label", d["key"]),
                   weight=float(d.get("weight", 1.0)), grader=d.get("grader", "code"))


@dataclass
class Rubric:
    name: str
    criteria: list[Criterion] = field(default_factory=list)
    pass_threshold: float = 0.6  # 加权分 >= 该值判通过

    def to_dict(self) -> dict:
        return {"name": self.name,
                "criteria": [c.to_dict() for c in self.criteria],
                "pass_threshold": self.pass_threshold}

    @classmethod
    def from_dict(cls, d: dict) -> "Rubric":
        return cls(name=d["name"],
                   criteria=[Criterion.from_dict(c) for c in d.get("criteria", [])],
                   pass_threshold=float(d.get("pass_threshold", 0.6)))

    def total_weight(self) -> float:
        return sum(c.weight for c in self.criteria) or 1.0


def _build_subcase(case: Case, grader_name: str) -> Case:
    """克隆用例但仅用单一 criterion 的评分器，便于复用已注册评分器。"""
    return Case(id=case.id, input=case.input, gold=case.gold, category=case.category,
                difficulty=case.difficulty, suite=case.suite, grader=grader_name,
                meta=dict(case.meta))


def score_case_with_rubric(pred: str, case: Case, rubric: Rubric,
                           judge_provider=None) -> dict:
    """对单条用例用 Rubric 逐准则打分，返回加权分与明细。

    judge 类 criterion 需要 judge_provider；未提供时该准则判 0（NEEDS_REVIEW），
    不污染其他确定性准则的分数。
    """
    details: list[dict] = []
    weighted = 0.0
    tw = rubric.total_weight()
    for c in rubric.criteria:
        sub = _build_subcase(case, c.grader)
        try:
            g = GRADERS.get(c.grader)
        except KeyError:
            details.append({"key": c.key, "score": 0.0, "passed": False,
                            "detail": f"未注册评分器 {c.grader}"})
            continue
        try:
            grader = g(judge_provider=judge_provider) if c.grader == "judge" else g()
        except TypeError:
            grader = g()
        res: GraderResult = grader.judge(pred, sub)
        details.append({"key": c.key, "label": c.label, "score": res.score,
                        "passed": res.passed, "detail": res.detail})
        weighted += res.score * c.weight
    rubric_score = weighted / tw if tw else 0.0
    return {
        "case_id": case.id,
        "rubric_score": round(rubric_score, 4),
        "rubric_pass": rubric_score >= rubric.pass_threshold,
        "details": details,
    }


def blind_anonymize(run_dict: dict, salt: str = "eval") -> dict:
    """盲评：把能识别「哪个模型」的字段替换成匿名标签。

    匿名标签由 (salt + provider/model_version) 哈希得到，保证同一运行内
    provider/model 映射到稳定但不可读的标签；评审者无法反推来源。
    保留通过率/失败明细等「与模型身份无关」的信息。
    """
    def _anon(*parts) -> str:
        h = hashlib.sha256(("|".join(str(p) for p in parts) + "|" + salt).encode()).hexdigest()[:8]
        return f"Anonymous-{h}"
    out = dict(run_dict)
    prov = run_dict.get("provider") or "?"
    model = run_dict.get("model_version") or "?"
    out["provider"] = _anon(prov)
    out["model_version"] = _anon(model)
    out["judge_provider"] = _anon(run_dict.get("judge_provider") or "?")
    # 剥除 git 来源，避免泄露
    for k in list(out):
        if k.startswith("git_"):
            out.pop(k, None)
    if isinstance(out.get("config_json"), dict):
        cfg = dict(out["config_json"])
        cfg.pop("provider", None)
        cfg.pop("model_version", None)
        cfg.pop("judge_provider", None)
        for k in list(cfg):
            if k.startswith("git_"):
                cfg.pop(k, None)
        out["config_json"] = cfg
    out["_blind"] = True
    return out
