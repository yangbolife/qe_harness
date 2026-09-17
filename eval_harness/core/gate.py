"""eval_harness · CI 质量闸门（N4）

把「评测通过」变成 CI 可判定的门禁：

- pass_rate_min      : 整体通过率下限（如 0.8 = 80%）
- inconclusive_max   : inconclusive 占比上限（如 0.34 = 熔断运行不可超 34%）
- regression_max_drop : 相对 baseline 通过率最大允许跌幅（如 0.05 = 跌 5pp 即失败）
- category_min       : 按类别（category）的最低通过率 {类别: 下限}

evaluate_gate 输出 GateResult（passed + 逐条 check），可：
- 直接决定是否让 CI 非零退出（exit_code）
- 导出 JUnit XML（--junit）供 CI 展示失败用例
- 导出 JSON（--gate-json）供归档
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import xml.etree.ElementTree as ET


@dataclass
class GateConfig:
    pass_rate_min: float = 0.0
    inconclusive_max: float = 1.0
    regression_max_drop: float = 0.0
    category_min: dict[str, float] = field(default_factory=dict)


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class GateResult:
    passed: bool
    checks: list[GateCheck]
    baseline_pass_rate: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "baseline_pass_rate": self.baseline_pass_rate,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }

    def exit_code(self) -> int:
        return 0 if self.passed else 1

    def junit_xml(self, suite_name: str = "eval_harness_gate") -> str:
        """导出 JUnit XML：每条 check 一个 testcase，失败写 <failure>。"""
        testsuite = ET.Element(
            "testsuite",
            name=suite_name, tests=str(len(self.checks)),
            failures=str(sum(1 for c in self.checks if not c.passed)),
            errors="0",
        )
        for c in self.checks:
            tc = ET.SubElement(testsuite, "testcase", name=c.name, classname=suite_name)
            if not c.passed:
                f = ET.SubElement(tc, "failure", message=c.detail)
                f.text = f"GATE FAILED: {c.name} | {c.detail}"
        return ET.tostring(testsuite, encoding="utf-8").decode("utf-8")


def evaluate_gate(agg: dict, baseline: Optional[dict], cfg: GateConfig) -> GateResult:
    """agg = engine.aggregate(results)；baseline = get_run(baseline_id) 或 None。"""
    checks: list[GateCheck] = []
    total = agg.get("total", 0) or 0
    pr = agg.get("pass_rate", 0.0)
    inc = agg.get("inconclusive", 0)

    # 1) 整体通过率
    checks.append(GateCheck(
        f"pass_rate>={cfg.pass_rate_min * 100:.0f}%",
        pr >= cfg.pass_rate_min,
        f"实际 {pr * 100:.1f}%",
    ))

    # 2) inconclusive 占比
    inc_ratio = (inc / total) if total else 0.0
    checks.append(GateCheck(
        f"inconclusive<={cfg.inconclusive_max * 100:.0f}%",
        inc_ratio <= cfg.inconclusive_max,
        f"实际 {inc_ratio * 100:.1f}%（{inc}/{total}）",
    ))

    # 3) 回归（相对 baseline 跌幅）
    base_pr = None
    if baseline is not None and cfg.regression_max_drop > 0:
        base_pr = baseline.get("pass_rate", 0.0)
        drop = base_pr - pr
        checks.append(GateCheck(
            f"regression<={cfg.regression_max_drop * 100:.0f}pp",
            drop <= cfg.regression_max_drop,
            f"基线 {base_pr * 100:.1f}% → 现 {pr * 100:.1f}%（跌 {drop * 100:+.1f}pp）",
        ))

    # 4) 按类别最低通过率
    by_cat = agg.get("by_category", {}) or {}
    for cat, minr in cfg.category_min.items():
        cr = by_cat.get(cat, {}).get("pass_rate", 0.0) if cat in by_cat else 0.0
        checks.append(GateCheck(
            f"cat[{cat}]>={minr * 100:.0f}%",
            cr >= minr,
            f"实际 {cr * 100:.1f}%" + ("" if cat in by_cat else "（无样本）"),
        ))

    passed = all(c.passed for c in checks)
    return GateResult(passed=passed, checks=checks, baseline_pass_rate=base_pr)
